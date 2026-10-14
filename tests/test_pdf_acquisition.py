"""Open-access PDF acquisition: direct links, OpenAlex, arXiv, versions, and HTTP failures."""

import os

import httpx
import pytest

from bibvault.config import ResolutionFile
from tests.helpers import (
    PDF,
    fetch,
    leftovers,
    locate,
    location,
    make_pdf,
    make_settings,
    mock_openalex,
    no_openalex,
    reference_problems,
    refs,
    with_key,
    work,
)

DIRECT = """
@inproceedings{lakehouse-comparison23,
    title = {Analyzing and Comparing Lakehouse Storage Systems},
    year  = {2023},
    url   = {https://www.cidrdb.org/cidr2023/papers/p92-jain.pdf}
}
"""
DIRECT_URL = "https://www.cidrdb.org/cidr2023/papers/p92-jain.pdf"

OA_DOI = "10.14778/3415478.3415560"
OA_ENTRY = """
@article{delta-lake-paper,
    title = {Delta Lake},
    year  = {2020},
    doi   = {10.14778/3415478.3415560}
}
"""


def test_direct_pdf_is_saved_under_the_citation_key(settings, respx_mock):
    pdf = make_pdf("lakehouse comparison")
    respx_mock.get(DIRECT_URL).respond(200, content=pdf, headers=PDF)
    result = fetch(settings, refs(DIRECT))["lakehouse-comparison23"]
    assert result.status == "downloaded"
    assert result.path == "papers/lakehouse-comparison23.pdf"
    assert (settings.library_root / "lakehouse-comparison23.pdf").read_bytes() == pdf
    pdf_info = locate(settings, "lakehouse-comparison23")["files"]["pdf"]
    assert pdf_info["provider"] == "direct"
    assert pdf_info["version"] == "provided"
    assert pdf_info["source_url"] == DIRECT_URL
    assert pdf_info["byte_size"] == len(pdf)
    assert reference_problems(settings) == []


@pytest.mark.parametrize("content_type", ["application/octet-stream", "binary/octet-stream", None])
def test_pdf_is_recognised_by_content_not_header(settings, respx_mock, content_type):
    headers = {"content-type": content_type} if content_type else {}
    respx_mock.get(DIRECT_URL).respond(200, content=make_pdf("x"), headers=headers)
    assert fetch(settings, refs(DIRECT))["lakehouse-comparison23"].status == "downloaded"


@pytest.mark.parametrize(
    "body",
    [
        b"<html><body>Article landing page</body></html>",
        b"",
        b"%PDF-1.5\ncorrupted",
    ],
)
def test_invalid_payloads_are_not_stored(settings, respx_mock, body):
    respx_mock.get(DIRECT_URL).respond(200, content=body, headers=PDF)
    result = fetch(settings, refs(DIRECT))["lakehouse-comparison23"]
    assert result.status == "failed"
    assert result.path is None
    assert not (settings.library_root / "lakehouse-comparison23.pdf").exists()
    assert leftovers(settings) == []


def test_open_access_doi_via_openalex(settings, respx_mock):
    pdf_url = "https://www.vldb.org/pvldb/vol13/p3411-armbrust.pdf"
    mock_openalex(respx_mock, OA_DOI, work(location(pdf_url)))
    respx_mock.get(pdf_url).respond(200, content=make_pdf("delta"), headers=PDF)
    result = fetch(settings, refs(OA_ENTRY))["delta-lake-paper"]
    assert result.status == "downloaded"
    assert result.reference_id == f"doi:{OA_DOI}"
    assert result.provider == "openalex"
    assert result.version == "published"


def test_arxiv_preprint_by_doi(settings, respx_mock):
    text = """
    @article{lance-format-preprint,
        title = {Lance}, year = {2025},
        doi = {10.48550/arXiv.2504.15247},
        url = {https://arxiv.org/abs/2504.15247}
    }
    """
    mock_openalex(respx_mock, "10.48550/arxiv.2504.15247", status=404)
    respx_mock.get("https://arxiv.org/pdf/2504.15247").respond(200, content=make_pdf("lance"))
    result = fetch(settings, refs(text))["lance-format-preprint"]
    assert result.status == "downloaded"
    assert result.reference_id == "arxiv:2504.15247"
    assert result.version == "preprint"
    assert result.provider == "arxiv"


