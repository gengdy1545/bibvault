"""SQLite mapping, citation-key files, and manifest export.

SQLite is the source of truth. ``manifest.json`` is rewritten from it.
File bytes for one publication are stored once per role; extra citation keys
link to those bytes when the filesystem allows.
"""

import contextlib
import hashlib
import json
import logging
import os
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from bibvault.errors import FileCollisionError
from bibvault.identity import normalize_doi
from bibvault.models import FileRecord, KeyRecord, Reference
from bibvault.naming import disambiguated_stem, filename_for, sanitize_citation_key

logger = logging.getLogger(__name__)

UNREFERENCED_DIR = "_unreferenced"
SCHEMA_VERSION = 2
# Version 1 wraps the citation-key map. Omitted version is the legacy map itself.
MANIFEST_VERSION = 1


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


class ManifestError(ValueError):
    """A manifest file cannot be read under the supported contract."""


def load_manifest(path: Path) -> dict[str, dict]:
    """Return the citation-key map from a versioned or legacy manifest.

    SQLite remains the source of truth. Version 1 stores that map under
    ``references``. A file with no integer ``manifest_version`` is the legacy
    map written before the version field existed. A newer version fails closed
    instead of being mistaken for citation keys.
    """

    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ManifestError("manifest must be a JSON object")
    version = document.get("manifest_version")
    if not isinstance(version, int):
        return document
    if version != MANIFEST_VERSION:
        raise ManifestError(
            f"manifest version {version} is not supported "
            f"(this BibVault writes version {MANIFEST_VERSION}); "
            "SQLite remains the source of truth"
        )
    references = document.get("references")
    if not isinstance(references, dict):
        raise ManifestError("manifest version 1 requires a references object")
    return references


