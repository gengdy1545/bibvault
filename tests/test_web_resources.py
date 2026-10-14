"""Web resources (@online, @misc, @software, ...).

Pages without a DOI, arXiv id, or PDF link are saved as Markdown. Web entries
that point at a publication or a PDF still go through the PDF path.
"""

import asyncio
from types import SimpleNamespace

import pytest

from bibvault.errors import BrowserRequiredError
from bibvault.network import HostLimiter, make_client
from bibvault.web import WebPage, WebPageFetcher
from tests.helpers import (
    article_html,
    cli,
    fetch,
    locate,
    make_pdf,
    make_settings,
    mock_openalex,
    refs,
    work,
    write_config,
)

HTML = {"content-type": "text/html; charset=utf-8"}

WEB_PAGES = [
    pytest.param(
        "@online{hudi-index, title = {Apache Hudi Indexes}, year = {n.d.}, url = {https://hudi.apache.org/docs/indexes/}}",
        id="online-html",
    ),
    pytest.param(
        "@misc{pixels-retina, title = {Pixels Retina}, url = {https://github.com/pixelsdb/pixels/tree/master/pixels-retina}}",
        id="misc-github",
    ),
    pytest.param(
        "@software{duckdb, title = {DuckDB}, year = {2024}, url = {https://github.com/duckdb/duckdb}}",
        id="software",
    ),
    pytest.param(
        "@dataset{tpch, title = {TPC-H}, year = {2022}, url = {https://www.tpc.org/tpch/}}",
        id="dataset",
    ),
    pytest.param("@electronic{blog, title = {A Blog Post}, url = {https://blog.example/post}}", id="electronic"),
]


@pytest.mark.parametrize("text", WEB_PAGES)
def test_web_pages_are_saved_as_markdown(settings, respx_mock, text):
    respx_mock.get(url__regex=r"^https://").respond(200, text=article_html("Page"), headers=HTML)
    [result] = fetch(settings, refs(text)).values()
    assert result.status == "downloaded"
    assert result.path is not None and result.path.endswith(".md")
    assert result.provider == "crawl4ai+trafilatura"
    assert result.version == "archived"
    body = (settings.project_root / result.path).read_text(encoding="utf-8")
    assert "Page is documented on this page." in body
    assert "![figure](" in body
    assert not (settings.library_root / "assets").exists()


def test_two_keys_for_one_page_share_one_progress_step(settings, respx_mock):
    url = "https://hudi.apache.org/docs/indexes/"
    respx_mock.get(url).respond(200, text=article_html("Apache Hudi Indexes"), headers=HTML)
    text = (
        f"@online{{hudi-index, title = {{Apache Hudi Indexes}}, url = {{{url}}}}}\n"
        f"@online{{hudi-alias, title = {{Apache Hudi Indexes}}, url = {{{url}}}}}"
    )
    ticks: list[tuple[int, int, str]] = []
    fetch(settings, refs(text), on_progress=lambda done, total, label: ticks.append((done, total, label)))
    assert ticks == [(0, 1, "Fetching"), (1, 1, "Fetching")]


def test_web_bot_check_does_not_suggest_import(settings, respx_mock):
    url = "https://medium.com/@simpsons/global-vs-non-global-index-in-apache-hudi-ac880b031cbc"
    respx_mock.get(url).respond(
        403,
        content=b"<html><body>Just a moment</body></html>",
        headers={"content-type": "text/html", "cf-mitigated": "challenge"},
    )
    text = f"@online{{hudi-global-index, title = {{Global Index}}, url = {{{url}}}}}"
    result = fetch(settings, refs(text))["hudi-global-index"]
    assert result.status == "manual_action_required"
    assert result.error == f"medium.com only serves browsers (bot check): {url}"


def test_saved_page_keeps_trafilatura_image_urls(settings, respx_mock):
    url = "https://hudi.apache.org/docs/indexes/"
    respx_mock.get(url).respond(200, text=article_html("Apache Hudi Indexes"), headers=HTML)
    text = f"@online{{hudi-index, title = {{Apache Hudi Indexes}}, url = {{{url}}}}}"
    result = fetch(settings, refs(text))["hudi-index"]
    body = (settings.project_root / result.path).read_text(encoding="utf-8")
    assert "url: https://hudi.apache.org/docs/indexes/" in body
    assert "![figure](https://hudi.apache.org/docs/indexes/images/figure.png)" in body