def test_old_style_arxiv_eprint(settings, respx_mock):
    text = "@article{old, title = {Old}, year = {2001}, eprint = {cs/0112017}, archivePrefix = {arXiv}}"
    respx_mock.get("https://arxiv.org/pdf/cs/0112017").respond(200, content=make_pdf("old"))
    assert fetch(settings, refs(text))["old"].status == "downloaded"


def test_redirects_are_followed(settings, respx_mock):
    respx_mock.get(DIRECT_URL).respond(302, headers={"location": "https://cdn.cidrdb.org/p92.pdf"})
    respx_mock.get("https://cdn.cidrdb.org/p92.pdf").respond(200, content=make_pdf("cdn"))
    assert fetch(settings, refs(DIRECT))["lakehouse-comparison23"].status == "downloaded"


def test_next_candidate_is_tried_when_the_first_fails(settings, respx_mock):
    payload = work(
        location("https://oa.example/dead.pdf"),
        location("https://repo.example/alive.pdf", "acceptedVersion"),
    )
    mock_openalex(respx_mock, OA_DOI, payload)
    respx_mock.get("https://oa.example/dead.pdf").respond(404)
    respx_mock.get("https://repo.example/alive.pdf").respond(200, content=make_pdf("alive"))
    result = fetch(settings, refs(OA_ENTRY))["delta-lake-paper"]
    assert result.status == "downloaded"
    assert result.version == "accepted"


def test_only_the_best_version_is_downloaded(settings, respx_mock):
    published = make_pdf("published")
    payload = work(
        location("https://repo.example/pre.pdf", "submittedVersion"),
        location("https://oa.example/pub.pdf"),
    )
    mock_openalex(respx_mock, OA_DOI, payload)
    respx_mock.get("https://oa.example/pub.pdf").respond(200, content=published)
    preprint_route = respx_mock.get("https://repo.example/pre.pdf").respond(200, content=make_pdf("preprint"))
    fetch(settings, refs(OA_ENTRY))
    assert sorted(path.name for path in settings.library_root.iterdir()) == ["delta-lake-paper.pdf"]
    assert (settings.library_root / "delta-lake-paper.pdf").read_bytes() == published
    assert locate(settings, "delta-lake-paper")["files"]["pdf"]["version"] == "published"
    assert preprint_route.call_count == 0


def test_identical_bytes_from_two_urls_are_stored_once(settings, respx_mock):
    same = make_pdf("same bytes")
    payload = work(
        location("https://oa.example/pub.pdf"),
        location("https://mirror.example/pre.pdf", "submittedVersion"),
    )
    mock_openalex(respx_mock, OA_DOI, payload)
    respx_mock.get("https://oa.example/pub.pdf").respond(200, content=same)
    respx_mock.get("https://mirror.example/pre.pdf").respond(200, content=same)
    fetch(settings, refs(OA_ENTRY))
    assert sorted(path.name for path in settings.library_root.iterdir()) == ["delta-lake-paper.pdf"]


def test_preprint_preferred_when_configured(project, respx_mock):
    settings = make_settings(project, resolution=ResolutionFile(prefer_published_version=False))
    payload = work(
        location("https://oa.example/pub.pdf"),
        location("https://repo.example/pre.pdf", "submittedVersion"),
    )
    mock_openalex(respx_mock, OA_DOI, payload)
    published_route = respx_mock.get("https://oa.example/pub.pdf").respond(200, content=make_pdf("pub"))
    respx_mock.get("https://repo.example/pre.pdf").respond(200, content=make_pdf("pre"))
    result = fetch(settings, refs(OA_ENTRY))["delta-lake-paper"]
    assert result.version == "preprint"
    assert published_route.call_count == 0
    assert sorted(path.name for path in settings.library_root.iterdir()) == ["delta-lake-paper.pdf"]


