"""Find openly accessible PDF URLs for a reference."""

import asyncio
import hashlib
import json
import logging
import re
import unicodedata
from datetime import UTC, datetime, timedelta
from difflib import SequenceMatcher
from html.parser import HTMLParser
from typing import NamedTuple
from urllib.parse import urljoin, urlparse

import httpx
from pydantic import BaseModel, Field, ValidationError

from bibvault.config import Settings
from bibvault.models import Candidate, Reference
from bibvault.network import make_client
from bibvault.storage import Library

logger = logging.getLogger(__name__)

_STOPPED = "openalex:stopped"
_PAUSED = "openalex:paused"
_KEYLESS = "openalex:keyless"
_KEYLESS_NOTED = "openalex:keyless-noted"
_SEARCH_CACHE_DAYS = 7
# About a tenth of what OpenAlex gives an IP address without a key, leaving the rest
# for the user's browser and anyone sharing the address.
KEYLESS_DAILY_REQUESTS = 100
_LOGIN_HINT = "run `bibvault auth login openalex` (keys are free)"


class OpenAlexUnavailable(Exception):
    """OpenAlex will not answer again in this run."""

    def __init__(self, message: str, *, rate_limited: bool) -> None:
        super().__init__(message)
        self.message = message
        self.rate_limited = rate_limited


class Lookup(BaseModel):
    """What one metadata source said about a publication."""

    found: bool = False
    candidates: list[Candidate] = Field(default_factory=list)
    landing_url: str | None = None


class Resolution(BaseModel):
    candidates: list[Candidate] = Field(default_factory=list)
    found: bool = False
    landing_url: str | None = None
    error: str | None = None
    rate_limited: bool = False


def is_pdf_url(url: str | None) -> bool:
    if not url:
        return False
    return urlparse(url).path.lower().endswith(".pdf")


def map_version(value: str | None) -> str:
    token = (value or "").lower()
    if "published" in token:
        return "published"
    if "accept" in token:
        return "accepted"
    if "submit" in token or "preprint" in token:
        return "preprint"
    return "unknown"


def sort_candidates(candidates: list[Candidate], settings: Settings) -> list[Candidate]:
    ranks = {"published": 0, "provided": 0, "unknown": 1, "accepted": 2, "preprint": 3}
    if not settings.resolution.prefer_published_version:
        ranks["preprint"] = 0
        ranks["published"] = 3
    selected: list[Candidate] = []
    seen: set[str] = set()
    for candidate in candidates:
        if (
            candidate.version == "preprint"
            and not settings.resolution.allow_preprints
            and candidate.provider != "direct"
        ):
            continue
        if candidate.url in seen:
            continue
        seen.add(candidate.url)
        selected.append(candidate)
    selected.sort(key=lambda item: (item.source_rank, ranks.get(item.version, 9), item.url))
    return selected


async def resolve_candidates(
    reference: Reference,
    client: httpx.AsyncClient,
    library: Library,
    settings: Settings,
    memory: dict[str, object],
) -> Resolution:
    found: list[Candidate] = []
    if is_pdf_url(reference.url) and reference.url:
        found.append(
            Candidate(url=reference.url, provider="direct", version="provided", source_rank=0)
        )
    published = await _publisher_pdf(reference, client, library, settings, memory)
    if published:
        provider, url = published
        found.append(Candidate(url=url, provider=provider, version="published", source_rank=1))
    if reference.arxiv_id and settings.resolution.allow_preprints:
        found.append(
            Candidate(
                url=f"https://arxiv.org/pdf/{reference.arxiv_id}",
                provider="arxiv",
                version="preprint",
                source_rank=2,
            )
        )

    lookup = Lookup()
    error: str | None = None
    rate_limited = False
    # The publisher's own PDF is all OpenAlex could add.
    use_openalex = settings.resolution.use_openalex and published is None
    try:
        if use_openalex and reference.doi:
            lookup = await _openalex_doi(reference, client, library, settings, memory)
        elif (
            use_openalex
            and reference.title
            and not reference.arxiv_id
            and not is_pdf_url(reference.url)
        ):
            lookup = await _openalex_title(reference, client, library, settings, memory)
    except OpenAlexUnavailable as exc:
        # Explained once per run in the log.
        error, rate_limited = exc.message, exc.rate_limited
    except httpx.HTTPStatusError as exc:
        error = f"OpenAlex lookup failed: HTTP {exc.response.status_code}"
        logger.warning("%s for %s", error, reference.citation_key)
    except httpx.HTTPError as exc:
        error = f"OpenAlex lookup failed: {exc.__class__.__name__}"
        logger.warning("%s for %s", error, reference.citation_key)

    found.extend(lookup.candidates)
    return Resolution(
        candidates=sort_candidates(found, settings),
        found=lookup.found,
        landing_url=lookup.landing_url,
        error=error,
        rate_limited=rate_limited,
    )


