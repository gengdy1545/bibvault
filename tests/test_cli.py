import json
import subprocess
import sys

import pytest
from typer.testing import CliRunner

from bibvault import __version__
from bibvault.cli import app
from bibvault.config import DEFAULT_CONFIG
from tests.helpers import cli, make_pdf, write_config

PAPER_URL = "https://oa.example/paper.pdf"
OTHER_URL = "https://oa.example/other.pdf"
BIB = f"""
@article{{paper, title = {{Paper}}, year = {{2020}}, url = {{{PAPER_URL}}}}}
@article{{other, title = {{Other}}, year = {{2021}}, url = {{{OTHER_URL}}}}}
"""


@pytest.fixture
def config(project):
    return write_config(project, "[resolution]\nuse_openalex = false\n")


@pytest.fixture
def bib(project):
    path = project / "refs.bib"
    path.write_text(BIB, encoding="utf-8")
    return path


@pytest.fixture
def paper_pdf(respx_mock):
    pdf = make_pdf("paper")
    respx_mock.get(PAPER_URL).respond(200, content=pdf)
    return pdf


@pytest.fixture
def fetched(config, bib, paper_pdf, respx_mock):
    respx_mock.get(OTHER_URL).respond(404)
    result = cli(config, "fetch", str(bib))
    assert result.exit_code == 0, result.output
    return result


def test_version():
    result = CliRunner().invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == __version__


def test_module_entry_point():
    completed = subprocess.run(
        [sys.executable, "-m", "bibvault", "version"], capture_output=True, text=True, check=True
    )
    assert completed.stdout.strip() == __version__


def test_help_lists_the_commands():
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in ("init", "fetch", "sync", "locate", "status", "verify", "import"):
        assert command in result.stdout


def test_init_creates_the_project_and_keeps_existing_config(project, monkeypatch):
    monkeypatch.chdir(project)
    first = CliRunner().invoke(app, ["init"])
    assert first.exit_code == 0, first.output
    config_path = project / ".bibvault" / "config.toml"
    assert config_path.read_text(encoding="utf-8") == DEFAULT_CONFIG
    assert (project / "papers").is_dir()
    assert (project / ".bibvault" / "index.sqlite").is_file()

    config_path.write_text("[download]\nconcurrency = 1\n", encoding="utf-8")
    second = CliRunner().invoke(app, ["init"])
    assert second.exit_code == 0
    assert "already initialized" in second.stdout
    assert config_path.read_text(encoding="utf-8") == "[download]\nconcurrency = 1\n"


def test_fetch_prints_a_report(fetched):
    assert "BibVault Acquisition Report" in fetched.stdout
    assert "Newly downloaded:     1" in fetched.stdout
    assert "other (not_found):" in fetched.stdout
    assert "paper: downloaded" not in fetched.stdout


def test_fetch_json_summary_adds_up(config, bib, paper_pdf, respx_mock):
    respx_mock.get(OTHER_URL).respond(404)
    result = cli(config, "fetch", str(bib), "--format", "json")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    summary = payload["summary"]
    assert summary["references"] == 2
    assert summary["newly_downloaded"] == 1
    assert summary["not_found"] == 1
    outcomes = {k: v for k, v in summary.items() if k not in {"references", "available_locally"}}
    assert sum(outcomes.values()) == summary["references"]
    assert summary["available_locally"] == summary["newly_downloaded"] + summary["already_present"]
    assert {item["citation_key"]: item["status"] for item in payload["results"]} == {
        "paper": "downloaded",
        "other": "not_found",
    }


def test_fetch_exits_1_when_a_download_failed(config, bib, paper_pdf, respx_mock):
    respx_mock.get(OTHER_URL).respond(200, content=b"<html>not a pdf</html>")
    result = cli(config, "fetch", str(bib))
    assert result.exit_code == 1
    assert "other (failed):" in result.stdout
    assert "Newly downloaded:     1" in result.stdout
    assert "paper: downloaded" not in result.stdout


@pytest.mark.parametrize(
    "args",
    [
        ["--format", "yaml"],
        ["--key", "nobody"],
    ],
)
def test_fetch_usage_errors_exit_2_without_network(config, bib, respx_mock, args):
    result = cli(config, "fetch", str(bib), *args)
    assert result.exit_code == 2
    assert len(respx_mock.calls) == 0


def test_fetch_missing_bib_file_exits_2(config, project, respx_mock):
    result = cli(config, "fetch", str(project / "nope.bib"))
    assert result.exit_code == 2
    assert "not found" in result.output


def test_unknown_key_is_named(config, bib):
    result = cli(config, "fetch", str(bib), "--key", "nobody")
    assert "nobody" in result.output


