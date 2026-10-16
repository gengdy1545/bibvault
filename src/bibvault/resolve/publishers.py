"""Choose a published PDF from the sources BibVault already knows.

The checks stay an explicit sequence. This is not a publisher plugin registry.
"""

from urllib.parse import urlparse

import httpx

from bibvault.config import Settings
from bibvault.database_sources import (
    SourceMatch,
    official_source_kind,
    resolve_dblp_fallback,
    resolve_official_database_source,
    should_try_dblp,
)
from bibvault.models import Candidate, Reference
from bibvault.resolve.cidr import cidr_pdf, is_cidr
from bibvault.resolve.ranking import is_pdf_url
from bibvault.resolve.usenix import USENIX_HOSTS, usenix_conference, usenix_pdf
from bibvault.resolve.vldb import is_pvldb, is_vldb_conference, vldb_conference_pdf, vldb_pdf
from bibvault.storage import Library

# OpenProceedings (EDBT, ICDT) registers DOIs that lead straight to the PDF.
_OPENPROCEEDINGS_DOIS = ("10.5441/002/", "10.48786/")


async def publisher_pdf(
    reference: Reference,
    client: httpx.AsyncClient,
    library: Library,
    settings: Settings,
    memory: dict[str, object],
) -> Candidate | None:
    """A published PDF on an official site that serves it openly."""

    known_source = official_source_kind(reference) is not None
    if is_pvldb(reference):
        known_source = True
        pdf = await vldb_pdf(reference, client, library, settings, memory)
        return published_candidate("vldb", pdf) if pdf else None
    if reference.doi and reference.doi.startswith(_OPENPROCEEDINGS_DOIS):
        return published_candidate(
            "openproceedings",
            f"https://doi.org/{reference.doi}",
        )
    if is_pdf_url(reference.url):
        return None
    official = await resolve_official_database_source(
        reference,
        client,
        library,
        settings.resolution.title_match_threshold,
        memory,
    )
    if official:
        return published_source(official)
    if urlparse(reference.url or "").hostname in USENIX_HOSTS or usenix_conference(reference):
        known_source = True
    if pdf := await usenix_pdf(reference, client, library, settings, memory):
        return published_candidate("usenix", pdf)
    if is_cidr(reference):
        known_source = True
    if pdf := await cidr_pdf(reference, client, library, settings, memory):
        return published_candidate("cidr", pdf)
    if is_vldb_conference(reference):
        known_source = True
    if pdf := await vldb_conference_pdf(reference, client, library, settings, memory):
        return published_candidate("vldb", pdf)
    if not known_source and should_try_dblp(reference):
        match = await resolve_dblp_fallback(
            reference,
            client,
            library,
            settings.resolution.title_match_threshold,
            memory,
        )
        if match:
            return published_source(match)
    return None


def published_candidate(
    provider: str,
    url: str,
    *,
    source_url: str | None = None,
) -> Candidate:
    return Candidate(
        url=url,
        provider=provider,
        version="published",
        source_rank=1,
        source_url=source_url,
    )


def published_source(match: SourceMatch) -> Candidate:
    return published_candidate(
        match.provider,
        match.url,
        source_url=match.source_url,
    )
