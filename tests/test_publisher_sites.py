"""USENIX, CIDR and OpenProceedings papers come from the publisher's own site."""

import pytest

from bibvault.models import Reference
from bibvault.resolve import _usenix_conference
from tests.helpers import (
    PDF,
    fetch,
    make_pdf,
    make_settings,
    refs,
    requested_hosts,
    with_key,
    work,
)

HTML = {"content-type": "text/html"}

RAFT = "In Search of an Understandable Consensus Algorithm"
ATC14 = "https://www.usenix.org/conference/atc14/technical-sessions"
RAFT_TALK = "https://www.usenix.org/conference/atc14/technical-sessions/presentation/ongaro"
RAFT_PDF = "https://www.usenix.org/system/files/conference/atc14/atc14-paper-ongaro.pdf"
WISCKEY_TALK = "https://www.usenix.org/conference/fast16/technical-sessions/presentation/lu"
WISCKEY_PDF = "https://www.usenix.org/system/files/conference/fast16/fast16-papers-lu.pdf"

UMBRA = "Umbra: A Disk-Based System with In-Memory Performance"
CIDR20 = "https://www.cidrdb.org/cidr2020/program.html"
UMBRA_PDF = "https://www.cidrdb.org/cidr2020/papers/p29-neumann-cidr20.pdf"


def usenix_entry(key: str = "raft", title: str = RAFT, url: str | None = None) -> str:
    link = f"url = {{{url}}}," if url else ""
    return f"""
@inproceedings{{{key},
    author    = {{Diego Ongaro and John K. Ousterhout}},
    title     = {{{title}}},
    booktitle = {{Proceedings of the 2014 {{USENIX}} Annual Technical Conference ({{USENIX}} {{ATC}})}},
    {link}
    year      = {{2014}}
}}
"""


def talk_page(pdf: str) -> str:
    return f'<html><head><meta name="citation_title" content="x"><meta name="citation_pdf_url" content="{pdf}"></head></html>'


def program_page(*talks: tuple[str, str]) -> str:
    links = "".join(f'<h2><a href="{path}">{title}</a></h2><a href="{path}">More</a>' for path, title in talks)
    return f"<html><body>{links}</body></html>"


def cidr_entry(key: str = "umbra", title: str = UMBRA, year: str = "2020", url: str | None = None) -> str:
    link = f"url = {{{url}}}," if url else ""
    return f"""
@inproceedings{{{key},
    author    = {{Thomas Neumann and Michael J. Freitag}},
    title     = {{{title}}},
    booktitle = {{10th Conference on Innovative Data Systems Research ({{CIDR}})}},
    {link}
    year      = {{{year}}}
}}
"""


def serve_pdf(respx_mock, url: str):
    return respx_mock.get(url).respond(200, content=make_pdf(url), headers=PDF)


@pytest.mark.parametrize(
    ("venue", "year", "expected"),
    [
        ("14th {USENIX} Conference on File and Storage Technologies ({FAST})", "2016", "fast16"),
        ("2018 {USENIX} Annual Technical Conference ({USENIX} {ATC})", "2018", "atc18"),
        ("17th {USENIX} Symposium on Networked Systems Design and Implementation ({NSDI})", "2020", "nsdi20"),
        ("15th {USENIX} Symposium on Operating Systems Design and Implementation ({OSDI})", "2021", "osdi21"),
        ("Symposium on Operating Systems Design and Implementation", "2021", None),
        ("30th {USENIX} Security Symposium", "2021", None),
        ("14th {USENIX} Conference on File and Storage Technologies ({FAST})", None, None),
    ],
)
def test_usenix_conference_is_named_from_venue_and_year(venue, year, expected):
    reference = Reference(citation_key="k", entry_type="inproceedings", venue=venue, year=year)
    assert _usenix_conference(reference) == expected


def test_linked_usenix_page_gives_the_pdf(settings, respx_mock):
    respx_mock.get(WISCKEY_TALK).respond(200, text=talk_page(WISCKEY_PDF), headers=HTML)
    serve_pdf(respx_mock, WISCKEY_PDF)
    result = fetch(settings, refs(usenix_entry("wisckey", "WiscKey", WISCKEY_TALK)))["wisckey"]
    assert result.status == "downloaded"
    assert result.provider == "usenix"
    assert result.version == "published"
    assert requested_hosts(respx_mock) == {"www.usenix.org"}


