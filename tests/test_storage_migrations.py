import asyncio
import sqlite3

from bibvault.identity import legacy_internal_id, reference_id
from bibvault.models import Reference
from bibvault.pipeline import run_fetch
from bibvault.storage import SCHEMA_VERSION, sha256_file
from tests.helpers import (
    FIXTURES,
    fetch,
    locate,
    make_settings,
    no_openalex,
    open_library,
    reference_problems,
)

OLD_REFERENCE_ID = "internal:9add8da7600e43ea1579"
SOURCE_URL = "https://legacy.example/paper.pdf"
RETRIEVED_AT = "2025-01-02T03:04:05+00:00"


def _install_v0_library(project) -> bytes:
    database = project / ".bibvault" / "index.sqlite"
    database.parent.mkdir(parents=True)
    connection = sqlite3.connect(database)
    try:
        connection.executescript((FIXTURES / "index-v0.sql").read_text(encoding="utf-8"))
    finally:
        connection.close()
    payload = (FIXTURES / "legacy-paper.pdf").read_bytes()
    paper = project / "papers" / "zhang2024.pdf"
    paper.parent.mkdir()
    paper.write_bytes(payload)
    return payload


def _legacy_reference() -> Reference:
    return Reference(
        citation_key="zhang2024",
        entry_type="article",
        title="数据密集型系统",
        year="2024",
        authors=["张三", "李四"],
    )


def test_v0_library_and_internal_identity_migrate_without_redownload(project, respx_mock):
    original = _install_v0_library(project)
    settings = make_settings(project, resolution=no_openalex())
    reference = _legacy_reference()
    new_reference_id = reference_id(reference)
    assert legacy_internal_id(reference) == OLD_REFERENCE_ID
    assert new_reference_id != OLD_REFERENCE_ID

    result = fetch(settings, [reference])["zhang2024"]

    assert result.status == "already_exists"
    assert result.reference_id == new_reference_id
    assert len(respx_mock.calls) == 0
    paper = project / "papers" / "zhang2024.pdf"
    assert paper.read_bytes() == original

    located = locate(settings, "zhang2024")
    assert located is not None
    provenance = located["files"]["pdf"]
    assert provenance["sha256"] == sha256_file(paper)
    assert provenance["source_url"] == SOURCE_URL
    assert provenance["provider"] == "legacy-provider"
    assert provenance["retrieved_at"] == RETRIEVED_AT
    assert provenance["version"] == "published"

    library = open_library(settings)
    try:
        assert library.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert library.get_publication(OLD_REFERENCE_ID) is None
        assert library.get_publication(new_reference_id) is not None
        assert library.get_key("zhang2024").reference_id == new_reference_id
        assert library.get_file(new_reference_id, "pdf") is not None
        assert library.cache_get("legacy-cache") == '{"preserved":true}'
    finally:
        library.close()
    assert reference_problems(settings) == []


def test_internal_identity_migration_survives_a_simultaneous_key_rename(
    project, respx_mock
):
    original = _install_v0_library(project)
    settings = make_settings(project, resolution=no_openalex())
    reference = _legacy_reference().model_copy(update={"citation_key": "renamed2024"})

    result = fetch(settings, [reference])["renamed2024"]

    assert result.status == "already_exists"
    assert len(respx_mock.calls) == 0
    assert not (project / "papers" / "zhang2024.pdf").exists()
    assert (project / "papers" / "renamed2024.pdf").read_bytes() == original
    assert locate(settings, "zhang2024") is None
    renamed = locate(settings, "renamed2024")
    assert renamed is not None
    assert renamed["files"]["pdf"]["source_url"] == SOURCE_URL
    assert reference_problems(settings) == []


def test_legacy_collision_keeps_file_with_matching_stored_metadata(
    project, respx_mock, caplog
):
    original = _install_v0_library(project)
    database = project / ".bibvault" / "index.sqlite"
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            """
            INSERT INTO citation_keys VALUES (
                'other2024', ?, 'article', '湖仓一体架构', 'other2024',
                'downloaded', NULL, NULL, '2025-01-02T00:00:00+00:00'
            )
            """,
            (OLD_REFERENCE_ID,),
        )
        connection.commit()
    finally:
        connection.close()
    (project / "papers" / "other2024.pdf").write_bytes(original)

    stored = _legacy_reference()
    other = Reference(
        citation_key="other2024",
        entry_type="article",
        title="湖仓一体架构",
        year="2024",
        authors=["张三", "李四"],
    )
    assert legacy_internal_id(stored) == legacy_internal_id(other) == OLD_REFERENCE_ID
    settings = make_settings(project, resolution=no_openalex())

    results = fetch(settings, [other, stored], only_keys=["zhang2024"])

    assert results["zhang2024"].status == "already_exists"
    assert len(respx_mock.calls) == 0
    assert (project / "papers" / "zhang2024.pdf").read_bytes() == original
    assert "title-matching identity" in caplog.text
    assert locate(settings, "zhang2024")["files"]["pdf"]["source_url"] == SOURCE_URL
    assert reference_problems(settings) == []


def test_low_information_identity_warning_is_reported_once(project):
    settings = make_settings(project, resolution=no_openalex())
    reference = Reference(citation_key="bare", entry_type="article", year="2024")
    messages: list[str] = []
    library = open_library(settings)
    try:
        asyncio.run(run_fetch(library, [reference], settings, on_status=messages.append))
    finally:
        library.close()
    assert len([message for message in messages if "may collide" in message]) == 1
