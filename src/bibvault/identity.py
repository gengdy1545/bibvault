"""Persistent publication identity.

A citation key is an alias. Identity prefers a DOI, then an arXiv id, then a
normalized URL, then a bibliographic fingerprint. The fingerprint is only an
internal id for records that have no external identifier.
"""

import hashlib
import re
import unicodedata
from urllib.parse import unquote, urlsplit, urlunsplit

from bibvault.models import Reference

_DOI_PREFIX = re.compile(r"^(https?://(dx\.)?doi\.org/|doi:)", re.IGNORECASE)
_DOI_URL = re.compile(r"^https?://(?:dx\.)?doi\.org/(10\.\S+)$", re.IGNORECASE)
_ARXIV_ID = r"(\d{4}\.\d{4,5}|[a-z][a-z-]*(?:\.[a-z]{2})?/\d{7})(?:v\d+)?"
_ARXIV_DOI = re.compile(rf"^10\.48550/arxiv\.{_ARXIV_ID}$", re.IGNORECASE)
_ARXIV_URL = re.compile(rf"arxiv\.org/(?:abs|pdf|html)/{_ARXIV_ID}", re.IGNORECASE)
_ARXIV_EPRINT = re.compile(rf"^(?:arxiv:)?{_ARXIV_ID}$", re.IGNORECASE)


def normalize_doi(value: str | None) -> str | None:
    if not value:
        return None
    text = _DOI_PREFIX.sub("", value.strip()).strip().rstrip(".")
    return text.lower() or None


def doi_from_url(url: str | None) -> str | None:
    match = _DOI_URL.match(url.strip()) if url else None
    return normalize_doi(unquote(match.group(1))) if match else None


def extract_arxiv_id(
    *,
    doi: str | None = None,
    url: str | None = None,
    eprint: str | None = None,
    archive_prefix: str | None = None,
) -> str | None:
    if doi:
        match = _ARXIV_DOI.match(doi)
        if match:
            return match.group(1)
    if url:
        match = _ARXIV_URL.search(url)
        if match:
            return match.group(1)
    if eprint and (archive_prefix or "arxiv").lower() == "arxiv":
        match = _ARXIV_EPRINT.match(eprint.strip())
        if match:
            return match.group(1)
    return None


def reference_id(reference: Reference) -> str:
    # An arXiv DOI and a bare arXiv id name the same preprint.
    if reference.doi and not _ARXIV_DOI.match(reference.doi):
        return f"doi:{reference.doi}"
    if reference.arxiv_id:
        return f"arxiv:{reference.arxiv_id.lower()}"
    if reference.doi:
        return f"doi:{reference.doi}"
    if reference.url:
        return f"url:{_digest(normalize_url(reference.url))}"
    return f"internal:{_digest(_fingerprint(reference))}"


def legacy_internal_id(reference: Reference) -> str:
    """Return the internal id produced before Unicode fingerprints."""

    return f"internal:{_digest(_fingerprint(reference, legacy=True))}"


def internal_identity_warning(reference: Reference) -> str | None:
    """Explain when an internal id lacks enough metadata to be safely distinctive."""

    if reference.doi or reference.arxiv_id or reference.url:
        return None
    title = _tokens(reference.title)
    year = _tokens(reference.year)
    authors = _tokens(" ".join(reference.authors))
    if title and (year or authors):
        return None
    return (
        f"citation key '{reference.citation_key}' has a low-information internal identity "
        "that may collide; add a title plus year or author, or an external identifier"
    )


def normalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    netloc = host
    if parts.port:
        netloc = f"{host}:{parts.port}"
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), netloc, path, parts.query, ""))


def _fingerprint(reference: Reference, *, legacy: bool = False) -> str:
    tokenize = _legacy_tokens if legacy else _tokens
    title = tokenize(reference.title)
    year = (
        (reference.year or "").strip().lower()
        if legacy
        else tokenize(reference.year)
    )
    authors = tokenize(" ".join(reference.authors))
    return f"{title}|{year}|{authors}"


def _tokens(value: str | None) -> str:
    normalized = unicodedata.normalize("NFKC", value or "").casefold()
    tokens: list[str] = []
    current: list[str] = []
    for character in normalized:
        if unicodedata.category(character)[0] in {"L", "M", "N"}:
            current.append(character)
        elif current:
            tokens.append("".join(current))
            current = []
    if current:
        tokens.append("".join(current))
    return " ".join(tokens)


def _legacy_tokens(value: str | None) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", (value or "").lower()))


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:20]
