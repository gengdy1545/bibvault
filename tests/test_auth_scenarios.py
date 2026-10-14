"""Subscription content.

V0.1 has no publisher login. These tests pin how BibVault reports restricted
papers, that it never tries to get around a paywall, and that manual import
closes the gap.
"""

import pytest

from tests.helpers import (
    PDF,
    cli,
    fetch,
    leftovers,
    locate,
    location,
    make_pdf,
    make_settings,
    mock_openalex,
    open_library,
    refs,
    requested_hosts,
    work,
    write_config,
)

PUBLISHERS = [
    pytest.param("10.1145/3802514.3812602", "https://dl.acm.org/doi/pdf/10.1145/3802514.3812602", id="acm"),
    pytest.param(
        "10.1109/icde53745.2022.00276",
        "https://ieeexplore.ieee.org/stampPDF/getPDF.jsp?arnumber=9835373",
        id="ieee",
    ),
    pytest.param("10.1007/s002360050048", "https://link.springer.com/content/pdf/10.1007/s002360050048.pdf", id="springer"),
    pytest.param("10.1016/j.is.2023.102200", "https://www.sciencedirect.com/science/article/pii/S0306437923000200/pdfft", id="elsevier"),
    pytest.param("10.1002/spe.3000", "https://onlinelibrary.wiley.com/doi/pdfdirect/10.1002/spe.3000", id="wiley"),
]


def entry(doi: str, key: str = "paper") -> str:
    return f"@inproceedings{{{key}, title = {{Restricted Paper}}, year = {{2022}}, doi = {{{doi}}}}}"


@pytest.mark.parametrize(("doi", "publisher_pdf"), PUBLISHERS)
def test_paper_without_an_open_copy_is_not_claimed_to_need_a_login(settings, respx_mock, doi, publisher_pdf):
    mock_openalex(respx_mock, doi, work(is_oa=False))
    result = fetch(settings, refs(entry(doi)))["paper"]
    assert result.status == "no_open_access_found"
    assert "bibvault import" in result.error
    assert result.path is None
    assert requested_hosts(respx_mock) == {"api.openalex.org"}
    assert not settings.library_root.exists() or not any(settings.library_root.iterdir())


@pytest.mark.parametrize(("doi", "publisher_pdf"), PUBLISHERS)
def test_sso_login_redirect_requires_authentication(project, respx_mock, doi, publisher_pdf):
    settings = make_settings(project, max_retries=3)
    mock_openalex(respx_mock, doi, work(location(publisher_pdf)))
    route = respx_mock.get(publisher_pdf).respond(
        302, headers={"location": "https://idp.university.edu/idp/profile/SAML2/Redirect/SSO"}
    )
    login = respx_mock.get("https://idp.university.edu/idp/profile/SAML2/Redirect/SSO").respond(
        200,
        content=b"<html><body><h1>Shibboleth</h1><form><input type='password'></form></body></html>",
        headers={"content-type": "text/html"},
    )
    result = fetch(settings, refs(entry(doi)))["paper"]
    assert result.status == "authentication_required"
    assert route.call_count == 1
    assert login.call_count == 1
    assert not (settings.library_root / "paper.pdf").exists()
    assert leftovers(settings) == []


def test_http_401_requires_authentication_without_retrying(project, respx_mock):
    settings = make_settings(project, max_retries=3)
    doi, pdf_url = "10.1145/3802514.3812602", "https://dl.acm.org/doi/pdf/10.1145/3802514.3812602"
    mock_openalex(respx_mock, doi, work(location(pdf_url)))
    route = respx_mock.get(pdf_url).respond(401)
    assert fetch(settings, refs(entry(doi)))["paper"].status == "authentication_required"
    assert route.call_count == 1


def test_http_403_is_access_denied_without_retrying(project, respx_mock):
    settings = make_settings(project, max_retries=3)
    doi, pdf_url = "10.1007/s002360050048", "https://link.springer.com/content/pdf/10.1007/s002360050048.pdf"
    mock_openalex(respx_mock, doi, work(location(pdf_url)))
    route = respx_mock.get(pdf_url).respond(403, content=b"<html>Forbidden</html>")
    result = fetch(settings, refs(entry(doi)))["paper"]
    assert result.status == "access_denied"
    assert route.call_count == 1