async def _openalex_doi(
    reference: Reference,
    client: httpx.AsyncClient,
    library: Library,
    settings: Settings,
    memory: dict[str, object],
) -> Lookup:
    cache_key = f"openalex:doi:{reference.doi}"
    known = memory.get(cache_key)
    if isinstance(known, Lookup):
        return known
    cached = _read_cache(library, cache_key)
    if cached is not None and _cache_is_enough(cached, settings):
        memory[cache_key] = cached
        return cached
    url = f"https://api.openalex.org/works/https://doi.org/{reference.doi}"
    payload = await _openalex_get(client, url, _mailto(settings), library, settings, memory)
    lookup = Lookup() if payload is None else _lookup_from_work(payload)
    library.cache_put(cache_key, lookup.model_dump_json())
    memory[cache_key] = lookup
    return lookup


async def _openalex_title(
    reference: Reference,
    client: httpx.AsyncClient,
    library: Library,
    settings: Settings,
    memory: dict[str, object],
) -> Lookup:
    # DOI lookups are free, but every search is billed against the daily budget.
    cache_key = f"openalex:title:{reference.year or ''}:{_normalize_title(reference.title)}"
    known = memory.get(cache_key)
    if isinstance(known, Lookup):
        return known
    lookup = _recent_search(library, cache_key)
    if lookup is None:
        if not _key_id(settings):
            _note_keyless(memory)
            raise OpenAlexUnavailable(
                f"finding a paper without a DOI by its title needs an OpenAlex API key; {_LOGIN_HINT}",
                rate_limited=True,
            )
        lookup = await _search_title(reference, client, library, settings, memory)
        entry = {"searched_at": datetime.now(UTC).isoformat(), "lookup": lookup.model_dump(mode="json")}
        library.cache_put(cache_key, json.dumps(entry))
    memory[cache_key] = lookup
    return lookup


def _recent_search(library: Library, cache_key: str) -> Lookup | None:
    raw = library.cache_get(cache_key)
    if raw is None:
        return None
    try:
        entry = json.loads(raw)
        searched_at = datetime.fromisoformat(entry["searched_at"])
        lookup = Lookup.model_validate(entry["lookup"])
    except (ValueError, KeyError, TypeError):
        return None
    if datetime.now(UTC) - searched_at > timedelta(days=_SEARCH_CACHE_DAYS):
        return None
    return lookup


async def _search_title(
    reference: Reference,
    client: httpx.AsyncClient,
    library: Library,
    settings: Settings,
    memory: dict[str, object],
) -> Lookup:
    params: dict[str, str] = {"search": reference.title or "", "per-page": "1"}
    if reference.year and reference.year.isdigit():
        params["filter"] = f"publication_year:{reference.year}"
    params.update(_mailto(settings))
    payload = await _openalex_get(client, "https://api.openalex.org/works", params, library, settings, memory)
    results = (payload or {}).get("results") or []
    if not results:
        return Lookup()
    work = results[0]
    if not isinstance(work, dict):
        return Lookup()
    if not _title_matches(reference, work, settings.resolution.title_match_threshold):
        return Lookup()
    return _lookup_from_work(work)


def _title_matches(reference: Reference, work: dict, threshold: float) -> bool:
    left = _normalize_title(reference.title)
    right = _normalize_title(str(work.get("display_name") or ""))
    if not left or not right:
        return False
    if SequenceMatcher(None, left, right).ratio() < threshold:
        return False
    ours = (reference.year or "").strip()
    theirs = str(work.get("publication_year") or "")
    if ours.isdigit() and theirs.isdigit():
        return ours == theirs
    return True


def _normalize_title(value: str | None) -> str:
    return " ".join("".join(char.lower() if char.isalnum() else " " for char in value or "").split())


