"""Project configuration.

CLI arguments win over environment variables, which win over the project file,
the user's credentials file, the user file, and built-in defaults.
"""

import json
import logging
import os
import tempfile
import tomllib
from pathlib import Path
from urllib.parse import urlparse

import tomlkit
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

USER_CONFIG = Path.home() / ".config" / "bibvault" / "config.toml"
logger = logging.getLogger(__name__)

_UNWIRED_DEFAULTS = {
    ("library", "mode"): "project",
    ("library", "naming"): "citation_key",
    ("library", "layout"): "flat",
    ("library", "verify_checksums"): True,
    ("download", "skip_existing"): True,
}


class ConfigError(ValueError):
    """A configuration file could not be read or validated."""


class LibraryFile(BaseModel):
    model_config = ConfigDict(extra="ignore")

    root: str = "./papers"
    mode: str = "project"
    naming: str = "citation_key"
    layout: str = "flat"
    verify_checksums: bool = True


class DownloadFile(BaseModel):
    model_config = ConfigDict(extra="ignore")

    concurrency: int = Field(4, ge=1)
    timeout_seconds: float = Field(30, gt=0, allow_inf_nan=False)
    max_retries: int = Field(3, ge=0)
    skip_existing: bool = True
    max_bytes: int = Field(80_000_000, gt=0)
    min_interval_seconds: float = Field(0.2, ge=0, allow_inf_nan=False)


class ResolutionFile(BaseModel):
    model_config = ConfigDict(extra="ignore")

    prefer_published_version: bool = True
    allow_preprints: bool = True
    use_openalex: bool = True
    email: str | None = None
    openalex_api_key: str | None = None
    title_match_threshold: float = Field(0.92, ge=0, le=1, allow_inf_nan=False)


class BrowserFile(BaseModel):
    model_config = ConfigDict(extra="ignore")

    executable: str | None = None
    interval_seconds: float = Field(5, ge=0, allow_inf_nan=False)
    max_per_run: int = Field(50, ge=0)
    timeout_seconds: float = Field(60, gt=0, allow_inf_nan=False)
    # Publisher host -> the host the library proxy serves it under.
    proxy_hosts: dict[str, str] = Field(default_factory=dict)

    @field_validator("proxy_hosts")
    @classmethod
    def _hosts_only(cls, value: dict[str, str]) -> dict[str, str]:
        # Users paste whole addresses from the address bar.
        return {host_of(publisher): host_of(proxy) for publisher, proxy in value.items()}


def host_of(value: str) -> str:
    text = value.strip().lower()
    return (urlparse(text).hostname or "") if "://" in text else text.rstrip("/")


class LoggingFile(BaseModel):
    model_config = ConfigDict(extra="ignore")

    level: str = "INFO"


class ConfigFile(BaseModel):
    model_config = ConfigDict(extra="ignore")

    library: LibraryFile = Field(default_factory=LibraryFile)
    download: DownloadFile = Field(default_factory=DownloadFile)
    resolution: ResolutionFile = Field(default_factory=ResolutionFile)
    browser: BrowserFile = Field(default_factory=BrowserFile)
    logging: LoggingFile = Field(default_factory=LoggingFile)


class Settings(BaseModel):
    project_root: Path
    library_root: Path
    db_path: Path
    manifest_path: Path
    config_path: Path | None = None
    library: LibraryFile = Field(default_factory=LibraryFile)
    download: DownloadFile = Field(default_factory=DownloadFile)
    resolution: ResolutionFile = Field(default_factory=ResolutionFile)
    browser: BrowserFile = Field(default_factory=BrowserFile)
    logging_level: str = "INFO"


def find_project_config(start: Path) -> Path | None:
    current = start.resolve()
    for directory in [current, *current.parents]:
        candidate = directory / ".bibvault" / "config.toml"
        if candidate.is_file():
            return candidate
    return None


def _read_config(path: Path) -> dict:
    try:
        with path.open("rb") as handle:
            loaded = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"could not read configuration {path}: {exc}") from exc
    if not isinstance(loaded, dict):
        return {}
    return loaded