def test_paper_with_a_pdf_is_not_fetched_again_for_a_better_version(project, respx_mock):
    settings = make_settings(project, resolution=with_key())
    preprint = make_pdf("preprint")
    route = mock_openalex(respx_mock, OA_DOI)
    route.side_effect = [
        httpx.Response(200, json=work(location("https://repo.example/pre.pdf", "submittedVersion"))),
        httpx.Response(
            200,
            json=work(
                location("https://oa.example/pub.pdf"),
                location("https://repo.example/pre.pdf", "submittedVersion"),
            ),
        ),
    ]
    respx_mock.get("https://repo.example/pre.pdf").respond(200, content=preprint)
    published_route = respx_mock.get("https://oa.example/pub.pdf").respond(200, content=make_pdf("published"))
    fetch(settings, refs(OA_ENTRY))
    assert fetch(settings, refs(OA_ENTRY))["delta-lake-paper"].status == "already_exists"
    assert route.call_count == 1
    assert published_route.call_count == 0
    assert sorted(path.name for path in settings.library_root.iterdir()) == ["delta-lake-paper.pdf"]
    assert (settings.library_root / "delta-lake-paper.pdf").read_bytes() == preprint


def test_transient_server_error_is_retried(project, respx_mock, sleeps):
    settings = make_settings(project, max_retries=2)
    route = respx_mock.get(DIRECT_URL)
    route.side_effect = [httpx.Response(503), httpx.Response(200, content=make_pdf("ok"))]
    assert fetch(settings, refs(DIRECT))["lakehouse-comparison23"].status == "downloaded"
    assert route.call_count == 2
    assert sleeps == [1]


def test_persistent_server_error_fails_after_the_retry_budget(project, respx_mock, sleeps):
    settings = make_settings(project, max_retries=2)
    route = respx_mock.get(DIRECT_URL).respond(503)
    result = fetch(settings, refs(DIRECT))["lakehouse-comparison23"]
    assert result.status == "failed"
    assert route.call_count == 3
    assert sleeps == [1, 2]


def test_timeouts_are_retried_then_reported(project, respx_mock):
    settings = make_settings(project, max_retries=1)
    route = respx_mock.get(DIRECT_URL).mock(side_effect=httpx.ConnectTimeout("slow"))
    result = fetch(settings, refs(DIRECT))["lakehouse-comparison23"]
    assert result.status == "failed"
    assert result.error
    assert route.call_count == 2


def test_rate_limit_is_reported_after_retries(project, respx_mock):
    settings = make_settings(project, max_retries=1)
    route = respx_mock.get(DIRECT_URL).respond(429)
    result = fetch(settings, refs(DIRECT))["lakehouse-comparison23"]
    assert result.status == "rate_limited"
    assert route.call_count == 2


def test_rate_limit_that_clears_is_retried(project, respx_mock):
    settings = make_settings(project, max_retries=1)
    route = respx_mock.get(DIRECT_URL)
    route.side_effect = [httpx.Response(429), httpx.Response(200, content=make_pdf("ok"))]
    assert fetch(settings, refs(DIRECT))["lakehouse-comparison23"].status == "downloaded"


@pytest.mark.parametrize(("status", "expected"), [(404, "not_found"), (410, "failed"), (400, "failed")])
def test_permanent_errors_are_not_retried(project, respx_mock, status, expected):
    settings = make_settings(project, max_retries=3)
    route = respx_mock.get(DIRECT_URL).respond(status)
    assert fetch(settings, refs(DIRECT))["lakehouse-comparison23"].status == expected
    assert route.call_count == 1


def test_unknown_doi_is_not_found(settings, respx_mock):
    text = "@article{ghost, title = {Ghost}, year = {2020}, doi = {10.1000/does-not-exist}}"
    mock_openalex(respx_mock, "10.1000/does-not-exist", status=404)
    result = fetch(settings, refs(text))["ghost"]
    assert result.status == "not_found"


def test_metadata_outage_is_a_failure_not_a_missing_paper(settings, respx_mock):
    mock_openalex(respx_mock, OA_DOI, status=500)
    result = fetch(settings, refs(OA_ENTRY))["delta-lake-paper"]
    assert result.status == "failed"
    assert "OpenAlex" in result.error


def test_metadata_rate_limit_is_reported(settings, respx_mock):
    mock_openalex(respx_mock, OA_DOI, status=429)
    assert fetch(settings, refs(OA_ENTRY))["delta-lake-paper"].status == "rate_limited"


