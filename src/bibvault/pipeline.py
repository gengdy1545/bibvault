"""Acquire PDFs and web pages, and keep citation-key mappings aligned with BibTeX."""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import sys
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import TextIO
from urllib.parse import urlparse

import httpx

from bibvault.browser import (
    BROWSER_UA,
    PUBLISHER_HOSTS,
    BrowserDownloads,
    auth_group,
    browser_link,
    matches_title,
    opens_in_browser,
    publisher_label,
    sign_in_label,
    sign_in_url,
)
from bibvault.config import Settings, host_of, save_proxy_host
from bibvault.errors import AuthenticationRequiredError, BibVaultError, FileCollisionError
from bibvault.identity import reference_id
from bibvault.models import AcquireResult, Reference
from bibvault.network import HostLimiter, fetch_valid_pdf, guard_request, make_client
from bibvault.pdfcheck import assert_pdf_file
from bibvault.resolve import Resolution, is_pdf_url, resolve_candidates
from bibvault.storage import Library, sha256_file
from bibvault.web import WebPageFetcher, html_to_markdown

logger = logging.getLogger(__name__)

# Statuses listed under "Could not acquire" in the slim text report.
_PROBLEM_STATUSES = frozenset(
    {
        "authentication_required",
        "manual_action_required",
        "access_denied",
        "not_found",
        "no_open_access_found",
        "rate_limited",
        "failed",
    }
)


def reconcile(library: Library, references: list[Reference]) -> None:
    """Bind citation keys to identities already in the library before downloading."""

    active = {item.citation_key for item in references}
    referenced = {reference_id(item) for item in references}
    # Free every name first so swapped or rotated keys can take each other's files.
    for reference in references:
        current = library.get_key(reference.citation_key)
        if current is not None and current.reference_id != reference_id(reference):
            library.release_key(current)
    for reference in references:
        ref_id = reference_id(reference)
        library.upsert_publication(reference, ref_id)
        existing = library.keys_for(ref_id)
        current = next((item for item in existing if item.citation_key == reference.citation_key), None)
        retired = [item for item in existing if item.citation_key not in active]
        if current is not None:
            library.upsert_key(reference, ref_id, current.filename_stem, current.status)
            library.ensure_alias_links(ref_id, current.filename_stem)
            continue
        if existing and library.files_for(ref_id) and retired and len(retired) == len(existing):
            old = retired[0]
            new_stem = library.allocate_stem(reference.citation_key, ref_id)
            library.rename_files(ref_id, old.filename_stem, new_stem)
            for item in retired:
                library.release_key(item)
            status = "already_exists" if library.preferred_present(ref_id) else "resolved"
            library.upsert_key(reference, ref_id, new_stem, status)
            continue
        stem = library.allocate_stem(reference.citation_key, ref_id)
        status = "already_exists" if library.preferred_present(ref_id) else "resolved"
        library.upsert_key(reference, ref_id, stem, status)
        if library.files_for(ref_id):
            library.ensure_alias_links(ref_id, stem)

    for key in library.all_keys():
        if key.citation_key in active:
            continue
        if key.reference_id in referenced:
            library.release_key(key)


