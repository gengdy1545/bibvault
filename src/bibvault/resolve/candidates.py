"""Assemble direct, publisher, arXiv, and OpenAlex candidates for one reference."""

import logging

import httpx

from bibvault.config import Settings
from bibvault.models import Candidate, Reference
from bibvault.resolve.models import Lookup, OpenAlexUnavailable, Resolution
from bibvault.resolve.openalex import openalex_doi, openalex_title
from bibvault.resolve.publishers import publisher_pdf
from bibvault.resolve.ranking import is_pdf_url, sort_candidates
from bibvault.storage import Library

logger = logging.getLogger(__name__)


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
    published = await publisher_pdf(reference, client, library, settings, memory)
    if published:
        found.append(published)
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
            lookup = await openalex_doi(reference, client, library, settings, memory)
        elif (
            use_openalex
            and reference.title
            and not reference.arxiv_id
            and not is_pdf_url(reference.url)
        ):
            lookup = await openalex_title(reference, client, library, settings, memory)
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
