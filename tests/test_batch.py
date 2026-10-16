"""End-to-end run over tests/fixtures/sample.bib with every remote service mocked."""

import json

import pytest

from bibvault.storage import load_manifest
from tests.helpers import (
    PDF,
    SAMPLE_BIB,
    article_html,
    cli,
    location,
    make_pdf,
    mock_openalex,
    mock_vldb_volume,
    vldb_paper,
    work,
    write_config,
)

EXPECTED = {
    "lakehouse-comparison23": "downloaded",
    "delta-lake-paper": "downloaded",
    "delta-lake-alias": "already_exists",
    "lance-format-preprint": "downloaded",
    "lance-eprint": "already_exists",
    "lake-tables-short": "no_open_access_found",
    "pixels-s3": "authentication_required",
    "lsm-tree96": "access_denied",
    "wisckey16": "downloaded",
    "lakehouse": "downloaded",
    "hudi-index": "downloaded",
    "rocksdb-checkpoints": "downloaded",
    "pixels-retina": "downloaded",
    "table-format-whitepaper": "downloaded",
    "cidr2021": "not_found",
    "crossref-child": "not_found",
}

WISCKEY_PDF = "https://www.usenix.org/system/files/conference/fast16/fast16-papers-lu.pdf"
LAKEHOUSE_PDF = "https://www.cidrdb.org/cidr2021/papers/cidr2021_paper17.pdf"
OPEN_PDFS = {
    "https://www.cidrdb.org/cidr2023/papers/p92-jain.pdf",
    "https://www.vldb.org/pvldb/vol13/p3411-armbrust.pdf",
    "https://arxiv.org/pdf/2504.15247",
    "https://docs.example.org/whitepaper.pdf",
    WISCKEY_PDF,
    LAKEHOUSE_PDF,
}
HTML = {"content-type": "text/html"}
WEB = {
    "https://hudi.apache.org/docs/indexes/": "Apache Hudi Indexes",
    "https://github.com/facebook/rocksdb/wiki/Checkpoints": "RocksDB Checkpoints",
    "https://github.com/pixelsdb/pixels/tree/master/pixels-retina": "Pixels Retina",
}


@pytest.fixture
def remote(respx_mock):
    for url in OPEN_PDFS:
        respx_mock.get(url).respond(200, content=make_pdf(url), headers=PDF)
    for url, title in WEB.items():
        respx_mock.get(url).respond(200, text=article_html(title), headers=HTML)

    respx_mock.get("https://www.usenix.org/conference/fast16/technical-sessions/presentation/lu").respond(
        200, text=f'<html><head><meta name="citation_pdf_url" content="{WISCKEY_PDF}"></head></html>', headers=HTML
    )
    lakehouse = "Lakehouse: A New Generation of Open Platforms that Unify Data Warehousing and Advanced Analytics"
    respx_mock.get("https://www.cidrdb.org/cidr2021/program.html").respond(
        200, text=f'<a href="papers/cidr2021_paper17.pdf">{lakehouse}</a>', headers=HTML
    )

    mock_vldb_volume(
        respx_mock,
        13,
        vldb_paper(
            "Delta Lake: High-Performance ACID Table Storage over Cloud Object Stores",
            3411,
            "https://www.vldb.org/pvldb/vol13/p3411-armbrust.pdf",
        ),
    )
    delta = mock_openalex(
        respx_mock,
        "10.14778/3415478.3415560",
        work(location("https://www.vldb.org/pvldb/vol13/p3411-armbrust.pdf")),
    )
    mock_openalex(respx_mock, "10.48550/arxiv.2504.15247", status=404)
    mock_openalex(respx_mock, "10.1145/3802514.3812602", work(is_oa=False))

    ieee_pdf = "https://ieeexplore.ieee.org/stampPDF/getPDF.jsp?arnumber=9835373"
    mock_openalex(respx_mock, "10.1109/icde53745.2022.00276", work(location(ieee_pdf)))
    respx_mock.get(ieee_pdf).respond(
        200,
        content=b"<html><head><title>IEEE Xplore</title></head><body>Sign in via your institution</body></html>",
        headers={"content-type": "text/html"},
    )

    springer_pdf = "https://link.springer.com/content/pdf/10.1007/s002360050048.pdf"
    mock_openalex(respx_mock, "10.1007/s002360050048", work(location(springer_pdf)))
    respx_mock.get(springer_pdf).respond(403)

    respx_mock.get(url__regex=r"^https://api\.openalex\.org/works\?").respond(200, json={"results": []})
    return {"delta": delta}


