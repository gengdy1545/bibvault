"""Official database-community sources and the conservative DBLP fallback."""

import asyncio
import copy
import json
from pathlib import Path

import pytest

from bibvault.browser import browser_link, opens_in_browser
from bibvault.database_sources import source_descriptors
from bibvault.models import Reference
from bibvault.network import make_client
from tests.helpers import (
    PDF,
    fetch,
    locate,
    make_pdf,
    make_settings,
    mock_openalex,
    no_openalex,
    open_library,
    refs,
    requested_hosts,
    work,
)

FIXTURES = Path(__file__).parent / "fixtures" / "database_sources"
ACM_CASES = json.loads((FIXTURES / "acm.json").read_text(encoding="utf-8"))

DROPS_DOI = "10.4230/lipics.icdt.2024.1"
DROPS_LANDING = "https://drops.dagstuhl.de/entities/document/10.4230/LIPIcs.ICDT.2024.1"
DROPS_PDF = (
    "https://drops.dagstuhl.de/storage/00lipics/lipics-vol290-icdt2024/"
    "LIPIcs.ICDT.2024.1/LIPIcs.ICDT.2024.1.pdf"
)
DROPS_TITLE = "Natural Language Data Interfaces: A Data Access Odyssey (Invited Talk)"

SIGMOD_ARCHIVE = "https://sigmodrecord.org/issues/"
SIGMOD_ISSUE = "https://sigmodrecord.org/sigmod-record-march-2025-vol-54-no-1/"
SIGMOD_TITLE = (
    "Information Theory Strikes Back: "
    "New Development in the Theory of Cardinality Estimation"
)
SIGMOD_PDF = "https://sigmodrecord.org/?smd_process_download=1&download_id=13996"
GPTUNER_TITLE = (
    "If All Else Fails, Read the Instructions! "
    "A Perspective on “GPTuner: An LLM-Based Database Tuning System”"
)
GPTUNER_PDF = "https://sigmodrecord.org/?smd_process_download=1&download_id=14013"

TCDE_ARCHIVE = "https://tc.computer.org/tcde/bulletin/issues/"
TCDE_ISSUE = "http://sites.computer.org/debull/A25mar/issue1.htm"
TCDE_TITLE = "Customizing Operator Implementations for SQL Processing via Large Language Models"
TCDE_PDF = "http://sites.computer.org/debull/A25mar/p45.pdf"

DBLP_API = r"^https://dblp\.org/search/publ/api\?"
DBLP_TITLE = "ALEX: An Updatable Adaptive Learned Index"
DBLP_DOI = "10.1145/3318464.3389711"
DBLP_PDF = f"https://dl.acm.org/doi/pdf/{DBLP_DOI}"


def html_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def json_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def icdt_entry(title: str = DROPS_TITLE) -> str:
    return f"""
@inproceedings{{icdt-natural-language,
    author    = {{Georgia Koutrika}},
    title     = {{{title}}},
    booktitle = {{27th International Conference on Database Theory}},
    year      = {{2024}},
    doi       = {{10.4230/LIPIcs.ICDT.2024.1}}
}}
"""


def sigmod_record_entry(
    key: str = "information-theory",
    title: str = SIGMOD_TITLE,
    author: str = "Mahmoud Abo Khamis",
) -> str:
    return f"""
@article{{{key},
    author  = {{{author}}},
    title   = {{{title}}},
    journal = {{SIGMOD Rec.}},
    volume  = {{54}},
    number  = {{1}},
    year    = {{2025}}
}}
"""


def tcde_entry(title: str = TCDE_TITLE) -> str:
    return f"""
@article{{tcde-operators,
    author  = {{Immanuel Trummer}},
    title   = {{{title}}},
    journal = {{IEEE Data Eng. Bull.}},
    volume  = {{49}},
    number  = {{1}},
    year    = {{2025}}
}}
"""


def dblp_entry() -> str:
    return f"""
@inproceedings{{alex,
    author    = {{Ding, Jialin and Umar Farooq Minhas}},
    title     = {{{DBLP_TITLE}}},
    booktitle = {{ACM SIGMOD International Conference on Management of Data}},
    year      = {{2020}}
}}
"""


@pytest.mark.parametrize("item", ACM_CASES, ids=lambda item: item["case"])
def test_acm_database_fixtures_use_the_existing_browser_path(settings, item):
    reference = Reference(citation_key="paper", entry_type="article", **item)
    library = open_library(settings)

    async def scenario():
        async with make_client(settings) as client:
            return await browser_link(reference, settings, client, library)

    try:
        link = asyncio.run(scenario())
    finally:
        library.close()

    assert opens_in_browser(reference, settings) is True
    assert link == (
        "acm",
        f"https://dl.acm.org/doi/pdf/{item['doi']}?download=true",
    )