def test_bot_check_needs_a_browser_and_is_not_retried(project, respx_mock):
    settings = make_settings(project, max_retries=3)
    doi, pdf_url = "10.1145/3722212.3724430", "https://dl.acm.org/doi/pdf/10.1145/3722212.3724430"
    mock_openalex(respx_mock, doi, work(location(pdf_url)))
    route = respx_mock.get(pdf_url).respond(
        403,
        content=b"<!DOCTYPE html><html><head><title>Just a moment...</title></head></html>",
        headers={"content-type": "text/html", "cf-mitigated": "challenge", "server": "cloudflare"},
    )
    result = fetch(settings, refs(entry(doi)))["paper"]
    assert result.status == "manual_action_required"
    assert "dl.acm.org" in result.error
    assert "bibvault import" in result.error
    assert route.call_count == 1
    assert leftovers(settings) == []


def test_reported_error_belongs_to_the_reported_status(settings, respx_mock):
    doi = "10.1145/3802514.3812602"
    denied = "https://dl.acm.org/doi/pdf/10.1145/3802514.3812602"
    broken = "https://infoscience.example.edu/paper.pdf"
    mock_openalex(respx_mock, doi, work(location(denied), location(broken, "acceptedVersion")))
    respx_mock.get(denied).respond(403)
    respx_mock.get(broken).respond(405)
    result = fetch(settings, refs(entry(doi)))["paper"]
    assert result.status == "access_denied"
    assert denied in result.error
    assert "405" not in result.error


def test_publisher_landing_page_instead_of_pdf_is_not_stored(settings, respx_mock):
    doi, pdf_url = "10.1109/icde53745.2022.00276", "https://ieeexplore.ieee.org/document/9835373"
    mock_openalex(respx_mock, doi, work(location(pdf_url)))
    respx_mock.get(pdf_url).respond(
        200, content=b"<!doctype html><html><body>Abstract only</body></html>", headers=PDF
    )
    result = fetch(settings, refs(entry(doi)))["paper"]
    assert result.status == "failed"
    assert "HTML" in result.error
    assert not (settings.library_root / "paper.pdf").exists()


def test_open_access_landing_page_needs_manual_download(settings, respx_mock):
    doi = "10.1145/3722212.3724430"
    landing = "https://dl.acm.org/doi/10.1145/3722212.3724430"
    mock_openalex(respx_mock, doi, work(location(None, landing=landing)))
    result = fetch(settings, refs(entry(doi)))["paper"]
    assert result.status == "manual_action_required"
    assert landing in result.error
    assert requested_hosts(respx_mock) == {"api.openalex.org"}


def test_acm_open_access_pdf_downloads_without_login(settings, respx_mock):
    doi = "10.1145/3722212.3724430"
    pdf_url = "https://dl.acm.org/doi/pdf/10.1145/3722212.3724430"
    mock_openalex(respx_mock, doi, work(location(pdf_url)))
    respx_mock.get(pdf_url).respond(200, content=make_pdf("acm open access"), headers=PDF)
    result = fetch(settings, refs(entry(doi, "autocomp25")))["autocomp25"]
    assert result.status == "downloaded"
    assert result.path == "papers/autocomp25.pdf"


def test_open_copy_is_used_when_the_publisher_copy_is_locked(settings, respx_mock):
    doi = "10.1145/3802514.3812602"
    publisher = "https://dl.acm.org/doi/pdf/10.1145/3802514.3812602"
    repository = "https://repository.university.edu/bitstream/lake-tables.pdf"
    mock_openalex(
        respx_mock,
        doi,
        work(location(publisher), location(repository, "acceptedVersion")),
    )
    respx_mock.get(publisher).respond(200, content=b"<html>Sign in</html>", headers={"content-type": "text/html"})
    respx_mock.get(repository).respond(200, content=make_pdf("accepted manuscript"))
    result = fetch(settings, refs(entry(doi)))["paper"]
    assert result.status == "downloaded"
    assert result.version == "accepted"


def test_restricted_status_is_recorded_for_agents(settings, respx_mock):
    doi = "10.1145/3802514.3812602"
    mock_openalex(respx_mock, doi, work(is_oa=False))
    fetch(settings, refs(entry(doi)))
    payload = locate(settings, "paper")
    assert payload["status"] == "no_open_access_found"
    assert payload["files"] == {}
    assert "bibvault import" in payload["error"]