def test_one_failure_does_not_stop_the_batch(settings, respx_mock):
    text = """
    @article{ghost, title = {Ghost}, year = {2020}, doi = {10.1000/ghost}}
    @article{broken, title = {Broken}, year = {2020}, url = {https://broken.example/x.pdf}}
    @article{good, title = {Good}, year = {2021}, url = {https://oa.example/good.pdf}}
    """
    mock_openalex(respx_mock, "10.1000/ghost", status=404)
    respx_mock.get("https://broken.example/x.pdf").mock(side_effect=RuntimeError("boom"))
    respx_mock.get("https://oa.example/good.pdf").respond(200, content=make_pdf("good"))
    results = fetch(settings, refs(text))
    assert {key: item.status for key, item in results.items()} == {
        "ghost": "not_found",
        "broken": "failed",
        "good": "downloaded",
    }


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_unwritable_library_is_reported_per_entry(settings, respx_mock):
    respx_mock.get(DIRECT_URL).respond(200, content=make_pdf("x"))
    settings.library_root.mkdir(parents=True)
    settings.library_root.chmod(0o555)
    try:
        result = fetch(settings, refs(DIRECT))["lakehouse-comparison23"]
    finally:
        settings.library_root.chmod(0o755)
    assert result.status == "failed"
    assert "Permission denied" in result.error


def test_rerun_downloads_nothing(settings, respx_mock):
    pdf_url = "https://www.vldb.org/pvldb/vol13/p3411-armbrust.pdf"
    metadata = mock_openalex(respx_mock, OA_DOI, work(location(pdf_url)))
    pdf_route = respx_mock.get(pdf_url).respond(200, content=make_pdf("delta"))
    fetch(settings, refs(OA_ENTRY))
    second = fetch(settings, refs(OA_ENTRY))["delta-lake-paper"]
    assert second.status == "already_exists"
    assert pdf_route.call_count == 1
    assert metadata.call_count == 1


def test_present_papers_make_no_requests(settings, respx_mock):
    respx_mock.get(DIRECT_URL).respond(200, content=make_pdf("x"))
    fetch(settings, refs(DIRECT))
    calls_before = len(respx_mock.calls)
    result = fetch(settings, refs(DIRECT))["lakehouse-comparison23"]
    assert result.status == "already_exists"
    assert result.path == "papers/lakehouse-comparison23.pdf"
    assert len(respx_mock.calls) == calls_before


def test_interrupted_partial_file_is_replaced(project, respx_mock):
    settings = make_settings(project, resolution=no_openalex(), max_retries=1)
    partial = settings.library_root / "lakehouse-comparison23.pdf.partial"
    partial.parent.mkdir(parents=True)
    partial.write_bytes(b"half a download")
    pdf = make_pdf("complete")
    respx_mock.get(DIRECT_URL).side_effect = [
        httpx.Response(503),
        httpx.Response(200, content=pdf),
    ]
    assert fetch(settings, refs(DIRECT))["lakehouse-comparison23"].status == "downloaded"
    assert (settings.library_root / "lakehouse-comparison23.pdf").read_bytes() == pdf
    assert leftovers(settings) == []


def test_deleted_pdf_is_downloaded_again(settings, respx_mock):
    pdf = make_pdf("restore me")
    route = respx_mock.get(DIRECT_URL).respond(200, content=pdf)
    fetch(settings, refs(DIRECT))
    (settings.library_root / "lakehouse-comparison23.pdf").unlink()
    result = fetch(settings, refs(DIRECT))["lakehouse-comparison23"]
    assert result.status == "downloaded"
    assert result.path == "papers/lakehouse-comparison23.pdf"
    assert (settings.library_root / "lakehouse-comparison23.pdf").read_bytes() == pdf
    assert route.call_count == 2
    assert reference_problems(settings) == []


def test_user_edited_pdf_is_kept(settings, respx_mock):
    route = respx_mock.get(DIRECT_URL).respond(200, content=make_pdf("original"))
    fetch(settings, refs(DIRECT))
    annotated = make_pdf("original with my highlights")
    target = settings.library_root / "lakehouse-comparison23.pdf"
    target.write_bytes(annotated)

    again = fetch(settings, refs(DIRECT))["lakehouse-comparison23"]
    assert again.status == "already_exists"
    assert target.read_bytes() == annotated
    assert route.call_count == 1
    assert locate(settings, "lakehouse-comparison23")["files"]["pdf"]["path"] == "papers/lakehouse-comparison23.pdf"
    assert any("checksum mismatch" in problem for problem in reference_problems(settings))
