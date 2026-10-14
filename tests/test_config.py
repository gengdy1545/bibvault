from pathlib import Path

from bibvault import config
from bibvault.config import DEFAULT_CONFIG, load_settings


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


def test_default_config_round_trips(tmp_path):
    write(tmp_path / ".bibvault" / "config.toml", DEFAULT_CONFIG)
    settings = load_settings(start=tmp_path)
    assert settings.library.naming == "citation_key"
    assert settings.download.concurrency == 4
    assert settings.resolution.email == ""
