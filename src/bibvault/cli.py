"""BibVault command line."""

import asyncio
import json
import logging
import os
import re
import sys
from enum import StrEnum
from pathlib import Path

import httpx
import typer

from bibvault import __version__
from bibvault.config import (
    DEFAULT_CONFIG,
    Settings,
    credentials_path,
    load_settings,
    remove_openalex_key,
    save_openalex_key,
    saved_openalex_key,
)
from bibvault.errors import BibVaultError, FileCollisionError, ParseError
from bibvault.models import Reference
from bibvault.parsing import parse_paths
from bibvault.pipeline import (
    FetchDisplay,
    format_report,
    import_file,
    lookup_reference,
    run_fetch,
    summarize,
)
from bibvault.resolve import (
    KEYLESS_DAILY_REQUESTS,
    check_openalex_key,
    keyless_requests_today,
    openalex_paused_until,
)
from bibvault.storage import Library

app = typer.Typer(add_completion=False, no_args_is_help=True)
auth_app = typer.Typer(no_args_is_help=True, help="Manage the API keys BibVault uses.")
app.add_typer(auth_app, name="auth")

_KEY_ENV = "BIBVAULT_OPENALEX_API_KEY"
_KEYLESS_HINT = (
    f"Until you add one, BibVault sends OpenAlex at most {KEYLESS_DAILY_REQUESTS} requests a day, "
    "only for papers without a local PDF, and no title searches. "
    "Get a free key at https://openalex.org/settings/api, then run `bibvault auth login openalex`."
)
# Printable ASCII without spaces; anything else is a paste accident.
_API_KEY = re.compile(r"[!-~]+")


class Service(StrEnum):
    openalex = "openalex"


@app.callback()
def main(
    ctx: typer.Context,
    config: Path | None = typer.Option(None, "--config", help="Path to .bibvault/config.toml"),
) -> None:
    """Acquire open-access papers and map them from BibTeX citation keys."""

    settings = load_settings(config_path=config)
    ctx.obj = settings
    logging.basicConfig(
        level=getattr(logging, settings.logging_level.upper(), logging.INFO),
        format="%(levelname)s %(message)s",
        stream=sys.stderr,
    )


@app.command()
def version() -> None:
    """Print the BibVault version."""

    typer.echo(__version__)


@app.command()
def init(ctx: typer.Context) -> None:
    """Create .bibvault/config.toml and the papers directory."""

    settings: Settings = ctx.obj
    config_path = settings.project_root / ".bibvault" / "config.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    settings.library_root.mkdir(parents=True, exist_ok=True)
    library = _library(settings)
    library.close()
    if config_path.exists():
        typer.echo(f"already initialized: {config_path}")
    else:
        config_path.write_text(DEFAULT_CONFIG, encoding="utf-8")
        typer.echo(f"initialized {config_path}")
    if not (settings.resolution.openalex_api_key or "").strip():
        typer.echo(f"OpenAlex has no API key yet. {_KEYLESS_HINT}")


@app.command()
def fetch(
    ctx: typer.Context,
    bib: list[Path] = typer.Argument(..., help="BibTeX files"),
    key: list[str] | None = typer.Option(None, "--key", help="Fetch only these citation keys"),
    # Papers with a PDF are always skipped now; the option stays so existing commands keep working.
    missing: bool = typer.Option(False, "--missing", hidden=True),
    output_format: str = typer.Option("text", "--format", help="text or json"),
    browser: bool = typer.Option(
        False,
        "--browser",
        help="Render dynamic web pages and open publisher links that require a browser",
    ),
) -> None:
    """Resolve BibTeX entries and download open-access PDFs and web pages."""

    _run_acquisition(ctx, bib, key, output_format, browser=browser)


@app.command()
def sync(
    ctx: typer.Context,
    bib: list[Path] = typer.Argument(..., help="BibTeX files"),
    output_format: str = typer.Option("text", "--format", help="text or json"),
) -> None:
    """Reconcile citation-key renames, then download missing open-access PDFs."""

    _run_acquisition(ctx, bib, None, output_format)


@app.command()
def locate(
    ctx: typer.Context,
    citation_key: str = typer.Argument(..., help="BibTeX citation key"),
    output_format: str = typer.Option("text", "--format", help="text or json"),
) -> None:
    """Resolve a citation key to local files and provenance."""

    settings: Settings = ctx.obj
    library = _library(settings)
    try:
        payload = lookup_reference(library, citation_key)
    finally:
        library.close()
    if output_format not in {"text", "json"}:
        raise typer.BadParameter("format must be text or json")
    chosen = None if payload is None else _located_file(payload)
    if payload is None or chosen is None:
        if output_format == "json":
            typer.echo(json.dumps(payload, indent=2, ensure_ascii=False))
        else:
            typer.echo(f"{citation_key}: not available")
        raise typer.Exit(1)
    if output_format == "json":
        typer.echo(json.dumps(payload, indent=2, ensure_ascii=False))
        return
    typer.echo(chosen["path"])
    typer.echo(f"reference_id: {payload['reference_id']}")
    typer.echo(f"status: {payload['status']}")
    typer.echo(f"version: {payload['version']}")
    typer.echo(f"sha256: {chosen['sha256']}")
    typer.echo(f"source: {chosen['source_url']}")
    typer.echo(f"provider: {chosen['provider']}")


