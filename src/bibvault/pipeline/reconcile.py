"""Bind citation keys to publication identities before downloading."""

from __future__ import annotations

import logging
import unicodedata

from bibvault.identity import legacy_internal_id, reference_id
from bibvault.models import Reference
from bibvault.storage import Library

logger = logging.getLogger(__name__)


def _metadata_text(value: object) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).casefold().split())


def _migrate_legacy_internal_identities(
    library: Library,
    references: list[Reference],
) -> None:
    planned: dict[str, list[tuple[str, Reference, bool]]] = {}
    for reference in references:
        current = library.get_key(reference.citation_key)
        new_reference_id = reference_id(reference)
        old_reference_id = legacy_internal_id(reference)
        if (
            not new_reference_id.startswith("internal:")
            or old_reference_id == new_reference_id
            or (current is not None and current.reference_id != old_reference_id)
        ):
            continue
        publication = library.get_publication(old_reference_id)
        if publication is None:
            continue
        metadata_matches = (
            _metadata_text(publication["title"]) == _metadata_text(reference.title)
            and _metadata_text(publication["year"]) == _metadata_text(reference.year)
        )
        planned.setdefault(old_reference_id, []).append(
            (new_reference_id, reference, metadata_matches)
        )

    for old_reference_id, candidates in planned.items():
        matching = [candidate for candidate in candidates if candidate[2]]
        targets = {new_reference_id for new_reference_id, _, _ in matching}
        if len(targets) != 1:
            logger.warning(
                "did not automatically migrate %s because its metadata may represent "
                "a legacy identity collision; stored files and provenance were preserved",
                old_reference_id,
            )
            continue
        new_reference_id, reference, _ = matching[0]
        if len(matching) != len(candidates):
            logger.warning(
                "migrated %s to the title-matching identity %s; other entries sharing "
                "the legacy identity will be reconciled separately",
                old_reference_id,
                new_reference_id,
            )
        library.migrate_legacy_internal_identity(
            old_reference_id,
            new_reference_id,
            reference,
        )


def reconcile(library: Library, references: list[Reference]) -> None:
    """Bind citation keys to identities already in the library before downloading."""

    _migrate_legacy_internal_identities(library, references)
    active = {item.citation_key for item in references}
    referenced = {reference_id(item) for item in references}
    # Free every name first so swapped or rotated keys can take each other's files.
    for reference in references:
        current = library.get_key(reference.citation_key)
        if current is not None and current.reference_id != reference_id(reference):
            library.release_key(current)
    for reference in references:
        ref_id = reference_id(reference)
        library.upsert_publication(reference, ref_id)
        existing = library.keys_for(ref_id)
        current = next((item for item in existing if item.citation_key == reference.citation_key), None)
        retired = [item for item in existing if item.citation_key not in active]
        if current is not None:
            library.upsert_key(reference, ref_id, current.filename_stem, current.status)
            library.ensure_alias_links(ref_id, current.filename_stem)
            continue
        if existing and library.files_for(ref_id) and retired and len(retired) == len(existing):
            old = retired[0]
            new_stem = library.allocate_stem(reference.citation_key, ref_id)
            library.rename_files(ref_id, old.filename_stem, new_stem)
            for item in retired:
                library.release_key(item)
            status = "already_exists" if library.preferred_present(ref_id) else "resolved"
            library.upsert_key(reference, ref_id, new_stem, status)
            continue
        stem = library.allocate_stem(reference.citation_key, ref_id)
        status = "already_exists" if library.preferred_present(ref_id) else "resolved"
        library.upsert_key(reference, ref_id, stem, status)
        if library.files_for(ref_id):
            library.ensure_alias_links(ref_id, stem)

    for key in library.all_keys():
        if key.citation_key in active:
            continue
        if key.reference_id in referenced:
            library.release_key(key)