def test_saved_page_is_not_fetched_again(settings, respx_mock):
    url = "https://hudi.apache.org/docs/indexes/"
    route = respx_mock.get(url).respond(200, text=article_html("Apache Hudi Indexes"), headers=HTML)
    text = f"@online{{hudi-index, title = {{Apache Hudi Indexes}}, url = {{{url}}}}}"
    fetch(settings, refs(text))
    second = fetch(settings, refs(text))["hudi-index"]
    assert second.status == "already_exists"
    assert second.path == "papers/hudi-index.md"
    assert route.call_count == 1


def test_renamed_web_key_moves_the_markdown(settings, respx_mock):
    url = "https://hudi.apache.org/docs/indexes/"
    route = respx_mock.get(url).respond(200, text=article_html("Apache Hudi Indexes"), headers=HTML)
    first = f"@online{{hudi-index, title = {{Apache Hudi Indexes}}, url = {{{url}}}}}"
    renamed = f"@online{{hudi-indexes, title = {{Apache Hudi Indexes}}, url = {{{url}}}}}"
    fetch(settings, refs(first))
    result = fetch(settings, refs(renamed))["hudi-indexes"]
    assert result.status == "already_exists"
    assert result.path == "papers/hudi-indexes.md"
    assert not (settings.library_root / "hudi-index.md").exists()
    assert route.call_count == 1


def test_two_keys_for_one_page_share_the_markdown(settings, respx_mock):
    url = "https://hudi.apache.org/docs/indexes/"
    route = respx_mock.get(url).respond(200, text=article_html("Apache Hudi Indexes"), headers=HTML)
    text = (
        f"@online{{hudi-index, title = {{Apache Hudi Indexes}}, url = {{{url}}}}}\n"
        f"@online{{hudi-alias, title = {{Apache Hudi Indexes}}, url = {{{url}}}}}"
    )
    results = fetch(settings, refs(text))
    assert results["hudi-index"].status == "downloaded"
    assert results["hudi-alias"].status == "already_exists"
    assert (settings.library_root / "hudi-index.md").read_bytes() == (
        settings.library_root / "hudi-alias.md"
    ).read_bytes()
    assert route.call_count == 1


def test_web_page_without_a_url_is_not_found(settings, respx_mock):
    result = fetch(settings, refs("@online{no-url, title = {Somewhere On The Web}}"))["no-url"]
    assert result.status == "not_found"
    assert result.error == "no URL to archive"
    assert len(respx_mock.calls) == 0


def test_local_web_url_is_refused(settings, respx_mock):
    text = "@online{local, title = {Local}, url = {http://127.0.0.1/secret}}"
    result = fetch(settings, refs(text))["local"]
    assert result.status == "failed"
    assert len(respx_mock.calls) == 0
    assert not any(settings.library_root.glob("*.md"))


def test_web_page_that_returns_a_pdf_fails(settings, respx_mock):
    url = "https://docs.example.org/notes"
    respx_mock.get(url).respond(200, content=b"%PDF-1.7\n", headers={"content-type": "application/pdf"})
    result = fetch(settings, refs(f"@online{{notes, title = {{Notes}}, url = {{{url}}}}}"))["notes"]
    assert result.status == "failed"
    assert "PDF" in result.error
    assert not any(settings.library_root.glob("*.md"))


def test_web_page_without_main_text_fails(settings, respx_mock):
    url = "https://docs.example.org/empty"
    respx_mock.get(url).respond(200, text="<html><body></body></html>", headers=HTML)
    result = fetch(settings, refs(f"@online{{empty, title = {{Empty}}, url = {{{url}}}}}"))["empty"]
    assert result.status == "failed"
    assert "no main text" in result.error


