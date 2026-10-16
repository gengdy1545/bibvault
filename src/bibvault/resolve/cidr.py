"""CIDR program pages on cidrdb.org, including older framed programs."""

import asyncio
import re
from urllib.parse import urlparse

import httpx

from bibvault.config import Settings
from bibvault.models import Reference
from bibvault.resolve.pages import Link, fetch_page, pick_link, plain_text
from bibvault.resolve.ranking import is_pdf_url
from bibvault.resolve.textutil import normalize_title
from bibvault.resolve.vldb import VLDB_HOSTS
from bibvault.storage import Library

_CIDR_HOSTS = {"cidrdb.org", "www.cidrdb.org"}


def is_cidr(reference: Reference) -> bool:
    venue = normalize_title(reference.venue)
    return "cidr" in venue.split() or "innovative data systems research" in venue


async def cidr_pdf(
    reference: Reference,
    client: httpx.AsyncClient,
    library: Library,
    settings: Settings,
    memory: dict[str, object],
) -> str | None:
    """Find the paper by title in its year's program on cidrdb.org."""

    year = (reference.year or "").strip()
    if not is_cidr(reference) or not re.fullmatch(r"\d{4}", year) or not reference.title:
        return None
    cache_key = f"cidr:{year}:{normalize_title(reference.title)}"
    cached = library.cache_get(cache_key)
    if cached:
        return cached
    program_key = f"cidr:program:{year}"
    papers = memory.get(program_key)
    if not isinstance(papers, asyncio.Future):
        papers = asyncio.ensure_future(cidr_program(client, year))
        memory[program_key] = papers
    pdf = pick_link(reference, await papers, settings.resolution.title_match_threshold)
    if pdf:
        library.cache_put(cache_key, pdf)
    return pdf


async def cidr_program(client: httpx.AsyncClient, year: str) -> list[Link]:
    page = await fetch_page(client, f"https://www.cidrdb.org/cidr{year}/program.html")
    if page is None:
        return []
    pages = [page]
    # Older programs sit in a frame.
    for frame in page.frames:
        if urlparse(frame).hostname in _CIDR_HOSTS and (inner := await fetch_page(client, frame)):
            pages.append(inner)
    return [paper for item in pages for paper in cidr_papers(item.parts)]


def cidr_papers(parts: list[str | Link]) -> list[Link]:
    """Pair each PDF with its title.

    Over the years the link has been the title itself, a "PDF" after the title,
    and an icon before the authors and title.
    """

    pdfs = [(index, part) for index, part in enumerate(parts) if isinstance(part, Link) and is_cidr_pdf(part.url)]
    papers = []
    for number, (index, link) in enumerate(pdfs):
        label = normalize_title(link.text)
        if label in {"pdf", "paper"}:
            start = pdfs[number - 1][0] + 1 if number else 0
            title = plain_text(parts[start:index])
        elif not label:
            end = pdfs[number + 1][0] if number + 1 < len(pdfs) else len(parts)
            title = plain_text(parts[index + 1 : end])
        else:
            title = link.text
        papers.append(Link(link.url, title))
    return papers


def is_cidr_pdf(url: str) -> bool:
    parts = urlparse(url)
    # Since 2025 the papers are kept on vldb.org.
    on_site = parts.hostname in _CIDR_HOSTS or (parts.hostname in VLDB_HOSTS and parts.path.startswith("/cidrdb/"))
    return on_site and is_pdf_url(url)
