"""Local MCP tool behavior: read-only default, paths, paging, and prune."""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pymupdf
import pytest

from bibvault.config import load_settings
from bibvault.mcp_server import (
    READ_DOCUMENT_MAX_CHARS,
    READ_TOOLS,
    WRITE_TOOLS,
    BibVaultSession,
    build_server,
)
from bibvault.models import AcquireResult
from bibvault.service import acquisition_document, locate_reference
from tests.helpers import make_pdf, open_library, write_config

PREPRINT = "https://oa.example/paper.pdf"


class Clock:
    def __init__(self) -> None:
        self.moment = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.moment


@pytest.fixture
def config(project):
    return write_config(project, "[resolution]\nuse_openalex = false\n")


def _session(config, *, allow_write: bool = False, now=None):
    settings = load_settings(config_path=config)
    return BibVaultSession(settings, allow_write=allow_write, now=now), settings


def _article(key: str, doi: str, title: str) -> str:
    return (
        f"@article{{{key}, title={{{title}}}, year={{2020}}, "
        f"doi={{{doi}}}, url={{{PREPRINT}}}}}\n"
    )


def _pages(*texts: str) -> bytes:
    document = pymupdf.open()
    for text in texts:
        page = document.new_page(width=300, height=200)
        page.insert_text((36, 72), text)
    payload = document.tobytes()
    document.close()
    return payload


def _bib(project, name: str, *entries: str):
    path = project / name
    path.write_text("".join(entries), encoding="utf-8")
    return path


