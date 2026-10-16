"""Core workflow commands and the CLI/MCP service contract."""

import json

import pytest

from bibvault.database_sources import source_descriptors
from bibvault.service import locate_exit, locate_reference, plan_prune
from bibvault.storage import ManifestError, load_manifest
from tests.helpers import (
    cli,
    make_pdf,
    make_settings,
    no_openalex,
    open_library,
    refs,
    write_config,
)

PREPRINT = "https://oa.example/preprint.pdf"
PUBLISHED = "https://oa.example/published.pdf"
OTHER = "https://oa.example/other.pdf"


@pytest.fixture
def config(project):
    return write_config(project, "[resolution]\nuse_openalex = false\n")


def _bib(*entries: str) -> str:
    return "\n".join(entries) + "\n"


def _article(key: str, url: str, *, doi: str | None = None, title: str = "Shared Paper") -> str:
    fields = [f"title = {{{title}}}", "year = {2020}"]
    if doi:
        fields.append(f"doi = {{{doi}}}")
    fields.append(f"url = {{{url}}}")
    return f"@article{{{key}, {', '.join(fields)}}}"


def _set_version(project, version: str) -> None:
    library = open_library(make_settings(project, resolution=no_openalex()))
    try:
        library.conn.execute("UPDATE files SET version = ?", (version,))
        library.conn.commit()
    finally:
        library.close()


def test_sync_browser_uses_the_shared_acquisition_path(config, project, monkeypatch):
    bib = project / "refs.bib"
    bib.write_text(_bib(_article("paper", PREPRINT)), encoding="utf-8")
    seen: list[dict] = []

    async def fake_run_fetch(_library, _references, _settings, **kwargs):
        seen.append({"browser": kwargs["browser"], "upgrade": kwargs["upgrade"]})
        return []

    monkeypatch.setattr("bibvault.service.run_fetch", fake_run_fetch)
    plain = cli(config, "sync", str(bib))
    flagged = cli(config, "sync", str(bib), "--browser")
    assert plain.exit_code == 0, plain.output
    assert flagged.exit_code == 0, flagged.output
    assert seen == [
        {"browser": False, "upgrade": False},
        {"browser": True, "upgrade": False},
    ]


def test_status_counts_bibtex_coverage(config, project, respx_mock):
    respx_mock.get(PREPRINT).respond(200, content=make_pdf("paper"))
    respx_mock.get(OTHER).respond(404)
    bib = project / "refs.bib"
    bib.write_text(
        _bib(_article("paper", PREPRINT, doi="10.1000/x"), _article("other", OTHER, title="Other")),
        encoding="utf-8",
    )
    assert cli(config, "fetch", str(bib)).exit_code == 0
    subset = project / "subset.bib"
    subset.write_text(
        _bib(_article("paper", PREPRINT, doi="10.1000/x"), _article("fresh", PREPRINT, title="Fresh")),
        encoding="utf-8",
    )

    text = cli(config, "status", str(subset))
    payload = json.loads(cli(config, "status", str(subset), "--format", "json").stdout)

    assert text.exit_code == 0, text.output
    assert "Available locally: 1" in text.stdout
    assert "Known, file missing: 0" in text.stdout
    assert "Not in library: 1" in text.stdout
    assert "Left in library: 1" in text.stdout
    coverage = payload["coverage"]
    assert coverage["keys"] == {
        "available_locally": ["paper"],
        "known_missing_file": [],
        "not_in_library": ["fresh"],
        "leftover": ["other"],
    }
    both = json.loads(cli(config, "status", str(bib), "--format", "json").stdout)["coverage"]
    assert both["known_missing_file"] == 1
    assert both["keys"]["known_missing_file"] == ["other"]
    assert both["leftover"] == 0


@pytest.mark.respx(assert_all_called=False)
def test_upgrade_replaces_only_a_higher_version(config, project, respx_mock):
    old = make_pdf("preprint")
    new = make_pdf("published")
    respx_mock.get(PREPRINT).respond(200, content=old)
    published = respx_mock.get(PUBLISHED).respond(200, content=new)
    bib = project / "refs.bib"
    bib.write_text(
        _bib(
            _article("paper", PREPRINT, doi="10.1000/x"),
            _article("alias", PREPRINT, doi="10.1000/x"),
        ),
        encoding="utf-8",
    )
    assert cli(config, "fetch", str(bib)).exit_code == 0

    bib.write_text(
        _bib(
            _article("paper", PUBLISHED, doi="10.1000/x"),
            _article("alias", PUBLISHED, doi="10.1000/x"),
        ),
        encoding="utf-8",
    )
    same_rank = cli(config, "fetch", str(bib), "--upgrade", "--key", "paper")
    assert same_rank.exit_code == 0, same_rank.output
    assert not published.called
    assert (project / "papers" / "paper.pdf").read_bytes() == old

    _set_version(project, "preprint")
    upgraded = cli(config, "fetch", str(bib), "--upgrade", "--key", "paper", "--format", "json")
    assert upgraded.exit_code == 0, upgraded.output
    payload = json.loads(upgraded.stdout)
    assert payload["schema_version"] == 1
    assert payload["results"][0]["availability"] == "available"
    assert payload["results"][0]["status"] == "downloaded"
    assert payload["results"][0]["version"] == "provided"
    assert (project / "papers" / "paper.pdf").read_bytes() == new
    assert (project / "papers" / "alias.pdf").read_bytes() == new
    assert cli(config, "verify", "--references").exit_code == 0
    assert not list(project.glob("papers/*upgrade*"))
    assert not list(project.glob("papers/*.replaced"))