async def run_fetch(
    library: Library,
    references: list[Reference],
    settings: Settings,
    *,
    only_keys: list[str] | None = None,
    browser: bool = False,
    open_url: Callable[[str], None] | None = None,
    on_status: Callable[[str], None] | None = None,
    on_progress: Callable[[int, int, str], None] | None = None,
    confirm: Callable[[str], str] | None = None,
) -> list[AcquireResult]:
    selected = references
    if only_keys:
        wanted = set(only_keys)
        selected = [item for item in references if item.citation_key in wanted]
        missing = sorted(wanted - {item.citation_key for item in selected})
        if missing:
            joined = ", ".join(missing)
            raise KeyError(f"citation key not in BibTeX: {joined}")
    reconcile(library, references)

    order: list[str] = []
    for item in selected:
        ref_id = reference_id(item)
        if ref_id not in order:
            order.append(ref_id)
    present = {ref_id for ref_id in order if library.preferred_present(ref_id)}
    progress = _RunProgress(len(order), len(present), on_progress)
    progress.pending = {ref_id for ref_id in order if ref_id not in present}
    progress.show()

    memory: dict[str, object] = {}
    if on_status is not None:
        memory["notify"] = on_status
    locks = {reference_id(item): asyncio.Lock() for item in selected}
    limiter = HostLimiter(settings.download.min_interval_seconds)
    semaphore = asyncio.Semaphore(settings.download.concurrency)
    # Papers whose preprint waited for the browser to try the publisher's PDF first.
    deferred: set[str] = set()

    async with make_client(settings) as client:
        web_fetcher = WebPageFetcher(settings, limiter, client)

        async def bounded(reference: Reference) -> AcquireResult:
            ref_id = reference_id(reference)
            defer = deferred if browser and opens_in_browser(reference, settings) else None
            async with semaphore:
                async with locks[ref_id]:
                    result = await _acquire_or_fail(
                        reference,
                        library,
                        settings,
                        client,
                        limiter,
                        memory,
                        web_fetcher,
                        render_web=browser,
                        defer=defer,
                    )
            if not (
                browser
                and opens_in_browser(reference, settings)
                and result.status not in {"downloaded", "already_exists"}
            ):
                await progress.finish(ref_id)
            return result

        try:
            results = list(await asyncio.gather(*[bounded(item) for item in selected]))
        finally:
            await web_fetcher.close()
    if browser:
        tried = await _browser_pass(
            library,
            selected,
            results,
            settings,
            open_url,
            progress,
            deferred,
            on_status=on_status,
            confirm=confirm,
        )
        fallback = [
            index
            for index in tried
            if reference_id(selected[index]) in deferred
            and results[index].status not in {"downloaded", "already_exists"}
        ]
        fallback_ids = {reference_id(selected[index]) for index in fallback}
        for ref_id in list(progress.pending):
            if ref_id not in fallback_ids:
                await progress.finish(ref_id)
        if fallback:
            if on_status:
                on_status(f"No publisher PDF for {len(fallback)} paper(s); taking the preprint instead.")
            async with make_client(settings) as client:
                for index in fallback:
                    results[index] = await _acquire_or_fail(
                        selected[index],
                        library,
                        settings,
                        client,
                        limiter,
                        memory,
                        None,
                    )
                    await progress.finish(reference_id(selected[index]))
    library.export_manifest()
    return results


async def _acquire_or_fail(
    reference: Reference,
    library: Library,
    settings: Settings,
    client: httpx.AsyncClient,
    limiter: HostLimiter,
    memory: dict[str, object],
    web_fetcher: WebPageFetcher | None,
    *,
    render_web: bool = False,
    defer: set[str] | None = None,
) -> AcquireResult:
    try:
        return await acquire_one(
            reference,
            library,
            settings,
            client,
            limiter,
            memory,
            web_fetcher,
            render_web=render_web,
            defer=defer,
        )
    except Exception as exc:
        logger.exception("acquisition failed for %s", reference.citation_key)
        library.set_status(
            reference.citation_key,
            "failed",
            error_category="failed",
            error_detail=str(exc),
        )
        return _result(reference, reference_id(reference), "failed", error=str(exc))


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


