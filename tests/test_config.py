from pathlib import Path

import pytest
from typer.testing import CliRunner

from bibvault import config
from bibvault.cli import app
from bibvault.config import DEFAULT_CONFIG, ConfigError, load_settings


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_defaults_without_any_config(tmp_path):
    settings = load_settings(start=tmp_path)
    assert settings.project_root == tmp_path.resolve()
    assert settings.library_root == tmp_path.resolve() / "papers"
    assert settings.db_path == tmp_path.resolve() / ".bibvault" / "index.sqlite"
    assert settings.manifest_path == tmp_path.resolve() / ".bibvault" / "manifest.json"
    assert settings.library.naming == "citation_key"
    assert settings.library.layout == "flat"
    assert settings.config_path is None


def test_project_config_is_found_from_a_subdirectory(tmp_path):
    write(tmp_path / ".bibvault" / "config.toml", '[library]\nroot = "./pdfs"\n')
    nested = tmp_path / "chapters" / "related-work"
    nested.mkdir(parents=True)
    settings = load_settings(start=nested)
    assert settings.project_root == tmp_path.resolve()
    assert settings.library_root == tmp_path.resolve() / "pdfs"


def test_absolute_library_root_is_kept(tmp_path):
    shared = tmp_path / "shared" / "papers"
    write(tmp_path / "proj" / ".bibvault" / "config.toml", f'[library]\nroot = "{shared.as_posix()}"\n')
    settings = load_settings(start=tmp_path / "proj")
    assert settings.library_root == shared


def test_explicit_config_path(tmp_path):
    path = write(tmp_path / "elsewhere" / ".bibvault" / "config.toml", "[download]\nconcurrency = 9\n")
    settings = load_settings(start=tmp_path, config_path=path)
    assert settings.download.concurrency == 9
    assert settings.project_root == (tmp_path / "elsewhere").resolve()


def test_missing_explicit_config_path_is_an_error(tmp_path):
    path = tmp_path / "missing.toml"
    with pytest.raises(ConfigError, match=str(path)):
        load_settings(start=tmp_path, config_path=path)


def test_cli_reports_config_path_errors_as_usage_errors(tmp_path):
    path = tmp_path / "missing.toml"
    result = CliRunner().invoke(app, ["--config", str(path), "version"])
    assert result.exit_code == 2
    assert "configuration file not found" in result.output
    assert path.name in result.output


def test_malformed_config_error_names_its_path(tmp_path):
    path = write(tmp_path / ".bibvault" / "config.toml", "[download\nconcurrency = 4\n")
    with pytest.raises(ConfigError, match=str(path)):
        load_settings(start=tmp_path)


def test_config_outside_a_bibvault_directory_uses_its_own_directory(tmp_path):
    path = write(tmp_path / "custom.toml", "[download]\nconcurrency = 2\n")
    settings = load_settings(config_path=path)
    assert settings.project_root == tmp_path.resolve()


def test_project_config_overrides_user_config(tmp_path):
    write(
        config.USER_CONFIG,
        '[download]\nconcurrency = 8\ntimeout_seconds = 99\n[resolution]\nemail = "me@uni.edu"\n',
    )
    write(tmp_path / ".bibvault" / "config.toml", "[download]\nconcurrency = 2\n")
    settings = load_settings(start=tmp_path)
    assert settings.download.concurrency == 2
    assert settings.download.timeout_seconds == 99
    assert settings.resolution.email == "me@uni.edu"


def test_environment_overrides_project_config(tmp_path, monkeypatch):
    write(
        tmp_path / ".bibvault" / "config.toml",
        '[library]\nroot = "./papers"\n[resolution]\nemail = "project@uni.edu"\n',
    )
    monkeypatch.setenv("BIBVAULT_EMAIL", "env@uni.edu")
    monkeypatch.setenv("BIBVAULT_LIBRARY_ROOT", "./env-papers")
    monkeypatch.setenv("BIBVAULT_OPENALEX_API_KEY", "env-key")
    settings = load_settings(start=tmp_path)
    assert settings.resolution.email == "env@uni.edu"
    assert settings.library_root == tmp_path.resolve() / "env-papers"
    assert settings.resolution.openalex_api_key == "env-key"


def test_unknown_keys_are_ignored(tmp_path):
    write(
        tmp_path / ".bibvault" / "config.toml",
        '[library]\nfuture_option = true\n[auth]\nmode = "interactive"\n',
    )
    settings = load_settings(start=tmp_path)
    assert settings.library.root == "./papers"


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("download", "concurrency", "0"),
        ("download", "timeout_seconds", "0"),
        ("download", "timeout_seconds", "inf"),
        ("download", "max_retries", "-1"),
        ("download", "max_bytes", "0"),
        ("download", "min_interval_seconds", "-0.1"),
        ("download", "min_interval_seconds", "nan"),
        ("resolution", "title_match_threshold", "1.1"),
        ("resolution", "title_match_threshold", "nan"),
        ("browser", "interval_seconds", "-1"),
        ("browser", "max_per_run", "-1"),
        ("browser", "timeout_seconds", "0"),
        ("browser", "timeout_seconds", "inf"),
    ],
)
def test_invalid_numeric_config_names_field_and_path(tmp_path, section, field, value):
    path = write(
        tmp_path / ".bibvault" / "config.toml",
        f"[{section}]\n{field} = {value}\n",
    )
    with pytest.raises(ConfigError) as raised:
        load_settings(start=tmp_path)
    assert str(path) in str(raised.value)
    assert f"{section}.{field}" in str(raised.value)


def test_legacy_unwired_defaults_remain_compatible(tmp_path):
    write(
        tmp_path / ".bibvault" / "config.toml",
        """
[library]
mode = "project"
naming = "citation_key"
layout = "flat"
verify_checksums = true
[download]
skip_existing = true
""",
    )
    settings = load_settings(start=tmp_path)
    assert settings.library.mode == "project"
    assert settings.download.skip_existing is True


@pytest.mark.parametrize(
    ("text", "field"),
    [
        ('[library]\nnaming = "title"\n', "library.naming"),
        ("[library]\nverify_checksums = false\n", "library.verify_checksums"),
        ("[download]\nskip_existing = false\n", "download.skip_existing"),
    ],
)
def test_unimplemented_legacy_options_remain_compatible_but_warn(
    tmp_path, caplog, text, field
):
    path = write(tmp_path / ".bibvault" / "config.toml", text)
    load_settings(start=tmp_path)
    assert str(path) in caplog.text
    assert field in caplog.text
    assert "not implemented and is ignored" in caplog.text


def test_default_config_round_trips(tmp_path):
    write(tmp_path / ".bibvault" / "config.toml", DEFAULT_CONFIG)
    settings = load_settings(start=tmp_path)
    assert settings.library.naming == "citation_key"
    assert settings.download.concurrency == 4
    assert settings.resolution.email == ""
    for unwired in ("mode =", "naming =", "layout =", "verify_checksums =", "skip_existing ="):
        assert unwired not in DEFAULT_CONFIG
