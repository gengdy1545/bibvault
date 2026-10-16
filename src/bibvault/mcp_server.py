"""Local stdio MCP server.

The process is read-only unless it is started with ``--allow-write``. Tool
handlers call :mod:`bibvault.service` in-process. They do not parse CLI output
and they do not start ``fetch --browser``.
"""

import argparse
import logging
import secrets
import shlex
import sys
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from bibvault import __version__
from bibvault.browser import opens_in_browser
from bibvault.config import ConfigError, Settings, load_settings
from bibvault.errors import BibVaultError, FileCollisionError, ParseError
from bibvault.identity import reference_id
from bibvault.models import AcquireResult, Reference
from bibvault.parsing import parse_paths
from bibvault.pipeline import import_file
from bibvault.service import (
    acquire,
    acquisition_document,
    apply_prune,
    find_references,
    locate_reference,
    plan_prune,
)
from bibvault.storage import Library, sha256_file

logger = logging.getLogger(__name__)

READ_DOCUMENT_MAX_CHARS = 8000
SEARCH_LIMIT_DEFAULT = 20
SEARCH_LIMIT_MAX = 50
PRUNE_TTL = timedelta(minutes=10)

READ_TOOLS = (
    "library_info",
    "search_references",
    "get_reference",
    "read_document",
    "verify_library",
)
WRITE_TOOLS = (
    "fetch_references",
    "sync_references",
    "import_document",
    "upgrade_reference",
    "plan_prune",
    "apply_prune",
)
_PERSON_STATUSES = frozenset(
    {"manual_action_required", "authentication_required", "access_denied"}
)
_SUCCESS_STATUSES = frozenset({"downloaded", "already_exists"})


class _ToolFailure(Exception):
    def __init__(self, payload: dict[str, Any]) -> None:
        super().__init__(payload["error"]["message"])
        self.payload = payload


def tool_error(
    code: str,
    message: str,
    *,
    citation_key: str | None = None,
    cli: str | None = None,
    **extra: object,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "code": code,
        "message": message,
        "citation_key": citation_key,
        "cli": cli,
    }
    body.update(extra)
    return {"error": body}


@dataclass
class _PruneTicket:
    expires_at: datetime
    detach: tuple[str, ...]
    bib_paths: tuple[Path, ...]
    display: tuple[str, ...]
    delete_files: tuple[str, ...] = ()
    used: bool = False