async def _publisher_pdf(
    reference: Reference,
    client: httpx.AsyncClient,
    library: Library,
    settings: Settings,
    memory: dict[str, object],
) -> tuple[str, str] | None:
    """The provider and URL of the published PDF on a site that serves it openly."""

    if is_pvldb(reference):
        pdf = await _vldb_pdf(reference, client, library, settings, memory)
        return ("vldb", pdf) if pdf else None
    if reference.doi and reference.doi.startswith(_OPENPROCEEDINGS_DOIS):
        return "openproceedings", f"https://doi.org/{reference.doi}"
    if is_pdf_url(reference.url):
        return None
    if pdf := await _usenix_pdf(reference, client, library, settings, memory):
        return "usenix", pdf
    if pdf := await _cidr_pdf(reference, client, library, settings, memory):
        return "cidr", pdf
    if pdf := await _vldb_conference_pdf(reference, client, library, settings, memory):
        return "vldb", pdf
    return None


_PVLDB_VENUES = {"proc vldb endow", "pvldb", "proceedings of the vldb endowment"}
_VLDB_HOSTS = {"vldb.org", "www.vldb.org"}
_NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.DOTALL)


def is_pvldb(reference: Reference) -> bool:
    if reference.doi and reference.doi.startswith("10.14778/"):
        return True
    return _normalize_title(reference.venue) in _PVLDB_VENUES


async def _vldb_pdf(
    reference: Reference,
    client: httpx.AsyncClient,
    library: Library,
    settings: Settings,
    memory: dict[str, object],
) -> str | None:
    """Find the paper on its vldb.org volume page.

    File names there follow vldb.org's first author, which is not always the
    BibTeX's, so the URL is looked up by title and built only when the page
    does not list the paper.
    """

    volume = (reference.volume or "").strip()
    if not volume.isdigit() or not reference.title:
        return None
    cache_key = f"vldb:{volume}:{_normalize_title(reference.title)}"
    cached = library.cache_get(cache_key)
    if cached:
        return cached
    volume_key = f"vldb:volume:{volume}"
    papers = memory.get(volume_key)
    if not isinstance(papers, asyncio.Future):
        papers = asyncio.ensure_future(_vldb_volume(client, volume))
        memory[volume_key] = papers
    pdf = _pick_vldb_paper(reference, await papers, settings.resolution.title_match_threshold)
    if pdf is None:
        pdf = await _guessed_vldb_pdf(reference, client, volume)
    if pdf:
        library.cache_put(cache_key, pdf)
    return pdf


async def _vldb_volume(client: httpx.AsyncClient, volume: str) -> list[dict]:
    try:
        response = await client.get(f"https://www.vldb.org/pvldb/volumes/{volume}/")
        response.raise_for_status()
    except httpx.HTTPError as exc:
        logger.warning("vldb.org volume %s unavailable: %s", volume, exc.__class__.__name__)
        return []
    match = _NEXT_DATA.search(response.text)
    if match is None:
        return []
    try:
        node = json.loads(match.group(1))
    except ValueError:
        return []
    for name in ("props", "pageProps", "groupedIssues"):
        node = node.get(name) if isinstance(node, dict) else None
    if not isinstance(node, dict):
        return []
    return [
        paper
        for issue in node.values()
        if isinstance(issue, list)
        for paper in issue
        if isinstance(paper, dict) and isinstance(paper.get("pdf"), str) and isinstance(paper.get("title"), str)
    ]


def _pick_vldb_paper(reference: Reference, papers: list[dict], threshold: float) -> str | None:
    wanted = _normalize_title(reference.title)
    first_page = _first_page(reference)
    scored = [
        (SequenceMatcher(None, wanted, _normalize_title(paper["title"])).ratio(), paper)
        for paper in papers
        if urlparse(paper["pdf"]).hostname in _VLDB_HOSTS
    ]
    # The start page already identifies the paper; the title only has to rule out a wrong page.
    on_page = [paper for ratio, paper in scored if ratio >= 0.8 and str(paper.get("start_page")) == first_page]
    by_title = [paper for ratio, paper in scored if ratio >= threshold]
    matches = on_page or by_title
    return matches[0]["pdf"] if matches else None


