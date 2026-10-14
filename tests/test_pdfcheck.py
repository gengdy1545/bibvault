import pytest

from bibvault.errors import AuthenticationRequiredError, NotPdfError
from bibvault.pdfcheck import assert_pdf_bytes, assert_pdf_file
from tests.helpers import make_pdf


def test_valid_pdf_is_accepted(tmp_path):
    data = make_pdf("hello")
    assert_pdf_bytes(data)
    path = tmp_path / "ok.pdf"
    path.write_bytes(data)
    assert_pdf_file(path)


def test_pdf_with_utf8_bom_is_accepted():
    assert_pdf_bytes(b"\xef\xbb\xbf" + make_pdf("bom"))


@pytest.mark.parametrize(
    "page",
    [
        b"<html><body><h1>Sign in to continue</h1></body></html>",
        b"<!DOCTYPE html><html><form action='/idp/profile/SAML2'>Shibboleth</form></html>",
        b"  \n<html><title>OpenAthens</title></html>",
        b"<html><body>Access through your institution</body></html>",
        b"<html><input type='password' name='pass'></html>",
    ],
)
def test_login_pages_mean_authentication_is_required(page):
    with pytest.raises(AuthenticationRequiredError):
        assert_pdf_bytes(page)


def test_other_html_is_not_a_pdf():
    with pytest.raises(NotPdfError):
        assert_pdf_bytes(b"<!doctype html><html><body>Article abstract</body></html>")


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"plain text, not a document",
        b'{"error": "not found"}',
        b"%PDF-1.7\n",
        b"%PDF-1.4\n" + bytes(range(256)) * 4,
    ],
)
def test_non_pdf_payloads_are_rejected(payload):
    with pytest.raises(NotPdfError):
        assert_pdf_bytes(payload)