class BibVaultSession:
    """In-process tool handlers. Tests call :meth:`call` directly."""

    def __init__(
        self,
        settings: Settings,
        *,
        allow_write: bool,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.settings = settings
        self.allow_write = allow_write
        self._now = now or (lambda: datetime.now(UTC))
        self._plans: dict[str, _PruneTicket] = {}

    async def call(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        if name in WRITE_TOOLS and not self.allow_write:
            return tool_error(
                "write_not_enabled",
                "this process is read-only; start bibvault-mcp with --allow-write",
            )
        handler = _HANDLERS.get(name)
        if handler is None:
            return tool_error("unknown_tool", f"unknown tool {name}")
        try:
            return await handler(self, arguments or {})
        except _ToolFailure as exc:
            return exc.payload
        except Exception as exc:
            logger.exception("tool %s failed", name)
            return tool_error("failed", str(exc) or exc.__class__.__name__)

    @contextmanager
    def _library(self):
        library = Library(
            self.settings.project_root,
            self.settings.library_root,
            self.settings.db_path,
            self.settings.manifest_path,
        )
        try:
            yield library
        finally:
            library.close()

    def _project_file(self, raw: str) -> Path:
        return _resolve_inside(raw, self.settings.project_root)

    def _bib_paths(self, raw: object) -> tuple[list[Path], list[str]]:
        display = _string_list(raw, "bib")
        return [self._project_file(path) for path in display], display


async def _library_info(session: BibVaultSession, _arguments: dict[str, Any]) -> dict[str, Any]:
    with session._library() as library:
        count = len(library.all_keys())
    config_path = session.settings.config_path
    return {
        "project_root": str(session.settings.project_root),
        "library_root": str(session.settings.library_root),
        "config_path": None if config_path is None else str(config_path),
        "read_only": not session.allow_write,
        "allow_write": session.allow_write,
        "reference_count": count,
        "read_document": {"max_chars_limit": READ_DOCUMENT_MAX_CHARS},
    }


async def _search_references(session: BibVaultSession, arguments: dict[str, Any]) -> dict[str, Any]:
    doi = arguments.get("doi")
    title = arguments.get("title")
    if doi is not None and not isinstance(doi, str):
        raise _ToolFailure(tool_error("invalid_argument", "doi must be a string"))
    if title is not None and not isinstance(title, str):
        raise _ToolFailure(tool_error("invalid_argument", "title must be a string"))
    limit = _optional_int(arguments.get("limit"), "limit")
    if limit is None:
        limit = SEARCH_LIMIT_DEFAULT
    elif limit < 1:
        raise _ToolFailure(tool_error("invalid_argument", "limit must be a positive integer"))
    cap = min(limit, SEARCH_LIMIT_MAX)
    with session._library() as library:
        try:
            found = find_references(library, doi=doi, title=title)
        except ValueError as exc:
            raise _ToolFailure(tool_error("invalid_argument", str(exc))) from exc
    shown = found[:cap]
    return {
        "matches": [
            {
                "citation_key": item["citation_key"],
                "title": item["title"],
                "reference_id": item["reference_id"],
                "status": item["status"],
                "path": item["path"],
            }
            for item in shown
        ],
        "returned": len(shown),
        "truncated": len(found) > len(shown),
    }


async def _get_reference(session: BibVaultSession, arguments: dict[str, Any]) -> dict[str, Any]:
    citation_key = _required_text(arguments.get("citation_key"), "citation_key")
    with session._library() as library:
        view = locate_reference(library, citation_key)
    if view["error_category"] == "unknown_key":
        return tool_error(
            "unknown_key",
            f"unknown citation key {citation_key}",
            citation_key=citation_key,
        )
    return view


async def _read_document(session: BibVaultSession, arguments: dict[str, Any]) -> dict[str, Any]:
    citation_key = _required_text(arguments.get("citation_key"), "citation_key")
    max_chars = _required_int(arguments.get("max_chars"), "max_chars")
    if not 1 <= max_chars <= READ_DOCUMENT_MAX_CHARS:
        raise _ToolFailure(
            tool_error(
                "invalid_argument",
                f"max_chars must be an integer from 1 through {READ_DOCUMENT_MAX_CHARS}",
                citation_key=citation_key,
            )
        )
    page = _optional_int(arguments.get("page"), "page")
    offset = _optional_int(arguments.get("offset"), "offset")
    with session._library() as library:
        view = locate_reference(library, citation_key)
        if view["error_category"] == "unknown_key":
            return tool_error(
                "unknown_key",
                f"unknown citation key {citation_key}",
                citation_key=citation_key,
            )
        role, record = _preferred_file(view)
        if role is None or record is None:
            return tool_error(
                "file_missing",
                f"no PDF or Markdown file for {citation_key}",
                citation_key=citation_key,
                status=view["status"],
            )
        path = _mapped_file(session, record["path"], citation_key)
        if role == "pdf":
            page_number = 1 if page is None else page
            text = _pdf_page(path, page_number, citation_key)
            response_page: int | None = page_number
            response_offset = None
        else:
            if offset is not None and offset < 0:
                raise _ToolFailure(
                    tool_error(
                        "invalid_argument",
                        "offset must be zero or greater",
                        citation_key=citation_key,
                    )
                )
            start = 0 if offset is None else offset
            text = _markdown_window(path, citation_key)
            text = text[start:]
            response_page = None
            response_offset = start
    truncated = len(text) > max_chars
    return {
        "citation_key": citation_key,
        "title": view["title"],
        "role": role,
        "page": response_page,
        "offset": response_offset,
        "text": text[:max_chars],
        "truncated": truncated,
        "sha256": sha256_file(path),
        "source_url": record.get("source_url"),
        "provider": record.get("provider"),
    }


async def _verify_library(session: BibVaultSession, arguments: dict[str, Any]) -> dict[str, Any]:
    references = arguments.get("references", False)
    if not isinstance(references, bool):
        raise _ToolFailure(tool_error("invalid_argument", "references must be a boolean"))
    with session._library() as library:
        problems = library.reference_problems() if references else library.checksum_problems()
    return {"ok": not problems, "problems": problems}


async def _fetch_references(session: BibVaultSession, arguments: dict[str, Any]) -> dict[str, Any]:
    paths, display = session._bib_paths(arguments.get("bib"))
    keys = _optional_keys(arguments.get("keys"))
    references = _parsed(paths)
    return await _run_acquire(session, references, display, only_keys=keys, upgrade=False)


async def _sync_references(session: BibVaultSession, arguments: dict[str, Any]) -> dict[str, Any]:
    paths, display = session._bib_paths(arguments.get("bib"))
    references = _parsed(paths)
    return await _run_acquire(session, references, display, only_keys=None, upgrade=False)


async def _import_document(session: BibVaultSession, arguments: dict[str, Any]) -> dict[str, Any]:
    citation_key = _required_text(arguments.get("citation_key"), "citation_key")
    raw_file = arguments.get("file")
    if not isinstance(raw_file, str) or not raw_file.strip():
        raise _ToolFailure(tool_error("invalid_argument", "file is required", citation_key=citation_key))
    source = session._project_file(raw_file)
    bib = arguments.get("bib")
    bib_path = None if bib is None else session._project_file(_required_text(bib, "bib"))
    source_url = arguments.get("source_url")
    if source_url is not None and not isinstance(source_url, str):
        raise _ToolFailure(tool_error("invalid_argument", "source_url must be a string", citation_key=citation_key))
    provider = _provider(arguments.get("provider"), citation_key)
    with session._library() as library:
        try:
            reference, ref_id = _reference_for_import(library, citation_key, bib_path)
            result = import_file(
                library,
                reference,
                source,
                ref_id=ref_id,
                source_url=source_url,
                provider=provider,
            )
        except ParseError as exc:
            code = "unknown_key" if exc.message.startswith("unknown citation key") else "invalid_argument"
            raise _ToolFailure(tool_error(code, exc.message, citation_key=citation_key)) from exc
        except (BibVaultError, FileCollisionError) as exc:
            raise _ToolFailure(tool_error("failed", exc.message, citation_key=citation_key)) from exc
    return acquisition_document([result])


async def _upgrade_reference(session: BibVaultSession, arguments: dict[str, Any]) -> dict[str, Any]:
    citation_key = _required_text(arguments.get("citation_key"), "citation_key")
    raw_bib = arguments.get("bib")
    if not isinstance(raw_bib, str) or not raw_bib.strip():
        raise _ToolFailure(tool_error("invalid_argument", "bib is required", citation_key=citation_key))
    path = session._project_file(raw_bib)
    references = _parsed([path])
    match = next((item for item in references if item.citation_key == citation_key), None)
    if match is None:
        raise _ToolFailure(
            tool_error(
                "invalid_argument",
                f"citation key not in BibTeX: {citation_key}",
                citation_key=citation_key,
            )
        )
    if opens_in_browser(match, session.settings):
        return _browser_upgrade(session, match, [raw_bib])
    return await _run_acquire(
        session,
        references,
        [raw_bib],
        only_keys=[citation_key],
        upgrade=True,
    )


async def _plan_prune(session: BibVaultSession, arguments: dict[str, Any]) -> dict[str, Any]:
    if arguments.get("confirm_complete_set") is not True:
        raise _ToolFailure(tool_error("invalid_argument", "confirm_complete_set must be true"))
    paths, display = session._bib_paths(arguments.get("bib"))
    references = _parsed(paths)
    with session._library() as library:
        detach = tuple(plan_prune(library, references))
    token = secrets.token_urlsafe(32)
    expires_at = session._now() + PRUNE_TTL
    session._plans[token] = _PruneTicket(
        expires_at=expires_at,
        detach=detach,
        bib_paths=tuple(paths),
        display=tuple(display),
    )
    return {
        "token": token,
        "expires_at": _iso_z(expires_at),
        "detach": list(detach),
        "delete_files": [],
    }


async def _apply_prune(session: BibVaultSession, arguments: dict[str, Any]) -> dict[str, Any]:
    token = arguments.get("token")
    if not isinstance(token, str) or not token:
        return tool_error("token_mismatch", "prune token is missing or does not match a plan from this process")
    ticket = session._plans.get(token)
    if ticket is None or ticket.used:
        return tool_error("token_mismatch", "prune token is missing, reused, or does not match a plan from this process")
    if session._now() >= ticket.expires_at:
        return tool_error("token_expired", "prune token has expired")
    if ticket.delete_files:
        return tool_error("invalid_argument", "apply_prune does not delete PDF or Markdown files")
    try:
        references = _parsed(list(ticket.bib_paths))
    except _ToolFailure as exc:
        return tool_error("token_mismatch", exc.payload["error"]["message"])
    with session._library() as library:
        current = tuple(plan_prune(library, references))
        if current != ticket.detach:
            return tool_error("token_mismatch", "the library no longer matches this prune plan")
        released = apply_prune(library, references)
        ticket.used = True
    return {"detach": released, "delete_files": []}


_HANDLERS = {
    "library_info": _library_info,
    "search_references": _search_references,
    "get_reference": _get_reference,
    "read_document": _read_document,
    "verify_library": _verify_library,
    "fetch_references": _fetch_references,
    "sync_references": _sync_references,
    "import_document": _import_document,
    "upgrade_reference": _upgrade_reference,
    "plan_prune": _plan_prune,
    "apply_prune": _apply_prune,
}


def _browser_upgrade(session: BibVaultSession, reference: Reference, display: list[str]) -> dict[str, Any]:
    """Refuse a browser publisher before any download, so the stored file stays put."""

    with session._library() as library:
        view = locate_reference(library, reference.citation_key)
    known = view["error_category"] != "unknown_key"
    result = AcquireResult(
        citation_key=reference.citation_key,
        title=view["title"] if known else reference.title,
        reference_id=view["reference_id"] if known and view["reference_id"] else reference_id(reference),
        status="manual_action_required",
        path=view["path"] if known else None,
        provider=view["provider"] if known else None,
        version=view["version"] if known else None,
        error="publisher PDF needs a visible browser; the stored file was not replaced",
    )
    document = acquisition_document([result])
    _annotate(document, [reference], display, upgrade=False, settings=session.settings)
    return document


async def _run_acquire(
    session: BibVaultSession,
    references: list[Reference],
    display: list[str],
    *,
    only_keys: list[str] | None,
    upgrade: bool,
) -> dict[str, Any]:
    with session._library() as library:
        try:
            results = await acquire(
                library,
                references,
                session.settings,
                only_keys=only_keys,
                browser=False,
                upgrade=upgrade,
            )
        except KeyError as exc:
            message = exc.args[0] if exc.args else "citation key not in BibTeX"
            raise _ToolFailure(tool_error("invalid_argument", str(message))) from exc
    document = acquisition_document(results)
    _annotate(document, references, display, upgrade=upgrade, settings=session.settings)
    return document


def _annotate(
    document: dict[str, Any],
    references: list[Reference],
    display: list[str],
    *,
    upgrade: bool,
    settings: Settings,
) -> None:
    by_key = {item.citation_key: item for item in references}
    for row in document["results"]:
        status = row["status"]
        if status in _SUCCESS_STATUSES:
            continue
        reference = by_key.get(row["citation_key"])
        browser = _browser_required(reference, settings, status, row.get("error"))
        if status not in _PERSON_STATUSES and not browser:
            continue
        row["cli"] = _command(
            display,
            row["citation_key"],
            browser=browser,
            upgrade=upgrade and not browser,
        )


def _browser_required(
    reference: Reference | None,
    settings: Settings,
    status: str,
    error: str | None,
) -> bool:
    if reference is not None and opens_in_browser(reference, settings):
        return True
    if status in {"authentication_required", "access_denied"}:
        return True
    text = (error or "").lower()
    return "browser" in text or "bot check" in text


def _command(bib: list[str], citation_key: str, *, browser: bool, upgrade: bool) -> str:
    parts = ["bibvault", "fetch", *bib, "--key", citation_key]
    if upgrade and not browser:
        parts.append("--upgrade")
    if browser:
        parts.append("--browser")
    return shlex.join(parts)


def _preferred_file(view: dict[str, Any]) -> tuple[str | None, dict[str, Any] | None]:
    files = view.get("files") or {}
    for role in ("pdf", "markdown"):
        record = files.get(role) or {}
        if record.get("path"):
            return role, record
    return None, None


def _mapped_file(session: BibVaultSession, stored: str, citation_key: str) -> Path:
    candidate = Path(stored)
    if not candidate.is_absolute():
        candidate = session.settings.project_root / candidate
    try:
        resolved = candidate.resolve()
    except OSError as exc:
        raise _ToolFailure(
            tool_error("path_not_allowed", f"{stored} is not an allowed path", citation_key=citation_key)
        ) from exc
    root = session.settings.library_root.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise _ToolFailure(
            tool_error(
                "path_not_allowed",
                f"{stored} resolves outside the library root",
                citation_key=citation_key,
            )
        ) from exc
    if not resolved.is_file():
        raise _ToolFailure(
            tool_error("file_missing", f"mapped file is missing: {stored}", citation_key=citation_key)
        )
    return resolved


def _pdf_page(path: Path, page: int, citation_key: str) -> str:
    if page < 1:
        raise _ToolFailure(
            tool_error("invalid_argument", "page must be 1 or greater", citation_key=citation_key)
        )
    import pymupdf

    try:
        document = pymupdf.open(path)
    except Exception as exc:
        raise _ToolFailure(
            tool_error("failed", f"could not open PDF: {exc}", citation_key=citation_key)
        ) from exc
    try:
        count = document.page_count
        if page > count:
            raise _ToolFailure(
                tool_error(
                    "invalid_argument",
                    f"page {page} is outside 1..{count}",
                    citation_key=citation_key,
                )
            )
        return str(document[page - 1].get_text())
    finally:
        document.close()


def _markdown_window(path: Path, citation_key: str) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise _ToolFailure(
            tool_error("failed", f"{path.name} is not UTF-8 Markdown", citation_key=citation_key)
        ) from exc


def _parsed(paths: list[Path]) -> list[Reference]:
    try:
        references, warnings = parse_paths(paths)
    except ParseError as exc:
        raise _ToolFailure(tool_error("invalid_argument", exc.message)) from exc
    except OSError as exc:
        raise _ToolFailure(tool_error("invalid_argument", f"could not read BibTeX: {exc}")) from exc
    for warning in warnings:
        logger.warning("%s", warning)
    return references


def _reference_for_import(
    library: Library, citation_key: str, bib: Path | None
) -> tuple[Reference, str | None]:
    if bib is not None:
        references = _parsed([bib])
        match = next((item for item in references if item.citation_key == citation_key), None)
        if match is None:
            raise ParseError(f"citation key not in BibTeX: {citation_key}")
        return match, None
    existing = library.get_key(citation_key)
    if existing is None:
        raise ParseError(f"unknown citation key {citation_key}; pass bib or sync the library first")
    publication = library.get_publication(existing.reference_id)
    reference = Reference(
        citation_key=existing.citation_key,
        entry_type=existing.entry_type or "misc",
        title=publication["title"] if publication else existing.title,
        year=publication["year"] if publication else None,
        doi=publication["doi"] if publication else None,
        arxiv_id=publication["arxiv_id"] if publication else None,
    )
    return reference, existing.reference_id


def _resolve_inside(raw: str, root: Path) -> Path:
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    try:
        resolved = candidate.resolve()
    except OSError as exc:
        raise _ToolFailure(tool_error("path_not_allowed", f"{raw} is not an allowed path")) from exc
    root_resolved = root.resolve()
    try:
        resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise _ToolFailure(tool_error("path_not_allowed", f"{raw} is outside {root_resolved}")) from exc
    if resolved == root_resolved or not resolved.is_file():
        raise _ToolFailure(tool_error("invalid_argument", f"file not found: {raw}"))
    return resolved


def _string_list(value: object, name: str) -> list[str]:
    if not isinstance(value, list) or not value or any(not isinstance(item, str) or not item.strip() for item in value):
        raise _ToolFailure(tool_error("invalid_argument", f"{name} must be a non-empty list of paths"))
    return list(value)


def _optional_keys(value: object) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise _ToolFailure(tool_error("invalid_argument", "keys must be a list of citation keys"))
    if not value:
        raise _ToolFailure(tool_error("invalid_argument", "keys must list at least one citation key"))
    return list(value)


def _required_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _ToolFailure(tool_error("invalid_argument", f"{name} is required"))
    return value.strip()


def _required_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _ToolFailure(tool_error("invalid_argument", f"{name} must be an integer"))
    return value


def _optional_int(value: object, name: str) -> int | None:
    if value is None:
        return None
    return _required_int(value, name)


def _provider(value: object, citation_key: str) -> str:
    if value is None:
        return "import"
    if not isinstance(value, str) or not value.strip():
        raise _ToolFailure(tool_error("invalid_argument", "provider must not be empty", citation_key=citation_key))
    return value.strip()


def _iso_z(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_server(session: BibVaultSession) -> Any:
    """Register the tools and hide write tools unless ``--allow-write`` was set."""

    try:
        from mcp.server import MCPServer
        from mcp.types import ToolAnnotations
    except ImportError as exc:
        raise ImportError("bibvault-mcp needs the optional mcp extra") from exc

    server = MCPServer(
        name="bibvault",
        instructions=(
            "Local BibVault library. Search, locate, paged read, and verify are always available. "
            "Fetch, sync, import, upgrade, and prune are advertised only when this process was "
            "started with --allow-write. This server never runs fetch --browser."
        ),
        version=__version__,
    )
    read_only = ToolAnnotations(read_only_hint=True, open_world_hint=False)
    local_write = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False)
    network_write = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True)

    def add(fn: Any, *, name: str, description: str, annotations: ToolAnnotations) -> None:
        server.add_tool(
            fn,
            name=name,
            description=description,
            annotations=annotations,
            structured_output=False,
        )

    async def library_info() -> dict[str, Any]:
        return await session.call("library_info")

    async def search_references(
        doi: str | None = None,
        title: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        return await session.call("search_references", {"doi": doi, "title": title, "limit": limit})

    async def get_reference(citation_key: str) -> dict[str, Any]:
        return await session.call("get_reference", {"citation_key": citation_key})

    async def read_document(
        citation_key: str,
        max_chars: int,
        page: int | None = None,
        offset: int | None = None,
    ) -> dict[str, Any]:
        return await session.call(
            "read_document",
            {"citation_key": citation_key, "max_chars": max_chars, "page": page, "offset": offset},
        )

    async def verify_library(references: bool = False) -> dict[str, Any]:
        return await session.call("verify_library", {"references": references})

    async def fetch_references(bib: list[str], keys: list[str] | None = None) -> dict[str, Any]:
        return await session.call("fetch_references", {"bib": bib, "keys": keys})

    async def sync_references(bib: list[str]) -> dict[str, Any]:
        return await session.call("sync_references", {"bib": bib})

    async def import_document(
        citation_key: str,
        file: str,
        bib: str | None = None,
        source_url: str | None = None,
        provider: str | None = None,
    ) -> dict[str, Any]:
        return await session.call(
            "import_document",
            {
                "citation_key": citation_key,
                "file": file,
                "bib": bib,
                "source_url": source_url,
                "provider": provider,
            },
        )

    async def upgrade_reference(citation_key: str, bib: str) -> dict[str, Any]:
        return await session.call("upgrade_reference", {"citation_key": citation_key, "bib": bib})

    async def plan_prune(bib: list[str], confirm_complete_set: bool) -> dict[str, Any]:
        return await session.call(
            "plan_prune",
            {"bib": bib, "confirm_complete_set": confirm_complete_set},
        )

    async def apply_prune(token: str) -> dict[str, Any]:
        return await session.call("apply_prune", {"token": token})

    add(library_info, name="library_info", description="Return library roots and whether write tools are advertised.", annotations=read_only)
    add(search_references, name="search_references", description="Find stored references by DOI, title, or both. Local metadata only.", annotations=read_only)
    add(get_reference, name="get_reference", description="Return one citation key with the same fields as bibvault locate --format json.", annotations=read_only)
    add(read_document, name="read_document", description="Read one PDF page or a Markdown window for a citation key inside the library.", annotations=read_only)
    add(verify_library, name="verify_library", description="Check stored checksums. Pass references=true to also check citation-key mappings.", annotations=read_only)
    add(fetch_references, name="fetch_references", description="Acquire missing files over HTTP. Does not launch a browser.", annotations=network_write)
    add(sync_references, name="sync_references", description="Reconcile a BibTeX set and acquire missing files over HTTP. Does not prune.", annotations=network_write)
    add(import_document, name="import_document", description="Register a local PDF, Markdown, or HTML file under an existing or BibTeX key.", annotations=local_write)
    add(upgrade_reference, name="upgrade_reference", description="Replace a stored file only when a higher-ranked HTTP download validates.", annotations=network_write)
    add(plan_prune, name="plan_prune", description="Dry-run mapping cleanup. Returns a short-lived token and never deletes files.", annotations=local_write)
    add(apply_prune, name="apply_prune", description="Apply the plan named by a token. Does not delete PDF or Markdown bytes.", annotations=local_write)

    if not session.allow_write:
        listed = server.list_tools

        async def list_tools() -> list[Any]:
            tools = await listed()
            return [tool for tool in tools if tool.name in READ_TOOLS]

        server.list_tools = list_tools  # type: ignore[method-assign]
    return server


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="bibvault-mcp",
        description="BibVault stdio MCP server. Read-only unless --allow-write is set.",
    )
    parser.add_argument("--config", type=Path, help="Path to .bibvault/config.toml")
    parser.add_argument(
        "--allow-write",
        action="store_true",
        help="Also advertise fetch, sync, import, upgrade, and prune tools",
    )
    args = parser.parse_args(argv)
    try:
        settings = load_settings(config_path=args.config)
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from exc
    logging.basicConfig(
        level=getattr(logging, settings.logging_level.upper(), logging.INFO),
        format="%(levelname)s %(message)s",
        stream=sys.stderr,
    )
    session = BibVaultSession(settings, allow_write=args.allow_write)
    try:
        server = build_server(session)
    except ImportError:
        print(
            "bibvault-mcp needs the optional mcp extra.\nInstall it with: uv tool install 'bibvault[mcp]'",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
