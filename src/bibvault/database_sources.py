"""Resolve papers from official database-community publication sites.

Every URL extracted from HTML is checked against a source-specific host
allowlist. Catalog pages are shared within a run and cached in SQLite across
runs; a catalog mismatch simply returns no candidate so the normal resolution
chain can continue.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from difflib import SequenceMatcher
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import httpx

from bibvault.models import Reference
from bibvault.storage import Library

logger = logging.getLogger(__name__)

DROPS_HOSTS = frozenset({"drops.dagstuhl.de"})
SIGMOD_RECORD_HOSTS = frozenset({"sigmodrecord.org", "www.sigmodrecord.org"})
TCDE_HOSTS = frozenset({"tc.computer.org", "sites.computer.org"})
DBLP_HOSTS = frozenset({"dblp.org", "www.dblp.org"})
ACM_HOSTS = frozenset({"dl.acm.org"})
DOI_HOSTS = frozenset({"doi.org", "dx.doi.org"})
OPENPROCEEDINGS_HOSTS = frozenset({"openproceedings.org", "www.openproceedings.org"})
VLDB_HOSTS = frozenset({"vldb.org", "www.vldb.org"})
CEUR_HOSTS = frozenset({"ceur-ws.org", "www.ceur-ws.org"})

_SIGMOD_ISSUES = "https://sigmodrecord.org/issues/"
_TCDE_ISSUES = "https://tc.computer.org/tcde/bulletin/issues/"
_DBLP_API = "https://dblp.org/search/publ/api"
_MAX_CATALOG_BYTES = 2_000_000
_ARCHIVE_CACHE_DAYS = 1
_DBLP_CACHE_DAYS = 14
_SOURCE_TITLE_THRESHOLD = 0.92
_ISSUE_MONTHS = {1: "march", 2: "june", 3: "september", 4: "december"}
_MONTH_TOKENS = {
    "march": ("march", "mar"),
    "june": ("june", "jun"),
    "september": ("september", "sept", "sep"),
    "december": ("december", "dec"),
}
_DROPS_ICDT = re.compile(
    r"10\.4230/lipics\.icdt\.(\d{4})(?:\.(\d+))?",
    re.IGNORECASE,
)
_OPENPROCEEDINGS_DOIS = ("10.5441/002/", "10.48786/")
_SIGMOD_RECORD_VENUES = {
    "acm sigmod rec",
    "acm sigmod record",
    "sigmod rec",
    "sigmod record",
}
_TCDE_VENUES = {
    "data engineering bulletin",
    "ieee data eng bull",
    "ieee data engineering bulletin",
}


@dataclass(frozen=True)
class SourceDescriptor:
    """Backend data for ``bibvault sources``."""

    provider: str
    hosts: tuple[str, ...]
    access: str


DATABASE_SOURCE_DESCRIPTORS = (
    SourceDescriptor("acm", tuple(sorted(ACM_HOSTS)), "browser"),
    SourceDescriptor("drops", tuple(sorted(DROPS_HOSTS)), "open"),
    SourceDescriptor("sigmod-record", tuple(sorted(SIGMOD_RECORD_HOSTS)), "open"),
    SourceDescriptor("ieee-tcde", tuple(sorted(TCDE_HOSTS)), "open"),
    SourceDescriptor("dblp", tuple(sorted(DBLP_HOSTS)), "metadata-fallback"),
)


@dataclass(frozen=True)
class SourceMatch:
    provider: str
    url: str
    source_url: str | None = None


@dataclass(frozen=True)
class _Link:
    url: str
    text: str


@dataclass(frozen=True)
class _Block:
    text: str
    links: tuple[_Link, ...]


@dataclass(frozen=True)
class _Catalog:
    blocks: tuple[_Block, ...]
    metadata: dict[str, str]


@dataclass(frozen=True)
class _DblpRecord:
    title: str
    year: str
    first_author: str
    doi: str | None
    electronic_editions: tuple[str, ...]
    record_url: str | None
    key: str | None


class _CatalogParser(HTMLParser):
    """Retain table rows and text blocks together with their links."""

    _BLOCK_TAGS = {"tr", "li", "p", "h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self, base: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base = base
        self.blocks: list[_Block] = []
        self.metadata: dict[str, str] = {}
        self._block_tag: str | None = None
        self._parts: list[str] = []
        self._links: list[_Link] = []
        self._href: str | None = None
        self._label: list[str] = []
        self._hidden: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag in {"script", "style"}:
            self._hidden = tag
            return
        if tag == "meta":
            name = (values.get("name") or values.get("property") or "").strip().lower()
            content = (values.get("content") or "").strip()
            if name and content:
                self.metadata.setdefault(name, content)
            return
        if self._block_tag is None and tag in self._BLOCK_TAGS:
            self._block_tag = tag
            self._parts = []
            self._links = []
        if tag == "br" and self._block_tag is not None:
            self._parts.append(" ")
        if tag == "a" and values.get("href"):
            if self._block_tag is None:
                self._block_tag = "a"
                self._parts = []
                self._links = []
            self._href = urljoin(self.base, values["href"] or "")
            self._label = []

    def handle_endtag(self, tag: str) -> None:
        if tag == self._hidden:
            self._hidden = None
            return
        if tag == "a" and self._href is not None:
            label = _clean_text(" ".join(self._label))
            self._links.append(_Link(self._href, label))
            self._href = None
            self._label = []
            if self._block_tag == "a":
                self._finish_block()
            return
        if tag == self._block_tag:
            self._finish_block()

    def handle_data(self, data: str) -> None:
        if self._hidden or self._block_tag is None:
            return
        self._parts.append(data)
        if self._href is not None:
            self._label.append(data)

    def close(self) -> None:
        super().close()
        if self._block_tag is not None:
            self._finish_block()

    def _finish_block(self) -> None:
        text = _clean_text(" ".join(self._parts))
        if text or self._links:
            self.blocks.append(_Block(text, tuple(self._links)))
        self._block_tag = None
        self._parts = []
        self._links = []


def source_descriptors() -> tuple[SourceDescriptor, ...]:
    """Return immutable source metadata for ``bibvault sources``."""

    return DATABASE_SOURCE_DESCRIPTORS


def official_source_kind(reference: Reference) -> str | None:
    """The official source whose catalog can resolve this reference."""

    if _canonical_icdt_doi(reference.doi):
        return "drops"
    venue = _normalize_title(reference.venue)
    if venue in _SIGMOD_RECORD_VENUES or "sigmod record" in venue:
        return "sigmod-record"
    if venue in _TCDE_VENUES:
        return "ieee-tcde"
    if "bulletin" in venue and (
        "data engineering" in venue or "technical committee on data engineering" in venue
    ):
        return "ieee-tcde"
    if "tcde" in venue and "bulletin" in venue:
        return "ieee-tcde"
    return None


async def resolve_official_database_source(
    reference: Reference,
    client: httpx.AsyncClient,
    library: Library,
    threshold: float,
    memory: dict[str, object],
) -> SourceMatch | None:
    """Resolve DROPS, SIGMOD Record, or IEEE TCDE from an official page."""

    kind = official_source_kind(reference)
    if kind == "drops":
        return await _drops_pdf(reference, client, library, threshold, memory)
    if kind == "sigmod-record":
        return await _sigmod_record_pdf(reference, client, library, threshold, memory)
    if kind == "ieee-tcde":
        return await _tcde_pdf(reference, client, library, threshold, memory)
    return None


def should_try_dblp(reference: Reference) -> bool:
    """Whether enough independent metadata exists for a high-confidence search."""

    return bool(
        not reference.doi
        and not _is_pdf_url(reference.url)
        and reference.title
        and reference.authors
        and reference.year
        and re.fullmatch(r"\d{4}", reference.year.strip())
    )


async def resolve_dblp_fallback(
    reference: Reference,
    client: httpx.AsyncClient,
    library: Library,
    threshold: float,
    memory: dict[str, object],
) -> SourceMatch | None:
    """Use DBLP only after title, year, and first author all agree."""

    if not should_try_dblp(reference):
        return None
    record = await _dblp_record(reference, client, library, threshold, memory)
    if record is None:
        return None
    doi_url = f"https://doi.org/{record.doi}" if record.doi else None

    if record.doi and _canonical_icdt_doi(record.doi):
        enriched = reference.model_copy(update={"doi": record.doi})
        match = await _drops_pdf(enriched, client, library, threshold, memory)
        if match:
            return SourceMatch("dblp", match.url, doi_url)

    for edition in record.electronic_editions:
        if _trusted_electronic_edition(edition):
            return SourceMatch("dblp", edition, doi_url or edition)

    if record.doi and record.doi.startswith(_OPENPROCEEDINGS_DOIS):
        return SourceMatch("dblp", doi_url or "", doi_url)
    if record.doi and record.doi.startswith("10.1145/"):
        pdf = f"https://dl.acm.org/doi/pdf/{record.doi}"
        return SourceMatch("dblp", pdf, doi_url)
    return None


async def _drops_pdf(
    reference: Reference,
    client: httpx.AsyncClient,
    library: Library,
    threshold: float,
    memory: dict[str, object],
) -> SourceMatch | None:
    canonical = _canonical_icdt_doi(reference.doi)
    if canonical is None or not reference.title:
        return None
    match = _DROPS_ICDT.fullmatch(canonical)
    assert match is not None
    doi_year = match.group(1)
    if reference.year and reference.year.isdigit() and reference.year != doi_year:
        return None
    landing = f"https://drops.dagstuhl.de/entities/document/{canonical}"
    catalog = await _catalog(
        client,
        library,
        memory,
        landing,
        DROPS_HOSTS,
        max_age_days=None,
    )
    found_title = catalog.metadata.get("citation_title") or catalog.metadata.get("dc.title")
    pdf = catalog.metadata.get("citation_pdf_url")
    if not found_title or not pdf:
        return None
    if not _title_agrees(reference.title, found_title, threshold):
        return None
    if not _allowed_url(pdf, DROPS_HOSTS) or not _is_pdf_url(pdf):
        return None
    return SourceMatch("drops", pdf)


async def _sigmod_record_pdf(
    reference: Reference,
    client: httpx.AsyncClient,
    library: Library,
    threshold: float,
    memory: dict[str, object],
) -> SourceMatch | None:
    if not _complete_issue_reference(reference):
        return None
    archive = await _catalog(
        client,
        library,
        memory,
        _SIGMOD_ISSUES,
        SIGMOD_RECORD_HOSTS,
        max_age_days=_ARCHIVE_CACHE_DAYS,
    )
    issue_url = _find_sigmod_issue(reference, archive)
    if issue_url is None:
        return None
    issue = await _catalog(
        client,
        library,
        memory,
        issue_url,
        SIGMOD_RECORD_HOSTS,
        max_age_days=None,
    )
    if not _issue_metadata_agrees(reference, issue):
        return None
    links = _article_links(issue, SIGMOD_RECORD_HOSTS, allow_sigmod_download=True)
    pdf = _pick_article(reference.title or "", links, threshold)
    return SourceMatch("sigmod-record", pdf) if pdf else None


async def _tcde_pdf(
    reference: Reference,
    client: httpx.AsyncClient,
    library: Library,
    threshold: float,
    memory: dict[str, object],
) -> SourceMatch | None:
    if not _complete_issue_reference(reference):
        return None
    archive = await _catalog(
        client,
        library,
        memory,
        _TCDE_ISSUES,
        TCDE_HOSTS,
        max_age_days=_ARCHIVE_CACHE_DAYS,
    )
    issue_url = _find_tcde_issue(reference, archive)
    if issue_url is None:
        return None
    issue = await _catalog(
        client,
        library,
        memory,
        issue_url,
        TCDE_HOSTS,
        max_age_days=None,
    )
    if not _issue_metadata_agrees(reference, issue):
        return None
    links = _article_links(issue, TCDE_HOSTS)
    pdf = _pick_article(reference.title or "", links, threshold)
    return SourceMatch("ieee-tcde", pdf) if pdf else None


def _canonical_icdt_doi(doi: str | None) -> str | None:
    match = _DROPS_ICDT.fullmatch((doi or "").strip())
    if match is None:
        return None
    suffix = f".{match.group(2)}" if match.group(2) else ""
    return f"10.4230/LIPIcs.ICDT.{match.group(1)}{suffix}"


def _complete_issue_reference(reference: Reference) -> bool:
    return bool(
        reference.title
        and reference.year
        and reference.volume
        and reference.number
        and re.fullmatch(r"\d{4}", reference.year.strip())
        and _integer(reference.volume) is not None
        and _integer(reference.number) is not None
    )


def _find_sigmod_issue(reference: Reference, archive: _Catalog) -> str | None:
    year = (reference.year or "").strip()
    volume = _integer(reference.volume)
    number = _integer(reference.number)
    month = _ISSUE_MONTHS.get(number or -1)
    if volume is None or number is None or month is None:
        return None
    matches: list[str] = []
    metadata = re.compile(
        rf"\bvol(?:ume)?\.?\s*{volume}\b.{{0,40}}\bno(?:\.|(?:umber))?\s*{number}\b",
        re.IGNORECASE,
    )
    for block in archive.blocks:
        block_matches = metadata.search(block.text) is not None
        for link in block.links:
            marker = f"{link.text} {urlparse(link.url).path}".lower()
            if (
                _allowed_url(link.url, SIGMOD_RECORD_HOSTS)
                and (block_matches or f"vol-{volume}-no-{number}" in marker)
                and year in marker
                and _contains_month(marker, month)
            ):
                matches.append(link.url)
    return _only(matches)


def _find_tcde_issue(reference: Reference, archive: _Catalog) -> str | None:
    year = (reference.year or "").strip()
    number = _integer(reference.number)
    month = _ISSUE_MONTHS.get(number or -1)
    if number is None or month is None:
        return None
    current_year: str | None = None
    matches: list[str] = []
    for block in archive.blocks:
        heading = re.fullmatch(r"\D*(\d{4})\D*", block.text)
        if heading:
            current_year = heading.group(1)
        for link in block.links:
            parsed = urlparse(link.url)
            marker = f"{link.text} {parsed.path}".lower()
            link_year = year in marker or f"a{year[2:]}" in marker
            if (
                _allowed_url(link.url, TCDE_HOSTS)
                and (link_year or current_year == year)
                and _contains_month(marker, month)
                and not _is_pdf_url(link.url)
                and parsed.path.lower().endswith((".htm", ".html", "/"))
            ):
                matches.append(link.url)
    return _only(matches)


def _issue_metadata_agrees(reference: Reference, issue: _Catalog) -> bool:
    year = re.escape((reference.year or "").strip())
    volume = _integer(reference.volume)
    number = _integer(reference.number)
    if volume is None or number is None:
        return False
    text = " ".join(block.text for block in issue.blocks[:12])
    issue_pattern = re.compile(
        rf"\bvol(?:ume)?\.?\s*{volume}\b.{{0,40}}\bno(?:\.|(?:umber))?\s*{number}\b",
        re.IGNORECASE,
    )
    return re.search(rf"\b{year}\b", text) is not None and issue_pattern.search(text) is not None


def _article_links(
    catalog: _Catalog,
    hosts: frozenset[str],
    *,
    allow_sigmod_download: bool = False,
) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for index, block in enumerate(catalog.blocks):
        for link in block.links:
            if not _article_file(link.url, hosts, allow_sigmod_download):
                continue
            title = link.text if _useful_title(link.text) else ""
            if not title:
                for previous in reversed(catalog.blocks[max(0, index - 4) : index]):
                    labels = [
                        item.text
                        for item in previous.links
                        if not _article_file(item.url, hosts, allow_sigmod_download)
                        and _useful_title(item.text)
                    ]
                    if labels:
                        title = labels[-1]
                        break
            if title:
                found.append((title, link.url))
    return found


def _article_file(url: str, hosts: frozenset[str], allow_sigmod_download: bool) -> bool:
    if not _allowed_url(url, hosts):
        return False
    if _is_pdf_url(url):
        return True
    parsed = urlparse(url)
    return bool(
        allow_sigmod_download
        and parsed.hostname in SIGMOD_RECORD_HOSTS
        and "smd_process_download=1" in parsed.query
        and "download_id=" in parsed.query
    )


def _pick_article(title: str, links: list[tuple[str, str]], threshold: float) -> str | None:
    deduplicated: dict[str, float] = {}
    for found_title, url in links:
        score = _title_score(title, found_title)
        deduplicated[url] = max(score, deduplicated.get(url, 0.0))
    ranked = sorted(
        ((score, url) for url, score in deduplicated.items()),
        reverse=True,
    )
    required = max(_SOURCE_TITLE_THRESHOLD, threshold)
    if not ranked or ranked[0][0] < required:
        return None
    if len(ranked) > 1 and ranked[1][0] >= required and ranked[0][0] - ranked[1][0] < 0.03:
        return None
    return ranked[0][1]


async def _catalog(
    client: httpx.AsyncClient,
    library: Library,
    memory: dict[str, object],
    url: str,
    hosts: frozenset[str],
    *,
    max_age_days: int | None,
) -> _Catalog:
    memory_key = f"database-source:page:{url}"
    known = memory.get(memory_key)
    if isinstance(known, _Catalog):
        return known
    if isinstance(known, asyncio.Future):
        return await known
    task = asyncio.create_task(
        _load_catalog(client, library, url, hosts, max_age_days=max_age_days)
    )
    memory[memory_key] = task
    result = await task
    memory[memory_key] = result
    return result


async def _load_catalog(
    client: httpx.AsyncClient,
    library: Library,
    url: str,
    hosts: frozenset[str],
    *,
    max_age_days: int | None,
) -> _Catalog:
    cache_key = _page_cache_key(url)
    stale, fetched_at = _read_catalog_cache(library.cache_get(cache_key), url)
    if stale is not None and (
        max_age_days is None
        or (
            fetched_at is not None
            and datetime.now(UTC) - fetched_at <= timedelta(days=max_age_days)
        )
    ):
        return stale
    try:
        response = await client.get(url)
        response.raise_for_status()
        if not _allowed_url(str(response.url), hosts):
            logger.warning("%s redirected away from its official host", url)
            return stale or _Catalog((), {})
        if len(response.content) > _MAX_CATALOG_BYTES:
            logger.warning("%s catalog is unexpectedly large", url)
            return stale or _Catalog((), {})
    except httpx.HTTPError as exc:
        logger.warning("%s unavailable: %s", url, exc.__class__.__name__)
        return stale or _Catalog((), {})
    parser = _CatalogParser(str(response.url))
    try:
        parser.feed(response.text)
        parser.close()
    except (UnicodeError, ValueError) as exc:
        logger.warning("%s catalog could not be parsed: %s", url, exc.__class__.__name__)
        return stale or _Catalog((), {})
    catalog = _Catalog(tuple(parser.blocks), parser.metadata)
    entry = {
        "version": 1,
        "url": url,
        "fetched_at": datetime.now(UTC).isoformat(),
        "catalog": _catalog_payload(catalog),
    }
    library.cache_put(cache_key, json.dumps(entry, ensure_ascii=False))
    return catalog


def _page_cache_key(url: str) -> str:
    digest = hashlib.sha256(url.encode()).hexdigest()[:24]
    return f"database-source:page:{digest}"


def _catalog_payload(catalog: _Catalog) -> dict:
    return {
        "metadata": catalog.metadata,
        "blocks": [
            {
                "text": block.text,
                "links": [{"url": link.url, "text": link.text} for link in block.links],
            }
            for block in catalog.blocks
        ],
    }


def _read_catalog_cache(raw: str | None, url: str) -> tuple[_Catalog | None, datetime | None]:
    if raw is None:
        return None, None
    try:
        entry = json.loads(raw)
        if entry["version"] != 1 or entry["url"] != url:
            return None, None
        fetched_at = datetime.fromisoformat(entry["fetched_at"])
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=UTC)
        payload = entry["catalog"]
        metadata = {
            str(key): str(value)
            for key, value in payload["metadata"].items()
            if isinstance(key, str) and isinstance(value, str)
        }
        blocks = tuple(
            _Block(
                str(block["text"]),
                tuple(
                    _Link(str(link["url"]), str(link["text"]))
                    for link in block["links"]
                    if isinstance(link, dict)
                ),
            )
            for block in payload["blocks"]
            if isinstance(block, dict)
        )
    except (KeyError, TypeError, ValueError):
        return None, None
    return _Catalog(blocks, metadata), fetched_at


async def _dblp_record(
    reference: Reference,
    client: httpx.AsyncClient,
    library: Library,
    threshold: float,
    memory: dict[str, object],
) -> _DblpRecord | None:
    author_key = "|".join(_author_parts(reference.authors[0]))
    cache_key = (
        f"dblp:publication:{reference.year}:"
        f"{_normalize_title(reference.title)}:{author_key}"
    )
    known = memory.get(cache_key)
    if isinstance(known, _DblpRecord):
        return known
    if known is False:
        return None
    if isinstance(known, asyncio.Future):
        return await known
    task = asyncio.create_task(
        _load_dblp_record(reference, client, library, cache_key, threshold)
    )
    memory[cache_key] = task
    record = await task
    memory[cache_key] = record if record is not None else False
    return record


async def _load_dblp_record(
    reference: Reference,
    client: httpx.AsyncClient,
    library: Library,
    cache_key: str,
    threshold: float,
) -> _DblpRecord | None:
    cached, searched_at = _read_dblp_cache(library.cache_get(cache_key))
    if searched_at is not None and datetime.now(UTC) - searched_at <= timedelta(
        days=_DBLP_CACHE_DAYS
    ):
        return cached
    try:
        response = await client.get(
            _DBLP_API,
            params={"q": reference.title or "", "format": "json", "h": "5"},
        )
        response.raise_for_status()
        if not _allowed_url(str(response.url), DBLP_HOSTS):
            return cached
        payload = response.json()
        if not isinstance(payload, dict):
            return cached
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("DBLP lookup failed: %s", exc.__class__.__name__)
        return cached
    record = _select_dblp_record(reference, payload, threshold)
    entry = {
        "version": 1,
        "searched_at": datetime.now(UTC).isoformat(),
        "record": _dblp_payload(record),
    }
    library.cache_put(cache_key, json.dumps(entry, ensure_ascii=False))
    return record


def _select_dblp_record(
    reference: Reference,
    payload: dict,
    threshold: float,
) -> _DblpRecord | None:
    try:
        hits = payload["result"]["hits"]["hit"]
    except (KeyError, TypeError):
        return None
    if isinstance(hits, dict):
        hits = [hits]
    if not isinstance(hits, list):
        return None
    matches: list[tuple[float, _DblpRecord]] = []
    for hit in hits:
        info = hit.get("info") if isinstance(hit, dict) else None
        if not isinstance(info, dict):
            continue
        title = _plain_markup(_text_value(info.get("title")))
        year = _text_value(info.get("year")).strip()
        authors = _dblp_authors(info.get("authors"))
        if not title or not authors or year != (reference.year or "").strip():
            continue
        if not _same_first_author(reference.authors[0], authors[0]):
            continue
        score = _title_score(reference.title or "", title)
        if score < max(_SOURCE_TITLE_THRESHOLD, threshold):
            continue
        doi = _normalize_doi(_text_value(info.get("doi"))) or None
        editions = tuple(
            edition
            for edition in _text_values(info.get("ee"))
            if _allowed_url(edition, _trusted_ee_hosts() | DOI_HOSTS)
        )
        record_url = _text_value(info.get("url")) or None
        if record_url and not _allowed_url(record_url, DBLP_HOSTS):
            record_url = None
        matches.append(
            (
                score,
                _DblpRecord(
                    title=title,
                    year=year,
                    first_author=authors[0],
                    doi=doi,
                    electronic_editions=editions,
                    record_url=record_url,
                    key=_text_value(info.get("key")) or None,
                ),
            )
        )
    matches.sort(key=lambda item: item[0], reverse=True)
    if not matches:
        return None
    if len(matches) > 1 and matches[0][0] - matches[1][0] < 0.03:
        return None
    return matches[0][1]


def _read_dblp_cache(raw: str | None) -> tuple[_DblpRecord | None, datetime | None]:
    if raw is None:
        return None, None
    try:
        entry = json.loads(raw)
        if entry["version"] != 1:
            return None, None
        searched_at = datetime.fromisoformat(entry["searched_at"])
        if searched_at.tzinfo is None:
            searched_at = searched_at.replace(tzinfo=UTC)
        payload = entry["record"]
        if payload is None:
            return None, searched_at
        record = _DblpRecord(
            title=str(payload["title"]),
            year=str(payload["year"]),
            first_author=str(payload["first_author"]),
            doi=str(payload["doi"]) if payload.get("doi") else None,
            electronic_editions=tuple(str(item) for item in payload["electronic_editions"]),
            record_url=str(payload["record_url"]) if payload.get("record_url") else None,
            key=str(payload["key"]) if payload.get("key") else None,
        )
    except (KeyError, TypeError, ValueError):
        return None, None
    return record, searched_at


def _dblp_payload(record: _DblpRecord | None) -> dict | None:
    if record is None:
        return None
    return {
        "title": record.title,
        "year": record.year,
        "first_author": record.first_author,
        "doi": record.doi,
        "electronic_editions": list(record.electronic_editions),
        "record_url": record.record_url,
        "key": record.key,
    }


def _trusted_electronic_edition(url: str) -> bool:
    if not _allowed_url(url, _trusted_ee_hosts()):
        return False
    if _is_pdf_url(url):
        return True
    parsed = urlparse(url)
    return bool(
        parsed.hostname in SIGMOD_RECORD_HOSTS
        and "smd_process_download=1" in parsed.query
        and "download_id=" in parsed.query
    )


def _trusted_ee_hosts() -> frozenset[str]:
    return frozenset().union(
        DROPS_HOSTS,
        SIGMOD_RECORD_HOSTS,
        TCDE_HOSTS,
        ACM_HOSTS,
        OPENPROCEEDINGS_HOSTS,
        VLDB_HOSTS,
        CEUR_HOSTS,
    )


def _dblp_authors(value: object) -> list[str]:
    node = value.get("author") if isinstance(value, dict) else value
    return [item for item in _text_values(node) if item]


def _text_values(value: object) -> list[str]:
    if isinstance(value, list):
        return [text for item in value if (text := _text_value(item))]
    text = _text_value(value)
    return [text] if text else []


def _text_value(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("text", "#text", "value"):
            if isinstance(value.get(key), str):
                return value[key]
    return ""


def _same_first_author(left: str, right: str) -> bool:
    left_surname, left_initial = _author_parts(left)
    right_surname, right_initial = _author_parts(right)
    if not left_surname or left_surname != right_surname:
        return False
    return not left_initial or not right_initial or left_initial == right_initial


def _author_parts(value: str) -> tuple[str, str]:
    normalized = _normalize_title(re.sub(r"\s+\d{4}$", "", value))
    tokens = normalized.split()
    if not tokens:
        return "", ""
    if "," in value:
        surname_text, given_text = value.split(",", 1)
        surname_tokens = _normalize_title(surname_text).split()
        given_tokens = _normalize_title(given_text).split()
        surname = surname_tokens[-1] if surname_tokens else ""
        initial = given_tokens[0][0] if given_tokens and given_tokens[0] else ""
        return surname, initial
    return tokens[-1], tokens[0][0]


def _title_agrees(left: str, right: str, threshold: float) -> bool:
    return _title_score(left, right) >= max(_SOURCE_TITLE_THRESHOLD, threshold)


def _title_score(left: str, right: str) -> float:
    normalized_left = _normalize_title(left)
    normalized_right = _normalize_title(right)
    if not normalized_left or not normalized_right:
        return 0.0
    return SequenceMatcher(None, normalized_left, normalized_right).ratio()


def _normalize_title(value: str | None) -> str:
    return " ".join(
        "".join(char.lower() if char.isalnum() else " " for char in value or "").split()
    )


def _plain_markup(value: str) -> str:
    return _clean_text(re.sub(r"<[^>]+>", " ", html.unescape(value)))


def _clean_text(value: str) -> str:
    return " ".join(html.unescape(value).split())


def _normalize_doi(value: str) -> str:
    lowered = value.strip().lower()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if lowered.startswith(prefix):
            lowered = lowered[len(prefix) :]
            break
    return lowered.rstrip(".")


def _useful_title(value: str) -> bool:
    normalized = _normalize_title(value)
    if len(normalized.split()) < 2:
        return False
    generic = {
        "available in pdf",
        "click here to like and comment on this article",
        "entire issue",
        "issue pdf",
        "table of contents",
    }
    return normalized not in generic and not normalized.startswith("click here")


def _contains_month(marker: str, month: str) -> bool:
    return any(re.search(rf"(?<![a-z]){token}(?![a-z])", marker) for token in _MONTH_TOKENS[month])


def _only(values: list[str]) -> str | None:
    unique = list(dict.fromkeys(values))
    return unique[0] if len(unique) == 1 else None


def _integer(value: str | None) -> int | None:
    match = re.search(r"\d+", value or "")
    return int(match.group()) if match else None


def _is_pdf_url(url: str | None) -> bool:
    return bool(url and urlparse(url).path.lower().endswith(".pdf"))


def _allowed_url(url: str, hosts: frozenset[str]) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    return parsed.scheme in {"http", "https"} and host in hosts