async def _guessed_vldb_pdf(reference: Reference, client: httpx.AsyncClient, volume: str) -> str | None:
    """Older volume pages miss papers whose files still follow ``p{page}-{first author}.pdf``."""

    first_page = _first_page(reference)
    surname = _surname(reference.authors[0]) if reference.authors else ""
    if not first_page or not surname:
        return None
    url = f"https://www.vldb.org/pvldb/vol{volume}/p{first_page}-{surname}.pdf"
    try:
        response = await client.head(url)
    except httpx.HTTPError:
        return None
    # vldb.org answers unknown paths with 200 and an HTML page.
    if response.status_code == 200 and response.headers.get("content-type", "").startswith("application/pdf"):
        return url
    return None


def _first_page(reference: Reference) -> str | None:
    match = re.match(r"\s*(\d+)", reference.pages or "")
    return match.group(1) if match else None


def _surname(author: str) -> str:
    name = author.replace("{", "").replace("}", "").strip()
    last = name.split(",")[0] if "," in name else (name.split() or [""])[-1]
    return "".join(char for char in unicodedata.normalize("NFKD", last) if char.isascii() and char.isalnum()).lower()


# OpenProceedings (EDBT, ICDT) registers DOIs that lead straight to the PDF.
_OPENPROCEEDINGS_DOIS = ("10.5441/002/", "10.48786/")
_USENIX_HOSTS = {"usenix.org", "www.usenix.org"}
# usenix.org names a conference's pages after its short name and year, e.g. atc14.
_USENIX_CONFERENCES = {
    "annual technical conference": "atc",
    "file and storage technologies": "fast",
    "networked systems design": "nsdi",
    "operating systems design": "osdi",
}
_CIDR_HOSTS = {"cidrdb.org", "www.cidrdb.org"}


class _Link(NamedTuple):
    url: str
    text: str


class _Page(HTMLParser):
    """The text and links of an HTML page in reading order, with its meta tags and frames."""

    def __init__(self, base: str) -> None:
        super().__init__()
        self.base = base
        self.parts: list[str | _Link] = []
        self.meta: dict[str, str] = {}
        self.frames: list[str] = []
        self._href: str | None = None
        self._label: list[str] = []
        self._hidden: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag in {"script", "style"}:
            self._hidden = tag
        elif tag == "a" and values.get("href"):
            self._href = urljoin(self.base, values["href"] or "")
            self._label = []
        elif tag == "meta" and values.get("name") and values.get("content"):
            self.meta.setdefault(values["name"] or "", values["content"] or "")
        elif tag in {"frame", "iframe"} and values.get("src"):
            self.frames.append(urljoin(self.base, values["src"] or ""))

    def handle_endtag(self, tag: str) -> None:
        if tag == self._hidden:
            self._hidden = None
        elif tag == "a" and self._href is not None:
            self.parts.append(_Link(self._href, " ".join("".join(self._label).split())))
            self._href = None

    def handle_data(self, data: str) -> None:
        if self._hidden:
            return
        if self._href is None:
            self.parts.append(data)
        else:
            self._label.append(data)


async def _page(client: httpx.AsyncClient, url: str) -> _Page | None:
    try:
        response = await client.get(url)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        logger.warning("%s unavailable: %s", url, exc.__class__.__name__)
        return None
    page = _Page(str(response.url))
    page.feed(response.text)
    page.close()
    return page


def _pick_link(reference: Reference, links: list[_Link], threshold: float) -> str | None:
    """The link whose text is the title, or contains it when titles share a block with authors."""

    wanted = _normalize_title(reference.title)
    best, best_score = None, threshold
    for link in links:
        text = _normalize_title(link.text)
        score = 1.0 if wanted and wanted in text else SequenceMatcher(None, wanted, text).ratio()
        if score >= best_score:
            best, best_score = link.url, score
    return best


def _usenix_conference(reference: Reference) -> str | None:
    venue = _normalize_title(reference.venue)
    year = (reference.year or "").strip()
    if "usenix" not in venue.split() or not re.fullmatch(r"\d{4}", year):
        return None
    for phrase, name in _USENIX_CONFERENCES.items():
        if phrase in venue:
            return f"{name}{year[2:]}"
    return None


