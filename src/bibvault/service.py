"""Application operations shared by the CLI and the local MCP server.

Callers pass a :class:`bibvault.storage.Library` and receive plain dictionaries.
This module does not parse CLI output or start a subprocess.
"""

from collections.abc import Callable

from bibvault.config import Settings
from bibvault.database_sources import source_descriptors
from bibvault.identity import normalize_doi, reference_id
from bibvault.models import AcquireResult, Reference
from bibvault.pipeline import lookup_reference, reconcile, run_fetch, summarize
from bibvault.storage import Library

SCHEMA_VERSION = 1
AVAILABILITY_AVAILABLE = "available"
AVAILABILITY_MISSING = "missing"
AVAILABILITY_UNKNOWN = "unknown"
EXIT_OK = 0
EXIT_INCOMPLETE = 1
EXIT_USAGE = 2

_AVAILABLE_STATUSES = frozenset({"downloaded", "already_exists"})


def locate_reference(library: Library, citation_key: str) -> dict:
    """Return one citation key, including an explicit unknown-key record."""

    found = lookup_reference(library, citation_key)
    if found is None:
        return {
            "schema_version": SCHEMA_VERSION,
            "citation_key": citation_key,
            "availability": AVAILABILITY_UNKNOWN,
            "status": "unknown",
            "reference_id": None,
            "type": None,
            "title": None,
            "version": None,
            "path": None,
            "provider": None,
            "files": {},
            "error": f"unknown citation key: {citation_key}",
            "error_category": "unknown_key",
        }
    path = None
    provider = None
    for role in ("pdf", "markdown"):
        record = found["files"].get(role) or {}
        if record.get("path"):
            path = record["path"]
            provider = record.get("provider")
            break
    availability = AVAILABILITY_AVAILABLE if path else AVAILABILITY_MISSING
    view = {
        "schema_version": SCHEMA_VERSION,
        **found,
        "availability": availability,
        "path": path,
        "provider": provider,
        "error_category": None if availability == AVAILABILITY_AVAILABLE else found.get("status"),
    }
    return view


def locate_exit(view: dict) -> int:
    if view["availability"] == AVAILABILITY_UNKNOWN:
        return EXIT_USAGE
    if view["availability"] == AVAILABILITY_MISSING:
        return EXIT_INCOMPLETE
    return EXIT_OK


def status_report(library: Library, references: list[Reference] | None = None) -> dict:
    """List stored keys. With BibTeX, also count coverage against that set."""

    rows = [locate_reference(library, key.citation_key) for key in library.all_keys()]
    coverage = None if references is None else _coverage(library, references)
    return {"schema_version": SCHEMA_VERSION, "references": rows, "coverage": coverage}


def find_references(
    library: Library,
    *,
    doi: str | None = None,
    title: str | None = None,
) -> list[dict]:
    """Find stored citation keys by DOI, title, or both, without contacting the network.

    When both are set, a row must match both. The CLI still requires exactly one.
    """

    doi_text = (doi or "").strip()
    title_text = (title or "").strip()
    if not doi_text and not title_text:
        raise ValueError("pass doi, title, or both")
    if doi_text and normalize_doi(doi_text) is None:
        raise ValueError("doi is empty")
    keys = library.search_keys(doi=doi_text or None, title=title_text or None)
    return [locate_reference(library, key.citation_key) for key in keys]


def list_sources() -> list[dict]:
    """Describe official database sources from the existing source registry."""

    return [
        {"provider": item.provider, "hosts": list(item.hosts), "access": item.access}
        for item in source_descriptors()
    ]


def plan_prune(library: Library, references: list[Reference]) -> list[str]:
    """Citation keys that are absent from ``references`` and not the same publication.

    Read-only. A key whose publication is still cited under another name is a
    rename, not a leftover, and is left for reconcile.
    """

    active = {item.citation_key for item in references}
    referenced = {reference_id(item) for item in references}
    return [
        key.citation_key
        for key in library.all_keys()
        if key.citation_key not in active and key.reference_id not in referenced
    ]


def apply_prune(library: Library, references: list[Reference]) -> list[str]:
    """Release leftover citation keys. PDF bytes move aside; they are not deleted."""

    reconcile(library, references)
    released = plan_prune(library, references)
    for name in released:
        key = library.get_key(name)
        if key is not None:
            library.release_key(key)
    library.export_manifest()
    return released


async def acquire(
    library: Library,
    references: list[Reference],
    settings: Settings,
    *,
    only_keys: list[str] | None = None,
    browser: bool = False,
    upgrade: bool = False,
    open_url: Callable[[str], None] | None = None,
    on_status: Callable[[str], None] | None = None,
    on_progress: Callable[[int, int, str], None] | None = None,
    confirm: Callable[[str], str] | None = None,
) -> list[AcquireResult]:
    """Download or upgrade references. MCP should call this instead of the CLI."""

    return await run_fetch(
        library,
        references,
        settings,
        only_keys=only_keys,
        browser=browser,
        upgrade=upgrade,
        open_url=open_url,
        on_status=on_status,
        on_progress=on_progress,
        confirm=confirm,
    )


def acquisition_document(results: list[AcquireResult], *, pruned: list[str] | None = None) -> dict:
    document = {
        "schema_version": SCHEMA_VERSION,
        "summary": summarize(results),
        "results": [_acquisition_view(item) for item in results],
    }
    if pruned is not None:
        document["pruned"] = pruned
    return document


def acquisition_exit(results: list[AcquireResult]) -> int:
    if any(item.status == "failed" for item in results):
        return EXIT_INCOMPLETE
    return EXIT_OK


def _coverage(library: Library, references: list[Reference]) -> dict:
    stored = {key.citation_key: key for key in library.all_keys()}
    available: list[str] = []
    missing: list[str] = []
    absent: list[str] = []
    seen: set[str] = set()
    for reference in references:
        if reference.citation_key in seen:
            continue
        seen.add(reference.citation_key)
        record = stored.get(reference.citation_key)
        if record is None:
            absent.append(reference.citation_key)
            continue
        view = locate_reference(library, reference.citation_key)
        if view["availability"] == AVAILABILITY_AVAILABLE:
            available.append(reference.citation_key)
        else:
            missing.append(reference.citation_key)
    leftover = [name for name in stored if name not in seen]
    return {
        "available_locally": len(available),
        "known_missing_file": len(missing),
        "not_in_library": len(absent),
        "leftover": len(leftover),
        "keys": {
            "available_locally": available,
            "known_missing_file": missing,
            "not_in_library": absent,
            "leftover": leftover,
        },
    }


def _acquisition_view(item: AcquireResult) -> dict:
    available = item.status in _AVAILABLE_STATUSES
    payload = item.model_dump()
    payload["schema_version"] = SCHEMA_VERSION
    payload["availability"] = AVAILABILITY_AVAILABLE if available else AVAILABILITY_MISSING
    payload["error_category"] = None if available else item.status
    files: dict[str, dict] = {}
    if item.path:
        role = "markdown" if item.path.endswith(".md") else "pdf"
        files[role] = {"path": item.path, "provider": item.provider, "version": item.version}
    payload["files"] = files
    return payload