def test_drops_icdt_pdf_is_resolved_and_cached_across_runs(settings, respx_mock):
    landing = respx_mock.get(DROPS_LANDING).respond(
        200,
        text=html_fixture("drops_icdt.html"),
        headers={"content-type": "text/html"},
    )
    pdf = respx_mock.get(DROPS_PDF).respond(
        200,
        content=make_pdf(DROPS_TITLE),
        headers=PDF,
    )
    references = refs(icdt_entry())

    first = fetch(settings, references)["icdt-natural-language"]
    assert first.status == "downloaded"
    assert first.provider == "drops"
    (settings.project_root / (first.path or "")).unlink()

    second = fetch(settings, references)["icdt-natural-language"]
    assert second.status == "downloaded"
    assert landing.call_count == 1
    assert pdf.call_count == 2
    provenance = locate(settings, "icdt-natural-language")["files"]["pdf"]
    assert provenance["provider"] == "drops"
    assert provenance["source_url"] == DROPS_PDF


@pytest.mark.parametrize(
    "page",
    [
        html_fixture("drops_icdt.html").replace(DROPS_TITLE, "An Unrelated ICDT Paper"),
        html_fixture("drops_icdt.html").replace(DROPS_PDF, "https://evil.example/paper.pdf"),
    ],
)
def test_drops_rejects_wrong_titles_and_nonofficial_hosts(project, respx_mock, page):
    settings = make_settings(project, resolution=no_openalex())
    respx_mock.get(DROPS_LANDING).respond(
        200,
        text=page,
        headers={"content-type": "text/html"},
    )
    result = fetch(settings, refs(icdt_entry()))["icdt-natural-language"]
    assert result.status == "not_found"
    assert "evil.example" not in requested_hosts(respx_mock)
    assert DROPS_PDF not in {str(call.request.url) for call in respx_mock.calls}


def test_unavailable_drops_page_falls_back_to_openalex(settings, respx_mock):
    fallback = "https://repository.example.edu/icdt-paper.pdf"
    respx_mock.get(DROPS_LANDING).respond(503)
    mock_openalex(respx_mock, DROPS_DOI, work({"pdf_url": fallback, "version": "publishedVersion"}))
    respx_mock.get(fallback).respond(200, content=make_pdf(DROPS_TITLE), headers=PDF)
    result = fetch(settings, refs(icdt_entry()))["icdt-natural-language"]
    assert result.status == "downloaded"
    assert result.provider == "openalex"


def test_sigmod_record_catalogs_are_shared_and_cached(project, respx_mock):
    settings = make_settings(project, concurrency=4)
    archive = respx_mock.get(SIGMOD_ARCHIVE).respond(
        200,
        text=html_fixture("sigmod_record_issues.html"),
        headers={"content-type": "text/html"},
    )
    issue = respx_mock.get(SIGMOD_ISSUE).respond(
        200,
        text=html_fixture("sigmod_record_issue.html"),
        headers={"content-type": "text/html"},
    )
    first_pdf = respx_mock.get(SIGMOD_PDF).respond(
        200,
        content=make_pdf(SIGMOD_TITLE),
        headers=PDF,
    )
    second_pdf = respx_mock.get(GPTUNER_PDF).respond(
        200,
        content=make_pdf(GPTUNER_TITLE),
        headers=PDF,
    )
    references = refs(
        sigmod_record_entry()
        + sigmod_record_entry("gptuner", GPTUNER_TITLE, "Immanuel Trummer")
    )

    first = fetch(settings, references)
    assert {item.provider for item in first.values()} == {"sigmod-record"}
    for result in first.values():
        (settings.project_root / (result.path or "")).unlink()

    second = fetch(settings, references)
    assert {item.status for item in second.values()} == {"downloaded"}
    assert archive.call_count == issue.call_count == 1
    assert first_pdf.call_count == second_pdf.call_count == 2
    provenance = locate(settings, "information-theory")["files"]["pdf"]
    assert provenance["provider"] == "sigmod-record"
    assert provenance["source_url"] == SIGMOD_PDF


@pytest.mark.parametrize(
    "issue_page",
    [
        html_fixture("sigmod_record_issue.html").replace(
            SIGMOD_TITLE,
            "Information Theory for an Unrelated Storage Survey",
        ),
        html_fixture("sigmod_record_issue.html").replace(
            "https://sigmodrecord.org/?smd_process_download=1&amp;download_id=13996",
            "https://evil.example/paper.pdf",
        ),
    ],
)
def test_sigmod_record_rejects_mismatches_and_external_links(
    project,
    respx_mock,
    issue_page,
):
    settings = make_settings(project, resolution=no_openalex())
    respx_mock.get(SIGMOD_ARCHIVE).respond(
        200,
        text=html_fixture("sigmod_record_issues.html"),
        headers={"content-type": "text/html"},
    )
    respx_mock.get(SIGMOD_ISSUE).respond(
        200,
        text=issue_page,
        headers={"content-type": "text/html"},
    )
    result = fetch(settings, refs(sigmod_record_entry()))["information-theory"]
    assert result.status == "not_found"
    assert "evil.example" not in requested_hosts(respx_mock)


