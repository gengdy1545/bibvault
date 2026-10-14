"""Acquire web pages with Crawl4AI and extract their main text with Trafilatura."""

from __future__ import annotations

import ipaddress
import socket
import tempfile
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

import httpx
from aiohttp import ClientSession, ClientTimeout, TCPConnector, TraceConfig
from aiohttp.abc import AbstractResolver
from aiohttp.resolver import DefaultResolver
from crawl4ai import (
    AsyncWebCrawler,
    BrowserConfig,
    CacheMode,
    CrawlerRunConfig,
    HTTPCrawlerConfig,
)
from crawl4ai.async_crawler_strategy import (
    AsyncHTTPCrawlerStrategy,
    AsyncPlaywrightCrawlerStrategy,
)
from crawl4ai.async_logger import AsyncLogger
from trafilatura import extract

from bibvault.config import Settings
from bibvault.errors import (
    AccessDeniedError,
    AuthenticationRequiredError,
    BibVaultError,
    BrowserRequiredError,
    DownloadTooLargeError,
    NotFoundError,
    RateLimitedError,
    UnsafeURLError,
)
from bibvault.network import (
    USER_AGENT,
    HostLimiter,
    _assert_html,
    fetch_html,
    guard_url,
    is_public_address,
)


@dataclass(frozen=True)
class WebPage:
    final_url: str
    html: str | bytes


class _CrawlFailed(Exception):
    pass


class _PublicResolver(AbstractResolver):
    """Make Crawl4AI's aiohttp transport connect only to public addresses."""

    def __init__(self) -> None:
        self._resolver = DefaultResolver()

    async def resolve(
        self,
        host: str,
        port: int = 0,
        family: socket.AddressFamily = socket.AF_INET,
    ) -> list:
        records = await self._resolver.resolve(host, port, family)
        for record in records:
            address = ipaddress.ip_address(record["host"].split("%", 1)[0])
            if not is_public_address(address):
                raise UnsafeURLError(f"refusing URL: {host} resolves to local address {address}")
        return records

    async def close(self) -> None:
        await self._resolver.close()


class _SafeHTTPCrawlerStrategy(AsyncHTTPCrawlerStrategy):
    """Crawl4AI's HTTP strategy with guarded DNS and redirect targets."""

    _session: ClientSession | None

    async def start(self) -> None:
        if self._session:
            return

        async def guard_redirect(_session, _context, params) -> None:
            location = params.response.headers.get("location")
            if location:
                await guard_url(urljoin(str(params.url), location))

        trace = TraceConfig()
        trace.on_request_redirect.append(guard_redirect)
        connector = TCPConnector(
            limit=self.max_connections,
            ttl_dns_cache=self.dns_cache_ttl,
            use_dns_cache=True,
            force_close=False,
            resolver=_PublicResolver(),
        )
        self._session = ClientSession(
            headers=dict(self._BASE_HEADERS),
            connector=connector,
            timeout=ClientTimeout(total=self.DEFAULT_TIMEOUT),
            trace_configs=[trace],
        )