async def _usenix_pdf(
    reference: Reference,
    client: httpx.AsyncClient,
    library: Library,
    settings: Settings,
    memory: dict[str, object],
) -> str | None:
    """Read the PDF link from the paper's page on usenix.org.

    BibTeX from DBLP links that page; otherwise it is found by title in the
    conference program.
    """

    given = reference.url if urlparse(reference.url or "").hostname in _USENIX_HOSTS else None
    conference = None if given else _usenix_conference(reference)
    if given:
        cache_key = f"usenix:{given}"
    elif conference and reference.title:
        cache_key = f"usenix:{conference}:{_normalize_title(reference.title)}"
    else:
        return None
    cached = library.cache_get(cache_key)
    if cached:
        return cached
    talk = given
    if talk is None and conference:
        program_key = f"usenix:program:{conference}"
        talks = memory.get(program_key)
        if not isinstance(talks, asyncio.Future):
            talks = asyncio.ensure_future(_usenix_program(client, conference))
            memory[program_key] = talks
        talk = _pick_link(reference, await talks, settings.resolution.title_match_threshold)
    page = await _page(client, talk) if talk else None
    pdf = page.meta.get("citation_pdf_url") if page else None
    if not pdf or urlparse(pdf).hostname not in _USENIX_HOSTS:
        return None
    library.cache_put(cache_key, pdf)
    return pdf


async def _usenix_program(client: httpx.AsyncClient, conference: str) -> list[_Link]:
    page = await _page(client, f"https://www.usenix.org/conference/{conference}/technical-sessions")
    if page is None:
        return []
    return [
        part
        for part in page.parts
        if isinstance(part, _Link)
        and part.text
        and urlparse(part.url).hostname in _USENIX_HOSTS
        and urlparse(part.url).path.startswith(f"/conference/{conference}/")
        and "/presentation/" in part.url
    ]


def _is_cidr(reference: Reference) -> bool:
    venue = _normalize_title(reference.venue)
    return "cidr" in venue.split() or "innovative data systems research" in venue


async def _cidr_pdf(
    reference: Reference,
    client: httpx.AsyncClient,
    library: Library,
    settings: Settings,
    memory: dict[str, object],
) -> str | None:
    """Find the paper by title in its year's program on cidrdb.org."""

    year = (reference.year or "").strip()
    if not _is_cidr(reference) or not re.fullmatch(r"\d{4}", year) or not reference.title:
        return None
    cache_key = f"cidr:{year}:{_normalize_title(reference.title)}"
    cached = library.cache_get(cache_key)
    if cached:
        return cached
    program_key = f"cidr:program:{year}"
    papers = memory.get(program_key)
    if not isinstance(papers, asyncio.Future):
        papers = asyncio.ensure_future(_cidr_program(client, year))
        memory[program_key] = papers
    pdf = _pick_link(reference, await papers, settings.resolution.title_match_threshold)
    if pdf:
        library.cache_put(cache_key, pdf)
    return pdf


async def _cidr_program(client: httpx.AsyncClient, year: str) -> list[_Link]:
    page = await _page(client, f"https://www.cidrdb.org/cidr{year}/program.html")
    if page is None:
        return []
    pages = [page]
    # Older programs sit in a frame.
    for frame in page.frames:
        if urlparse(frame).hostname in _CIDR_HOSTS and (inner := await _page(client, frame)):
            pages.append(inner)
    return [paper for item in pages for paper in _cidr_papers(item.parts)]


def _cidr_papers(parts: list[str | _Link]) -> list[_Link]:
    """Pair each PDF with its title.

    Over the years the link has been the title itself, a "PDF" after the title,
    and an icon before the authors and title.
    """

    pdfs = [(index, part) for index, part in enumerate(parts) if isinstance(part, _Link) and _is_cidr_pdf(part.url)]
    papers = []
    for number, (index, link) in enumerate(pdfs):
        label = _normalize_title(link.text)
        if label in {"pdf", "paper"}:
            start = pdfs[number - 1][0] + 1 if number else 0
            title = _plain_text(parts[start:index])
        elif not label:
            end = pdfs[number + 1][0] if number + 1 < len(pdfs) else len(parts)
            title = _plain_text(parts[index + 1 : end])
        else:
            title = link.text
        papers.append(_Link(link.url, title))
    return papers


def _is_cidr_pdf(url: str) -> bool:
    parts = urlparse(url)
    # Since 2025 the papers are kept on vldb.org.
    on_site = parts.hostname in _CIDR_HOSTS or (parts.hostname in _VLDB_HOSTS and parts.path.startswith("/cidrdb/"))
    return on_site and is_pdf_url(url)