@pytest.mark.respx(assert_all_called=False)
def test_upgrade_refuses_a_modified_file(config, project, respx_mock):
    old = make_pdf("preprint")
    published = respx_mock.get(PUBLISHED).respond(200, content=make_pdf("published"))
    respx_mock.get(PREPRINT).respond(200, content=old)
    bib = project / "refs.bib"
    bib.write_text(_bib(_article("paper", PREPRINT, doi="10.1000/x")), encoding="utf-8")
    assert cli(config, "fetch", str(bib)).exit_code == 0
    edited = make_pdf("annotated")
    (project / "papers" / "paper.pdf").write_bytes(edited)
    _set_version(project, "preprint")
    bib.write_text(_bib(_article("paper", PUBLISHED, doi="10.1000/x")), encoding="utf-8")

    refused = cli(config, "fetch", str(bib), "--upgrade", "--key", "paper")

    assert refused.exit_code == 1
    assert "left it in place" in refused.stdout
    assert not published.called
    assert (project / "papers" / "paper.pdf").read_bytes() == edited
    located = json.loads(cli(config, "locate", "paper", "--format", "json").stdout)
    assert located["availability"] == "available"


@pytest.mark.respx(assert_all_called=False)
def test_upgrade_failure_does_not_replace_bytes(config, project, respx_mock):
    old = make_pdf("preprint")
    respx_mock.get(PREPRINT).respond(200, content=old)
    respx_mock.get(PUBLISHED).respond(200, content=b"<html>not a pdf</html>")
    bib = project / "refs.bib"
    bib.write_text(_bib(_article("paper", PREPRINT, doi="10.1000/x")), encoding="utf-8")
    assert cli(config, "fetch", str(bib)).exit_code == 0
    _set_version(project, "preprint")
    bib.write_text(_bib(_article("paper", PUBLISHED, doi="10.1000/x")), encoding="utf-8")

    failed = cli(config, "fetch", str(bib), "--upgrade", "--key", "paper")

    assert failed.exit_code == 1
    assert (project / "papers" / "paper.pdf").read_bytes() == old
    assert json.loads(cli(config, "locate", "paper", "--format", "json").stdout)["status"] == "downloaded"


def test_upgrade_and_dry_run_usage(config, project):
    bib = project / "refs.bib"
    bib.write_text(_bib(_article("paper", PREPRINT)), encoding="utf-8")
    assert cli(config, "fetch", str(bib), "--upgrade").exit_code == 2
    assert cli(config, "fetch", str(bib), "--upgrade", "--browser", "--key", "paper").exit_code == 2
    assert cli(config, "sync", str(bib), "--dry-run").exit_code == 2


@pytest.mark.respx(assert_all_called=False)
def test_prune_keys_dry_run_then_moves_bytes(config, project, respx_mock):
    paper = make_pdf("paper")
    other = make_pdf("other")
    respx_mock.get(PREPRINT).respond(200, content=paper)
    respx_mock.get(OTHER).respond(200, content=other)
    bib = project / "refs.bib"
    bib.write_text(
        _bib(_article("paper", PREPRINT, doi="10.1000/x"), _article("other", OTHER, title="Other")),
        encoding="utf-8",
    )
    assert cli(config, "fetch", str(bib)).exit_code == 0
    subset = project / "subset.bib"
    subset.write_text(_bib(_article("paper", PREPRINT, doi="10.1000/x")), encoding="utf-8")
    calls = len(respx_mock.calls)

    preview = cli(config, "sync", str(subset), "--prune-keys", "--dry-run", "--format", "json")
    assert preview.exit_code == 0, preview.output
    assert json.loads(preview.stdout)["pruned"] == ["other"]
    assert len(respx_mock.calls) == calls
    assert (project / "papers" / "other.pdf").read_bytes() == other
    library = open_library(make_settings(project, resolution=no_openalex()))
    try:
        assert plan_prune(library, refs(_bib(_article("paper", PREPRINT, doi="10.1000/x")))) == ["other"]
        assert library.get_key("other") is not None
    finally:
        library.close()

    applied = cli(config, "sync", str(subset), "--prune-keys")
    assert applied.exit_code == 0, applied.output
    assert "Released 1 leftover citation key" in applied.stdout
    assert cli(config, "locate", "paper").exit_code == 0
    assert cli(config, "locate", "other").exit_code == 2
    assert not (project / "papers" / "other.pdf").exists()
    moved = list((project / "papers" / "_unreferenced").glob("*.pdf"))
    assert [path.read_bytes() for path in moved] == [other]