async def acquire_one(
    reference: Reference,
    library: Library,
    settings: Settings,
    client: httpx.AsyncClient,
    limiter: HostLimiter,
    memory: dict[str, object],
    web_fetcher: WebPageFetcher | None,
    *,
    render_web: bool = False,
    defer: set[str] | None = None,
) -> AcquireResult:
    """Store the best PDF of one paper, or the Markdown of one web page.

    With ``defer``, only a published (or the entry's own) PDF is taken; a paper
    whose other versions were passed over is added to ``defer``.
    """

    ref_id = reference_id(reference)
    library.upsert_publication(reference, ref_id)
    stem = library.allocate_stem(reference.citation_key, ref_id)
    current = library.get_key(reference.citation_key)
    status = current.status if current and current.reference_id == ref_id else "resolved"
    library.upsert_key(reference, ref_id, stem, status)
    library.prune_missing(ref_id)

    # Once the preferred file is on disk, no other version is fetched for it.
    preferred = library.preferred_file(ref_id)
    if preferred is not None:
        library.ensure_alias_links(ref_id, stem)
        library.set_status(reference.citation_key, "already_exists")
        return _result(
            reference,
            ref_id,
            "already_exists",
            path=_preferred_path(library, ref_id, stem),
            provider=preferred.provider,
            version=preferred.version,
        )

    if _web_only(reference):
        assert web_fetcher is not None
        return await _acquire_web(
            reference,
            library,
            web_fetcher,
            ref_id,
            stem,
            render=render_web,
        )

    resolution = await resolve_candidates(reference, client, library, settings, memory)
    stored_new = False
    problems: dict[str, str] = {}

    # Candidates come best version first, so the first one that downloads is the one kept.
    for candidate in resolution.candidates:
        if defer is not None and candidate.version not in {"published", "provided"}:
            defer.add(ref_id)
            continue
        destination = library.path_for(stem, "pdf")
        partial = destination.with_name(destination.name + ".partial")
        try:
            await fetch_valid_pdf(client, candidate.url, partial, settings, limiter)
            library.commit_download(
                reference_id=ref_id,
                stem=stem,
                role="pdf",
                version=candidate.version,
                partial=partial,
                source_url=candidate.url,
                provider=candidate.provider,
            )
            stored_new = True
            break
        except BibVaultError as exc:
            problems[exc.category] = exc.message
        except httpx.HTTPError as exc:
            problems["failed"] = str(exc) or exc.__class__.__name__

    for key in library.keys_for(ref_id):
        library.ensure_alias_links(ref_id, key.filename_stem)

    if not stored_new and defer is not None and ref_id in defer:
        problems["manual_action_required"] = (
            "the preprint waits until the browser has tried the publisher's PDF; run again with --browser"
        )
    if resolution.rate_limited and resolution.error:
        problems.setdefault("rate_limited", resolution.error)
    final, error = _final_status(
        stored_new=stored_new,
        present=library.preferred_present(ref_id),
        problems=problems,
        resolution=resolution,
    )
    category = None if final in {"downloaded", "already_exists"} else final
    library.set_status(reference.citation_key, final, error_category=category, error_detail=error)
    preferred = library.get_file(ref_id, "pdf")
    return _result(
        reference,
        ref_id,
        final,
        path=_preferred_path(library, ref_id, stem),
        provider=preferred.provider if preferred else None,
        version=preferred.version if preferred else None,
        error=error,
    )


def import_file(
    library: Library,
    reference: Reference,
    file_path: Path,
    *,
    ref_id: str | None = None,
    source_url: str | None = None,
    provider: str = "import",
) -> AcquireResult:
    suffix = file_path.suffix.lower()
    if suffix == ".pdf":
        role = "pdf"
        assert_pdf_file(file_path)
    elif suffix == ".md":
        role = "markdown"
        try:
            text = file_path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise BibVaultError(f"{file_path} is not UTF-8 Markdown") from exc
        if not text.strip():
            raise BibVaultError(f"{file_path} is empty")
    else:
        raise BibVaultError(f"cannot import {file_path.suffix or 'a file without an extension'}; expected .pdf or .md")

    incoming = sha256_file(file_path)
    ref_id = ref_id or reference_id(reference)
    library.upsert_publication(reference, ref_id)
    current = library.get_key(reference.citation_key)
    if current is not None and current.reference_id != ref_id:
        library.release_key(current)
    stem = library.allocate_stem(reference.citation_key, ref_id, accept_sha256=incoming)
    status = current.status if current and current.reference_id == ref_id else "resolved"
    library.upsert_key(reference, ref_id, stem, status)
    library.prune_missing(ref_id)
    existing = library.get_file(ref_id, role)
    if existing:
        located = library.resolve_file(stem, existing)
    else:
        located = None
    if existing and located is not None:
        if existing.sha256 == incoming:
            library.ensure_alias_links(ref_id, stem)
            library.set_status(reference.citation_key, "already_exists")
            library.export_manifest()
            return _result(
                reference,
                ref_id,
                "already_exists",
                path=library.relative(located),
                provider=existing.provider,
                version=existing.version,
            )
        raise FileCollisionError(
            f"{role} file for {reference.citation_key} already exists with different content"
        )
    saved = library.commit_bytes(
        reference_id=ref_id,
        stem=stem,
        role=role,
        version="provided",
        data=file_path.read_bytes(),
        source_url=source_url,
        provider=provider,
    )
    library.set_status(reference.citation_key, "downloaded")
    library.export_manifest()
    return _result(
        reference,
        ref_id,
        "downloaded",
        path=library.relative(saved),
        provider=provider,
        version="provided",
    )