def _plain_text(parts: list[str | _Link]) -> str:
    return " ".join(part for part in parts if isinstance(part, str))


# Later VLDB papers are in PVLDB.
_LAST_VLDB_CONFERENCE = 2007
_PAGE_FILE = re.compile(r"p0*(\d+)(?:-[^/]*)?\.pdf", re.IGNORECASE)


def _is_vldb_conference(reference: Reference) -> bool:
    words = _normalize_title(reference.venue).split()
    return ("very large data bases" in " ".join(words) or "vldb" in words) and "journal" not in words


async def _vldb_conference_pdf(
    reference: Reference,
    client: httpx.AsyncClient,
    library: Library,
    settings: Settings,
    memory: dict[str, object],
) -> str | None:
    """Find a VLDB conference paper in its year's proceedings on vldb.org."""

    year = (reference.year or "").strip()
    if not _is_vldb_conference(reference) or not re.fullmatch(r"\d{4}", year) or not reference.title:
        return None
    if int(year) > _LAST_VLDB_CONFERENCE:
        return None
    cache_key = f"vldb:conf:{year}:{_normalize_title(reference.title)}"
    cached = library.cache_get(cache_key)
    if cached:
        return cached
    proceedings_key = f"vldb:proceedings:{year}"
    files = memory.get(proceedings_key)
    if not isinstance(files, asyncio.Future):
        files = asyncio.ensure_future(_vldb_proceedings(client, year))
        memory[proceedings_key] = files
    links = await files
    pdf = _pick_link(reference, links, settings.resolution.title_match_threshold) or _vldb_file_by_page(reference, links)
    if pdf:
        library.cache_put(cache_key, pdf)
    return pdf


async def _vldb_proceedings(client: httpx.AsyncClient, year: str) -> list[_Link]:
    folder = f"/conf/{year}/"
    page = await _page(client, f"https://www.vldb.org{folder}")
    if page is None:
        return []
    files = _vldb_files(page, folder)
    if files:
        return files
    # Some years list the papers in a frame, one keeps them in a subdirectory.
    subdirectories = [
        part.url
        for part in page.parts
        if isinstance(part, _Link) and _in_folder(part.url, folder) and re.fullmatch(r"[^/]+/", urlparse(part.url).path[len(folder) :])
    ]
    for url in page.frames + subdirectories:
        if _in_folder(url, folder) and (inner := await _page(client, url)):
            files += _vldb_files(inner, folder)
    return files


def _vldb_files(page: _Page, folder: str) -> list[_Link]:
    return [part for part in page.parts if isinstance(part, _Link) and _in_folder(part.url, folder) and is_pdf_url(part.url)]


def _in_folder(url: str, folder: str) -> bool:
    parts = urlparse(url)
    return parts.hostname in _VLDB_HOSTS and parts.path.startswith(folder)


def _vldb_file_by_page(reference: Reference, links: list[_Link]) -> str | None:
    """Index pages give no titles, but most years name the files after the first page: P169.pdf, p553-stonebraker.pdf."""

    first_page = _first_page(reference)
    if not first_page:
        return None
    by_page: dict[int, str] = {}
    for link in links:
        if match := _PAGE_FILE.fullmatch(urlparse(link.url).path.rsplit("/", 1)[-1]):
            by_page.setdefault(int(match.group(1)), link.url)
    pages = sorted(by_page)
    # 1999 numbers its files P1, P2, ... in order instead.
    if len(pages) < 2 or pages[-1] - pages[0] < 2 * (len(pages) - 1):
        return None
    return by_page.get(int(first_page))