def _located_file(payload: dict) -> dict | None:
    for role in ("pdf", "markdown"):
        record = payload["files"].get(role) or {}
        if record.get("path"):
            return record
    return None


@app.command()
def status(
    ctx: typer.Context,
    output_format: str = typer.Option("text", "--format", help="text or json"),
) -> None:
    """Show the stored acquisition status of each citation key."""

    if output_format not in {"text", "json"}:
        raise typer.BadParameter("format must be text or json")
    settings: Settings = ctx.obj
    library = _library(settings)
    try:
        rows = []
        for key in library.all_keys():
            found = lookup_reference(library, key.citation_key)
            if found is not None:
                rows.append(found)
    finally:
        library.close()
    if output_format == "json":
        typer.echo(json.dumps(rows, indent=2, ensure_ascii=False))
        return
    if not rows:
        typer.echo("No references stored.")
        return
    for row in rows:
        pdf = row["files"].get("pdf", {}).get("path") or ""
        typer.echo(f"{row['citation_key']}: {row['status']} {pdf}".rstrip())


@app.command()
def verify(
    ctx: typer.Context,
    references: bool = typer.Option(False, "--references", help="Also check citation-key mappings"),
) -> None:
    """Check stored files against their checksums."""

    settings: Settings = ctx.obj
    library = _library(settings)
    try:
        problems = library.reference_problems() if references else library.checksum_problems()
    finally:
        library.close()
    if problems:
        for problem in problems:
            typer.echo(problem)
        raise typer.Exit(1)
    typer.echo("verified")


