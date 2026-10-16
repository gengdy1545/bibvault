"""Second pass that hands publisher links to the user's browser."""

from __future__ import annotations

import re
import shutil
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlparse

import httpx

from bibvault.browser import (
    BROWSER_UA,
    PUBLISHER_HOSTS,
    BrowserDownloads,
    auth_group,
    browser_link,
    matches_title,
    publisher_label,
    sign_in_label,
    sign_in_url,
)
from bibvault.config import Settings, host_of, save_proxy_host
from bibvault.errors import AuthenticationRequiredError, BibVaultError
from bibvault.identity import reference_id
from bibvault.models import AcquireResult, Reference
from bibvault.network import HostLimiter, fetch_valid_pdf, guard_request, make_client
from bibvault.pdfcheck import assert_pdf_file
from bibvault.pipeline.report import _RunProgress, _say
from bibvault.pipeline.results import _preferred_path, _result
from bibvault.storage import Library


async def _browser_pass(
    library: Library,
    references: list[Reference],
    results: list[AcquireResult],
    settings: Settings,
    open_url: Callable[[str], None] | None,
    progress: _RunProgress,
    deferred: set[str],
    *,
    on_status: Callable[[str], None] | None = None,
    confirm: Callable[[str], str] | None = None,
) -> list[int]:
    """Retry, one at a time and slowly, what only a real browser can download.

    Returns the indices of the papers it tried and may fall back to a preprint for.
    """

    missing = [
        (index, reference)
        for index, (reference, result) in enumerate(zip(references, results, strict=True))
        if result.status not in {"downloaded", "already_exists"}
    ]
    async with make_client(settings) as client:
        pending = [
            (index, reference, link)
            for index, reference in missing
            if (link := await browser_link(reference, settings, client, library))
        ]
    if not pending:
        return []
    downloads = BrowserDownloads(settings, open_url)
    if not downloads.available():
        _say(on_status, "no Chromium-family browser found; set [browser] executable in the config")
        return []
    downloads.prepare()
    if len(pending) > settings.browser.max_per_run:
        _say(
            on_status,
            f"{len(pending) - settings.browser.max_per_run} papers are left for the next run "
            f"with --browser ([browser] max_per_run = {settings.browser.max_per_run})",
        )
    queue = pending[: settings.browser.max_per_run]
    signed_in: set[str] = set()
    pasted_hosts: dict[str, str] = {}
    # Papers in a row for which the browser saved nothing.
    silent: list[int] = []
    library_cookies: httpx.Cookies | None = None
    library_http = False
    http_limiter = HostLimiter(settings.browser.interval_seconds)
    tried: list[int] = []
    # A login page after sign-in means that publisher's session did not stick.
    failed_auth: set[str] = set()
    session_failed: set[int] = set()
    for index, reference, (provider, url) in queue:
        if len(silent) == 2:
            _say(
                on_status,
                "the browser saved nothing twice in a row; finish any login or check in its window and run again",
            )
            break
        if provider in failed_auth:
            await _settle_browser_item(
                library,
                results,
                index,
                reference,
                progress,
                deferred,
                "authentication_required",
                f"{publisher_label(provider)} asked to sign in again, so this paper was not tried",
            )
            continue
        tried.append(index)
        group = auth_group(provider)
        if group not in signed_in:
            portal = sign_in_url(group, settings)
            if portal:
                if on_status:
                    on_status(f"Open the BibVault browser window and sign in for {sign_in_label(group)}.")
                downloads.open(portal)
                if confirm and group == "library":
                    pasted_hosts = _ask_proxy_hosts(queue, settings, confirm, on_status)
                    confirm(
                        "Close the BibVault browser window, then press Enter to download silently."
                    )
                elif confirm:
                    confirm("Press Enter when sign-in (or the bot check) is done.")
                if group == "library":
                    library_cookies = downloads.profile_cookies()
                    library_http = any(True for _ in library_cookies.jar)
                    if on_status:
                        if library_http:
                            on_status(f"Signed in. Downloading {len(queue)} paper(s)…")
                        else:
                            on_status(
                                f"Signed in. Opening {len(queue)} paper(s) in the browser…"
                            )
                elif on_status:
                    on_status(f"Signed in. Opening {len(queue)} paper(s) in the browser…")
            signed_in.add(group)
        if provider in pasted_hosts:
            url = urlparse(url)._replace(netloc=pasted_hosts[provider]).geturl()
        ref_id = reference_id(reference)
        if library.preferred_present(ref_id):
            status, error = "already_exists", None
        elif group == "library" and urlparse(url).hostname == PUBLISHER_HOSTS[provider]:
            # Off the proxy the publisher only shows its paywall.
            status = "manual_action_required"
            error = f"paste {publisher_label(provider)}'s address from the library portal when `--browser` asks"
        else:
            use_http = group == "library" and library_http
            status, error, empty = await _acquire_browser_item(
                library,
                reference,
                ref_id,
                url,
                provider,
                downloads,
                settings,
                cookies=library_cookies if use_http else None,
                limiter=http_limiter if use_http else None,
            )
            if use_http and status == "authentication_required":
                # The sign-in step already happened. Opening each link and waiting
                # out the browser timeout will not repair a session that returned a login page.
                failed_auth.add(provider)
                session_failed.add(index)
                _say(
                    on_status,
                    f"{publisher_label(provider)} asked to sign in again; skipping its remaining papers.",
                )
            silent = [*silent, index] if empty else []
        await _settle_browser_item(
            library, results, index, reference, progress, deferred, status, error
        )
    # A failed sign-in is the session, not the paper: leave those for the next run instead of taking a preprint.
    reached = [index for index in tried if index not in session_failed]
    if len(silent) >= 2:
        # Two links in a row that saved nothing point at the window, not at those papers: try them again next run.
        return [index for index in reached if index not in silent]
    return reached