@pytest.fixture
def project_bib(project):
    bib = project / "references.bib"
    bib.write_bytes(SAMPLE_BIB.read_bytes())
    return write_config(project, '\n[resolution]\nopenalex_api_key = "test-key"\n'), bib


def run(config, bib) -> dict:
    result = cli(config, "fetch", str(bib), "--format", "json")
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def test_sample_bibliography_end_to_end(project, project_bib, remote, respx_mock):
    config, bib = project_bib
    payload = run(config, bib)

    statuses = {item["citation_key"]: item["status"] for item in payload["results"]}
    assert statuses == EXPECTED
    assert payload["summary"] == {
        "references": 16,
        "available_locally": 11,
        "newly_downloaded": 9,
        "already_present": 2,
        "authentication_required": 1,
        "no_open_access_found": 1,
        "access_denied": 1,
        "not_found": 2,
        "manual_action_required": 0,
        "unsupported": 0,
        "rate_limited": 0,
        "failed": 0,
    }
    # vldb.org answers delta-lake-paper, and delta-lake-alias finds that PDF already on disk.
    assert remote["delta"].call_count == 0
    for url in OPEN_PDFS | WEB.keys():
        assert sum(call.request.url == url for call in respx_mock.calls) == 1

    papers = sorted(path.name for path in (project / "papers").iterdir())
    assert papers == [
        "delta-lake-alias.pdf",
        "delta-lake-paper.pdf",
        "hudi-index.md",
        "lakehouse-comparison23.pdf",
        "lakehouse.pdf",
        "lance-eprint.pdf",
        "lance-format-preprint.pdf",
        "pixels-retina.md",
        "rocksdb-checkpoints.md",
        "table-format-whitepaper.pdf",
        "wisckey16.pdf",
    ]
    assert (project / "papers" / "delta-lake-alias.pdf").read_bytes() == (
        project / "papers" / "delta-lake-paper.pdf"
    ).read_bytes()

    manifest = load_manifest(project / ".bibvault" / "manifest.json")
    assert set(manifest) == set(EXPECTED)
    assert manifest["lance-eprint"]["reference_id"] == manifest["lance-format-preprint"]["reference_id"]
    assert "bibvault import" in json.loads(cli(config, "locate", "lake-tables-short", "--format", "json").stdout)["error"]

    verified = cli(config, "verify", "--references")
    assert verified.exit_code == 0, verified.stdout


def test_second_run_downloads_nothing(project, project_bib, remote, respx_mock):
    config, bib = project_bib
    run(config, bib)
    first_calls = len(respx_mock.calls)

    payload = run(config, bib)
    later = respx_mock.calls[first_calls:]
    assert not any(str(call.request.url) in OPEN_PDFS or str(call.request.url) in WEB for call in later)
    assert remote["delta"].call_count == 0
    statuses = {item["citation_key"]: item["status"] for item in payload["results"]}
    downloaded_before = [key for key, status in EXPECTED.items() if status in {"downloaded", "already_exists"}]
    assert all(statuses[key] == "already_exists" for key in downloaded_before)
    assert payload["summary"]["newly_downloaded"] == 0


def test_missing_only_run_only_contacts_unresolved_entries(project, project_bib, remote, respx_mock):
    config, bib = project_bib
    run(config, bib)
    first_calls = len(respx_mock.calls)
    result = cli(config, "fetch", str(bib), "--missing")
    assert result.exit_code == 0
    later = respx_mock.calls[first_calls:]
    assert not any(str(call.request.url) in OPEN_PDFS or str(call.request.url) in WEB for call in later)
    assert not any(
        call.request.url.host == "api.openalex.org"
        and "3415478.3415560" in str(call.request.url)
        for call in later
    )
