"""PVLDB journal volumes and VLDB conference proceedings on vldb.org."""

import asyncio
import json
import logging
import re
import unicodedata
from difflib import SequenceMatcher
from urllib.parse import urlparse

import httpx

from bibvault.config import Settings
from bibvault.models import Reference
from bibvault.resolve.pages import Link, Page, fetch_page, pick_link
from bibvault.resolve.ranking import is_pdf_url
from bibvault.resolve.textutil import normalize_title
from bibvault.storage import Library

logger = logging.getLogger(__name__)

_PVLDB_VENUES = {"proc vldb endow", "pvldb", "proceedings of the vldb endowment"}
VLDB_HOSTS = {"vldb.org", "www.vldb.org"}
_NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.DOTALL)

# Later VLDB papers are in PVLDB.
_LAST_VLDB_CONFERENCE = 2007
_PAGE_FILE = re.compile(r"p0*(\d+)(?:-[^/]*)?\.pdf", re.IGNORECASE)


def is_pvldb(reference: Reference) -> bool:
    if reference.doi and reference.doi.startswith("10.14778/"):
        return True
    return normalize_title(reference.venue) in _PVLDB_VENUES


async def vldb_pdf(
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
    cache_key = f"vldb:{volume}:{normalize_title(reference.title)}"
    cached = library.cache_get(cache_key)
    if cached:
        return cached
    volume_key = f"vldb:volume:{volume}"
    papers = memory.get(volume_key)
    if not isinstance(papers, asyncio.Future):
        papers = asyncio.ensure_future(vldb_volume(client, volume))
        memory[volume_key] = papers
    pdf = pick_vldb_paper(reference, await papers, settings.resolution.title_match_threshold)
    if pdf is None:
        pdf = await guessed_vldb_pdf(reference, client, volume)
    if pdf:
        library.cache_put(cache_key, pdf)
    return pdf


async def vldb_volume(client: httpx.AsyncClient, volume: str) -> list[dict]:
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


def pick_vldb_paper(reference: Reference, papers: list[dict], threshold: float) -> str | None:
    wanted = normalize_title(reference.title)
    first_page = page_number(reference)
    scored = [
        (SequenceMatcher(None, wanted, normalize_title(paper["title"])).ratio(), paper)
        for paper in papers
        if urlparse(paper["pdf"]).hostname in VLDB_HOSTS
    ]
    # The start page already identifies the paper; the title only has to rule out a wrong page.
    on_page = [paper for ratio, paper in scored if ratio >= 0.8 and str(paper.get("start_page")) == first_page]
    by_title = [paper for ratio, paper in scored if ratio >= threshold]
    matches = on_page or by_title
    return matches[0]["pdf"] if matches else None


async def guessed_vldb_pdf(reference: Reference, client: httpx.AsyncClient, volume: str) -> str | None:
    """Older volume pages miss papers whose files still follow ``p{page}-{first author}.pdf``."""

    first_page = page_number(reference)
    surname = author_surname(reference.authors[0]) if reference.authors else ""
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


def page_number(reference: Reference) -> str | None:
    match = re.match(r"\s*(\d+)", reference.pages or "")
    return match.group(1) if match else None


def author_surname(author: str) -> str:
    name = author.replace("{", "").replace("}", "").strip()
    last = name.split(",")[0] if "," in name else (name.split() or [""])[-1]
    return "".join(char for char in unicodedata.normalize("NFKD", last) if char.isascii() and char.isalnum()).lower()


def is_vldb_conference(reference: Reference) -> bool:
    words = normalize_title(reference.venue).split()
    return ("very large data bases" in " ".join(words) or "vldb" in words) and "journal" not in words


async def vldb_conference_pdf(
    reference: Reference,
    client: httpx.AsyncClient,
    library: Library,
    settings: Settings,
    memory: dict[str, object],
) -> str | None:
    """Find a VLDB conference paper in its year's proceedings on vldb.org."""

    year = (reference.year or "").strip()
    if not is_vldb_conference(reference) or not re.fullmatch(r"\d{4}", year) or not reference.title:
        return None
    if int(year) > _LAST_VLDB_CONFERENCE:
        return None
    cache_key = f"vldb:conf:{year}:{normalize_title(reference.title)}"
    cached = library.cache_get(cache_key)
    if cached:
        return cached
    proceedings_key = f"vldb:proceedings:{year}"
    files = memory.get(proceedings_key)
    if not isinstance(files, asyncio.Future):
        files = asyncio.ensure_future(vldb_proceedings(client, year))
        memory[proceedings_key] = files
    links = await files
    pdf = pick_link(reference, links, settings.resolution.title_match_threshold) or vldb_file_by_page(
        reference, links
    )
    if pdf:
        library.cache_put(cache_key, pdf)
    return pdf


async def vldb_proceedings(client: httpx.AsyncClient, year: str) -> list[Link]:
    folder = f"/conf/{year}/"
    page = await fetch_page(client, f"https://www.vldb.org{folder}")
    if page is None:
        return []
    files = vldb_files(page, folder)
    if files:
        return files
    # Some years list the papers in a frame, one keeps them in a subdirectory.
    subdirectories = [
        part.url
        for part in page.parts
        if isinstance(part, Link) and in_folder(part.url, folder) and re.fullmatch(r"[^/]+/", urlparse(part.url).path[len(folder) :])
    ]
    for url in page.frames + subdirectories:
        if in_folder(url, folder) and (inner := await fetch_page(client, url)):
            files += vldb_files(inner, folder)
    return files


def vldb_files(page: Page, folder: str) -> list[Link]:
    return [part for part in page.parts if isinstance(part, Link) and in_folder(part.url, folder) and is_pdf_url(part.url)]


def in_folder(url: str, folder: str) -> bool:
    parts = urlparse(url)
    return parts.hostname in VLDB_HOSTS and parts.path.startswith(folder)


def vldb_file_by_page(reference: Reference, links: list[Link]) -> str | None:
    """Index pages give no titles, but most years name the files after the first page: P169.pdf, p553-stonebraker.pdf."""

    first_page = page_number(reference)
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
