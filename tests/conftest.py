import asyncio
from pathlib import Path

import pytest

from bibvault import browser, config, network, web
from tests.helpers import make_settings

PUBLIC_ADDRESS = "93.184.216.34"


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "USER_CONFIG", tmp_path / "user-config" / "config.toml")
    for name in ("BIBVAULT_EMAIL", "BIBVAULT_LIBRARY_ROOT", "BIBVAULT_OPENALEX_API_KEY"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def no_installed_browser(monkeypatch):
    """Never find the machine's real browser; tests that launch one name a fake executable.

    Nor look at, or move, the focus of the machine's real apps.
    """

    monkeypatch.setattr(browser, "_INSTALLED", ())
    monkeypatch.setattr(browser, "_COMMANDS", ())
    monkeypatch.setattr(browser, "_frontmost", lambda: None)


@pytest.fixture(autouse=True)
def sleeps(request, monkeypatch):
    """Record backoff delays instead of waiting. Live tests keep real sleeps."""

    delays: list[float] = []
    if request.node.get_closest_marker("live"):
        return delays

    async def record(delay, result=None):
        delays.append(delay)
        return result

    monkeypatch.setattr(asyncio, "sleep", record)
    return delays


@pytest.fixture(autouse=True)
def dns(request, monkeypatch):
    """Resolve every host to a public address unless the test maps it elsewhere."""

    table: dict[str, list[str]] = {}
    if request.node.get_closest_marker("live"):
        return table

    async def resolve(host, port):
        return table.get(host, [PUBLIC_ADDRESS])

    monkeypatch.setattr(network, "_resolve", resolve)
    return table


@pytest.fixture(autouse=True)
def crawl4ai_http(request, monkeypatch):
    """Keep ordinary tests on their mocked HTTP routes; adapter tests opt in explicitly."""

    if request.node.get_closest_marker("live") or request.node.get_closest_marker("crawl4ai"):
        return

    async def fetch_http(fetcher, url):
        return await fetcher._httpx_fallback(url)

    monkeypatch.setattr(web.WebPageFetcher, "fetch_http", fetch_http)


@pytest.fixture
def project(tmp_path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    return root


@pytest.fixture
def settings(project):
    return make_settings(project)