@pytest.mark.respx(assert_all_called=False)
def test_key_option_limits_downloads(config, bib, paper_pdf, respx_mock):
    other = respx_mock.get(OTHER_URL).respond(200, content=make_pdf("other"))
    result = cli(config, "fetch", str(bib), "--key", "paper")
    assert result.exit_code == 0, result.output
    assert "Newly downloaded:     1" in result.stdout
    assert "other" not in result.stdout
    assert not other.called


def test_parse_warnings_go_to_stderr(config, project, paper_pdf):
    bib = project / "dup.bib"
    bib.write_text(
        f"@article{{paper, title = {{Paper}}, url = {{{PAPER_URL}}}}}\n@article{{paper, title = {{Again}}}}\n",
        encoding="utf-8",
    )
    result = cli(config, "fetch", str(bib))
    assert result.exit_code == 0
    assert "warning: duplicate citation key 'paper'" in result.stderr
    assert "warning" not in result.stdout


def test_multiple_bib_files(config, project, paper_pdf, respx_mock):
    respx_mock.get(OTHER_URL).respond(200, content=make_pdf("other"))
    first, second = project / "a.bib", project / "b.bib"
    first.write_text(BIB.split("\n@article{other")[0], encoding="utf-8")
    second.write_text("@article{other" + BIB.split("\n@article{other")[1], encoding="utf-8")
    result = cli(config, "fetch", str(first), str(second))
    assert result.exit_code == 0, result.output
    assert "Newly downloaded:     2" in result.stdout


def test_locate_text(fetched, config):
    result = cli(config, "locate", "paper")
    assert result.exit_code == 0
    lines = result.stdout.splitlines()
    assert lines[0] == "papers/paper.pdf"
    assert "status: downloaded" in lines
    assert "provider: direct" in lines
    assert f"source: {PAPER_URL}" in lines


def test_locate_json(fetched, config):
    result = cli(config, "locate", "paper", "--format", "json")
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["files"]["pdf"]["path"] == "papers/paper.pdf"


@pytest.mark.parametrize("key", ["other", "nobody"])
def test_locate_without_a_pdf_exits_1(fetched, config, key):
    text = cli(config, "locate", key)
    assert text.exit_code == 1
    assert f"{key}: not available" in text.stdout
    as_json = cli(config, "locate", key, "--format", "json")
    assert as_json.exit_code == 1
    payload = json.loads(as_json.stdout)
    assert payload is None or payload["files"] == {}


def test_locate_rejects_bad_format(config):
    assert cli(config, "locate", "paper", "--format", "xml").exit_code == 2


def test_status(fetched, config):
    text = cli(config, "status")
    assert text.exit_code == 0
    assert "paper: downloaded papers/paper.pdf" in text.stdout
    assert "other: not_found" in text.stdout
    rows = json.loads(cli(config, "status", "--format", "json").stdout)
    assert {row["citation_key"] for row in rows} == {"paper", "other"}
    assert cli(config, "status", "--format", "csv").exit_code == 2


def test_status_of_an_empty_library(config):
    result = cli(config, "status")
    assert result.exit_code == 0
    assert "No references stored." in result.stdout


def test_verify_detects_tampering_and_loss(fetched, config, project):
    assert cli(config, "verify").stdout.strip() == "verified"
    pdf = project / "papers" / "paper.pdf"
    pdf.write_bytes(make_pdf("edited"))
    tampered = cli(config, "verify")
    assert tampered.exit_code == 1
    assert "checksum mismatch" in tampered.stdout
    pdf.unlink()
    lost = cli(config, "verify")
    assert lost.exit_code == 1
    assert "missing" in lost.stdout


def test_sync_follows_a_renamed_key(fetched, config, bib, respx_mock):
    bib.write_text(BIB.replace("{paper,", "{paper2020,"), encoding="utf-8")
    calls = len(respx_mock.calls)
    result = cli(config, "sync", str(bib))
    assert result.exit_code == 0, result.output
    assert "Fetching" in result.output
    assert "Already on disk:      1" in result.stdout
    assert len([c for c in respx_mock.calls[calls:] if c.request.url == PAPER_URL]) == 0
    assert cli(config, "verify", "--references").exit_code == 0
    assert cli(config, "locate", "paper").exit_code == 1
    assert cli(config, "locate", "paper2020").stdout.splitlines()[0] == "papers/paper2020.pdf"


def test_import_missing_file_exits_2(config, project):
    result = cli(config, "import", str(project / "nope.pdf"), "--key", "paper")
    assert result.exit_code == 2


def test_import_rejects_other_file_types(config, project):
    text = project / "notes.txt"
    text.write_text("not an import format", encoding="utf-8")
    bib = project / "refs.bib"
    bib.write_text("@online{paper, title={Paper}, url={https://example.org}}", encoding="utf-8")
    result = cli(config, "import", str(text), "--key", "paper", "--bib", str(bib))
    assert result.exit_code == 1
    assert "expected .pdf or .md" in result.output