def _lookup_from_work(work: dict) -> Lookup:
    locations: list[dict] = []
    best = work.get("best_oa_location")
    if isinstance(best, dict):
        locations.append(best)
    for location in work.get("locations") or []:
        if isinstance(location, dict):
            locations.append(location)
    found: list[Candidate] = []
    seen: set[str] = set()
    for location in locations:
        pdf_url = location.get("pdf_url")
        if not isinstance(pdf_url, str) or not pdf_url or pdf_url in seen:
            continue
        seen.add(pdf_url)
        version = location.get("version")
        found.append(
            Candidate(
                url=pdf_url,
                provider="openalex",
                version=map_version(version if isinstance(version, str) else None),
                source_rank=1,
            )
        )
    open_access = work.get("open_access")
    if not isinstance(open_access, dict):
        open_access = {}
    oa_url = open_access.get("oa_url")
    if isinstance(oa_url, str) and oa_url not in seen and is_pdf_url(oa_url):
        found.append(Candidate(url=oa_url, provider="openalex", version="unknown", source_rank=1))

    landing_url = None
    if not found and open_access.get("is_oa"):
        landing = best.get("landing_page_url") if isinstance(best, dict) else None
        landing_url = landing if isinstance(landing, str) and landing else oa_url
        if not isinstance(landing_url, str):
            landing_url = None
    return Lookup(found=True, candidates=found, landing_url=landing_url)


def _cache_is_enough(lookup: Lookup, settings: Settings) -> bool:
    if not lookup.candidates:
        return False
    if not settings.resolution.prefer_published_version:
        return True
    return any(item.version == "published" for item in lookup.candidates)


def _read_cache(library: Library, cache_key: str) -> Lookup | None:
    raw = library.cache_get(cache_key)
    if raw is None:
        return None
    try:
        return Lookup.model_validate_json(raw)
    except ValidationError:
        return None


async def _openalex_get(
    client: httpx.AsyncClient,
    url: str,
    params: dict[str, str],
    library: Library,
    settings: Settings,
    memory: dict[str, object],
) -> dict | None:
    """GET from OpenAlex unless it refused earlier in this run or its daily budget is still spent."""

    stopped = memory.get(_STOPPED)
    if stopped is None:
        until = openalex_paused_until(library, settings)
        if until is not None:
            stopped = memory[_STOPPED] = (_budget_message(until, settings), True)
            _tell(memory, stopped[0])
    if isinstance(stopped, tuple):
        raise OpenAlexUnavailable(stopped[0], rate_limited=stopped[1])
    if not _key_id(settings):
        _note_keyless(memory)
        _spend_keyless_request(library, memory)
    try:
        return await _get_json(client, url, params, _api_key_header(settings))
    except httpx.HTTPStatusError as exc:
        refusal = _refusal(exc.response, settings)
        if refusal is None:
            raise
        message, rate_limited, until = refusal
        if _STOPPED not in memory:
            memory[_STOPPED] = (message, rate_limited)
            if until is not None:
                pause = {"until": until.isoformat(), "key": _key_id(settings)}
                library.cache_put(_PAUSED, json.dumps(pause))
            _tell(memory, message)
        raise OpenAlexUnavailable(message, rate_limited=rate_limited) from exc


def _tell(memory: dict[str, object], message: str) -> None:
    notify = memory.get("notify")
    if callable(notify):
        notify(message)
    else:
        logger.warning("%s", message)


def _note_keyless(memory: dict[str, object]) -> None:
    if _KEYLESS_NOTED in memory:
        return
    memory[_KEYLESS_NOTED] = True
    _tell(
        memory,
        "no OpenAlex API key: BibVault sends OpenAlex at most "
        f"{KEYLESS_DAILY_REQUESTS} requests a day, only for papers "
        f"without a local PDF, and no title searches; {_LOGIN_HINT}",
    )


def _spend_keyless_request(library: Library, memory: dict[str, object]) -> None:
    used = keyless_requests_today(library)
    if used >= KEYLESS_DAILY_REQUESTS:
        at = _next_utc_midnight().astimezone().strftime("%H:%M")
        message = (
            f"BibVault's {KEYLESS_DAILY_REQUESTS} OpenAlex requests a day without an API key "
            f"are used up until {at}; {_LOGIN_HINT}"
        )
        memory[_STOPPED] = (message, True)
        _tell(memory, message)
        raise OpenAlexUnavailable(message, rate_limited=True)
    today = datetime.now(UTC).date().isoformat()
    library.cache_put(_KEYLESS, json.dumps({"day": today, "used": used + 1}))


def keyless_requests_today(library: Library) -> int:
    """OpenAlex requests sent without a key since midnight UTC."""

    raw = library.cache_get(_KEYLESS)
    if raw is None:
        return 0
    try:
        entry = json.loads(raw)
        if entry["day"] != datetime.now(UTC).date().isoformat():
            return 0
        return int(entry["used"])
    except (ValueError, KeyError, TypeError):
        return 0


