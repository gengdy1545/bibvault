"""PDF validation.

HTML login pages and other non-PDF payloads are rejected before a download is
recorded. PyMuPDF must be able to open the file and see at least one page.
"""

from pathlib import Path

import pymupdf

from bibvault.errors import AuthenticationRequiredError, NotPdfError

_LOGIN_MARKERS = (
    b"sign in",
    b"log in",
    b"login",
    b"password",
    b"shibboleth",
    b"openathens",
    b"institution",
)


def assert_pdf_file(path: Path) -> None:
    data = path.read_bytes()
    assert_pdf_bytes(data)


def assert_pdf_bytes(data: bytes) -> None:
    if _looks_like_html(data):
        if _looks_like_login(data):
            raise AuthenticationRequiredError("publisher returned a login page instead of a PDF")
        raise NotPdfError("response is HTML, not a PDF")
    payload = data[3:] if data.startswith(b"\xef\xbb\xbf") else data
    if not payload.startswith(b"%PDF-"):
        raise NotPdfError("response does not start with a PDF header")
    try:
        document = pymupdf.open(stream=payload, filetype="pdf")
    except Exception as exc:
        raise NotPdfError(f"invalid PDF structure: {exc}") from exc
    try:
        if document.page_count < 1:
            raise NotPdfError("PDF has no pages")
        document.load_page(0)
    finally:
        document.close()


def first_page_text(path: Path) -> str:
    document = pymupdf.open(path)
    try:
        return str(document.load_page(0).get_text())
    finally:
        document.close()


def _looks_like_html(data: bytes) -> bool:
    head = data[:2048].lstrip().lower()
    return head.startswith((b"<!doctype html", b"<html", b"<head", b"<body"))


def _looks_like_login(data: bytes) -> bool:
    sample = data[:8000].lower()
    return any(marker in sample for marker in _LOGIN_MARKERS)