def _overlay(base: dict, extra: dict) -> dict:
    merged = dict(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _overlay(merged[key], value)
        else:
            merged[key] = value
    return merged


def _validation_source(
    location: tuple[object, ...],
    sources: list[tuple[Path, dict]],
) -> Path | None:
    for path, values in reversed(sources):
        current: object = values
        for part in location:
            if not isinstance(part, str) or not isinstance(current, dict) or part not in current:
                break
            current = current[part]
        else:
            return path
    return None


def _config_validation_error(
    error: ValidationError,
    sources: list[tuple[Path, dict]],
) -> ConfigError:
    details: list[str] = []
    for item in error.errors():
        location = tuple(item["loc"])
        field = ".".join(str(part) for part in location)
        source = _validation_source(location, sources)
        prefix = f"{source}: " if source is not None else ""
        details.append(f"{prefix}{field}: {item['msg']}")
    return ConfigError("invalid configuration: " + "; ".join(details))


def _warn_unwired_options(data: dict, sources: list[tuple[Path, dict]]) -> None:
    for (section, field), implemented_value in _UNWIRED_DEFAULTS.items():
        section_data = data.get(section)
        if not isinstance(section_data, dict) or field not in section_data:
            continue
        configured = section_data[field]
        if configured == implemented_value:
            continue
        source = _validation_source((section, field), sources)
        location = f"{source}: " if source is not None else ""
        logger.warning(
            "%s%s.%s is not implemented and is ignored; BibVault uses %r",
            location,
            section,
            field,
            implemented_value,
        )


def load_settings(start: Path | None = None, config_path: Path | None = None) -> Settings:
    cwd = (start or Path.cwd()).resolve()
    project_config: Path | None
    if config_path is not None:
        project_config = config_path.expanduser().resolve()
        if not project_config.exists():
            raise ConfigError(f"configuration file not found: {project_config}")
        if not project_config.is_file():
            raise ConfigError(f"configuration path is not a file: {project_config}")
    else:
        project_config = find_project_config(cwd)
    data: dict = {}
    sources: list[tuple[Path, dict]] = []
    if USER_CONFIG.is_file():
        user_data = _read_config(USER_CONFIG)
        data = _overlay(data, user_data)
        sources.append((USER_CONFIG, user_data))
    credentials = credentials_path()
    if credentials.is_file():
        credential_data = _read_config(credentials)
        data = _overlay(data, credential_data)
        sources.append((credentials, credential_data))
    if project_config and project_config.is_file():
        project_data = _read_config(project_config)
        data = _overlay(data, project_data)
        sources.append((project_config, project_data))

    email = os.environ.get("BIBVAULT_EMAIL")
    if email:
        data = _overlay(data, {"resolution": {"email": email}})
    api_key = os.environ.get("BIBVAULT_OPENALEX_API_KEY")
    if api_key:
        data = _overlay(data, {"resolution": {"openalex_api_key": api_key}})
    library_root_env = os.environ.get("BIBVAULT_LIBRARY_ROOT")
    if library_root_env:
        data = _overlay(data, {"library": {"root": library_root_env}})

    try:
        parsed = ConfigFile.model_validate(data)
    except ValidationError as exc:
        raise _config_validation_error(exc, sources) from exc
    _warn_unwired_options(data, sources)
    if project_config and project_config.is_file():
        if project_config.parent.name == ".bibvault":
            project_root = project_config.parent.parent
        else:
            project_root = project_config.parent
    else:
        project_root = cwd

    library_root = Path(parsed.library.root)
    if not library_root.is_absolute():
        library_root = project_root / library_root

    vault = project_root / ".bibvault"
    return Settings(
        project_root=project_root,
        library_root=library_root,
        db_path=vault / "index.sqlite",
        manifest_path=vault / "manifest.json",
        config_path=project_config if project_config and project_config.is_file() else None,
        library=parsed.library,
        download=parsed.download,
        resolution=parsed.resolution,
        browser=parsed.browser,
        logging_level=parsed.logging.level,
    )


def credentials_path() -> Path:
    return USER_CONFIG.parent / "credentials.toml"


def saved_openalex_key() -> str | None:
    path = credentials_path()
    if not path.is_file():
        return None
    key = _read_config(path).get("resolution", {}).get("openalex_api_key")
    return key if isinstance(key, str) else None


def save_openalex_key(key: str) -> Path:
    """Write the key to the credentials file, which only the user can read."""

    path = credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    text = (
        "# Written by `bibvault auth login openalex`. The key is also your openalex.org login; keep it private.\n"
        f"[resolution]\nopenalex_api_key = {json.dumps(key)}\n"
    )
    # mkstemp creates the file as 0600, so the key is never readable by others, not even briefly.
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".credentials-")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return path


def save_proxy_host(publisher: str, proxy: str) -> Path:
    """Set one entry under [browser.proxy_hosts] in the user's config, keeping the rest of the file as written."""

    path = USER_CONFIG
    document = tomlkit.parse(path.read_text(encoding="utf-8")) if path.is_file() else tomlkit.document()
    browser = document.setdefault("browser", tomlkit.table())
    browser.setdefault("proxy_hosts", tomlkit.table())[publisher] = proxy
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(tomlkit.dumps(document), encoding="utf-8")
    return path


def remove_openalex_key() -> Path | None:
    path = credentials_path()
    if not path.is_file():
        return None
    path.unlink()
    return path


DEFAULT_CONFIG = """\
[library]
root = "./papers"

[download]
concurrency = 4
timeout_seconds = 30
max_retries = 3
max_bytes = 80000000
min_interval_seconds = 0.2

[resolution]
prefer_published_version = true
allow_preprints = true
use_openalex = true
# The OpenAlex API key does not belong in this file: run `bibvault auth login openalex`.
email = ""
title_match_threshold = 0.92

[browser]
interval_seconds = 5
max_per_run = 50
timeout_seconds = 60
# Publishers reached through your library's proxy go under [browser.proxy_hosts]; see the README.

[logging]
level = "INFO"
"""