def test_read_only_rejects_writes_and_hides_them(config, project, monkeypatch):
    bib = _bib(project, "refs.bib", _article("paper", "10.1000/x", "Shared Paper"))
    reader, _settings = _session(config)
    called = False

    async def fail_acquire(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("read-only process must not acquire")

    monkeypatch.setattr("bibvault.mcp_server.acquire", fail_acquire)
    refused = asyncio.run(reader.call("fetch_references", {"bib": [str(bib)]}))
    for name in WRITE_TOOLS:
        payload = asyncio.run(reader.call(name, {"bib": [str(bib)], "citation_key": "paper", "file": str(bib), "token": "nope", "confirm_complete_set": True}))
        assert payload["error"]["code"] == "write_not_enabled", name
    assert refused["error"]["code"] == "write_not_enabled"
    assert called is False
    assert not (project / ".bibvault" / "index.sqlite").exists()

    server = build_server(reader)
    listed = [tool.name for tool in asyncio.run(server.list_tools())]
    assert listed == list(READ_TOOLS)
    hidden = asyncio.run(server.call_tool("fetch_references", {"bib": [str(bib)]}))
    assert json.loads(hidden.content[0].text)["error"]["code"] == "write_not_enabled"
    assert hidden.is_error is False

    writer, _settings = _session(config, allow_write=True)
    advertised = [tool.name for tool in asyncio.run(build_server(writer).list_tools())]
    assert advertised == list(READ_TOOLS + WRITE_TOOLS)

    info = asyncio.run(reader.call("library_info"))
    assert info["read_only"] is True
    assert info["allow_write"] is False
    assert info["read_document"]["max_chars_limit"] == READ_DOCUMENT_MAX_CHARS
    assert info["reference_count"] == 0


def test_paths_outside_the_project_are_rejected(config, project, tmp_path):
    writer, settings = _session(config, allow_write=True)
    outside = tmp_path / "outside.bib"
    outside.write_text(_article("paper", "10.1000/x", "Shared Paper"), encoding="utf-8")
    secret = tmp_path / "secret.pdf"
    secret.write_bytes(make_pdf("SECRET-OUTSIDE"))

    escaped = asyncio.run(writer.call("fetch_references", {"bib": [str(project / ".." / "outside.bib")]}))
    linked = project / "linked.bib"
    linked.symlink_to(outside)
    via_link = asyncio.run(writer.call("sync_references", {"bib": [str(linked)]}))
    imported = asyncio.run(
        writer.call(
            "import_document",
            {"citation_key": "paper", "file": str(secret), "bib": str(outside)},
        )
    )
    assert escaped["error"]["code"] == "path_not_allowed"
    assert via_link["error"]["code"] == "path_not_allowed"
    assert imported["error"]["code"] == "path_not_allowed"
    assert "SECRET-OUTSIDE" not in json.dumps(imported)

    bib = _bib(project, "refs.bib", _article("paper", "10.1000/x", "Shared Paper"))
    source = project / "local.pdf"
    source.write_bytes(make_pdf("inside"))
    stored = asyncio.run(
        writer.call(
            "import_document",
            {"citation_key": "paper", "file": str(source), "bib": str(bib), "source_url": PREPRINT},
        )
    )
    assert stored["results"][0]["status"] == "downloaded"
    mapped = project / "papers" / "paper.pdf"
    mapped.unlink()
    mapped.symlink_to(secret)
    read = asyncio.run(
        writer.call("read_document", {"citation_key": "paper", "max_chars": 200, "page": 1})
    )
    assert read["error"]["code"] == "path_not_allowed"
    assert "SECRET-OUTSIDE" not in json.dumps(read)
    library = open_library(settings)
    try:
        assert locate_reference(library, "paper")["citation_key"] == "paper"
    finally:
        library.close()


def test_read_document_pages_and_limits(config, project):
    writer, _settings = _session(config, allow_write=True)
    bib = _bib(project, "refs.bib", _article("paper", "10.1000/x", "Shared Paper"))
    source = project / "local.pdf"
    source.write_bytes(_pages("PAGE-ONE-MARKER", "PAGE-TWO-MARKER"))
    asyncio.run(
        writer.call(
            "import_document",
            {
                "citation_key": "paper",
                "file": str(source),
                "bib": str(bib),
                "source_url": PREPRINT,
            },
        )
    )
    first = asyncio.run(writer.call("read_document", {"citation_key": "paper", "max_chars": 8000}))
    second = asyncio.run(writer.call("read_document", {"citation_key": "paper", "page": 2, "max_chars": 8000}))
    short = asyncio.run(writer.call("read_document", {"citation_key": "paper", "page": 1, "max_chars": 5}))
    assert "PAGE-ONE-MARKER" in first["text"]
    assert "PAGE-TWO-MARKER" not in first["text"]
    assert first["page"] == 1
    assert first["offset"] is None
    assert first["role"] == "pdf"
    assert first["title"] == "Shared Paper"
    assert first["source_url"] == PREPRINT
    assert first["sha256"]
    assert "PAGE-TWO-MARKER" in second["text"]
    assert "PAGE-ONE-MARKER" not in second["text"]
    assert second["page"] == 2
    assert short["truncated"] is True
    assert short["text"] == first["text"][:5]
    assert asyncio.run(writer.call("read_document", {"citation_key": "paper", "max_chars": 0}))["error"]["code"] == (
        "invalid_argument"
    )
    assert asyncio.run(
        writer.call("read_document", {"citation_key": "paper", "max_chars": READ_DOCUMENT_MAX_CHARS + 1})
    )["error"]["code"] == "invalid_argument"

    note_bib = _bib(project, "notes.bib", _article("note", "10.1000/n", "A Note"))
    markdown = project / "note.md"
    markdown.write_text("abcdefghij", encoding="utf-8")
    asyncio.run(
        writer.call(
            "import_document",
            {"citation_key": "note", "file": str(markdown), "bib": str(note_bib)},
        )
    )
    window = asyncio.run(
        writer.call("read_document", {"citation_key": "note", "offset": 2, "max_chars": 4})
    )
    assert window["role"] == "markdown"
    assert window["page"] is None
    assert window["offset"] == 2
    assert window["text"] == "cdef"
    assert window["truncated"] is True

    stored = project / "papers" / "paper.pdf"
    stored.unlink()
    missing = asyncio.run(writer.call("read_document", {"citation_key": "paper", "max_chars": 20}))
    assert missing["error"]["code"] == "file_missing"
    assert missing["error"]["status"]
    unknown = asyncio.run(writer.call("read_document", {"citation_key": "absent", "max_chars": 20}))
    assert unknown["error"]["code"] == "unknown_key"


def test_prune_token_keeps_pdf_bytes(config, project):
    clock = Clock()
    writer, settings = _session(config, allow_write=True, now=clock)
    reader, _settings = _session(config)
    full = _bib(
        project,
        "refs.bib",
        _article("other", "10.1000/y", "Other Paper"),
        _article("paper", "10.1000/x", "Shared Paper"),
    )
    subset = _bib(project, "subset.bib", _article("paper", "10.1000/x", "Shared Paper"))
    other_pdf = make_pdf("other-bytes")
    for key, payload in (("paper", make_pdf("paper-bytes")), ("other", other_pdf)):
        source = project / f"{key}.src.pdf"
        source.write_bytes(payload)
        stored = asyncio.run(
            writer.call("import_document", {"citation_key": key, "file": str(source), "bib": str(full)})
        )
        assert stored["results"][0]["status"] == "downloaded", stored

    refused = asyncio.run(
        reader.call("plan_prune", {"bib": [str(subset)], "confirm_complete_set": True})
    )
    assert refused["error"]["code"] == "write_not_enabled"
    unconfirmed = asyncio.run(
        writer.call("plan_prune", {"bib": [str(subset)], "confirm_complete_set": False})
    )
    assert unconfirmed["error"]["code"] == "invalid_argument"
    assert (project / "papers" / "other.pdf").read_bytes() == other_pdf

    plan = asyncio.run(writer.call("plan_prune", {"bib": [str(subset)], "confirm_complete_set": True}))
    assert plan["detach"] == ["other"]
    assert plan["delete_files"] == []
    assert plan["expires_at"] == "2026-10-15T12:10:00Z"
    missing = asyncio.run(writer.call("apply_prune", {"token": "not-a-token"}))
    assert missing["error"]["code"] == "token_mismatch"
    assert (project / "papers" / "other.pdf").read_bytes() == other_pdf

    clock.moment += timedelta(minutes=11)
    expired = asyncio.run(writer.call("apply_prune", {"token": plan["token"]}))
    assert expired["error"]["code"] == "token_expired"
    assert (project / "papers" / "other.pdf").is_file()
    clock.moment -= timedelta(minutes=11)

    subset.write_text(
        _article("other", "10.1000/y", "Other Paper") + _article("paper", "10.1000/x", "Shared Paper"),
        encoding="utf-8",
    )
    mismatched = asyncio.run(writer.call("apply_prune", {"token": plan["token"]}))
    assert mismatched["error"]["code"] == "token_mismatch"
    subset.write_text(_article("paper", "10.1000/x", "Shared Paper"), encoding="utf-8")

    fresh = asyncio.run(writer.call("plan_prune", {"bib": [str(subset)], "confirm_complete_set": True}))
    writer._plans[fresh["token"]].delete_files = ("papers/other.pdf",)
    blocked = asyncio.run(writer.call("apply_prune", {"token": fresh["token"]}))
    assert blocked["error"]["code"] == "invalid_argument"
    assert (project / "papers" / "other.pdf").read_bytes() == other_pdf

    again = asyncio.run(writer.call("plan_prune", {"bib": [str(subset)], "confirm_complete_set": True}))
    applied = asyncio.run(writer.call("apply_prune", {"token": again["token"]}))
    assert applied == {"detach": ["other"], "delete_files": []}
    reused = asyncio.run(writer.call("apply_prune", {"token": again["token"]}))
    assert reused["error"]["code"] == "token_mismatch"
    library = open_library(settings)
    try:
        assert library.get_key("other") is None
        assert library.get_key("paper") is not None
    finally:
        library.close()
    assert not (project / "papers" / "other.pdf").exists()
    moved = list((project / "papers" / "_unreferenced").glob("*.pdf"))
    assert [path.read_bytes() for path in moved] == [other_pdf]


def test_reference_fields_match_the_service(config, project, monkeypatch):
    writer, settings = _session(config, allow_write=True)
    bib = _bib(
        project,
        "refs.bib",
        _article("other", "10.1000/y", "Other Paper"),
        _article("paper", "10.1000/x", "Shared Paper"),
    )
    source = project / "local.pdf"
    source.write_bytes(make_pdf("paper"))
    asyncio.run(
        writer.call(
            "import_document",
            {
                "citation_key": "paper",
                "file": str(source),
                "bib": str(bib),
                "source_url": PREPRINT,
                "provider": "test",
            },
        )
    )
    other = project / "other.pdf"
    other.write_bytes(make_pdf("other"))
    asyncio.run(
        writer.call("import_document", {"citation_key": "other", "file": str(other), "bib": str(bib)})
    )

    library = open_library(settings)
    try:
        expected = locate_reference(library, "paper")
    finally:
        library.close()
    got = asyncio.run(writer.call("get_reference", {"citation_key": "paper"}))
    assert got == expected
    unknown = asyncio.run(writer.call("get_reference", {"citation_key": "absent"}))
    assert unknown["error"]["code"] == "unknown_key"
    assert "path" not in unknown

    found = asyncio.run(writer.call("search_references", {"doi": "https://doi.org/10.1000/X", "title": "shared paper"}))
    assert found["returned"] == 1
    assert found["truncated"] is False
    match = found["matches"][0]
    assert match["citation_key"] == expected["citation_key"]
    assert match["title"] == expected["title"]
    assert match["reference_id"] == expected["reference_id"]
    assert match["status"] == expected["status"]
    assert match["path"] == expected["path"]
    both_miss = asyncio.run(
        writer.call("search_references", {"doi": "10.1000/x", "title": "Other Paper"})
    )
    assert both_miss == {"matches": [], "returned": 0, "truncated": False}
    limited = asyncio.run(writer.call("search_references", {"title": "paper", "limit": 1}))
    assert limited["returned"] == 1
    assert limited["truncated"] is True

    verified = asyncio.run(writer.call("verify_library", {"references": True}))
    assert verified == {"ok": True, "problems": []}

    seen: dict[str, object] = {}

    async def fake_acquire(_library, _references, _settings, **kwargs):
        seen.update(kwargs)
        return [
            AcquireResult(
                citation_key="iceberg2020",
                title="Iceberg",
                reference_id="doi:10.1145/1",
                status="manual_action_required",
                error="ACM download needs a visible browser",
            )
        ]

    monkeypatch.setattr("bibvault.mcp_server.acquire", fake_acquire)
    acm = _bib(project, "acm.bib", _article("iceberg2020", "10.1145/1", "Iceberg"))
    fetched = asyncio.run(writer.call("fetch_references", {"bib": [str(acm)], "keys": ["iceberg2020"]}))
    row = fetched["results"][0]
    cli = row.pop("cli")
    assert fetched == acquisition_document(
        [
            AcquireResult(
                citation_key="iceberg2020",
                title="Iceberg",
                reference_id="doi:10.1145/1",
                status="manual_action_required",
                error="ACM download needs a visible browser",
            )
        ]
    )
    assert seen["browser"] is False
    assert seen["upgrade"] is False
    assert "confirm" not in seen
    assert cli == f"bibvault fetch {acm} --key iceberg2020 --browser"

    acm_pdf = project / "acm.pdf"
    acm_pdf.write_bytes(make_pdf("acm-bytes"))
    asyncio.run(
        writer.call(
            "import_document",
            {"citation_key": "iceberg2020", "file": str(acm_pdf), "bib": str(acm)},
        )
    )
    before = (project / "papers" / "iceberg2020.pdf").read_bytes()

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("upgrade must not download a browser publisher")

    monkeypatch.setattr("bibvault.mcp_server.acquire", forbidden)
    upgraded = asyncio.run(
        writer.call("upgrade_reference", {"citation_key": "iceberg2020", "bib": str(acm)})
    )
    assert upgraded["results"][0]["status"] == "manual_action_required"
    command = upgraded["results"][0]["cli"]
    assert command.endswith("--browser")
    assert "--upgrade" not in command.split()
    assert "iceberg2020" in command.split()
    assert (project / "papers" / "iceberg2020.pdf").read_bytes() == before
