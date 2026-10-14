"""VLDB papers come from vldb.org: PVLDB from its volume page, older conference papers from their year's proceedings."""

import pytest

from bibvault.models import Reference
from bibvault.resolve import is_pvldb
from tests.helpers import (
    PDF,
    fetch,
    make_pdf,
    make_settings,
    mock_openalex,
    mock_vldb_unknown_files,
    mock_vldb_volume,
    refs,
    requested_hosts,
    vldb_paper,
    work,
)

TITLE = "OceanBase Paetica: A Hybrid Shared-nothing/Shared-everything Database"
# vldb.org names the file after its own first author, not the BibTeX's.
PDF_URL = "https://www.vldb.org/pvldb/vol16/p3728-xu.pdf"
DOI = "10.14778/3611540.3611560"


def entry(key: str = "oceanbase-htap", title: str = TITLE, pages: str = "3728--3740", doi: str = DOI) -> str:
    return f"""
@article{{{key},
    author  = {{Zhifeng Yang and Quanqing Xu}},
    title   = {{{title}}},
    journal = {{Proc. {{VLDB}} Endow.}},
    volume  = {{16}},
    pages   = {{{pages}}},
    year    = {{2023}},
    doi     = {{{doi}}}
}}
"""


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({"doi": "10.14778/3415478.3415560"}, True),
        ({"venue": "Proc. {VLDB} Endow."}, True),
        ({"venue": "PVLDB"}, True),
        ({"venue": "Proceedings of the VLDB Endowment"}, True),
        ({"venue": "The {VLDB} Journal"}, False),
        ({"venue": "Proceedings of the 31st International Conference on Very Large Data Bases"}, False),
        ({"doi": "10.1007/s00778-020-00632-7"}, False),
    ],
)
def test_pvldb_is_recognized_by_doi_or_journal(fields, expected):
    assert is_pvldb(Reference(citation_key="k", entry_type="article", **fields)) is expected


def test_paper_is_found_by_title_on_the_volume_page(settings, respx_mock):
    volume = mock_vldb_volume(respx_mock, 16, vldb_paper("Another Paper", "3715", "https://www.vldb.org/pvldb/vol16/p3715-zheng.pdf"), vldb_paper(TITLE, "3728", PDF_URL))
    respx_mock.get(PDF_URL).respond(200, content=make_pdf("oceanbase"), headers=PDF)
    result = fetch(settings, refs(entry()))["oceanbase-htap"]
    assert result.status == "downloaded"
    assert result.path == "papers/oceanbase-htap.pdf"
    assert result.provider == "vldb"
    assert result.version == "published"
    assert volume.call_count == 1
    assert "api.openalex.org" not in requested_hosts(respx_mock)


def test_wrong_pages_in_bibtex_still_match_the_title(settings, respx_mock):
    mock_vldb_volume(respx_mock, 16, vldb_paper(TITLE, 3728, PDF_URL))
    respx_mock.get(PDF_URL).respond(200, content=make_pdf("oceanbase"), headers=PDF)
    assert fetch(settings, refs(entry(pages="1--12")))["oceanbase-htap"].status == "downloaded"


def test_start_page_picks_between_papers_with_the_same_title(settings, respx_mock):
    other = "https://www.vldb.org/pvldb/vol16/p100-demo.pdf"
    mock_vldb_volume(respx_mock, 16, vldb_paper(TITLE, 100, other), vldb_paper(TITLE, 3728, PDF_URL))
    respx_mock.get(PDF_URL).respond(200, content=make_pdf("oceanbase"), headers=PDF)
    fetch(settings, refs(entry()))
    assert [str(call.request.url) for call in respx_mock.calls if call.request.url.path.endswith(".pdf")] == [PDF_URL]


def test_a_reworded_title_is_accepted_on_the_right_page(settings, respx_mock):
    hyrise = "https://vldb.org/pvldb/vol4/p105-grund.pdf"
    mock_vldb_volume(respx_mock, 16, vldb_paper("HYRISE - A Main Memory Hybrid Storage Engine", 105, hyrise))
    respx_mock.get(hyrise).respond(200, content=make_pdf("hyrise"), headers=PDF)
    text = entry("hyrise", "HYRISE: a main memory hybrid database storage engine", "105--116")
    assert fetch(settings, refs(text))["hyrise"].status == "downloaded"


def test_a_different_title_on_the_same_page_is_not_taken(settings, respx_mock):
    mock_vldb_volume(respx_mock, 16, vldb_paper("Unrelated Work on Graph Databases", 3728, PDF_URL))
    mock_vldb_unknown_files(respx_mock)
    mock_openalex(respx_mock, DOI, work(is_oa=False))
    result = fetch(settings, refs(entry()))["oceanbase-htap"]
    assert result.status == "no_open_access_found"
    assert PDF_URL not in {str(call.request.url) for call in respx_mock.calls}


