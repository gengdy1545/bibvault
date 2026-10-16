"""Text reports, progress display, and one-reference lookups."""

from __future__ import annotations

import asyncio
import logging
import shutil
import sys
from collections import Counter
from collections.abc import Callable
from typing import TextIO

from bibvault.models import AcquireResult
from bibvault.storage import Library

logger = logging.getLogger(__name__)

# Statuses listed under "Could not acquire" in the slim text report.
_PROBLEM_STATUSES = frozenset(
    {
        "authentication_required",
        "manual_action_required",
        "access_denied",
        "not_found",
        "no_open_access_found",
        "rate_limited",
        "failed",
    }
)


def lookup_reference(library: Library, citation_key: str) -> dict | None:
    key = library.get_key(citation_key)
    if key is None:
        return None
    files: dict[str, dict] = {}
    for record in library.files_for(key.reference_id):
        located = library.resolve_file(key.filename_stem, record)
        files[record.role] = {
            "path": None if located is None else library.relative(located),
            "sha256": record.sha256,
            "source_url": record.source_url,
            "provider": record.provider,
            "retrieved_at": record.retrieved_at,
            "version": record.version,
            "byte_size": record.byte_size,
        }
    preferred = files.get("pdf") or files.get("markdown")
    return {
        "citation_key": key.citation_key,
        "reference_id": key.reference_id,
        "type": key.entry_type,
        "title": key.title,
        "status": key.status,
        "version": None if preferred is None else preferred["version"],
        "files": files,
        "error": key.error_detail,
    }


def summarize(results: list[AcquireResult]) -> dict[str, int]:
    counts = Counter(item.status for item in results)
    downloaded = counts["downloaded"]
    present = counts["already_exists"]
    return {
        "references": len(results),
        "available_locally": downloaded + present,
        "newly_downloaded": downloaded,
        "already_present": present,
        "authentication_required": counts["authentication_required"],
        "no_open_access_found": counts["no_open_access_found"],
        "access_denied": counts["access_denied"],
        "not_found": counts["not_found"],
        "manual_action_required": counts["manual_action_required"],
        "unsupported": counts["unsupported"],
        "rate_limited": counts["rate_limited"],
        "failed": counts["failed"],
    }


def format_report(results: list[AcquireResult]) -> str:
    """Slim text report: counts plus only the papers that still need attention."""

    summary = summarize(results)
    problems = [item for item in results if item.status in _PROBLEM_STATUSES]
    lines = [
        "BibVault Acquisition Report",
        "",
        f"Already on disk:   {summary['already_present']:4d}",
        f"Newly downloaded:  {summary['newly_downloaded']:4d}",
        f"Could not acquire: {len(problems):4d}",
    ]
    if summary["unsupported"]:
        lines.append(f"Unsupported:       {summary['unsupported']:4d}")
    if problems:
        lines.append("")
        lines.append("Could not acquire:")
        for item in problems:
            reason = item.error or item.status
            lines.append(f"  {item.citation_key} ({item.status}): {reason}")
    return "\n".join(lines)


def ascii_progress(done: int, total: int, label: str = "", width: int = 20) -> str:
    if total <= 0:
        return ""
    filled = min(width, int(width * done / total))
    bar = "#" * filled + "-" * (width - filled)
    prefix = f"{label} " if label else ""
    return f"{prefix}[{bar}] {done}/{total}"


class FetchDisplay:
    """Keep the progress line as the first line and print later messages under it.

    A terminal rewrites that line with ANSI cursor motion. A pipe or other
    non-TTY stream gets plain lines instead, so warnings stay in order on stderr.
    """

    def __init__(self, stream: TextIO | None = None, *, tty: bool | None = None) -> None:
        self.stream = sys.stderr if stream is None else stream
        self.lines_below = 0
        self.visible = False
        if tty is None:
            isatty = getattr(self.stream, "isatty", None)
            tty = bool(isatty and isatty())
        self.tty = tty

    def progress(self, done: int, total: int, label: str = "") -> None:
        line = ascii_progress(done, total, label)
        if not line:
            return
        if not self.tty:
            self.stream.write(line + "\n")
            self.stream.flush()
            return
        if not self.visible:
            self.stream.write(line + "\n")
            self.visible = True
            self.stream.flush()
            return
        up = self.lines_below + 1
        self.stream.write(f"\033[{up}A\r\033[K{line}\033[{up}B")
        self.stream.flush()

    def status(self, message: str) -> None:
        text = message.rstrip("\n")
        self.stream.write(text + "\n")
        self.lines_below += _terminal_rows(text)
        self.stream.flush()


def _terminal_rows(text: str) -> int:
    width = shutil.get_terminal_size(fallback=(80, 24)).columns or 80
    rows = 0
    for line in text.splitlines() or [""]:
        rows += max(1, (len(line) + width - 1) // width)
    return rows


class _RunProgress:
    """One step per publication in this run, including files already on disk."""

    def __init__(
        self,
        total: int,
        done: int,
        on_progress: Callable[[int, int, str], None] | None,
    ) -> None:
        self.total = total
        self.done = done
        self.pending: set[str] = set()
        self.on_progress = on_progress
        self._lock = asyncio.Lock()

    def show(self) -> None:
        self._emit()

    async def finish(self, ref_id: str) -> None:
        async with self._lock:
            if ref_id not in self.pending:
                return
            self.pending.remove(ref_id)
            self.done += 1
            self._emit()

    def _emit(self) -> None:
        if self.on_progress is not None and self.total:
            self.on_progress(self.done, self.total, "Fetching")


def _say(on_status: Callable[[str], None] | None, message: str) -> None:
    if on_status is not None:
        on_status(message)
    else:
        logger.warning("%s", message)