class Library:
    def __init__(self, project_root: Path, library_root: Path, db_path: Path, manifest_path: Path) -> None:
        self.project_root = project_root
        self.library_root = library_root
        self.unreferenced_root = library_root / UNREFERENCED_DIR
        self.db_path = db_path
        self.manifest_path = manifest_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.library_root.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._migrate()
        # casefold(stem) -> reference_id -> row count. Built on first conflict check.
        self._stem_counts: dict[str, dict[str, int]] | None = None

    def close(self) -> None:
        self.conn.close()

    def _migrate(self) -> None:
        current = int(self.conn.execute("PRAGMA user_version").fetchone()[0])
        if current > SCHEMA_VERSION:
            raise RuntimeError(
                f"database schema version {current} is newer than supported version {SCHEMA_VERSION}"
            )
        migrations = (self._migrate_v1, self._migrate_v2)
        for target in range(current + 1, SCHEMA_VERSION + 1):
            self.conn.execute("BEGIN")
            try:
                migrations[target - 1]()
                self.conn.execute(f"PRAGMA user_version = {target}")
            except BaseException:
                self.conn.rollback()
                raise
            else:
                self.conn.commit()

    def _migrate_v1(self) -> None:
        statements = (
            """
            CREATE TABLE IF NOT EXISTS publications (
                reference_id TEXT PRIMARY KEY,
                title TEXT,
                year TEXT,
                doi TEXT,
                arxiv_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS citation_keys (
                citation_key TEXT PRIMARY KEY,
                reference_id TEXT NOT NULL,
                entry_type TEXT,
                title TEXT,
                filename_stem TEXT NOT NULL,
                status TEXT NOT NULL,
                error_category TEXT,
                error_detail TEXT,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (reference_id) REFERENCES publications(reference_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS files (
                reference_id TEXT NOT NULL,
                role TEXT NOT NULL,
                version TEXT NOT NULL,
                relative_path TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                byte_size INTEGER NOT NULL,
                source_url TEXT,
                provider TEXT,
                retrieved_at TEXT NOT NULL,
                PRIMARY KEY (reference_id, role),
                FOREIGN KEY (reference_id) REFERENCES publications(reference_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS resolution_cache (
                cache_key TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                fetched_at TEXT NOT NULL
            )
            """,
        )
        for statement in statements:
            self.conn.execute(statement)

    def _migrate_v2(self) -> None:
        self.conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_citation_reference
                ON citation_keys(reference_id)
            """
        )

    def upsert_publication(self, reference: Reference, reference_id: str) -> None:
        timestamp = now_iso()
        self.conn.execute(
            """
            INSERT INTO publications (reference_id, title, year, doi, arxiv_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(reference_id) DO UPDATE SET
                title = excluded.title,
                year = excluded.year,
                doi = excluded.doi,
                arxiv_id = excluded.arxiv_id,
                updated_at = excluded.updated_at
            """,
            (
                reference_id,
                reference.title,
                reference.year,
                reference.doi,
                reference.arxiv_id,
                timestamp,
                timestamp,
            ),
        )
        self.conn.commit()

    def get_publication(self, reference_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM publications WHERE reference_id = ?",
            (reference_id,),
        ).fetchone()

    def migrate_legacy_internal_identity(
        self,
        old_reference_id: str,
        new_reference_id: str,
        reference: Reference,
    ) -> bool:
        """Rebind a legacy internal id without moving bytes or rewriting provenance."""

        if old_reference_id == new_reference_id:
            return True
        if not old_reference_id.startswith("internal:") or not new_reference_id.startswith("internal:"):
            raise ValueError("internal identity migration requires internal ids")
        old = self.get_publication(old_reference_id)
        if old is None:
            return False
        conflict = self.conn.execute(
            """
            SELECT 1
            FROM files AS old
            JOIN files AS target ON target.role = old.role
            WHERE old.reference_id = ? AND target.reference_id = ?
            LIMIT 1
            """,
            (old_reference_id, new_reference_id),
        ).fetchone()
        if conflict is not None:
            logger.warning(
                "left legacy identity %s unchanged because %s already has stored provenance",
                old_reference_id,
                new_reference_id,
            )
            return False

        timestamp = now_iso()
        moved_stems = self._citation_stems(old_reference_id) if self._stem_counts is not None else []
        title = reference.title if reference.title is not None else old["title"]
        year = reference.year if reference.year is not None else old["year"]
        doi = reference.doi if reference.doi is not None else old["doi"]
        arxiv_id = reference.arxiv_id if reference.arxiv_id is not None else old["arxiv_id"]
        self.conn.execute("BEGIN")
        try:
            target = self.get_publication(new_reference_id)
            if target is None:
                self.conn.execute(
                    """
                    INSERT INTO publications (
                        reference_id, title, year, doi, arxiv_id, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        new_reference_id,
                        title,
                        year,
                        doi,
                        arxiv_id,
                        old["created_at"],
                        timestamp,
                    ),
                )
            else:
                self.conn.execute(
                    """
                    UPDATE publications
                    SET title = ?, year = ?, doi = ?, arxiv_id = ?, updated_at = ?
                    WHERE reference_id = ?
                    """,
                    (title, year, doi, arxiv_id, timestamp, new_reference_id),
                )
            self.conn.execute(
                "UPDATE files SET reference_id = ? WHERE reference_id = ?",
                (new_reference_id, old_reference_id),
            )
            self.conn.execute(
                "UPDATE citation_keys SET reference_id = ? WHERE reference_id = ?",
                (new_reference_id, old_reference_id),
            )
            self.conn.execute(
                "DELETE FROM publications WHERE reference_id = ?",
                (old_reference_id,),
            )
        except BaseException:
            self.conn.rollback()
            raise
        else:
            self.conn.commit()
            self._move_stem_counts(moved_stems, old_reference_id, new_reference_id)
        return True

    def allocate_stem(
        self,
        citation_key: str,
        reference_id: str,
        accept_sha256: str | None = None,
    ) -> str:
        """Pick the filename stem for a key.

        An existing file at ``{key}.pdf`` is reused only when its bytes already
        belong to this publication or equal ``accept_sha256``.
        """

        current = self.get_key(citation_key)
        if current and current.reference_id == reference_id:
            return current.filename_stem
        base = sanitize_citation_key(citation_key)
        if self._stem_conflicts(base, reference_id, accept_sha256):
            return disambiguated_stem(citation_key, reference_id)
        return base

    def _stem_conflicts(self, stem: str, reference_id: str, accept_sha256: str | None) -> bool:
        owners = self._stem_index().get(stem.casefold(), {})
        if any(owner != reference_id for owner in owners):
            return True
        allowed: set[str] | None = None
        for role in ("pdf", "markdown"):
            path = self.library_root / filename_for(stem, role)
            if not path.is_file():
                if path.exists():
                    return True
                continue
            if allowed is None:
                allowed = {record.sha256 for record in self.files_for(reference_id)}
                if accept_sha256:
                    allowed.add(accept_sha256)
            if sha256_file(path) not in allowed:
                return True
        return False

    def _stem_index(self) -> dict[str, dict[str, int]]:
        if self._stem_counts is None:
            counts: dict[str, dict[str, int]] = {}
            rows = self.conn.execute("SELECT filename_stem, reference_id FROM citation_keys")
            for row in rows:
                _bump_stem(counts, str(row["filename_stem"]), str(row["reference_id"]), 1)
            self._stem_counts = counts
        return self._stem_counts

    def _citation_stems(self, reference_id: str, stem: str | None = None) -> list[str]:
        if stem is None:
            rows = self.conn.execute(
                "SELECT filename_stem FROM citation_keys WHERE reference_id = ?",
                (reference_id,),
            )
        else:
            rows = self.conn.execute(
                "SELECT filename_stem FROM citation_keys WHERE reference_id = ? AND filename_stem = ?",
                (reference_id, stem),
            )
        return [str(row["filename_stem"]) for row in rows]

    def _move_stem_counts(
        self,
        stems: list[str],
        old_reference_id: str,
        new_reference_id: str,
        new_stem: str | None = None,
    ) -> None:
        if self._stem_counts is None:
            return
        for stem in stems:
            _bump_stem(self._stem_counts, stem, old_reference_id, -1)
            _bump_stem(
                self._stem_counts,
                stem if new_stem is None else new_stem,
                new_reference_id,
                1,
            )

    def upsert_key(self, reference: Reference, reference_id: str, stem: str, status: str) -> None:
        previous = self.get_key(reference.citation_key) if self._stem_counts is not None else None
        self.conn.execute(
            """
            INSERT INTO citation_keys (
                citation_key, reference_id, entry_type, title, filename_stem, status,
                error_category, error_detail, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?)
            ON CONFLICT(citation_key) DO UPDATE SET
                reference_id = excluded.reference_id,
                entry_type = excluded.entry_type,
                title = excluded.title,
                filename_stem = excluded.filename_stem,
                status = excluded.status,
                error_category = NULL,
                error_detail = NULL,
                updated_at = excluded.updated_at
            """,
            (
                reference.citation_key,
                reference_id,
                reference.entry_type,
                reference.title,
                stem,
                status,
                now_iso(),
            ),
        )
        self.conn.commit()
        if self._stem_counts is not None:
            if previous is not None:
                _bump_stem(self._stem_counts, previous.filename_stem, previous.reference_id, -1)
            _bump_stem(self._stem_counts, stem, reference_id, 1)

    def set_status(
        self,
        citation_key: str,
        status: str,
        *,
        error_category: str | None = None,
        error_detail: str | None = None,
    ) -> None:
        self.conn.execute(
            """
            UPDATE citation_keys
            SET status = ?, error_category = ?, error_detail = ?, updated_at = ?
            WHERE citation_key = ?
            """,
            (status, error_category, error_detail, now_iso(), citation_key),
        )
        self.conn.commit()

    def get_key(self, citation_key: str) -> KeyRecord | None:
        row = self.conn.execute(
            "SELECT * FROM citation_keys WHERE citation_key = ?",
            (citation_key,),
        ).fetchone()
        return _key(row) if row else None

    def keys_for(self, reference_id: str) -> list[KeyRecord]:
        rows = self.conn.execute(
            "SELECT * FROM citation_keys WHERE reference_id = ? ORDER BY citation_key",
            (reference_id,),
        )
        return [_key(row) for row in rows]

    def all_keys(self) -> list[KeyRecord]:
        rows = self.conn.execute("SELECT * FROM citation_keys ORDER BY citation_key")
        return [_key(row) for row in rows]

    def delete_key(self, citation_key: str) -> None:
        previous = self.get_key(citation_key) if self._stem_counts is not None else None
        self.conn.execute("DELETE FROM citation_keys WHERE citation_key = ?", (citation_key,))
        self.conn.commit()
        if previous is not None and self._stem_counts is not None:
            _bump_stem(self._stem_counts, previous.filename_stem, previous.reference_id, -1)

    def files_for(self, reference_id: str) -> list[FileRecord]:
        rows = self.conn.execute(
            "SELECT * FROM files WHERE reference_id = ? ORDER BY role",
            (reference_id,),
        )
        return [_file(row) for row in rows]

    def get_file(self, reference_id: str, role: str) -> FileRecord | None:
        row = self.conn.execute(
            "SELECT * FROM files WHERE reference_id = ? AND role = ?",
            (reference_id, role),
        ).fetchone()
        return _file(row) if row else None

    def search_keys(self, *, doi: str | None = None, title: str | None = None) -> list[KeyRecord]:
        """Return citation keys matching a stored DOI or title. This does not use the network."""

        wanted_doi = normalize_doi(doi) if doi else None
        needle = " ".join((title or "").casefold().split())
        rows = self.conn.execute(
            """
            SELECT
                citation_keys.*,
                publications.doi AS publication_doi,
                publications.title AS publication_title
            FROM citation_keys
            JOIN publications ON publications.reference_id = citation_keys.reference_id
            ORDER BY citation_keys.citation_key
            """
        )
        matched: list[KeyRecord] = []
        for row in rows:
            if wanted_doi is not None:
                stored = normalize_doi(row["publication_doi"])
                if stored != wanted_doi and row["reference_id"] != f"doi:{wanted_doi}":
                    continue
            if needle:
                haystack = " ".join(
                    str(row["publication_title"] or row["title"] or "").casefold().split()
                )
                if needle not in haystack:
                    continue
            matched.append(_key(row))
        return matched

    def cache_get(self, cache_key: str) -> str | None:
        row = self.conn.execute(
            "SELECT payload FROM resolution_cache WHERE cache_key = ?",
            (cache_key,),
        ).fetchone()
        return str(row["payload"]) if row else None

    def cache_put(self, cache_key: str, payload: str) -> None:
        self.conn.execute(
            """
            INSERT INTO resolution_cache (cache_key, payload, fetched_at)
            VALUES (?, ?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET payload = excluded.payload, fetched_at = excluded.fetched_at
            """,
            (cache_key, payload, now_iso()),
        )
        self.conn.commit()

    def absolute(self, record: FileRecord) -> Path:
        return self.project_root / record.relative_path

    def path_for(self, stem: str, role: str) -> Path:
        return self.library_root / filename_for(stem, role)

    def relative(self, path: Path) -> str:
        resolved = path.resolve()
        try:
            return resolved.relative_to(self.project_root.resolve()).as_posix()
        except ValueError:
            return resolved.as_posix()

    def resolve_file(self, stem: str, record: FileRecord) -> Path | None:
        """Return the file a key should open, or None when no copy is on disk.

        The key's own alias wins when it is the stored file or has its bytes.
        An alias with other bytes is never returned.
        """

        canonical = self.absolute(record)
        alias = self.path_for(stem, record.role)
        if alias.is_file():
            if canonical.is_file() and alias.samefile(canonical):
                return alias
            if sha256_file(alias) == record.sha256:
                return alias
        if canonical.is_file():
            return canonical
        return None

    def commit_download(
        self,
        *,
        reference_id: str,
        stem: str,
        role: str,
        version: str,
        partial: Path,
        source_url: str | None,
        provider: str,
    ) -> Path:
        """Move a validated partial file into the library without replacing unrelated bytes."""

        destination = self.path_for(stem, role)
        digest = sha256_file(partial)
        if destination.exists() and sha256_file(destination) != digest:
            partial.unlink(missing_ok=True)
            raise FileCollisionError(f"refusing to overwrite {destination.name} with different bytes")
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.replace(partial, destination)
        self._upsert_file(
            reference_id=reference_id,
            role=role,
            version=version,
            path=destination,
            digest=digest,
            source_url=source_url,
            provider=provider,
        )
        return destination

    def replace_download(
        self,
        *,
        reference_id: str,
        stem: str,
        role: str,
        version: str,
        partial: Path,
        source_url: str | None,
        provider: str,
        destination: Path | None = None,
        previous_sha256: str | None = None,
    ) -> Path:
        """Atomically replace a stored file with a validated partial, restoring it if the record fails."""

        target = destination or self.path_for(stem, role)
        digest = sha256_file(partial)
        target.parent.mkdir(parents=True, exist_ok=True)
        backup: Path | None = None
        if target.exists():
            backup = target.with_name(target.name + ".replaced")
            backup.unlink(missing_ok=True)
            try:
                os.link(target, backup)
            except OSError:
                shutil.copy2(target, backup)
        try:
            os.replace(partial, target)
            self._upsert_file(
                reference_id=reference_id,
                role=role,
                version=version,
                path=target,
                digest=digest,
                source_url=source_url,
                provider=provider,
            )
        except Exception:
            if backup is not None and backup.is_file():
                os.replace(backup, target)
            partial.unlink(missing_ok=True)
            raise
        if backup is not None:
            backup.unlink(missing_ok=True)
        self._refresh_role_aliases(reference_id, role, previous_sha256, digest)
        return target

    def _refresh_role_aliases(
        self,
        reference_id: str,
        role: str,
        previous_sha256: str | None,
        new_sha256: str,
    ) -> None:
        record = self.get_file(reference_id, role)
        if record is None:
            return
        source = self.absolute(record)
        if not source.is_file():
            return
        for key in self.keys_for(reference_id):
            alias = self.path_for(key.filename_stem, role)
            if alias.resolve() == source.resolve():
                continue
            if alias.exists():
                digest = sha256_file(alias)
                if digest == new_sha256:
                    continue
                if previous_sha256 is not None and digest != previous_sha256:
                    logger.warning("left %s in place: it does not hold the replaced bytes", alias.name)
                    continue
                alias.unlink()
            _link_or_copy(source, alias)

    def commit_bytes(
        self,
        *,
        reference_id: str,
        stem: str,
        role: str,
        version: str,
        data: bytes,
        source_url: str | None,
        provider: str,
    ) -> Path:
        destination = self.path_for(stem, role)
        partial = destination.with_name(destination.name + ".partial")
        partial.write_bytes(data)
        return self.commit_download(
            reference_id=reference_id,
            stem=stem,
            role=role,
            version=version,
            partial=partial,
            source_url=source_url,
            provider=provider,
        )

    def _upsert_file(
        self,
        *,
        reference_id: str,
        role: str,
        version: str,
        path: Path,
        digest: str,
        source_url: str | None,
        provider: str | None,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO files (
                reference_id, role, version, relative_path, sha256, byte_size,
                source_url, provider, retrieved_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(reference_id, role) DO UPDATE SET
                version = excluded.version,
                relative_path = excluded.relative_path,
                sha256 = excluded.sha256,
                byte_size = excluded.byte_size,
                source_url = excluded.source_url,
                provider = excluded.provider,
                retrieved_at = excluded.retrieved_at
            """,
            (
                reference_id,
                role,
                version,
                self.relative(path),
                digest,
                path.stat().st_size,
                source_url,
                provider,
                now_iso(),
            ),
        )
        self.conn.commit()

    def ensure_alias_links(self, reference_id: str, stem: str) -> None:
        for record in self.files_for(reference_id):
            source = self.absolute(record)
            if not source.is_file():
                continue
            destination = self.path_for(stem, record.role)
            if destination.resolve() == source.resolve():
                continue
            if source.parent.resolve() == self.unreferenced_root.resolve() and not destination.exists():
                os.replace(source, destination)
                self._set_path(record, destination)
                with contextlib.suppress(OSError):
                    self.unreferenced_root.rmdir()
                continue
            _link_or_copy(source, destination)

    def release_key(self, key: KeyRecord) -> None:
        """Drop a key and free its ``{stem}.*`` names for the next publication.

        Bytes that another key of the same publication still has are removed
        from this name. Anything else moves to ``_unreferenced/``.
        """

        self.delete_key(key.citation_key)
        for record in self.files_for(key.reference_id):
            path = self.path_for(key.filename_stem, record.role)
            if not path.is_file():
                continue
            canonical = self.absolute(record)
            if path.resolve() == canonical.resolve():
                keeper = self._other_copy(key.reference_id, record, path)
                if keeper is None:
                    moved = self._move_unreferenced(path, key, record.role)
                    if moved is not None:
                        self._set_path(record, moved)
                    continue
                self._set_path(record, keeper)
                canonical = keeper
            if canonical.is_file() and (path.samefile(canonical) or sha256_file(path) == sha256_file(canonical)):
                path.unlink()
            else:
                self._move_unreferenced(path, key, record.role)

    def _other_copy(self, reference_id: str, record: FileRecord, path: Path) -> Path | None:
        accepted = {record.sha256, sha256_file(path)}
        for key in self.keys_for(reference_id):
            alias = self.path_for(key.filename_stem, record.role)
            if not alias.is_file() or alias.resolve() == path.resolve():
                continue
            if alias.samefile(path) or sha256_file(alias) in accepted:
                return alias
        return None

    def _move_unreferenced(self, path: Path, key: KeyRecord, role: str) -> Path | None:
        target = self.unreferenced_root / filename_for(
            disambiguated_stem(key.citation_key, key.reference_id), role
        )
        if target.exists():
            if sha256_file(target) == sha256_file(path):
                path.unlink()
                return target
            logger.warning("left %s in place: %s holds other bytes", path.name, self.relative(target))
            return None
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(path, target)
        return target

    def _set_path(self, record: FileRecord, path: Path) -> None:
        self.conn.execute(
            "UPDATE files SET relative_path = ? WHERE reference_id = ? AND role = ?",
            (self.relative(path), record.reference_id, record.role),
        )
        self.conn.commit()

    def rename_files(self, reference_id: str, old_stem: str, new_stem: str) -> None:
        if old_stem == new_stem:
            return
        moved = self._citation_stems(reference_id, old_stem) if self._stem_counts is not None else []
        for record in self.files_for(reference_id):
            source = self.path_for(old_stem, record.role)
            if not source.is_file():
                source = self.absolute(record)
            destination = self.path_for(new_stem, record.role)
            if source.is_file() and source.resolve() != destination.resolve():
                if destination.exists():
                    if not destination.samefile(source) and sha256_file(destination) != record.sha256:
                        logger.warning("left %s in place: %s holds other bytes", source.name, destination.name)
                        continue
                else:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(source, destination)
            if destination.is_file():
                self.conn.execute(
                    "UPDATE files SET relative_path = ? WHERE reference_id = ? AND role = ?",
                    (self.relative(destination), reference_id, record.role),
                )
        self.conn.execute(
            "UPDATE citation_keys SET filename_stem = ?, updated_at = ? WHERE reference_id = ? AND filename_stem = ?",
            (new_stem, now_iso(), reference_id, old_stem),
        )
        self.conn.commit()
        self._move_stem_counts(moved, reference_id, reference_id, new_stem)

    def prune_missing(self, reference_id: str) -> None:
        """Forget stored files that were deleted, unless another key still has the bytes."""

        for record in self.files_for(reference_id):
            if self.absolute(record).is_file():
                continue
            replacement = None
            for key in self.keys_for(reference_id):
                alias = self.path_for(key.filename_stem, record.role)
                if alias.is_file() and sha256_file(alias) == record.sha256:
                    replacement = alias
                    break
            if replacement is None:
                self.conn.execute(
                    "DELETE FROM files WHERE reference_id = ? AND role = ?",
                    (reference_id, record.role),
                )
            else:
                self.conn.execute(
                    "UPDATE files SET relative_path = ? WHERE reference_id = ? AND role = ?",
                    (self.relative(replacement), reference_id, record.role),
                )
        self.conn.commit()

    def preferred_file(self, reference_id: str) -> FileRecord | None:
        """The file a citation key should open: its PDF, or its archived page."""

        for role in ("pdf", "markdown"):
            record = self.get_file(reference_id, role)
            if record is not None and self.absolute(record).is_file():
                return record
        return None

    def preferred_present(self, reference_id: str) -> bool:
        """True when the preferred PDF or archived page is on disk.

        A user-edited copy still counts.
        """

        return self.preferred_file(reference_id) is not None

    def export_manifest(self) -> None:
        files_by_reference: dict[str, list[FileRecord]] = {}
        for row in self.conn.execute("SELECT * FROM files ORDER BY reference_id, role"):
            record = _file(row)
            files_by_reference.setdefault(record.reference_id, []).append(record)
        references: dict[str, dict] = {}
        for key in self.all_keys():
            files: dict[str, str | None] = {}
            provenance: dict[str, dict] = {}
            preferred_version = None
            for record in files_by_reference.get(key.reference_id, ()):
                located = self.resolve_file(key.filename_stem, record)
                files[record.role] = None if located is None else self.relative(located)
                provenance[record.role] = {
                    "sha256": record.sha256,
                    "source_url": record.source_url,
                    "provider": record.provider,
                    "retrieved_at": record.retrieved_at,
                    "version": record.version,
                }
                if record.role == "pdf":
                    preferred_version = record.version
                elif record.role == "markdown" and preferred_version is None:
                    preferred_version = record.version
            references[key.citation_key] = {
                "reference_id": key.reference_id,
                "citation_key": key.citation_key,
                "type": key.entry_type,
                "filename_stem": key.filename_stem,
                "files": files,
                "version": preferred_version,
                "status": key.status,
                "provenance": provenance,
            }
        document = {"manifest_version": MANIFEST_VERSION, "references": references}
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.manifest_path.with_suffix(".json.partial")
        temporary.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(temporary, self.manifest_path)

    def checksum_problems(self) -> list[str]:
        problems: list[str] = []
        rows = self.conn.execute("SELECT * FROM files")
        for row in rows:
            record = _file(row)
            path = self.absolute(record)
            label = f"{record.reference_id} {record.role}"
            if not path.is_file():
                problems.append(f"{label}: missing {record.relative_path}")
                continue
            actual = sha256_file(path)
            if actual != record.sha256:
                problems.append(f"{label}: checksum mismatch for {record.relative_path}")
        return problems

    def reference_problems(self) -> list[str]:
        problems = self.checksum_problems()
        for key in self.all_keys():
            if key.reference_id == key.citation_key:
                problems.append(f"{key.citation_key}: citation key was stored as the publication identity")
            if not key.reference_id.startswith(("doi:", "arxiv:", "url:", "internal:")):
                problems.append(f"{key.citation_key}: reference id {key.reference_id} is not namespaced")
            for record in self.files_for(key.reference_id):
                alias = self.path_for(key.filename_stem, record.role)
                if not alias.is_file():
                    problems.append(f"{key.citation_key}: missing {alias.name}")
                    continue
                if sha256_file(alias) != record.sha256:
                    problems.append(f"{key.citation_key}: checksum mismatch for {alias.name}")
        return problems


def _bump_stem(counts: dict[str, dict[str, int]], stem: str, reference_id: str, delta: int) -> None:
    folded = stem.casefold()
    bucket = counts.get(folded) if delta < 0 else counts.setdefault(folded, {})
    if bucket is None:
        return
    updated = bucket.get(reference_id, 0) + delta
    if updated <= 0:
        bucket.pop(reference_id, None)
        if not bucket:
            counts.pop(folded, None)
        return
    bucket[reference_id] = updated


def _link_or_copy(source: Path, destination: Path) -> None:
    if destination.exists():
        if not destination.samefile(source) and sha256_file(destination) != sha256_file(source):
            logger.warning("did not link %s: the file already holds other bytes", destination.name)
        return
    try:
        os.link(source, destination)
    except OSError:
        shutil.copyfile(source, destination)


def _key(row: sqlite3.Row) -> KeyRecord:
    return KeyRecord(
        citation_key=row["citation_key"],
        reference_id=row["reference_id"],
        entry_type=row["entry_type"],
        title=row["title"],
        filename_stem=row["filename_stem"],
        status=row["status"],
        error_category=row["error_category"],
        error_detail=row["error_detail"],
    )


def _file(row: sqlite3.Row) -> FileRecord:
    return FileRecord(
        reference_id=row["reference_id"],
        role=row["role"],
        version=row["version"],
        relative_path=row["relative_path"],
        sha256=row["sha256"],
        byte_size=row["byte_size"],
        source_url=row["source_url"],
        provider=row["provider"],
        retrieved_at=row["retrieved_at"],
    )