@pytest.mark.respx(assert_all_called=False)
def test_prune_does_not_list_a_renamed_key(config, project, respx_mock):
    pdf = make_pdf("paper")
    respx_mock.get(PREPRINT).respond(200, content=pdf)
    bib = project / "refs.bib"
    bib.write_text(_bib(_article("paper", PREPRINT, doi="10.1000/x")), encoding="utf-8")
    assert cli(config, "fetch", str(bib)).exit_code == 0
    bib.write_text(_bib(_article("paper2020", PREPRINT, doi="10.1000/x")), encoding="utf-8")

    preview = cli(config, "sync", str(bib), "--prune-keys", "--dry-run")
    assert preview.exit_code == 0, preview.output
    assert "No leftover citation keys." in preview.stdout
    assert (project / "papers" / "paper.pdf").read_bytes() == pdf

    applied = cli(config, "sync", str(bib), "--prune-keys")
    assert applied.exit_code == 0, applied.output
    assert (project / "papers" / "paper2020.pdf").read_bytes() == pdf
    assert cli(config, "locate", "paper").exit_code == 2


def test_find_queries_the_library_without_network(config, project, respx_mock):
    respx_mock.get(PREPRINT).respond(200, content=make_pdf("paper"))
    bib = project / "refs.bib"
    bib.write_text(_bib(_article("paper", PREPRINT, doi="10.1000/x", title="Shared Paper")), encoding="utf-8")
    assert cli(config, "fetch", str(bib)).exit_code == 0
    calls = len(respx_mock.calls)

    missing_flag = cli(config, "find")
    found = cli(config, "find", "--doi", "https://doi.org/10.1000/X")
    titled = json.loads(cli(config, "find", "--title", "shared paper", "--format", "json").stdout)
    absent = cli(config, "find", "--title", "not in the library")

    assert missing_flag.exit_code == 2
    assert found.exit_code == 0, found.output
    assert found.stdout.splitlines()[0] == "paper: papers/paper.pdf"
    assert titled["matches"][0]["citation_key"] == "paper"
    assert titled["matches"][0]["path"] == "papers/paper.pdf"
    assert absent.exit_code == 1
    assert len(respx_mock.calls) == calls
    library = open_library(make_settings(project, resolution=no_openalex()))
    try:
        view = locate_reference(library, "nobody")
    finally:
        library.close()
    assert view["availability"] == "unknown"
    assert locate_exit(view) == 2


def test_sources_lists_registered_descriptors(config, project):
    text = cli(config, "sources")
    payload = json.loads(cli(config, "sources", "--format", "json").stdout)
    assert text.exit_code == 0, text.output
    assert [(item["provider"], item["access"], item["hosts"]) for item in payload["sources"]] == [
        (item.provider, item.access, list(item.hosts)) for item in source_descriptors()
    ]
    for item in source_descriptors():
        assert item.provider in text.stdout
    assert not (project / ".bibvault" / "index.sqlite").exists()


def test_load_manifest_accepts_legacy_maps_and_rejects_newer_versions(tmp_path):
    legacy = tmp_path / "legacy.json"
    legacy.write_text(json.dumps({"paper": {"citation_key": "paper"}}), encoding="utf-8")
    assert load_manifest(legacy)["paper"]["citation_key"] == "paper"
    newer = tmp_path / "newer.json"
    newer.write_text(json.dumps({"manifest_version": 99, "references": {}}), encoding="utf-8")
    with pytest.raises(ManifestError, match="not supported"):
        load_manifest(newer)


def test_fetch_on_a_pipe_prints_plain_progress(config, project, respx_mock):
    respx_mock.get(PREPRINT).respond(200, content=make_pdf("paper"))
    bib = project / "refs.bib"
    bib.write_text(_bib(_article("paper", PREPRINT)), encoding="utf-8")
    result = cli(config, "fetch", str(bib))
    assert result.exit_code == 0, result.output
    assert "Fetching" in result.stderr
    assert "\033" not in result.stderr