def lookup_reference(library: Library, citation_key: str) -> dict | None:
    key = library.get_key(citation_key)
    if key is None:
        return None
    files: dict[str, dict] = {}
    for record in library.files_for(key.reference_id):
        located = library.resolve_file(key.filename_stem, record)
        files[record.role] = {
            "path": None if located is None else library.relative(located),
            "sha256": record.sha256,
            "source_url": record.source_url,
            "provider": record.provider,
            "retrieved_at": record.retrieved_at,
            "version": record.version,
            "byte_size": record.byte_size,
        }
    preferred = files.get("pdf") or files.get("markdown")
    return {
        "citation_key": key.citation_key,
        "reference_id": key.reference_id,
        "type": key.entry_type,
        "title": key.title,
        "status": key.status,
        "version": None if preferred is None else preferred["version"],
        "files": files,
        "error": key.error_detail,
    }


def summarize(results: list[AcquireResult]) -> dict[str, int]:
    counts = Counter(item.status for item in results)
    downloaded = counts["downloaded"]
    present = counts["already_exists"]
    return {
        "references": len(results),
        "available_locally": downloaded + present,
        "newly_downloaded": downloaded,
        "already_present": present,
        "authentication_required": counts["authentication_required"],
        "no_open_access_found": counts["no_open_access_found"],
        "access_denied": counts["access_denied"],
        "not_found": counts["not_found"],
        "manual_action_required": counts["manual_action_required"],
        "unsupported": counts["unsupported"],
        "rate_limited": counts["rate_limited"],
        "failed": counts["failed"],
    }


def format_report(results: list[AcquireResult]) -> str:
    """Slim text report: counts plus only the papers that still need attention."""

    summary = summarize(results)
    problems = [item for item in results if item.status in _PROBLEM_STATUSES]
    lines = [
        "BibVault Acquisition Report",
        "",
        f"Already on disk:   {summary['already_present']:4d}",
        f"Newly downloaded:  {summary['newly_downloaded']:4d}",
        f"Could not acquire: {len(problems):4d}",
    ]
    if summary["unsupported"]:
        lines.append(f"Unsupported:       {summary['unsupported']:4d}")
    if problems:
        lines.append("")
        lines.append("Could not acquire:")
        for item in problems:
            reason = item.error or item.status
            lines.append(f"  {item.citation_key} ({item.status}): {reason}")
    return "\n".join(lines)


def ascii_progress(done: int, total: int, label: str = "", width: int = 20) -> str:
    if total <= 0:
        return ""
    filled = min(width, int(width * done / total))
    bar = "#" * filled + "-" * (width - filled)
    prefix = f"{label} " if label else ""
    return f"{prefix}[{bar}] {done}/{total}"


class FetchDisplay:
    """Keep the progress line as the first line and print later messages under it."""

    def __init__(self, stream: TextIO | None = None) -> None:
        self.stream = sys.stderr if stream is None else stream
        self.lines_below = 0
        self.visible = False

    def progress(self, done: int, total: int, label: str = "") -> None:
        line = ascii_progress(done, total, label)
        if not line:
            return
        if not self.visible:
            self.stream.write(line + "\n")
            self.visible = True
            self.stream.flush()
            return
        up = self.lines_below + 1
        self.stream.write(f"\033[{up}A\r\033[K{line}\033[{up}B")
        self.stream.flush()

    def status(self, message: str) -> None:
        text = message.rstrip("\n")
        self.stream.write(text + "\n")
        self.lines_below += _terminal_rows(text)
        self.stream.flush()