async def _settle_browser_item(
    library: Library,
    results: list[AcquireResult],
    index: int,
    reference: Reference,
    progress: _RunProgress,
    deferred: set[str],
    status: str,
    error: str | None,
) -> None:
    ref_id = reference_id(reference)
    key = library.get_key(reference.citation_key)
    stem = key.filename_stem if key else reference.citation_key
    library.ensure_alias_links(ref_id, stem)
    library.set_status(
        reference.citation_key,
        status,
        error_category=None if status in {"downloaded", "already_exists"} else status,
        error_detail=error,
    )
    preferred = library.get_file(ref_id, "pdf")
    results[index] = _result(
        reference,
        ref_id,
        status,
        path=_preferred_path(library, ref_id, stem),
        provider=preferred.provider if preferred else None,
        version=preferred.version if preferred else None,
        error=error,
    )
    if status in {"downloaded", "already_exists"} or ref_id not in deferred:
        await progress.finish(ref_id)


def _ask_proxy_hosts(
    queue: list[tuple[int, Reference, tuple[str, str]]],
    settings: Settings,
    confirm: Callable[[str], str],
    on_status: Callable[[str], None] | None,
) -> dict[str, str]:
    """The proxy host the user pastes for each library publisher; the proxy changes them over time.

    A new host is saved to the user's config, so Enter keeps it next time.
    """

    configured = settings.browser.proxy_hosts
    hosts: dict[str, str] = {}
    for provider in sorted({provider for _, _, (provider, _) in queue if auth_group(provider) == "library"}):
        label, publisher = publisher_label(provider), PUBLISHER_HOSTS[provider]
        enter = "Enter keeps the configured one" if publisher in configured else f"Enter skips {label} this run"
        answer = confirm(f"Open {label} from the library portal in that window, then paste its address here ({enter}).")
        host = host_of(answer)
        if not host:
            continue
        if not re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)+", host):
            if on_status:
                on_status(f"Not an address: {answer.strip()}")
            continue
        hosts[provider] = host
        if host != configured.get(publisher):
            path = save_proxy_host(publisher, host)
            if on_status:
                on_status(f"Saved {label}'s address to {path}.")
    return hosts