def test_browser_renders_a_javascript_placeholder(
    settings, respx_mock, monkeypatch
):
    url = "https://docs.example.org/app"
    respx_mock.get(url).respond(
        200,
        text=(
            "<html><head><title>JavaScript is disabled</title></head>"
            "<body><p>Please enable JavaScript to use this page.</p></body></html>"
        ),
        headers=HTML,
    )

    async def render(_fetcher, requested):
        assert requested == url
        return WebPage(final_url=url, html=article_html("Rendered App"))

    monkeypatch.setattr(WebPageFetcher, "fetch_browser", render)
    result = fetch(
        settings,
        refs(f"@online{{app, title = {{Rendered App}}, url = {{{url}}}}}"),
        browser=True,
    )["app"]
    assert result.status == "downloaded"
    assert result.provider == "crawl4ai+trafilatura"
    body = (settings.project_root / result.path).read_text(encoding="utf-8")
    assert "Rendered App is documented on this page." in body


@pytest.mark.crawl4ai
def test_crawl4ai_http_result_supplies_html(settings, monkeypatch):
    url = "https://docs.example.org/article"
    crawl_result = SimpleNamespace(
        success=True,
        error_message=None,
        status_code=200,
        redirected_status_code=None,
        redirected_url=url,
        url=url,
        response_headers=HTML,
        html=article_html("Crawled"),
    )

    class StubCrawler:
        async def arun(self, requested, *, config):
            assert requested == url
            assert config.cache_mode.value == "bypass"
            return crawl_result

    async def scenario():
        async with make_client(settings) as client:
            fetcher = WebPageFetcher(settings, HostLimiter(0), client)

            async def crawler():
                return StubCrawler()

            monkeypatch.setattr(fetcher, "_http", crawler)
            try:
                return await fetcher.fetch_http(url)
            finally:
                await fetcher.close()

    page = asyncio.run(scenario())
    assert page.final_url == url
    assert "Crawled is documented" in page.html


@pytest.mark.crawl4ai
def test_crawl4ai_http_failure_keeps_bot_check_classification(
    settings, respx_mock, monkeypatch
):
    url = "https://medium.example/article"
    respx_mock.get(url).respond(
        403,
        text="<html><body>Just a moment</body></html>",
        headers={"content-type": "text/html", "cf-mitigated": "challenge"},
    )
    crawl_result = SimpleNamespace(
        success=False,
        error_message="HTTP 403",
    )

    class StubCrawler:
        async def arun(self, _url, *, config):
            return crawl_result

    async def scenario():
        async with make_client(settings) as client:
            fetcher = WebPageFetcher(settings, HostLimiter(0), client)

            async def crawler():
                return StubCrawler()

            monkeypatch.setattr(fetcher, "_http", crawler)
            try:
                await fetcher.fetch_http(url)
            finally:
                await fetcher.close()

    with pytest.raises(BrowserRequiredError):
        asyncio.run(scenario())


@pytest.mark.crawl4ai
def test_crawl4ai_browser_reports_antibot_challenge(settings, monkeypatch):
    url = "https://dzone.example/article"

    class StubCrawler:
        async def arun(self, _url, *, config):
            raise RuntimeError("Blocked by anti-bot protection: Cloudflare JS challenge")

    async def scenario():
        async with make_client(settings) as client:
            fetcher = WebPageFetcher(settings, HostLimiter(0), client)

            async def crawler():
                return StubCrawler()

            monkeypatch.setattr(fetcher, "_browser", crawler)
            try:
                await fetcher.fetch_browser(url)
            finally:
                await fetcher.close()

    with pytest.raises(BrowserRequiredError):
        asyncio.run(scenario())


def test_missing_web_page_is_not_found(settings, respx_mock):
    url = "https://blog.example/missing"
    respx_mock.get(url).respond(404)
    result = fetch(settings, refs(f"@online{{gone, title = {{Gone}}, url = {{{url}}}}}"))["gone"]
    assert result.status == "not_found"
    assert not any(settings.library_root.glob("*.md"))


def test_online_pdf_is_downloaded(settings, respx_mock):
    text = "@online{whitepaper, title = {Table Formats}, url = {https://docs.example.org/whitepaper.pdf}}"
    respx_mock.get("https://docs.example.org/whitepaper.pdf").respond(200, content=make_pdf("whitepaper"))
    result = fetch(settings, refs(text))["whitepaper"]
    assert result.status == "downloaded"
    assert result.path == "papers/whitepaper.pdf"


