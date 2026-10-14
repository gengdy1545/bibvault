"""HTTP downloads with scheme checks, size limits, and retries."""

import asyncio
import ipaddress
import logging
import socket
import tempfile
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlparse

import httpx

from bibvault.config import Settings
from bibvault.errors import (
    AccessDeniedError,
    AuthenticationRequiredError,
    BrowserRequiredError,
    DownloadTooLargeError,
    NotFoundError,
    NotHtmlError,
    NotPdfError,
    RateLimitedError,
    UnsafeURLError,
)
from bibvault.pdfcheck import assert_pdf_file

logger = logging.getLogger(__name__)

USER_AGENT = "bibvault/0.1"
_BLOCKED_HOSTS = {"localhost", "localhost.localdomain"}


class HostLimiter:
    def __init__(self, min_interval: float) -> None:
        self.min_interval = min_interval
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._next: dict[str, float] = {}

    async def wait(self, host: str) -> None:
        if self.min_interval <= 0:
            return
        async with self._locks[host]:
            loop = asyncio.get_running_loop()
            now = loop.time()
            scheduled = self._next.get(host, 0.0)
            if now < scheduled:
                await asyncio.sleep(scheduled - now)
            self._next[host] = loop.time() + self.min_interval


async def _resolve(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


def is_public_http_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    host = parsed.hostname.lower().rstrip(".")
    if host in _BLOCKED_HOSTS or host.endswith(".localhost") or host.endswith(".local"):
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return True
    return is_public_address(address)


def is_public_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


async def guard_request(request: httpx.Request) -> None:
    await guard_url(str(request.url))


async def guard_url(url: str) -> None:
    """Reject non-HTTP and private-network targets before a request is made."""

    if not is_public_http_url(url):
        raise UnsafeURLError(f"refusing URL: {url}")
    parsed = urlparse(url)
    assert parsed.hostname is not None
    host = parsed.hostname
    try:
        ipaddress.ip_address(host)
        return
    except ValueError:
        pass
    try:
        addresses = await _resolve(
            host,
            parsed.port or (443 if parsed.scheme == "https" else 80),
        )
    except OSError:
        return  # the connection attempt reports the DNS failure
    for value in addresses:
        if not is_public_address(ipaddress.ip_address(value.split("%", 1)[0])):
            raise UnsafeURLError(f"refusing URL: {host} resolves to local address {value}")


def make_client(settings: Settings) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(settings.download.timeout_seconds),
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT},
        event_hooks={"request": [guard_request]},
    )


async def fetch_valid_pdf(
    client: httpx.AsyncClient,
    url: str,
    partial: Path,
    settings: Settings,
    limiter: HostLimiter,
) -> None:
    """Download ``url`` into ``partial`` and leave a valid PDF there.

    A transport failure keeps the partial file so a later run can replace it.
    An invalid payload is deleted because retrying those bytes will not help.
    """

    await _fetch_to(client, url, partial, settings, limiter, for_pdf=True)
    try:
        assert_pdf_file(partial)
    except (AuthenticationRequiredError, NotPdfError):
        partial.unlink(missing_ok=True)
        raise


async def fetch_html(
    client: httpx.AsyncClient,
    url: str,
    settings: Settings,
    limiter: HostLimiter,
) -> tuple[str, bytes]:
    """Download ``url`` and return ``(final_url, html_bytes)``."""

    with tempfile.NamedTemporaryFile(prefix="bibvault-", delete=False) as handle:
        partial = Path(handle.name)
    try:
        final_url, content_type = await _fetch_to(client, url, partial, settings, limiter, for_pdf=False)
        data = partial.read_bytes()
    finally:
        partial.unlink(missing_ok=True)
    _assert_html(final_url, data, content_type)
    return final_url, data


async def _fetch_to(
    client: httpx.AsyncClient,
    url: str,
    partial: Path,
    settings: Settings,
    limiter: HostLimiter,
    *,
    for_pdf: bool,
) -> tuple[str, str]:
    attempts = settings.download.max_retries + 1
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            return await _download_once(client, url, partial, settings, limiter, for_pdf=for_pdf)
        except (
            AuthenticationRequiredError,
            UnsafeURLError,
            DownloadTooLargeError,
            AccessDeniedError,
            BrowserRequiredError,
            NotFoundError,
        ):
            partial.unlink(missing_ok=True)
            raise
        except RateLimitedError as exc:
            last_error = exc
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last_error = exc
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            partial.unlink(missing_ok=True)
            if status == 401:
                raise AuthenticationRequiredError(f"authentication required for {url}") from exc
            if status == 403:
                raise AccessDeniedError(f"access denied for {url}") from exc
            if status == 404:
                raise NotFoundError(f"not found: {url}") from exc
            if status == 429:
                last_error = RateLimitedError(f"rate limited: {url}")
            elif status in {500, 502, 503, 504}:
                last_error = exc
            else:
                raise
        if attempt + 1 < attempts:
            logger.info(
                "retrying %s (%s/%s)",
                urlparse(url).hostname or url,
                attempt + 1,
                attempts - 1,
            )
            await asyncio.sleep(min(2**attempt, 8))
    if isinstance(last_error, RateLimitedError):
        raise last_error
    message = str(last_error) if last_error else "download failed"
    raise httpx.TransportError(message)


async def _download_once(
    client: httpx.AsyncClient,
    url: str,
    partial: Path,
    settings: Settings,
    limiter: HostLimiter,
    *,
    for_pdf: bool,
) -> tuple[str, str]:
    if not is_public_http_url(url):
        raise UnsafeURLError(f"refusing URL: {url}")
    await limiter.wait(urlparse(url).hostname or "")
    partial.parent.mkdir(parents=True, exist_ok=True)
    async with client.stream("GET", url) as response:
        if not is_public_http_url(str(response.url)):
            raise UnsafeURLError(f"refusing redirect to {response.url}")
        # Cloudflare's bot check. Passing it takes a real browser; BibVault does not imitate one.
        if response.headers.get("cf-mitigated") == "challenge":
            message = f"{response.url.host} only serves browsers (bot check): {url}"
            if for_pdf:
                message += "; download it in your browser and run `bibvault import`"
            raise BrowserRequiredError(message)
        if response.status_code == 429:
            raise RateLimitedError(f"rate limited: {url}")
        response.raise_for_status()
        declared = response.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > settings.download.max_bytes:
            raise DownloadTooLargeError(f"response exceeds {settings.download.max_bytes} bytes")
        content_type = response.headers.get("content-type", "")
        final_url = str(response.url)
        size = 0
        with partial.open("wb") as handle:
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > settings.download.max_bytes:
                    raise DownloadTooLargeError(
                        f"response exceeds {settings.download.max_bytes} bytes"
                    )
                handle.write(chunk)
    return final_url, content_type


def _assert_html(url: str, data: bytes, content_type: str) -> None:
    if data.lstrip().startswith(b"%PDF-"):
        raise NotHtmlError(f"{url} returned a PDF, not an HTML page")
    media = content_type.split(";", 1)[0].strip().lower()
    if media in {"text/html", "application/xhtml+xml"}:
        return
    head = data[:2048].lstrip().lower()
    if head.startswith((b"<!doctype html", b"<html", b"<head", b"<body", b"<article")):
        return
    label = media or "an unknown type"
    raise NotHtmlError(f"{url} returned {label}, not HTML")