def test_links_away_from_vldb_org_are_ignored(settings, respx_mock):
    mock_vldb_volume(respx_mock, 16, vldb_paper(TITLE, 3728, "https://evil.example/p3728.pdf"))
    mock_vldb_unknown_files(respx_mock)
    mock_openalex(respx_mock, DOI, work(is_oa=False))
    assert fetch(settings, refs(entry()))["oceanbase-htap"].status == "no_open_access_found"
    assert "evil.example" not in requested_hosts(respx_mock)


def test_paper_missing_from_the_volume_page_is_tried_at_its_usual_address(settings, respx_mock):
    guessed = "https://www.vldb.org/pvldb/vol16/p128-bottcher.pdf"
    mock_vldb_volume(respx_mock, 16, vldb_paper("Some Other Paper", 1, "https://www.vldb.org/pvldb/vol16/p1-a.pdf"))
    probe = respx_mock.head(guessed).respond(200, headers=PDF)
    respx_mock.get(guessed).respond(200, content=make_pdf("steam"), headers=PDF)
    text = """
@article{steam-gc,
    author  = {Jan B{\\"o}ttcher and Viktor Leis},
    title   = {Scalable Garbage Collection for In-Memory MVCC Systems},
    journal = {Proc. {VLDB} Endow.},
    volume  = {16},
    pages   = {128--141},
    year    = {2019}
}
"""
    result = fetch(settings, refs(text))["steam-gc"]
    assert result.status == "downloaded"
    assert result.provider == "vldb"
    assert probe.call_count == 1


def test_guessed_address_that_is_not_a_pdf_is_not_used(settings, respx_mock):
    mock_vldb_volume(respx_mock, 16)
    probe = mock_vldb_unknown_files(respx_mock)
    mock_openalex(respx_mock, DOI, work(is_oa=False))
    result = fetch(settings, refs(entry()))["oceanbase-htap"]
    assert result.status == "no_open_access_found"
    assert str(probe.calls.last.request.url) == "https://www.vldb.org/pvldb/vol16/p3728-yang.pdf"
    assert not any(call.request.method == "GET" and call.request.url.path.endswith(".pdf") for call in respx_mock.calls)


@pytest.mark.parametrize("page", ["<html><body>No data</body></html>", "<script id=\"__NEXT_DATA__\" type=\"application/json\">{broken</script>"])
def test_unexpected_volume_page_falls_back_to_openalex(settings, respx_mock, page):
    respx_mock.get("https://www.vldb.org/pvldb/volumes/16/").respond(200, text=page)
    mock_vldb_unknown_files(respx_mock)
    mock_openalex(respx_mock, DOI, work(is_oa=False))
    assert fetch(settings, refs(entry()))["oceanbase-htap"].status == "no_open_access_found"


def test_unavailable_volume_page_falls_back_to_openalex(settings, respx_mock):
    mock_vldb_volume(respx_mock, 16, status=503)
    mock_vldb_unknown_files(respx_mock)
    openalex = mock_openalex(respx_mock, DOI, work(is_oa=False))
    assert fetch(settings, refs(entry()))["oceanbase-htap"].status == "no_open_access_found"
    assert openalex.call_count == 1


def test_entry_without_a_volume_does_not_ask_vldb_org(settings, respx_mock):
    text = f"@article{{paper, title = {{{TITLE}}}, journal = {{PVLDB}}, year = {{2023}}, doi = {{{DOI}}}}}"
    mock_openalex(respx_mock, DOI, work(is_oa=False))
    fetch(settings, refs(text))
    assert requested_hosts(respx_mock) == {"api.openalex.org"}


def test_volume_page_is_read_once_per_run(project, respx_mock):
    settings = make_settings(project, concurrency=4)
    papers = [(f"Paper Number {index} About Storage", 100 * index) for index in range(1, 5)]
    volume = mock_vldb_volume(
        respx_mock,
        16,
        *[vldb_paper(title, page, f"https://www.vldb.org/pvldb/vol16/p{page}-x.pdf") for title, page in papers],
    )
    for title, page in papers:
        respx_mock.get(f"https://www.vldb.org/pvldb/vol16/p{page}-x.pdf").respond(200, content=make_pdf(title), headers=PDF)
    text = "".join(entry(f"paper{page}", title, f"{page}--{page + 9}", f"10.14778/{page}") for title, page in papers)
    results = fetch(settings, refs(text))
    assert {item.status for item in results.values()} == {"downloaded"}
    assert volume.call_count == 1


def test_a_matched_link_is_remembered_across_runs(settings, respx_mock):
    volume = mock_vldb_volume(respx_mock, 16, vldb_paper(TITLE, 3728, PDF_URL))
    respx_mock.get(PDF_URL).respond(200, content=make_pdf("oceanbase"), headers=PDF)
    fetch(settings, refs(entry()))
    second = fetch(settings, refs(entry()))["oceanbase-htap"]
    assert second.status == "already_exists"
    assert volume.call_count == 1


HTML = {"content-type": "text/html"}
WEAVING = "Weaving Relations for Cache Performance"
CONF_2001 = "https://www.vldb.org/conf/2001/"