def test_online_pdf_that_turns_out_to_be_html_fails(settings, respx_mock):
    text = "@online{whitepaper, title = {Table Formats}, url = {https://docs.example.org/whitepaper.pdf}}"
    respx_mock.get("https://docs.example.org/whitepaper.pdf").respond(
        200, content=b"<html><body>Moved</body></html>", headers={"content-type": "text/html"}
    )
    assert fetch(settings, refs(text))["whitepaper"].status == "failed"


def test_online_entry_with_a_doi_uses_the_publication_path(settings, respx_mock):
    doi = "10.5281/zenodo.123456"
    text = f"@online{{dataset-paper, title = {{Benchmark}}, year = {{2023}}, doi = {{{doi}}}, url = {{https://zenodo.org/record/123456}}}}"
    mock_openalex(respx_mock, doi, work(is_oa=False))
    result = fetch(settings, refs(text))["dataset-paper"]
    assert result.status == "no_open_access_found"
    assert result.reference_id == f"doi:{doi}"


@pytest.mark.parametrize(
    "text",
    [
        pytest.param(
            "@misc{lance-eprint, title = {Lance}, year = {2025}, eprint = {2504.15247v2}, archivePrefix = {arXiv}}",
            id="misc-eprint",
        ),
        pytest.param(
            "@online{lance-abs, title = {Lance}, year = {2025}, url = {https://arxiv.org/abs/2504.15247}}",
            id="online-arxiv-abs",
        ),
    ],
)
def test_web_entries_pointing_at_arxiv_download_the_preprint(settings, respx_mock, text):
    respx_mock.get("https://arxiv.org/pdf/2504.15247").respond(200, content=make_pdf("lance"))
    [result] = fetch(settings, refs(text)).values()
    assert result.status == "downloaded"
    assert result.reference_id == "arxiv:2504.15247"


def test_web_pdf_link_with_a_query_string_is_downloaded(settings, respx_mock):
    url = "https://github.com/org/repo/blob/main/paper.pdf?raw=true"
    respx_mock.get(url).respond(200, content=make_pdf("repo paper"))
    text = f"@misc{{repo, title = {{Repo}}, url = {{{url}}}}}"
    [result] = fetch(settings, refs(text)).values()
    assert result.status == "downloaded"


def test_archived_pages_are_visible_to_agents(settings, respx_mock):
    url = "https://hudi.apache.org/docs/indexes/"
    respx_mock.get(url).respond(200, text=article_html("Apache Hudi Indexes"), headers=HTML)
    text = f"@online{{hudi-index, title = {{Apache Hudi Indexes}}, url = {{{url}}}}}"
    fetch(settings, refs(text))
    payload = locate(settings, "hudi-index")
    assert payload["status"] == "downloaded"
    assert payload["version"] == "archived"
    assert payload["files"]["markdown"]["path"] == "papers/hudi-index.md"
    assert payload["files"]["markdown"]["provider"] == "crawl4ai+trafilatura"
    assert payload["files"]["markdown"]["source_url"] == url

    manifest = settings.manifest_path.read_text(encoding="utf-8")
    assert '"hudi-index"' in manifest
    assert '"papers/hudi-index.md"' in manifest


def test_cli_locates_an_archived_page(project, respx_mock):
    url = "https://hudi.apache.org/docs/indexes/"
    respx_mock.get(url).respond(200, text=article_html("Apache Hudi Indexes"), headers=HTML)
    config = write_config(project)
    bib = project / "refs.bib"
    bib.write_text(
        f"@online{{hudi-index, title = {{Apache Hudi Indexes}}, url = {{{url}}}}}",
        encoding="utf-8",
    )
    fetched = cli(config, "fetch", str(bib))
    assert fetched.exit_code == 0, fetched.output
    assert "Newly downloaded:" in fetched.output

    located = cli(config, "locate", "hudi-index")
    assert located.exit_code == 0, located.output
    assert "papers/hudi-index.md" in located.output


def saved_article(title: str = "HeatWave") -> str:
    # Long enough that Trafilatura keeps the article instead of falling back to the whole page.
    paragraph = f"{title} stores analytic data in memory and answers SQL without a separate warehouse. "
    return (
        "<!DOCTYPE html><html><head>"
        f"<title>{title}</title></head><body>"
        "<nav>Home Docs Blog</nav><article>"
        f"<h1>{title}</h1><p>{paragraph * 4}See <a href=\"/docs/start\">the guide</a>.</p>"
        '<img src="images/figure.png" alt="figure">'
        "</article><footer>Copyright 2026</footer></body></html>"
    )