def test_manual_import_closes_the_gap(project, respx_mock):
    config = write_config(project)
    bib = project / "references.bib"
    doi = "10.1145/3802514.3812602"
    bib.write_text(entry(doi, "lake-tables-short"), encoding="utf-8")
    mock_openalex(respx_mock, doi, work(is_oa=False))

    first = cli(config, "fetch", str(bib))
    assert first.exit_code == 0, first.output
    assert "lake-tables-short (no_open_access_found):" in first.output

    downloaded = project / "Downloads" / "3802514.3812602.pdf"
    downloaded.parent.mkdir()
    downloaded.write_bytes(make_pdf("downloaded through the library proxy"))
    imported = cli(config, "import", str(downloaded), "--key", "lake-tables-short", "--bib", str(bib))
    assert imported.exit_code == 0, imported.output
    assert "papers/lake-tables-short.pdf" in imported.output

    pdf_info = locate(make_settings(project), "lake-tables-short")["files"]["pdf"]
    assert pdf_info["provider"] == "import"
    assert pdf_info["source_url"] is None

    calls = len(respx_mock.calls)
    again = cli(config, "fetch", str(bib), "--missing")
    assert "Fetching" in again.output
    assert "Already on disk:      1" in again.output
    assert "Could not acquire:    0" in again.output
    assert len(respx_mock.calls) == calls

    full = cli(config, "fetch", str(bib))
    assert "Already on disk:      1" in full.output
    assert requested_hosts(respx_mock) == {"api.openalex.org"}


def test_import_without_bib_keeps_the_known_identity(project, respx_mock):
    config = write_config(project)
    bib = project / "references.bib"
    doi = "10.1145/3802514.3812602"
    bib.write_text(entry(doi, "lake-tables-short"), encoding="utf-8")
    mock_openalex(respx_mock, doi, work(is_oa=False))
    cli(config, "fetch", str(bib))

    pdf = project / "download.pdf"
    pdf.write_bytes(make_pdf("from the proxy"))
    result = cli(config, "import", str(pdf), "--key", "lake-tables-short")
    assert result.exit_code == 0, result.output

    settings = make_settings(project)
    payload = locate(settings, "lake-tables-short")
    assert payload["reference_id"] == f"doi:{doi}"
    assert payload["files"]["pdf"]["path"] == "papers/lake-tables-short.pdf"
    library = open_library(settings)
    try:
        row = library.conn.execute("SELECT doi FROM publications WHERE reference_id = ?", (f"doi:{doi}",)).fetchone()
    finally:
        library.close()
    assert row["doi"] == doi

    again = cli(config, "fetch", str(bib))
    assert "Already on disk:      1" in again.output


def test_import_rejects_a_saved_login_page(project):
    config = write_config(project)
    bib = project / "references.bib"
    bib.write_text(entry("10.1145/3802514.3812602"), encoding="utf-8")
    fake = project / "paper.pdf"
    fake.write_bytes(b"<html><body>Sign in through your institution</body></html>")
    result = cli(config, "import", str(fake), "--key", "paper", "--bib", str(bib))
    assert result.exit_code == 1
    assert not (project / "papers" / "paper.pdf").exists()


def test_import_refuses_to_replace_a_different_pdf(project):
    config = write_config(project)
    bib = project / "references.bib"
    bib.write_text(entry("10.1145/3802514.3812602"), encoding="utf-8")
    first, second = project / "first.pdf", project / "second.pdf"
    first.write_bytes(make_pdf("first"))
    second.write_bytes(make_pdf("second"))
    assert cli(config, "import", str(first), "--key", "paper", "--bib", str(bib)).exit_code == 0
    refused = cli(config, "import", str(second), "--key", "paper", "--bib", str(bib))
    assert refused.exit_code == 1
    assert (project / "papers" / "paper.pdf").read_bytes() == first.read_bytes()

    repeated = cli(config, "import", str(first), "--key", "paper", "--bib", str(bib))
    assert repeated.exit_code == 0
    assert "already_exists" in repeated.output


def test_import_adopts_a_pdf_already_in_the_library(project, respx_mock):
    config = write_config(project)
    bib = project / "references.bib"
    bib.write_text(
        "@article{paper, title = {P}, year = {2020}, url = {https://oa.example/paper.pdf}}",
        encoding="utf-8",
    )
    existing = project / "papers" / "paper.pdf"
    existing.parent.mkdir()
    existing.write_bytes(make_pdf("collected before BibVault"))
    result = cli(config, "import", str(existing), "--key", "paper", "--bib", str(bib))
    assert result.exit_code == 0, result.output
    assert sorted(path.name for path in existing.parent.iterdir()) == ["paper.pdf"]

    again = cli(config, "fetch", str(bib))
    assert "Already on disk:      1" in again.output
    assert len(respx_mock.calls) == 0


def test_import_needs_a_known_key(project):
    config = write_config(project)
    pdf = project / "paper.pdf"
    pdf.write_bytes(make_pdf("x"))
    result = cli(config, "import", str(pdf), "--key", "unknown")
    assert result.exit_code == 1
    assert "unknown" in result.output