def _terminal_rows(text: str) -> int:
    width = shutil.get_terminal_size(fallback=(80, 24)).columns or 80
    rows = 0
    for line in text.splitlines() or [""]:
        rows += max(1, (len(line) + width - 1) // width)
    return rows


class _RunProgress:
    """One step per publication in this run, including files already on disk."""

    def __init__(
        self,
        total: int,
        done: int,
        on_progress: Callable[[int, int, str], None] | None,
    ) -> None:
        self.total = total
        self.done = done
        self.pending: set[str] = set()
        self.on_progress = on_progress
        self._lock = asyncio.Lock()

    def show(self) -> None:
        self._emit()

    async def finish(self, ref_id: str) -> None:
        async with self._lock:
            if ref_id not in self.pending:
                return
            self.pending.remove(ref_id)
            self.done += 1
            self._emit()

    def _emit(self) -> None:
        if self.on_progress is not None and self.total:
            self.on_progress(self.done, self.total, "Fetching")


def _say(on_status: Callable[[str], None] | None, message: str) -> None:
    if on_status is not None:
        on_status(message)
    else:
        logger.warning("%s", message)


_WEB_TYPES = {"online", "misc", "software", "dataset", "electronic", "www", "webpage"}


def _web_only(reference: Reference) -> bool:
    if reference.entry_type not in _WEB_TYPES:
        return False
    if reference.doi or reference.arxiv_id:
        return False
    return not is_pdf_url(reference.url)


_PROBLEM_ORDER = (
    "authentication_required",
    "manual_action_required",
    "access_denied",
    "rate_limited",
    "failed",
    "not_found",
)


def _final_status(
    *,
    stored_new: bool,
    present: bool,
    problems: dict[str, str],
    resolution: Resolution,
) -> tuple[str, str | None]:
    if stored_new:
        return "downloaded", None
    if present:
        return "already_exists", None
    for status in _PROBLEM_ORDER:
        if status in problems:
            return status, problems[status]
    if resolution.error:
        return "failed", resolution.error
    if resolution.landing_url:
        return (
            "manual_action_required",
            f"open-access page has no direct PDF link: {resolution.landing_url}; "
            "download it and run `bibvault import`",
        )
    if resolution.found:
        return (
            "no_open_access_found",
            "OpenAlex lists no open-access copy; if you can get the PDF, run `bibvault import`",
        )
    return "not_found", "no matching publication or PDF found"


async def _acquire_web(
    reference: Reference,
    library: Library,
    fetcher: WebPageFetcher,
    ref_id: str,
    stem: str,
    *,
    render: bool,
) -> AcquireResult:
    """Save one web page as Markdown. Image links stay as Trafilatura wrote them."""

    url = reference.url
    if not url:
        detail = "no URL to archive"
        library.set_status(reference.citation_key, "not_found", error_category="not_found", error_detail=detail)
        return _result(reference, ref_id, "not_found", error=detail)
    try:
        rendered = False
        try:
            page = await fetcher.fetch_http(url)
        except AuthenticationRequiredError:
            raise
        except BibVaultError as exc:
            if not render or exc.category != "manual_action_required":
                raise
            page = await fetcher.fetch_browser(url)
            rendered = True
        markdown = await asyncio.to_thread(html_to_markdown, page.html, page.final_url)
        if markdown is None and render and not rendered:
            page = await fetcher.fetch_browser(url)
            markdown = await asyncio.to_thread(html_to_markdown, page.html, page.final_url)
    except BibVaultError as exc:
        library.set_status(
            reference.citation_key,
            exc.category,
            error_category=exc.category,
            error_detail=exc.message,
        )
        return _result(reference, ref_id, exc.category, error=exc.message)
    except httpx.HTTPError as exc:
        detail = str(exc) or exc.__class__.__name__
        library.set_status(reference.citation_key, "failed", error_category="failed", error_detail=detail)
        return _result(reference, ref_id, "failed", error=detail)
    if markdown is None:
        detail = f"no main text extracted from {page.final_url}"
        library.set_status(reference.citation_key, "failed", error_category="failed", error_detail=detail)
        return _result(reference, ref_id, "failed", error=detail)
    saved = library.commit_bytes(
        reference_id=ref_id,
        stem=stem,
        role="markdown",
        version="archived",
        data=markdown.encode("utf-8"),
        source_url=page.final_url,
        provider="crawl4ai+trafilatura",
    )
    for key in library.keys_for(ref_id):
        library.ensure_alias_links(ref_id, key.filename_stem)
    library.set_status(reference.citation_key, "downloaded")
    return _result(
        reference,
        ref_id,
        "downloaded",
        path=library.relative(saved),
        provider="crawl4ai+trafilatura",
        version="archived",
    )


def _preferred_path(library: Library, reference_id_value: str, stem: str) -> str | None:
    record = library.preferred_file(reference_id_value)
    if record is None:
        return None
    located = library.resolve_file(stem, record)
    return None if located is None else library.relative(located)


def _result(
    reference: Reference,
    ref_id: str,
    status: str,
    *,
    path: str | None = None,
    provider: str | None = None,
    version: str | None = None,
    error: str | None = None,
) -> AcquireResult:
    return AcquireResult(
        citation_key=reference.citation_key,
        title=reference.title,
        reference_id=ref_id,
        status=status,
        path=path,
        provider=provider,
        version=version,
        error=error,
    )