def test_usenix_paper_without_a_link_is_found_in_the_program(settings, respx_mock):
    respx_mock.get(ATC14).respond(
        200, text=program_page(("/conference/atc14/technical-sessions/presentation/x", "Other Talk"), (RAFT_TALK.removeprefix("https://www.usenix.org"), RAFT)), headers=HTML
    )
    respx_mock.get(RAFT_TALK).respond(200, text=talk_page(RAFT_PDF), headers=HTML)
    serve_pdf(respx_mock, RAFT_PDF)
    result = fetch(settings, refs(usenix_entry()))["raft"]
    assert result.status == "downloaded"
    assert result.provider == "usenix"


def test_usenix_program_is_read_once_per_run(project, respx_mock):
    settings = make_settings(project, concurrency=4)
    titles = [f"Paper Number {index} About Consensus" for index in range(4)]
    program = respx_mock.get(ATC14).respond(
        200, text=program_page(*[(f"/conference/atc14/presentation/p{index}", title) for index, title in enumerate(titles)]), headers=HTML
    )
    for index in range(4):
        pdf = f"https://www.usenix.org/system/files/atc14-p{index}.pdf"
        respx_mock.get(f"https://www.usenix.org/conference/atc14/presentation/p{index}").respond(200, text=talk_page(pdf), headers=HTML)
        serve_pdf(respx_mock, pdf)
    results = fetch(settings, refs("".join(usenix_entry(f"p{index}", title) for index, title in enumerate(titles))))
    assert {item.status for item in results.values()} == {"downloaded"}
    assert program.call_count == 1


def test_usenix_pdf_link_away_from_usenix_org_is_ignored(settings, respx_mock):
    respx_mock.get(WISCKEY_TALK).respond(200, text=talk_page("https://evil.example/paper.pdf"), headers=HTML)
    result = fetch(settings, refs(usenix_entry("wisckey", "WiscKey", WISCKEY_TALK)))["wisckey"]
    assert result.status != "downloaded"
    assert "evil.example" not in requested_hosts(respx_mock)


def test_usenix_paper_missing_from_the_program_falls_back_to_openalex(project, respx_mock):
    settings = make_settings(project, resolution=with_key())
    respx_mock.get(ATC14).respond(200, text=program_page(("/conference/atc14/presentation/x", "Other Talk")), headers=HTML)
    search = respx_mock.get(url__regex=r"^https://api\.openalex\.org/works\?").respond(
        200, json={"results": [work(is_oa=False, title=RAFT, year=2014)]}
    )
    assert fetch(settings, refs(usenix_entry()))["raft"].status == "no_open_access_found"
    assert search.call_count == 1


def test_unavailable_usenix_page_falls_back_to_openalex(project, respx_mock):
    settings = make_settings(project, resolution=with_key())
    respx_mock.get(WISCKEY_TALK).respond(503)
    search = respx_mock.get(url__regex=r"^https://api\.openalex\.org/works\?").respond(200, json={"results": []})
    assert fetch(settings, refs(usenix_entry("wisckey", "WiscKey", WISCKEY_TALK)))["wisckey"].status == "not_found"
    assert search.call_count == 1


def test_found_usenix_pdf_is_remembered_across_runs(settings, respx_mock):
    talk = respx_mock.get(WISCKEY_TALK).respond(200, text=talk_page(WISCKEY_PDF), headers=HTML)
    serve_pdf(respx_mock, WISCKEY_PDF)
    text = usenix_entry("wisckey", "WiscKey", WISCKEY_TALK)
    fetch(settings, refs(text))
    assert fetch(settings, refs(text))["wisckey"].status == "already_exists"
    assert talk.call_count == 1


def test_cidr_link_named_after_the_paper(settings, respx_mock):
    page = f'<a href="papers/p1-other.pdf">Another Paper</a><a href="papers/p29-neumann-cidr20.pdf">{UMBRA}</a>'
    respx_mock.get(CIDR20).respond(200, text=page, headers=HTML)
    serve_pdf(respx_mock, UMBRA_PDF)
    result = fetch(settings, refs(cidr_entry()))["umbra"]
    assert result.status == "downloaded"
    assert result.provider == "cidr"
    assert requested_hosts(respx_mock) == {"www.cidrdb.org"}