@app.command("import")
def import_content(
    ctx: typer.Context,
    file: Path = typer.Argument(..., help="PDF or Markdown file to import"),
    key: str = typer.Option(..., "--key", help="Citation key that should own this file"),
    bib: Path | None = typer.Option(None, "--bib", help="BibTeX file used to resolve the key"),
    source_url: str | None = typer.Option(None, "--source-url", help="Original URL for provenance"),
    provider: str = typer.Option("import", "--provider", help="Provider recorded in the manifest"),
) -> None:
    """Associate a local PDF or Markdown file with a citation key."""

    if not file.is_file():
        raise typer.BadParameter(f"file not found: {file}")
    provider = provider.strip()
    if not provider:
        raise typer.BadParameter("provider must not be empty")
    settings: Settings = ctx.obj
    library = _library(settings)
    try:
        reference, ref_id = _reference_for_import(library, key, bib)
        result = import_file(
            library,
            reference,
            file,
            ref_id=ref_id,
            source_url=source_url,
            provider=provider,
        )
    except (BibVaultError, ParseError, FileCollisionError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    finally:
        library.close()
    typer.echo(f"{result.citation_key}: {result.status} {result.path or ''}".rstrip())


@auth_app.command("login")
def auth_login(
    ctx: typer.Context,
    service: Service = typer.Argument(..., help="Service whose key to store"),
) -> None:
    """Store an API key. It is typed without echo and never printed."""

    settings: Settings = ctx.obj
    key = typer.prompt("OpenAlex API key (input is hidden)", hide_input=True).strip()
    if not _API_KEY.fullmatch(key):
        typer.echo("that does not look like an API key; nothing was saved", err=True)
        raise typer.Exit(1)
    try:
        accepted, budget = asyncio.run(check_openalex_key(key, settings))
    except httpx.HTTPError as exc:
        accepted, budget = True, ""
        typer.echo(
            f"warning: could not check the key with OpenAlex ({exc.__class__.__name__}); saving it anyway",
            err=True,
        )
    if not accepted:
        typer.echo("OpenAlex rejected this key; nothing was saved", err=True)
        raise typer.Exit(1)
    path = save_openalex_key(key)
    typer.echo(f"saved OpenAlex key …{key[-4:]} to {path}")
    if budget:
        typer.echo(budget)
    if os.environ.get(_KEY_ENV):
        typer.echo(f"warning: {_KEY_ENV} is set and overrides the saved key", err=True)


@auth_app.command("logout")
def auth_logout(
    service: Service = typer.Argument(..., help="Service whose key to delete"),
) -> None:
    """Delete a stored API key."""

    path = remove_openalex_key()
    typer.echo(f"removed the OpenAlex key from {path}" if path else "no OpenAlex key was saved")
    if os.environ.get(_KEY_ENV):
        typer.echo(f"warning: {_KEY_ENV} is still set in your environment", err=True)


@auth_app.command("status")
def auth_status(ctx: typer.Context) -> None:
    """Show which keys are in use, without printing them."""

    settings: Settings = ctx.obj
    key = (settings.resolution.openalex_api_key or "").strip()
    if not key:
        typer.echo(f"OpenAlex: no API key. {_KEYLESS_HINT}")
    else:
        if os.environ.get(_KEY_ENV):
            source = _KEY_ENV
        elif saved_openalex_key() == key:
            source = str(credentials_path())
        else:
            source = "a config file"
        typer.echo(f"OpenAlex: API key …{key[-4:]} from {source}")
    if settings.db_path.is_file():
        library = _library(settings)
        try:
            until = openalex_paused_until(library, settings)
            used = keyless_requests_today(library)
        finally:
            library.close()
        if not key:
            typer.echo(f"OpenAlex: {used} of {KEYLESS_DAILY_REQUESTS} requests without a key used today (UTC)")
        if until is not None:
            typer.echo(f"OpenAlex: paused until {until.astimezone():%H:%M}, when its daily budget refills")


def _run_acquisition(
    ctx: typer.Context,
    bib: list[Path],
    key: list[str] | None,
    output_format: str,
    *,
    browser: bool = False,
) -> None:
    if output_format not in {"text", "json"}:
        raise typer.BadParameter("format must be text or json")
    for path in bib:
        if not path.is_file():
            raise typer.BadParameter(f"BibTeX file not found: {path}")
    settings: Settings = ctx.obj
    try:
        references, warnings = parse_paths(bib)
    except ParseError as exc:
        typer.echo(exc.message, err=True)
        raise typer.Exit(1) from exc
    for warning in warnings:
        typer.echo(f"warning: {warning}", err=True)
    library = _library(settings)
    live = output_format == "text"
    display = FetchDisplay()
    saved_levels: list[tuple[logging.Logger, int]] = []
    if live:
        # Library logs must not land on the progress line. The messages worth
        # showing are sent through on_status instead.
        for name in ("httpx", "httpcore", "crawl4ai", "trafilatura", "bibvault"):
            log = logging.getLogger(name)
            saved_levels.append((log, log.level))
            log.setLevel(logging.CRITICAL if name == "trafilatura" else logging.ERROR)

    def on_status(message: str) -> None:
        display.status(message)

    def on_progress(done: int, total: int, label: str) -> None:
        display.progress(done, total, label)

    def confirm(message: str) -> str:
        # Prompt under the progress line; read the answer from the real terminal stdin.
        display.status(message)
        if sys.platform != "win32" and sys.stdin.isatty():
            import termios

            # Enter pressed while a download was waiting must not answer this prompt.
            termios.tcflush(sys.stdin, termios.TCIFLUSH)
        try:
            return sys.stdin.readline()
        except EOFError:
            return ""

    try:
        results = asyncio.run(
            run_fetch(
                library,
                references,
                settings,
                only_keys=key,
                browser=browser,
                on_status=on_status if live else None,
                on_progress=on_progress if live else None,
                confirm=confirm if live and browser else None,
            )
        )
    except KeyError as exc:
        typer.echo(str(exc).strip("'"), err=True)
        raise typer.Exit(2) from exc
    finally:
        for log, level in saved_levels:
            log.setLevel(level)
        library.close()
    if output_format == "json":
        typer.echo(
            json.dumps(
                {"summary": summarize(results), "results": [item.model_dump() for item in results]},
                indent=2,
                ensure_ascii=False,
            )
        )
    else:
        typer.echo(format_report(results))
    if any(item.status == "failed" for item in results):
        raise typer.Exit(1)


def _reference_for_import(
    library: Library, citation_key: str, bib: Path | None
) -> tuple[Reference, str | None]:
    if bib is not None:
        if not bib.is_file():
            raise ParseError(f"BibTeX file not found: {bib}")
        references, _warnings = parse_paths([bib])
        match = next((item for item in references if item.citation_key == citation_key), None)
        if match is None:
            raise ParseError(f"citation key not in BibTeX: {citation_key}")
        return match, None
    existing = library.get_key(citation_key)
    if existing is None:
        raise ParseError(f"unknown citation key {citation_key}; pass --bib or sync the library first")
    publication = library.get_publication(existing.reference_id)
    reference = Reference(
        citation_key=existing.citation_key,
        entry_type=existing.entry_type or "misc",
        title=publication["title"] if publication else existing.title,
        year=publication["year"] if publication else None,
        doi=publication["doi"] if publication else None,
        arxiv_id=publication["arxiv_id"] if publication else None,
    )
    return reference, existing.reference_id


def _library(settings: Settings) -> Library:
    return Library(
        settings.project_root,
        settings.library_root,
        settings.db_path,
        settings.manifest_path,
    )
