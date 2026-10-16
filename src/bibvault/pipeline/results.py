"""Shared acquisition results and status selection."""

from __future__ import annotations

from bibvault.models import AcquireResult, Reference
from bibvault.resolve import Resolution, is_pdf_url
from bibvault.storage import Library

_WEB_TYPES = {"online", "misc", "software", "dataset", "electronic", "www", "webpage"}

_PROBLEM_ORDER = (
    "authentication_required",
    "manual_action_required",
    "access_denied",
    "rate_limited",
    "failed",
    "not_found",
)


def _web_only(reference: Reference) -> bool:
    if reference.entry_type not in _WEB_TYPES:
        return False
    if reference.doi or reference.arxiv_id:
        return False
    return not is_pdf_url(reference.url)


def _final_status(
    *,
    stored_new: bool,
    present: bool,
    problems: dict[str, str],
    resolution: Resolution,
) -> tuple[str, str | None]:
    if stored_new:
        return "downloaded", None
    if present:
        return "already_exists", None
    for status in _PROBLEM_ORDER:
        if status in problems:
            return status, problems[status]
    if resolution.error:
        return "failed", resolution.error
    if resolution.landing_url:
        return (
            "manual_action_required",
            f"open-access page has no direct PDF link: {resolution.landing_url}; "
            "download it and run `bibvault import`",
        )
    if resolution.found:
        return (
            "no_open_access_found",
            "OpenAlex lists no open-access copy; if you can get the PDF, run `bibvault import`",
        )
    return "not_found", "no matching publication or PDF found"


def _preferred_path(library: Library, reference_id_value: str, stem: str) -> str | None:
    record = library.preferred_file(reference_id_value)
    if record is None:
        return None
    located = library.resolve_file(stem, record)
    return None if located is None else library.relative(located)


def _result(
    reference: Reference,
    ref_id: str,
    status: str,
    *,
    path: str | None = None,
    provider: str | None = None,
    version: str | None = None,
    error: str | None = None,
) -> AcquireResult:
    return AcquireResult(
        citation_key=reference.citation_key,
        title=reference.title,
        reference_id=ref_id,
        status=status,
        path=path,
        provider=provider,
        version=version,
        error=error,
    )
