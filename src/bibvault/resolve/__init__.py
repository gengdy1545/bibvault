"""Find openly accessible PDF URLs for a reference."""

from bibvault.resolve.candidates import resolve_candidates
from bibvault.resolve.models import Lookup, OpenAlexUnavailable, Resolution
from bibvault.resolve.openalex import (
    KEYLESS_DAILY_REQUESTS,
    check_openalex_key,
    keyless_requests_today,
    openalex_paused_until,
)
from bibvault.resolve.ranking import is_pdf_url, map_version, sort_candidates, version_ranks
from bibvault.resolve.usenix import usenix_conference as _usenix_conference
from bibvault.resolve.vldb import is_pvldb

__all__ = [
    "KEYLESS_DAILY_REQUESTS",
    "Lookup",
    "OpenAlexUnavailable",
    "Resolution",
    "_usenix_conference",
    "check_openalex_key",
    "is_pdf_url",
    "is_pvldb",
    "keyless_requests_today",
    "map_version",
    "openalex_paused_until",
    "resolve_candidates",
    "sort_candidates",
    "version_ranks",
]
