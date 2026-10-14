import asyncio
import json
import re
from pathlib import Path

import pymupdf
from typer.testing import CliRunner, Result

from bibvault.cli import app
from bibvault.config import DownloadFile, ResolutionFile, Settings
from bibvault.models import AcquireResult, Reference
from bibvault.parsing import parse_bibtex
from bibvault.pipeline import lookup_reference, run_fetch
from bibvault.storage import Library

FIXTURES = Path(__file__).parent / "fixtures"
SAMPLE_BIB = FIXTURES / "sample.bib"
PDF = {"content-type": "application/pdf"}


def make_pdf(text: str) -> bytes:
    document = pymupdf.open()
    page = document.new_page(width=300, height=200)
    page.insert_text((36, 72), text)
    payload = document.tobytes()
    document.close()
    return payload


def article_html(title: str) -> str:
    return (
        "<!DOCTYPE html><html><head>"
        f"<title>{title}</title></head><body><article>"
        f"<h1>{title}</h1><p>{title} is documented on this page.</p>"
        '<img src="images/figure.png" alt="figure">'
        "</article></body></html>"
    )


def make_settings(
    root: Path,
    *,
    library_root: Path | None = None,
    resolution: ResolutionFile | None = None,
    **download,
) -> Settings:
    options = dict(concurrency=1, max_retries=0, min_interval_seconds=0, timeout_seconds=5)
    options.update(download)
    return Settings(
        project_root=root,
        library_root=library_root or root / "papers",
        db_path=root / ".bibvault" / "index.sqlite",
        manifest_path=root / ".bibvault" / "manifest.json",
        download=DownloadFile(**options),
        resolution=resolution or ResolutionFile(),
        logging_level="WARNING",
    )


def no_openalex() -> ResolutionFile:
    return ResolutionFile(use_openalex=False)


def with_key(**options) -> ResolutionFile:
    """Resolution settings of a user who configured an OpenAlex key, so no keyless limits apply."""

    return ResolutionFile(openalex_api_key="test-key", **options)


def open_library(settings: Settings) -> Library:
    return Library(
        settings.project_root,
        settings.library_root,
        settings.db_path,
        settings.manifest_path,
    )


def refs(text: str) -> list[Reference]:
    references, warnings = parse_bibtex(text)
    assert warnings == []
    return references


def fetch(settings: Settings, references: list[Reference], **kwargs) -> dict[str, AcquireResult]:
    library = open_library(settings)
    # Tests never wait for a human at the browser sign-in gate.
    kwargs.setdefault("confirm", lambda _message: "")
    try:
        results = asyncio.run(run_fetch(library, references, settings, **kwargs))
    finally:
        library.close()
    return {item.citation_key: item for item in results}


def locate(settings: Settings, citation_key: str) -> dict | None:
    library = open_library(settings)
    try:
        return lookup_reference(library, citation_key)
    finally:
        library.close()


def located_bytes(settings: Settings, citation_key: str, role: str = "pdf") -> bytes:
    payload = locate(settings, citation_key)
    assert payload is not None, citation_key
    path = payload["files"][role]["path"]
    assert path, f"{citation_key} has no {role} on disk"
    return (settings.project_root / path).read_bytes()


def reference_problems(settings: Settings) -> list[str]:
    library = open_library(settings)
    try:
        return library.reference_problems()
    finally:
        library.close()


def leftovers(settings: Settings) -> list[str]:
    if not settings.library_root.exists():
        return []
    return sorted(path.name for path in settings.library_root.glob("*.partial"))


def openalex_doi_pattern(doi: str) -> str:
    return rf"^https://api\.openalex\.org/works/https://doi\.org/{re.escape(doi)}(\?.*)?$"


def location(pdf_url: str | None = None, version: str = "publishedVersion", landing: str | None = None) -> dict:
    return {"pdf_url": pdf_url, "version": version, "landing_page_url": landing}


def work(*locations: dict, is_oa: bool | None = None, oa_url: str | None = None, title: str = "", year: int | None = None) -> dict:
    return {
        "display_name": title,
        "publication_year": year,
        "best_oa_location": locations[0] if locations else None,
        "locations": list(locations[1:]),
        "open_access": {
            "is_oa": bool(locations) if is_oa is None else is_oa,
            "oa_url": oa_url,
        },
    }


def mock_openalex(respx_mock, doi: str, payload: dict | None = None, status: int = 200):
    route = respx_mock.get(url__regex=openalex_doi_pattern(doi))
    if payload is None:
        return route.respond(status)
    return route.respond(status, json=payload)


def requested_hosts(respx_mock) -> set[str]:
    return {call.request.url.host for call in respx_mock.calls}


def vldb_paper(title: str, start_page: int | str, pdf: str) -> dict:
    return {"title": title, "authors": "A. Author", "start_page": start_page, "pdf": pdf}


def vldb_volume_page(*papers: dict) -> str:
    """A vldb.org volume page: the paper list sits in Next.js page data."""

    data = {"props": {"pageProps": {"groupedIssues": {"12": list(papers)}}}}
    return f'<html><body><script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script></body></html>'


def mock_vldb_volume(respx_mock, volume: int, *papers: dict, status: int = 200):
    route = respx_mock.get(f"https://www.vldb.org/pvldb/volumes/{volume}/")
    return route.respond(status, text=vldb_volume_page(*papers), headers={"content-type": "text/html"})


def mock_vldb_unknown_files(respx_mock):
    """vldb.org answers a PDF path it does not have with 200 and an HTML page."""

    route = respx_mock.head(url__regex=r"^https://www\.vldb\.org/pvldb/vol\d+/.+\.pdf$")
    return route.respond(200, text="<html></html>", headers={"content-type": "text/html"})


TEST_CONFIG = """\
[download]
concurrency = 1
max_retries = 0
min_interval_seconds = 0

[logging]
level = "WARNING"
"""


def write_config(project: Path, extra: str = "") -> Path:
    path = project / ".bibvault" / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(TEST_CONFIG + extra, encoding="utf-8")
    return path


def cli(config: Path, *args: str) -> Result:
    return CliRunner().invoke(app, ["--config", str(config), *args])