async def _acquire_browser_item(
    library: Library,
    reference: Reference,
    ref_id: str,
    url: str,
    provider: str,
    downloads: BrowserDownloads,
    settings: Settings,
    *,
    cookies: httpx.Cookies | None,
    limiter: HostLimiter | None,
) -> tuple[str, str | None, bool]:
    """Return status, error, and whether the browser saved nothing (for the stop streak)."""

    if cookies is not None and limiter is not None:
        status, error = await _store_http_file(
            library, reference, ref_id, url, provider, settings, cookies, limiter, downloads.inbox
        )
        return status, error, False
    saved = await downloads.download(url, background=True)
    return (*_store_browser_file(library, reference, ref_id, url, saved, provider), saved is None)


async def _store_http_file(
    library: Library,
    reference: Reference,
    ref_id: str,
    url: str,
    provider: str,
    settings: Settings,
    cookies: httpx.Cookies,
    limiter: HostLimiter,
    inbox: Path,
) -> tuple[str, str | None]:
    key = library.get_key(reference.citation_key)
    stem = key.filename_stem if key else library.allocate_stem(reference.citation_key, ref_id)
    destination = library.path_for(stem, "pdf")
    partial = destination.with_name(destination.name + ".partial")
    try:
        async with httpx.AsyncClient(
            cookies=cookies,
            timeout=httpx.Timeout(settings.browser.timeout_seconds),
            follow_redirects=True,
            headers={"User-Agent": BROWSER_UA},
            event_hooks={"request": [guard_request]},
        ) as client:
            await fetch_valid_pdf(client, url, partial, settings, limiter)
    except AuthenticationRequiredError as exc:
        partial.unlink(missing_ok=True)
        return exc.category, exc.message
    except BibVaultError as exc:
        partial.unlink(missing_ok=True)
        return exc.category, f"silent download failed for {url}: {exc.message}"
    except httpx.HTTPError as exc:
        partial.unlink(missing_ok=True)
        return "failed", f"silent download failed for {url}: {exc}"
    if not matches_title(partial, reference.title):
        inbox.mkdir(parents=True, exist_ok=True)
        kept = inbox / f"{reference.citation_key}.pdf"
        shutil.move(partial, kept)
        return "manual_action_required", f"{kept} does not look like this paper; left it there for you to check"
    library.commit_download(
        reference_id=ref_id,
        stem=stem,
        role="pdf",
        version="published",
        partial=partial,
        source_url=url,
        provider=provider,
    )
    return "downloaded", None


def _store_browser_file(
    library: Library,
    reference: Reference,
    ref_id: str,
    url: str,
    saved: Path | None,
    provider: str,
) -> tuple[str, str | None]:
    if saved is None:
        return (
            "manual_action_required",
            f"the browser did not save a PDF from {url}; check the BibVault browser window",
        )
    try:
        assert_pdf_file(saved)
    except BibVaultError as exc:
        saved.unlink(missing_ok=True)
        return exc.category, f"the browser saved something that is not a PDF from {url}: {exc.message}"
    if not matches_title(saved, reference.title):
        return "manual_action_required", f"{saved} does not look like this paper; left it there for you to check"
    key = library.get_key(reference.citation_key)
    stem = key.filename_stem if key else library.allocate_stem(reference.citation_key, ref_id)
    destination = library.path_for(stem, "pdf")
    partial = destination.with_name(destination.name + ".partial")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(saved, partial)
    library.commit_download(
        reference_id=ref_id,
        stem=stem,
        role="pdf",
        version="published",
        partial=partial,
        source_url=url,
        provider=provider,
    )
    saved.unlink()
    return "downloaded", None