def conference_entry(
    key: str = "weaving",
    title: str = WEAVING,
    year: str = "2001",
    pages: str = "169--180",
    venue: str = "Proceedings of the 27th International Conference on Very Large Data Bases ({VLDB})",
) -> str:
    return f"""
@inproceedings{{{key},
    author    = {{Anastassia Ailamaki and David J. DeWitt}},
    title     = {{{title}}},
    booktitle = {{{venue}}},
    pages     = {{{pages}}},
    year      = {{{year}}}
}}
"""


def directory_listing(*names: str) -> str:
    """An Apache index page: sort links, the parent directory, then the files."""

    sort = '<a href="?C=N;O=D">Name</a><a href="?C=M;O=A">Last modified</a>'
    return sort + '<a href="/conf/">Parent Directory</a>' + "".join(f'<a href="{name}">{name}</a>' for name in names)


def test_old_conference_paper_is_found_by_its_first_page(settings, respx_mock):
    respx_mock.get(CONF_2001).respond(200, text=directory_listing("P001.pdf", "P159.pdf", "P169.pdf", "P181.pdf"), headers=HTML)
    respx_mock.get(f"{CONF_2001}P169.pdf").respond(200, content=make_pdf("weaving"), headers=PDF)
    result = fetch(settings, refs(conference_entry()))["weaving"]
    assert result.status == "downloaded"
    assert result.provider == "vldb"
    assert result.version == "published"
    assert requested_hosts(respx_mock) == {"www.vldb.org"}


def test_old_conference_files_in_a_subdirectory(settings, respx_mock):
    top = respx_mock.get("https://www.vldb.org/conf/2005/").respond(200, text=directory_listing("papers/"), headers=HTML)
    respx_mock.get("https://www.vldb.org/conf/2005/papers/").respond(200, text=directory_listing("p1-gray.pdf", "p553-stonebraker.pdf", "p565-lau.pdf"), headers=HTML)
    pdf = respx_mock.get("https://www.vldb.org/conf/2005/papers/p553-stonebraker.pdf").respond(200, content=make_pdf("cstore"), headers=PDF)
    text = conference_entry("cstore", "C-Store: A Column-oriented DBMS", "2005", "553--564")
    assert fetch(settings, refs(text))["cstore"].status == "downloaded"
    assert pdf.call_count == 1
    assert top.call_count == 1


def test_old_conference_paper_found_by_title_in_a_framed_table_of_contents(settings, respx_mock):
    title = "On Efficient Spatial Matching"
    respx_mock.get("https://www.vldb.org/conf/2007/").respond(200, text='<frame src="header.html"><frame src="toc.html">', headers=HTML)
    respx_mock.get("https://www.vldb.org/conf/2007/header.html").respond(200, text="VLDB 2007", headers=HTML)
    toc = f'<a href="papers/research/p1-other.pdf">Another Paper</a><a href="papers/research/p579-wong.pdf">{title}</a>'
    respx_mock.get("https://www.vldb.org/conf/2007/toc.html").respond(200, text=toc, headers=HTML)
    respx_mock.get("https://www.vldb.org/conf/2007/papers/research/p579-wong.pdf").respond(200, content=make_pdf("spatial"), headers=PDF)
    assert fetch(settings, refs(conference_entry("spatial", title, "2007", "")))["spatial"].status == "downloaded"


def test_files_numbered_in_order_are_not_taken_for_page_numbers(settings, respx_mock):
    respx_mock.get("https://www.vldb.org/conf/1999/").respond(200, text=directory_listing(*[f"P{index}.pdf" for index in range(1, 11)]), headers=HTML)
    result = fetch(settings, refs(conference_entry(year="1999", pages="3--12")))["weaving"]
    assert result.status != "downloaded"
    assert not any(call.request.url.path.endswith(".pdf") for call in respx_mock.calls)


@pytest.mark.parametrize(
    ("year", "venue"),
    [
        ("2001", "The {VLDB} Journal"),
        ("2010", "Proceedings of the 36th International Conference on Very Large Data Bases ({VLDB})"),
    ],
)
def test_journal_and_pvldb_era_papers_are_not_looked_up_in_old_proceedings(settings, respx_mock, year, venue):
    fetch(settings, refs(conference_entry(year=year, venue=venue)))
    assert "www.vldb.org" not in requested_hosts(respx_mock)


def test_old_proceedings_are_read_once_per_run(project, respx_mock):
    settings = make_settings(project, concurrency=4)
    pages = ["001", "011", "021", "033"]
    listing = respx_mock.get(CONF_2001).respond(200, text=directory_listing(*[f"P{page}.pdf" for page in pages]), headers=HTML)
    for page in pages:
        respx_mock.get(f"{CONF_2001}P{page}.pdf").respond(200, content=make_pdf(page), headers=PDF)
    text = "".join(conference_entry(f"p{page}", f"Paper Number {page} About Caches", pages=f"{int(page)}--{int(page) + 9}") for page in pages)
    results = fetch(settings, refs(text))
    assert {item.status for item in results.values()} == {"downloaded"}
    assert listing.call_count == 1