def test_tcde_issue_resolves_an_article_with_provenance(settings, respx_mock):
    archive = respx_mock.get(TCDE_ARCHIVE).respond(
        200,
        text=html_fixture("tcde_issues.html"),
        headers={"content-type": "text/html"},
    )
    issue = respx_mock.get(TCDE_ISSUE).respond(
        200,
        text=html_fixture("tcde_issue.html"),
        headers={"content-type": "text/html"},
    )
    respx_mock.get(TCDE_PDF).respond(200, content=make_pdf(TCDE_TITLE), headers=PDF)
    result = fetch(settings, refs(tcde_entry()))["tcde-operators"]
    assert result.status == "downloaded"
    assert result.provider == "ieee-tcde"
    assert archive.call_count == issue.call_count == 1
    provenance = locate(settings, "tcde-operators")["files"]["pdf"]
    assert provenance["source_url"] == TCDE_PDF


def test_tcde_ignores_an_article_link_off_the_official_hosts(project, respx_mock):
    settings = make_settings(project, resolution=no_openalex())
    respx_mock.get(TCDE_ARCHIVE).respond(
        200,
        text=html_fixture("tcde_issues.html"),
        headers={"content-type": "text/html"},
    )
    bad_issue = html_fixture("tcde_issue.html").replace(
        TCDE_PDF,
        "https://evil.example/p45.pdf",
    )
    respx_mock.get(TCDE_ISSUE).respond(
        200,
        text=bad_issue,
        headers={"content-type": "text/html"},
    )
    result = fetch(settings, refs(tcde_entry()))["tcde-operators"]
    assert result.status == "not_found"
    assert "evil.example" not in requested_hosts(respx_mock)


def test_dblp_fallback_preserves_identity_and_records_discovered_doi(project, respx_mock):
    settings = make_settings(project, resolution=no_openalex())
    search = respx_mock.get(url__regex=DBLP_API).respond(
        200,
        json=json_fixture("dblp_search.json"),
    )
    pdf = respx_mock.get(DBLP_PDF).respond(
        200,
        content=make_pdf(DBLP_TITLE),
        headers=PDF,
    )
    references = refs(dblp_entry())

    first = fetch(settings, references)["alex"]
    assert first.status == "downloaded"
    assert first.provider == "dblp"
    assert first.reference_id.startswith("internal:")
    provenance = locate(settings, "alex")["files"]["pdf"]
    assert provenance["provider"] == "dblp"
    assert provenance["source_url"] == f"https://doi.org/{DBLP_DOI}"
    (settings.project_root / (first.path or "")).unlink()

    assert fetch(settings, references)["alex"].status == "downloaded"
    assert search.call_count == 1
    assert pdf.call_count == 2


@pytest.mark.parametrize("mismatch", ["title", "year", "author", "ambiguous"])
def test_dblp_requires_title_year_first_author_and_an_unambiguous_hit(
    project,
    respx_mock,
    mismatch,
):
    settings = make_settings(project, resolution=no_openalex())
    payload = json_fixture("dblp_search.json")
    hit = payload["result"]["hits"]["hit"][0]
    if mismatch == "title":
        hit["info"]["title"] = "A Completely Different Learned Index."
    elif mismatch == "year":
        hit["info"]["year"] = "2019"
    elif mismatch == "author":
        hit["info"]["authors"]["author"][0]["text"] = "Ada Lovelace"
    else:
        other = copy.deepcopy(hit)
        other["info"]["doi"] = "10.1145/3318464.3389999"
        other["info"]["url"] = "https://dblp.org/rec/conf/sigmod/Other20"
        payload["result"]["hits"]["hit"].append(other)
    respx_mock.get(url__regex=DBLP_API).respond(200, json=payload)

    result = fetch(settings, refs(dblp_entry()))["alex"]
    assert result.status == "not_found"
    assert "dl.acm.org" not in requested_hosts(respx_mock)


def test_dblp_does_not_follow_untrusted_electronic_editions(project, respx_mock):
    settings = make_settings(project, resolution=no_openalex())
    payload = json_fixture("dblp_search.json")
    info = payload["result"]["hits"]["hit"][0]["info"]
    info.pop("doi")
    info["ee"] = "https://evil.example/alex.pdf"
    respx_mock.get(url__regex=DBLP_API).respond(200, json=payload)

    result = fetch(settings, refs(dblp_entry()))["alex"]
    assert result.status == "not_found"
    assert "evil.example" not in requested_hosts(respx_mock)


def test_source_descriptor_interface_exposes_official_hosts():
    descriptors = {item.provider: item for item in source_descriptors()}
    assert descriptors["acm"].access == "browser"
    assert descriptors["drops"].hosts == ("drops.dagstuhl.de",)
    assert set(descriptors["sigmod-record"].hosts) == {
        "sigmodrecord.org",
        "www.sigmodrecord.org",
    }
    assert set(descriptors["ieee-tcde"].hosts) == {
        "sites.computer.org",
        "tc.computer.org",
    }
    assert descriptors["dblp"].access == "metadata-fallback"
