"""USENIX ATC, FAST, NSDI, and OSDI program pages."""

import asyncio
import re
from urllib.parse import urlparse

import httpx

from bibvault.config import Settings
from bibvault.models import Reference
from bibvault.resolve.pages import Link, fetch_page, pick_link
from bibvault.resolve.textutil import normalize_title
from bibvault.storage import Library

USENIX_HOSTS = {"usenix.org", "www.usenix.org"}
# usenix.org names a conference's pages after its short name and year, e.g. atc14.
_USENIX_CONFERENCES = {
    "annual technical conference": "atc",
    "file and storage technologies": "fast",
    "networked systems design": "nsdi",
    "operating systems design": "osdi",
}


def usenix_conference(reference: Reference) -> str | None:
    venue = normalize_title(reference.venue)
    year = (reference.year or "").strip()
    if "usenix" not in venue.split() or not re.fullmatch(r"\d{4}", year):
        return None
    for phrase, name in _USENIX_CONFERENCES.items():
        if phrase in venue:
            return f"{name}{year[2:]}"
    return None


async def usenix_pdf(
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

    given = reference.url if urlparse(reference.url or "").hostname in USENIX_HOSTS else None
    conference = None if given else usenix_conference(reference)
    if given:
        cache_key = f"usenix:{given}"
    elif conference and reference.title:
        cache_key = f"usenix:{conference}:{normalize_title(reference.title)}"
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
            talks = asyncio.ensure_future(usenix_program(client, conference))
            memory[program_key] = talks
        talk = pick_link(reference, await talks, settings.resolution.title_match_threshold)
    page = await fetch_page(client, talk) if talk else None
    pdf = page.meta.get("citation_pdf_url") if page else None
    if not pdf or urlparse(pdf).hostname not in USENIX_HOSTS:
        return None
    library.cache_put(cache_key, pdf)
    return pdf


async def usenix_program(client: httpx.AsyncClient, conference: str) -> list[Link]:
    page = await fetch_page(client, f"https://www.usenix.org/conference/{conference}/technical-sessions")
    if page is None:
        return []
    return [
        part
        for part in page.parts
        if isinstance(part, Link)
        and part.text
        and urlparse(part.url).hostname in USENIX_HOSTS
        and urlparse(part.url).path.startswith(f"/conference/{conference}/")
        and "/presentation/" in part.url
    ]
