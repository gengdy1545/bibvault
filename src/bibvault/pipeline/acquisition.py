"""Download one reference and import a file the user already has."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path

import httpx

from bibvault.browser import opens_in_browser
from bibvault.config import Settings
from bibvault.errors import AuthenticationRequiredError, BibVaultError, FileCollisionError
from bibvault.identity import internal_identity_warning, reference_id
from bibvault.models import AcquireResult, FileRecord, Reference
from bibvault.network import HostLimiter, fetch_valid_pdf, make_client
from bibvault.pdfcheck import assert_pdf_file
from bibvault.pipeline.browser_pass import _browser_pass
from bibvault.pipeline.reconcile import _migrate_legacy_internal_identities, reconcile
from bibvault.pipeline.report import _RunProgress, _say
from bibvault.pipeline.results import _final_status, _preferred_path, _result, _web_only
from bibvault.resolve import resolve_candidates, version_ranks
from bibvault.storage import Library, sha256_bytes, sha256_file
from bibvault.web import WebPageFetcher, html_to_markdown

logger = logging.getLogger(__name__)


async def run_fetch(
    library: Library,
    references: list[Reference],
    settings: Settings,
    *,
    only_keys: list[str] | None = None,
    browser: bool = False,
    upgrade: bool = False,
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
    identity_warnings = [
        warning
        for reference in references
        if (warning := internal_identity_warning(reference)) is not None
    ]
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
    for warning in identity_warnings:
        _say(on_status, f"warning: {warning}")

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
                        upgrade=upgrade,
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
    if browser and not upgrade:
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
    upgrade: bool = False,
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
            upgrade=upgrade,
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


async def _upgrade_pdf(
    reference: Reference,
    library: Library,
    settings: Settings,
    client: httpx.AsyncClient,
    limiter: HostLimiter,
    memory: dict[str, object],
    ref_id: str,
    stem: str,
    current: FileRecord,
) -> AcquireResult:
    """Download a strictly better PDF and replace the stored file only after it validates.

    A failed download or a file whose bytes no longer match the stored checksum
    leaves the original in place. The stored status is not downgraded; the
    returned result is ``failed`` so the command can exit non-zero.
    """

    located = library.resolve_file(stem, current)
    kept = _result(
        reference,
        ref_id,
        "failed",
        path=None if located is None else library.relative(located),
        provider=current.provider,
        version=current.version,
    )
    if located is None or sha256_file(located) != current.sha256:
        kept.error = "stored PDF was modified or is missing; left it in place"
        return kept

    resolution = await resolve_candidates(reference, client, library, settings, memory)
    ranks = version_ranks(settings)
    current_rank = ranks.get(current.version, 9)
    better = [
        candidate
        for candidate in resolution.candidates
        if ranks.get(candidate.version, 9) < current_rank
    ]
    if not better:
        library.set_status(reference.citation_key, "already_exists")
        return _result(
            reference,
            ref_id,
            "already_exists",
            path=library.relative(located),
            provider=current.provider,
            version=current.version,
        )

    problems: dict[str, str] = {}
    for candidate in better:
        partial = located.with_name(located.name + ".upgrade.partial")
        try:
            await fetch_valid_pdf(client, candidate.url, partial, settings, limiter)
            saved = library.replace_download(
                reference_id=ref_id,
                stem=stem,
                role="pdf",
                version=candidate.version,
                partial=partial,
                source_url=candidate.source_url or candidate.url,
                provider=candidate.provider,
                destination=located,
                previous_sha256=current.sha256,
            )
        except BibVaultError as exc:
            partial.unlink(missing_ok=True)
            problems[exc.category] = exc.message
            continue
        except httpx.HTTPError as exc:
            partial.unlink(missing_ok=True)
            problems["failed"] = str(exc) or exc.__class__.__name__
            continue
        library.set_status(reference.citation_key, "downloaded")
        return _result(
            reference,
            ref_id,
            "downloaded",
            path=library.relative(saved),
            provider=candidate.provider,
            version=candidate.version,
        )

    detail = next(iter(problems.values()), "no higher version could be downloaded")
    kept.error = f"{detail}; kept the stored file"
    return kept


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
    upgrade: bool = False,
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
    # ``upgrade`` is the explicit exception: a strictly better PDF may replace it.
    preferred = library.preferred_file(ref_id)
    if preferred is not None and upgrade and preferred.role == "pdf":
        return await _upgrade_pdf(
            reference,
            library,
            settings,
            client,
            limiter,
            memory,
            ref_id,
            stem,
            preferred,
        )
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
                source_url=candidate.source_url or candidate.url,
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
    payload: bytes | None = None
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
    elif suffix in {".html", ".htm"}:
        role = "markdown"
        markdown = html_to_markdown(file_path.read_bytes(), source_url or "")
        if markdown is None:
            raise BibVaultError(f"no main text extracted from {file_path}")
        payload = markdown.encode("utf-8")
        if provider == "import":
            provider = "trafilatura"
    else:
        raise BibVaultError(
            f"cannot import {file_path.suffix or 'a file without an extension'}; expected .pdf, .html, or .md"
        )

    incoming = sha256_bytes(payload) if payload is not None else sha256_file(file_path)
    if ref_id is None:
        if warning := internal_identity_warning(reference):
            logger.warning("%s", warning)
        _migrate_legacy_internal_identities(library, [reference])
        ref_id = reference_id(reference)
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
        data=payload if payload is not None else file_path.read_bytes(),
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