def openalex_paused_until(library: Library, settings: Settings) -> datetime | None:
    """When the daily budget that the current key (or no key) used up refills."""

    raw = library.cache_get(_PAUSED)
    if raw is None:
        return None
    try:
        pause = json.loads(raw)
        until = datetime.fromisoformat(pause["until"])
        key = pause["key"]
    except (ValueError, KeyError, TypeError):
        return None
    if key != _key_id(settings) or until <= datetime.now(UTC):
        return None
    return until


def _refusal(response: httpx.Response, settings: Settings) -> tuple[str, bool, datetime | None] | None:
    """Message, rate-limited flag, and pause end for answers that will repeat for every request."""

    if response.status_code == 401:
        if _key_id(settings):
            return "OpenAlex rejected the API key; run `bibvault auth login openalex` to replace it", False, None
        return "OpenAlex asks for an API key; run `bibvault auth login openalex` (keys are free)", False, None
    if response.status_code != 429:
        return None
    if _per_second_limit(response):
        return "OpenAlex kept answering HTTP 429 (too many requests per second)", True, None
    until = datetime.now(UTC) + timedelta(seconds=_reset_seconds(response))
    return _budget_message(until, settings), True, until


def _per_second_limit(response: httpx.Response) -> bool:
    # A 429 with budget left means too many requests per second, which a short wait cures.
    try:
        return float(response.headers["x-ratelimit-remaining"]) > 0
    except (KeyError, ValueError):
        return False


def _reset_seconds(response: httpx.Response) -> float:
    try:
        return max(0.0, float(response.headers["x-ratelimit-reset"]))
    except (KeyError, ValueError):
        return (_next_utc_midnight() - datetime.now(UTC)).total_seconds()


def _next_utc_midnight() -> datetime:
    # OpenAlex budgets refill at midnight UTC.
    tomorrow = datetime.now(UTC) + timedelta(days=1)
    return tomorrow.replace(hour=0, minute=0, second=0, microsecond=0)


def _budget_message(until: datetime, settings: Settings) -> str:
    at = until.astimezone().strftime("%H:%M")
    if _key_id(settings):
        return f"OpenAlex daily budget of your API key is used up until {at}"
    return (
        f"OpenAlex daily budget for requests without an API key is used up until {at}; "
        "run `bibvault auth login openalex` for a budget of your own (free)"
    )


def _key_id(settings: Settings) -> str:
    # Tells keys apart without storing one.
    key = (settings.resolution.openalex_api_key or "").strip()
    return hashlib.sha256(key.encode()).hexdigest()[:12] if key else ""


async def check_openalex_key(key: str, settings: Settings) -> tuple[bool, str]:
    """Whether OpenAlex accepts the key, and what is left of today's budget if it says."""

    async with make_client(settings) as client:
        response = await client.get(
            "https://api.openalex.org/rate-limit", headers={"Authorization": f"Bearer {key}"}
        )
    if response.status_code in (401, 403):
        return False, ""
    response.raise_for_status()
    try:
        limits = response.json()["rate_limit"]
        left, total = float(limits["daily_remaining_usd"]), float(limits["daily_budget_usd"])
    except (ValueError, KeyError, TypeError):
        return True, ""
    return True, f"today's OpenAlex budget: ${left:.2f} of ${total:.2f} left"


async def _get_json(
    client: httpx.AsyncClient, url: str, params: dict[str, str], headers: dict[str, str]
) -> dict | None:
    delay = 0.5
    for attempt in range(3):
        response = await client.get(url, params=params, headers=headers)
        if response.status_code == 404:
            return None
        retry = response.status_code >= 500 or (
            response.status_code == 429 and _per_second_limit(response)
        )
        if retry and attempt < 2:
            await asyncio.sleep(delay)
            delay *= 2
            continue
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            return None
        return payload
    return None


def _mailto(settings: Settings) -> dict[str, str]:
    email = (settings.resolution.email or "").strip()
    if not email:
        return {}
    return {"mailto": email}


def _api_key_header(settings: Settings) -> dict[str, str]:
    # A header, not ?api_key=: httpx logs request URLs, and the key must stay out of logs.
    key = (settings.resolution.openalex_api_key or "").strip()
    if not key:
        return {}
    return {"Authorization": f"Bearer {key}"}
