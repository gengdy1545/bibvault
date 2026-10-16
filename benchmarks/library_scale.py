"""Time allocate_stem and manifest export at 100, 1,000, and 10,000 records.

The library is built with one SQL transaction. allocate_stem is then called once
per existing record for a new key, which is the full-table stem scan. Manifest
export runs once, with one file row per record and no PDF bytes on disk, so the
time is queries and path checks rather than hashing.

    uv run python benchmarks/library_scale.py
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

from bibvault.storage import Library

SIZES = (100, 1_000, 10_000)


def _library(root: Path) -> Library:
    vault = root / ".bibvault"
    return Library(root, root / "papers", vault / "index.sqlite", vault / "manifest.json")


def _seed(library: Library, count: int) -> None:
    now = "2020-01-01T00:00:00+00:00"
    publications = [
        (f"doi:10.1000/{index}", f"Title {index}", "2020", f"10.1000/{index}", None, now, now)
        for index in range(count)
    ]
    keys = [
        (
            f"key{index}",
            f"doi:10.1000/{index}",
            "article",
            f"Title {index}",
            f"key{index}",
            "resolved",
            None,
            None,
            now,
        )
        for index in range(count)
    ]
    files = [
        (
            f"doi:10.1000/{index}",
            "pdf",
            "published",
            f"papers/key{index}.pdf",
            "a" * 64,
            128,
            f"https://example.org/{index}.pdf",
            "direct",
            now,
        )
        for index in range(count)
    ]
    library.conn.execute("BEGIN")
    library.conn.executemany(
        """
        INSERT INTO publications (reference_id, title, year, doi, arxiv_id, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        publications,
    )
    library.conn.executemany(
        """
        INSERT INTO citation_keys (
            citation_key, reference_id, entry_type, title, filename_stem, status,
            error_category, error_detail, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        keys,
    )
    library.conn.executemany(
        """
        INSERT INTO files (
            reference_id, role, version, relative_path, sha256, byte_size,
            source_url, provider, retrieved_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        files,
    )
    library.conn.commit()


def _measure(count: int) -> tuple[float, float]:
    with tempfile.TemporaryDirectory() as directory:
        library = _library(Path(directory))
        _seed(library, count)
        started = time.perf_counter()
        for index in range(count):
            library.allocate_stem(f"fresh{index}", f"doi:10.2000/{index}")
        allocate_seconds = time.perf_counter() - started
        started = time.perf_counter()
        library.export_manifest()
        manifest_seconds = time.perf_counter() - started
        library.close()
    return allocate_seconds, manifest_seconds


def main() -> None:
    print(f"{'records':>8}  {'allocate_stem x N':>18}  {'export_manifest':>16}")
    for count in SIZES:
        allocate_seconds, manifest_seconds = _measure(count)
        print(f"{count:8d}  {allocate_seconds:17.3f}s  {manifest_seconds:15.3f}s")


if __name__ == "__main__":
    main()