def test_cidr_pdf_link_after_the_title_in_a_framed_program(settings, respx_mock):
    title = "High Performance Transactions in Deuteronomy"
    pdf = "http://www.cidrdb.org/cidr2015/Papers/CIDR15_Paper15.pdf"
    respx_mock.get("https://www.cidrdb.org/cidr2015/program.html").respond(
        200, text='<iframe src="CIDR2015TOC_Final1.html" width="600" // a comment </iframe>', headers=HTML
    )
    toc = (
        '<style><!-- p {font-family:"Cambria"} --></style>'
        '<p><b>OTHER PAPER (</b><a href="Papers/CIDR15_Paper14.pdf">PDF</a>) (<a href="Slides/14.pdf">SLIDES</a>)</p>'
        f'<p><b>{title.upper()} (</b><a href="{pdf}"><b>PDF</b></a>) (<a href="Slides/15.pdf">SLIDES</a>)</p>'
    )
    respx_mock.get("https://www.cidrdb.org/cidr2015/CIDR2015TOC_Final1.html").respond(200, text=toc, headers=HTML)
    serve_pdf(respx_mock, pdf)
    assert fetch(settings, refs(cidr_entry("deuteronomy", title, "2015")))["deuteronomy"].status == "downloaded"


def test_cidr_icon_link_before_the_authors_and_title(settings, respx_mock):
    title = "OligoArchive: Using DNA in the DBMS storage hierarchy"
    page = (
        '<a href="papers/p98-appuswamy-cidr19.pdf"><i class="fa fa-file-pdf-o"></i></a>'
        f"1. Raja Appuswamy (Eurecom): “{title}”"
        '<a href="papers/p127-chrysogelos-cidr19.pdf"><i class="fa fa-file-pdf-o"></i></a>'
        "2. Periklis Chrysogelos (EPFL): “Another Paper”"
    )
    respx_mock.get("https://www.cidrdb.org/cidr2019/program.html").respond(200, text=page, headers=HTML)
    serve_pdf(respx_mock, "https://www.cidrdb.org/cidr2019/papers/p98-appuswamy-cidr19.pdf")
    assert fetch(settings, refs(cidr_entry("oligo", title, "2019")))["oligo"].status == "downloaded"


def test_cidr_papers_kept_on_vldb_org(settings, respx_mock):
    pdf = "https://vldb.org/cidrdb/papers/2025/p29-neumann.pdf"
    respx_mock.get("https://www.cidrdb.org/cidr2025/program.html").respond(200, text=f'<a href="{pdf}">{UMBRA}</a>', headers=HTML)
    serve_pdf(respx_mock, pdf)
    assert fetch(settings, refs(cidr_entry(year="2025")))["umbra"].status == "downloaded"


def test_cidr_link_away_from_the_site_is_ignored(settings, respx_mock):
    respx_mock.get(CIDR20).respond(200, text=f'<a href="https://evil.example/umbra.pdf">{UMBRA}</a>', headers=HTML)
    assert fetch(settings, refs(cidr_entry()))["umbra"].status != "downloaded"
    assert "evil.example" not in requested_hosts(respx_mock)


def test_cidr_program_is_read_once_per_run(project, respx_mock):
    settings = make_settings(project, concurrency=4)
    titles = [f"Paper Number {index} About Databases" for index in range(4)]
    page = "".join(f'<a href="papers/p{index}.pdf">{title}</a>' for index, title in enumerate(titles))
    program = respx_mock.get(CIDR20).respond(200, text=page, headers=HTML)
    for index in range(4):
        serve_pdf(respx_mock, f"https://www.cidrdb.org/cidr2020/papers/p{index}.pdf")
    results = fetch(settings, refs("".join(cidr_entry(f"p{index}", title) for index, title in enumerate(titles))))
    assert {item.status for item in results.values()} == {"downloaded"}
    assert program.call_count == 1


def test_cidr_entry_that_links_its_pdf_does_not_read_the_program(settings, respx_mock):
    serve_pdf(respx_mock, UMBRA_PDF)
    assert fetch(settings, refs(cidr_entry(url=UMBRA_PDF)))["umbra"].status == "downloaded"
    assert [str(call.request.url) for call in respx_mock.calls] == [UMBRA_PDF]


@pytest.mark.parametrize("doi", ["10.5441/002/edbt.2016.42", "10.48786/edbt.2022.33"])
def test_openproceedings_doi_leads_to_the_pdf(settings, respx_mock, doi):
    pdf = "https://openproceedings.org/2016/conf/edbt/paper-12.pdf"
    respx_mock.get(f"https://doi.org/{doi}").respond(302, headers={"location": pdf})
    serve_pdf(respx_mock, pdf)
    text = f"@inproceedings{{rum, title = {{Designing Access Methods: The RUM Conjecture}}, year = {{2016}}, doi = {{{doi}}}}}"
    result = fetch(settings, refs(text))["rum"]
    assert result.status == "downloaded"
    assert result.provider == "openproceedings"
    assert "api.openalex.org" not in requested_hosts(respx_mock)