def test_cli_imports_saved_html_as_markdown(project):
    url = "https://dzone.com/articles/heatwave"
    config = write_config(project)
    bib = project / "refs.bib"
    bib.write_text(f"@online{{heatwave, title = {{HeatWave}}, url = {{{url}}}}}", encoding="utf-8")
    saved = project / "Downloads" / "heatwave.html"
    saved.parent.mkdir()
    saved.write_text(saved_article(), encoding="utf-8")

    imported = cli(
        config,
        "import",
        str(saved),
        "--key",
        "heatwave",
        "--bib",
        str(bib),
        "--source-url",
        url,
    )
    assert imported.exit_code == 0, imported.output
    assert "papers/heatwave.md" in imported.output
    body = (project / "papers" / "heatwave.md").read_text(encoding="utf-8")
    assert "stores analytic data in memory" in body
    assert "[the guide](https://dzone.com/docs/start)" in body
    assert "![figure](https://dzone.com/articles/images/figure.png)" in body
    assert "Home Docs Blog" not in body
    assert "Copyright 2026" not in body

    payload = locate(make_settings(project), "heatwave")
    assert payload["version"] == "provided"
    assert payload["files"]["markdown"]["provider"] == "trafilatura"
    assert payload["files"]["markdown"]["source_url"] == url

    again = cli(config, "import", str(saved), "--key", "heatwave", "--bib", str(bib), "--source-url", url)
    assert again.exit_code == 0, again.output
    assert "already_exists" in again.output


def test_saved_html_without_a_source_url_keeps_relative_links(project):
    config = write_config(project)
    bib = project / "refs.bib"
    bib.write_text("@online{heatwave, title = {HeatWave}, url = {https://dzone.com/articles/heatwave}}", encoding="utf-8")
    saved = project / "heatwave.htm"
    saved.write_text(saved_article(), encoding="utf-8")
    imported = cli(config, "import", str(saved), "--key", "heatwave", "--bib", str(bib))
    assert imported.exit_code == 0, imported.output
    body = (project / "papers" / "heatwave.md").read_text(encoding="utf-8")
    assert "[the guide](/docs/start)" in body
    assert "![figure](images/figure.png)" in body


def test_saved_html_without_main_text_is_rejected(project):
    config = write_config(project)
    bib = project / "refs.bib"
    bib.write_text("@online{empty, title = {Empty}, url = {https://example.org/empty}}", encoding="utf-8")
    saved = project / "empty.html"
    saved.write_text("<html><body></body></html>", encoding="utf-8")
    result = cli(config, "import", str(saved), "--key", "empty", "--bib", str(bib))
    assert result.exit_code == 1
    assert "no main text" in result.output
    assert not (project / "papers" / "empty.md").exists()


def test_cli_imports_markdown_with_provenance(project, respx_mock):
    url = "https://dzone.com/articles/an-in-depth-look-at-oracle-mysql-heatwave"
    config = write_config(project)
    bib = project / "refs.bib"
    bib.write_text(
        f"@online{{heatwave, title = {{Oracle MySQL HeatWave}}, url = {{{url}}}}}",
        encoding="utf-8",
    )
    downloaded = project / "Downloads" / "heatwave.md"
    downloaded.parent.mkdir()
    downloaded.write_text("# Oracle MySQL HeatWave\n\nArticle body.\n", encoding="utf-8")

    imported = cli(
        config,
        "import",
        str(downloaded),
        "--key",
        "heatwave",
        "--bib",
        str(bib),
        "--source-url",
        url,
        "--provider",
        "jina",
    )
    assert imported.exit_code == 0, imported.output
    assert "papers/heatwave.md" in imported.output

    payload = locate(make_settings(project), "heatwave")
    assert payload["status"] == "downloaded"
    assert payload["version"] == "provided"
    assert payload["files"]["markdown"]["provider"] == "jina"
    assert payload["files"]["markdown"]["source_url"] == url

    fetched = cli(config, "fetch", str(bib))
    assert fetched.exit_code == 0, fetched.output
    assert "Already on disk:      1" in fetched.output
    assert len(respx_mock.calls) == 0