class WebPageFetcher:
    """One Crawl4AI HTTP crawler and one lazy browser crawler per fetch run."""

    def __init__(
        self,
        settings: Settings,
        limiter: HostLimiter,
        diagnostic_client: httpx.AsyncClient,
    ) -> None:
        self.settings = settings
        self.limiter = limiter
        self.diagnostic_client = diagnostic_client
        self._temporary = tempfile.TemporaryDirectory(prefix="bibvault-crawl4ai-")
        self._logger = AsyncLogger(verbose=False)
        self._http_crawler: AsyncWebCrawler | None = None
        self._browser_crawler: AsyncWebCrawler | None = None

    async def close(self) -> None:
        if self._browser_crawler is not None:
            await self._browser_crawler.close()
        if self._http_crawler is not None:
            await self._http_crawler.close()
        self._temporary.cleanup()

    async def fetch_http(self, url: str) -> WebPage:
        """Fetch with Crawl4AI's lightweight strategy, retaining the old client for diagnostics."""

        await guard_url(url)
        await self.limiter.wait(urlparse(url).hostname or "")
        try:
            crawler = await self._http()
            result = await crawler.arun(url, config=self._http_config())
            return await self._page(result, url)
        except BibVaultError:
            raise
        except Exception:
            # Crawl4AI 0.9 discards status and headers for non-2xx responses.
            # The established client is used only to preserve BibVault's exact
            # retry and error classification for those failures.
            return await self._httpx_fallback(url)

    async def fetch_browser(self, url: str) -> WebPage:
        """Render one page with Crawl4AI's ordinary Chromium mode."""

        await guard_url(url)
        await self.limiter.wait(urlparse(url).hostname or "")
        try:
            crawler = await self._browser()
            result = await crawler.arun(url, config=self._browser_config())
            return await self._page(result, url)
        except BibVaultError:
            raise
        except Exception as exc:
            detail = str(exc) or exc.__class__.__name__
            if "anti-bot protection" in detail.lower() or "cloudflare js challenge" in detail.lower():
                host = urlparse(url).hostname or url
                raise BrowserRequiredError(
                    f"{host} only serves browsers (bot check): {url}"
                ) from exc
            if "executable doesn't exist" in detail.lower() or "browsertype.launch" in detail.lower():
                detail += "; run `uv run crawl4ai-setup`"
            raise BibVaultError(f"Crawl4AI browser failed for {url}: {detail}") from exc

    async def _httpx_fallback(self, url: str) -> WebPage:
        final_url, html = await fetch_html(
            self.diagnostic_client,
            url,
            self.settings,
            self.limiter,
        )
        return WebPage(final_url=final_url, html=html)

    async def _http(self) -> AsyncWebCrawler:
        if self._http_crawler is None:
            config = HTTPCrawlerConfig(
                headers={"User-Agent": USER_AGENT},
                follow_redirects=True,
                verify_ssl=True,
                downloads_path=self._temporary.name,
            )
            strategy = _SafeHTTPCrawlerStrategy(
                browser_config=config,
                logger=self._logger,
                max_connections=self.settings.download.concurrency,
            )
            self._http_crawler = AsyncWebCrawler(
                crawler_strategy=strategy,
                base_directory=self._temporary.name,
                logger=self._logger,
            )
            await self._http_crawler.start()
        return self._http_crawler

    async def _browser(self) -> AsyncWebCrawler:
        if self._browser_crawler is None:
            browser = BrowserConfig(
                browser_type="chromium",
                headless=True,
                ignore_https_errors=False,
                enable_stealth=False,
                verbose=False,
            )
            strategy = AsyncPlaywrightCrawlerStrategy(
                browser_config=browser,
                logger=self._logger,
            )

            async def protect_context(page, *, context, **_kwargs):
                if getattr(context, "_bibvault_routes_guarded", False):
                    return page

                async def route_request(route) -> None:
                    request = route.request
                    if request.resource_type in {"image", "media", "font"}:
                        await route.abort()
                        return
                    scheme = urlparse(request.url).scheme
                    if scheme in {"data", "blob", "about"}:
                        await route.continue_()
                        return
                    try:
                        await guard_url(request.url)
                    except UnsafeURLError:
                        await route.abort()
                    else:
                        await route.continue_()

                await context.route("**/*", route_request)
                context._bibvault_routes_guarded = True
                return page

            strategy.set_hook("on_page_context_created", protect_context)
            self._browser_crawler = AsyncWebCrawler(
                crawler_strategy=strategy,
                config=browser,
                base_directory=self._temporary.name,
                logger=self._logger,
            )
            await self._browser_crawler.start()
        return self._browser_crawler

    def _http_config(self) -> CrawlerRunConfig:
        return CrawlerRunConfig(
            cache_mode=CacheMode.BYPASS,
            page_timeout=int(self.settings.download.timeout_seconds * 1000),
            word_count_threshold=1,
            verbose=False,
        )

    def _browser_config(self) -> CrawlerRunConfig:
        return CrawlerRunConfig(
            cache_mode=CacheMode.BYPASS,
            page_timeout=int(self.settings.browser.timeout_seconds * 1000),
            wait_until="domcontentloaded",
            delay_before_return_html=6.0,
            word_count_threshold=1,
            verbose=False,
        )

    async def _page(self, result, requested_url: str) -> WebPage:
        if not result.success:
            raise _CrawlFailed(result.error_message or f"Crawl4AI could not fetch {requested_url}")
        status = result.redirected_status_code or result.status_code
        final_url = result.redirected_url or result.url or requested_url
        await guard_url(final_url)
        headers = {str(key).lower(): str(value) for key, value in (result.response_headers or {}).items()}
        if headers.get("cf-mitigated") == "challenge":
            host = urlparse(final_url).hostname or final_url
            raise BrowserRequiredError(f"{host} only serves browsers (bot check): {requested_url}")
        if status == 401:
            raise AuthenticationRequiredError(f"authentication required for {requested_url}")
        if status == 403:
            raise AccessDeniedError(f"access denied for {requested_url}")
        if status == 404:
            raise NotFoundError(f"not found: {requested_url}")
        if status == 429:
            raise RateLimitedError(f"rate limited: {requested_url}")
        if status is not None and status >= 400:
            raise BibVaultError(f"HTTP {status} while fetching {requested_url}")
        declared = headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > self.settings.download.max_bytes:
            raise DownloadTooLargeError(
                f"response exceeds {self.settings.download.max_bytes} bytes"
            )
        html = result.html or ""
        payload = html.encode("utf-8")
        if len(payload) > self.settings.download.max_bytes:
            raise DownloadTooLargeError(
                f"response exceeds {self.settings.download.max_bytes} bytes"
            )
        lowered = payload[:8192].lower()
        if (
            b"verify that you're not a robot" in lowered
            and b"this requires javascript" in lowered
        ):
            host = urlparse(final_url).hostname or final_url
            raise BrowserRequiredError(
                f"{host} only serves browsers (bot check): {requested_url}"
            )
        _assert_html(final_url, payload, headers.get("content-type", ""))
        return WebPage(final_url=final_url, html=html)


def html_to_markdown(html: str | bytes, url: str) -> str | None:
    """Return Trafilatura's Markdown for ``html``, or None when it finds no main text."""

    sample = html.encode("utf-8") if isinstance(html, str) else html
    lowered = sample[:8192].lower()
    if (
        b"<title>javascript is disabled</title>" in lowered
        or (
            b"verify that you're not a robot" in lowered
            and b"this requires javascript" in lowered
        )
    ):
        return None
    text = extract(
        html,
        url=url,
        output_format="markdown",
        favor_recall=True,
        include_images=True,
        include_links=True,
        include_tables=True,
        with_metadata=True,
    )
    if not isinstance(text, str) or not text.strip():
        return None
    if not text.endswith("\n"):
        text += "\n"
    return text
