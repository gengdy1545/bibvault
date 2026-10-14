import asyncio
import json

import httpx
import pytest

from bibvault.config import ResolutionFile
from bibvault.models import Reference
from bibvault.network import make_client
from bibvault.resolve import Lookup, map_version, resolve_candidates
from tests.helpers import location, make_settings, mock_openalex, open_library, with_key, work

DOI = "10.1000/iceberg"


@pytest.fixture
def keyed(project):
    """Title searches need an OpenAlex key."""

    return make_settings(project, resolution=with_key())


def ref(**fields) -> Reference:
    fields.setdefault("citation_key", "iceberg2020")
    fields.setdefault("entry_type", "inproceedings")
    fields.setdefault("title", "Iceberg: A Table Format")
    fields.setdefault("year", "2020")
    return Reference(**fields)


def resolve(settings, reference, memory=None):
    async def scenario():
        library = open_library(settings)
        try:
            async with make_client(settings) as client:
                return await resolve_candidates(
                    reference, client, library, settings, {} if memory is None else memory
                )
        finally:
            library.close()

    return asyncio.run(scenario())


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("publishedVersion", "published"),
        ("acceptedVersion", "accepted"),
        ("submittedVersion", "preprint"),
        (None, "unknown"),
        ("", "unknown"),
    ],
)
def test_map_version(raw, expected):
    assert map_version(raw) == expected


def test_provided_pdf_url_comes_first(settings, respx_mock):
    mock_openalex(respx_mock, DOI, work(location("https://oa.example/pub.pdf")))
    result = resolve(settings, ref(doi=DOI, url="https://authors.example/iceberg.pdf"))
    assert [item.provider for item in result.candidates] == ["direct", "openalex"]


def test_published_is_preferred_and_duplicate_urls_collapse(settings, respx_mock):
    payload = work(
        location("https://repo.example/pre.pdf", "submittedVersion"),
        location("https://oa.example/pub.pdf", "publishedVersion"),
        location("https://repo.example/pre.pdf", "submittedVersion"),
    )
    mock_openalex(respx_mock, DOI, payload)
    result = resolve(settings, ref(doi=DOI))
    assert [item.url for item in result.candidates] == [
        "https://oa.example/pub.pdf",
        "https://repo.example/pre.pdf",
    ]
    assert result.found is True


def test_preprint_first_when_configured(project, respx_mock):
    settings = make_settings(project, resolution=ResolutionFile(prefer_published_version=False))
    payload = work(
        location("https://oa.example/pub.pdf", "publishedVersion"),
        location("https://repo.example/pre.pdf", "submittedVersion"),
    )
    mock_openalex(respx_mock, DOI, payload)
    result = resolve(settings, ref(doi=DOI))
    assert result.candidates[0].version == "preprint"


def test_preprints_can_be_disabled(project, respx_mock):
    settings = make_settings(project, resolution=ResolutionFile(allow_preprints=False))
    payload = work(
        location("https://oa.example/pub.pdf", "publishedVersion"),
        location("https://repo.example/pre.pdf", "submittedVersion"),
    )
    mock_openalex(respx_mock, "10.48550/arxiv.2504.15247", payload)
    reference = ref(doi="10.48550/arxiv.2504.15247", arxiv_id="2504.15247")
    result = resolve(settings, reference)
    assert [item.version for item in result.candidates] == ["published"]


def test_oa_url_pdf_is_used_when_locations_have_none(settings, respx_mock):
    payload = work(location(None), is_oa=True, oa_url="https://oa.example/direct.pdf")
    mock_openalex(respx_mock, DOI, payload)
    result = resolve(settings, ref(doi=DOI))
    assert [item.url for item in result.candidates] == ["https://oa.example/direct.pdf"]


def test_unknown_doi_is_not_found(settings, respx_mock):
    mock_openalex(respx_mock, DOI, status=404)
    result = resolve(settings, ref(doi=DOI))
    assert result.candidates == []
    assert result.found is False
    assert result.error is None


def test_known_paywalled_work_is_found_without_candidates(settings, respx_mock):
    mock_openalex(respx_mock, DOI, work(is_oa=False))
    result = resolve(settings, ref(doi=DOI))
    assert result.found is True
    assert result.candidates == []
    assert result.landing_url is None


def test_open_access_landing_page_without_pdf(settings, respx_mock):
    payload = work(location(None, landing="https://www.usenix.org/conference/fast16/presentation/lu"))
    mock_openalex(respx_mock, DOI, payload)
    result = resolve(settings, ref(doi=DOI))
    assert result.candidates == []
    assert result.landing_url == "https://www.usenix.org/conference/fast16/presentation/lu"


def test_transient_openalex_error_is_retried(settings, respx_mock, sleeps):
    route = mock_openalex(respx_mock, DOI)
    route.side_effect = [
        httpx.Response(503),
        httpx.Response(200, json=work(location("https://oa.example/pub.pdf"))),
    ]
    result = resolve(settings, ref(doi=DOI))
    assert len(result.candidates) == 1
    assert route.call_count == 2
    assert sleeps


def test_persistent_openalex_failure_is_reported(settings, respx_mock):
    route = mock_openalex(respx_mock, DOI, status=500)
    result = resolve(settings, ref(doi=DOI))
    assert result.error and "500" in result.error
    assert result.rate_limited is False
    assert route.call_count == 3


def test_persistent_openalex_rate_limit_is_reported(settings, respx_mock):
    route = mock_openalex(respx_mock, DOI, status=429)
    result = resolve(settings, ref(doi=DOI))
    assert result.rate_limited is True
    assert result.error and "bibvault auth login openalex" in result.error
    assert route.call_count == 1


def test_api_key_goes_in_a_header_not_the_url(project, respx_mock):
    keyed = make_settings(project, resolution=ResolutionFile(openalex_api_key="secret-key"))
    route = mock_openalex(respx_mock, DOI, work())
    resolve(keyed, ref(doi=DOI))
    request = route.calls.last.request
    assert request.headers["authorization"] == "Bearer secret-key"
    assert "secret-key" not in str(request.url)

    other = mock_openalex(respx_mock, "10.1000/hudi", work())
    resolve(make_settings(project), ref(doi="10.1000/hudi"))
    assert "authorization" not in other.calls.last.request.headers


def test_api_key_is_sent_with_title_searches(project, respx_mock):
    keyed = make_settings(project, resolution=ResolutionFile(openalex_api_key="secret-key"))
    route = respx_mock.get(url__regex=r"^https://api\.openalex\.org/works\?").respond(200, json={"results": []})
    resolve(keyed, ref(title="Some Paper", year="2020"))
    assert route.calls.last.request.headers["authorization"] == "Bearer secret-key"


def test_openalex_network_error_is_reported(settings, respx_mock):
    mock_openalex(respx_mock, DOI).mock(side_effect=httpx.ConnectError("offline"))
    result = resolve(settings, ref(doi=DOI))
    assert result.error and "ConnectError" in result.error


def test_mailto_is_sent_only_when_configured(project, respx_mock):
    with_email = make_settings(project, resolution=ResolutionFile(email="me@uni.edu"))
    route = mock_openalex(respx_mock, DOI, work())
    resolve(with_email, ref(doi=DOI))
    assert route.calls.last.request.url.params.get("mailto") == "me@uni.edu"

    without = make_settings(project, resolution=ResolutionFile(email=""))
    resolve(without, ref(doi=DOI))
    assert "mailto" not in route.calls.last.request.url.params


def test_same_doi_is_resolved_once_per_run(settings, respx_mock):
    route = mock_openalex(respx_mock, DOI, work(is_oa=False))
    memory: dict[str, Lookup] = {}
    resolve(settings, ref(citation_key="a", doi=DOI), memory)
    resolve(settings, ref(citation_key="b", doi=DOI), memory)
    assert route.call_count == 1


def test_published_result_is_cached_across_runs(settings, respx_mock):
    route = mock_openalex(respx_mock, DOI, work(location("https://oa.example/pub.pdf")))
    resolve(settings, ref(doi=DOI))
    second = resolve(settings, ref(doi=DOI))
    assert route.call_count == 1
    assert second.candidates[0].url == "https://oa.example/pub.pdf"


@pytest.mark.parametrize(
    "payload",
    [
        work(location("https://repo.example/pre.pdf", "submittedVersion")),
        work(is_oa=False),
    ],
)
def test_results_that_may_improve_are_queried_again(settings, respx_mock, payload):
    route = mock_openalex(respx_mock, DOI, payload)
    resolve(settings, ref(doi=DOI))
    resolve(settings, ref(doi=DOI))
    assert route.call_count == 2


def test_corrupt_cache_entry_is_ignored(settings, respx_mock):
    library = open_library(settings)
    library.cache_put(f"openalex:doi:{DOI}", "{not json")
    library.close()
    route = mock_openalex(respx_mock, DOI, work(location("https://oa.example/pub.pdf")))
    result = resolve(settings, ref(doi=DOI))
    assert route.call_count == 1
    assert result.candidates


@pytest.mark.parametrize(
    "payload",
    [
        {"best_oa_location": "nope", "locations": "nope", "open_access": None},
        {"best_oa_location": {"pdf_url": 42}, "locations": [None, 3]},
        {},
    ],
)
def test_malformed_openalex_payloads_do_not_crash(settings, respx_mock, payload):
    mock_openalex(respx_mock, DOI, payload)
    result = resolve(settings, ref(doi=DOI))
    assert result.candidates == []


def test_openalex_list_payload_is_treated_as_not_found(settings, respx_mock):
    respx_mock.get(url__regex=r"https://api\.openalex\.org/works/.*").respond(200, content=json.dumps([1, 2]))
    result = resolve(settings, ref(doi=DOI))
    assert result.found is False


def title_search(respx_mock, results):
    return respx_mock.get(url__regex=r"^https://api\.openalex\.org/works\?").respond(
        200, json={"results": results}
    )


def test_title_search_accepts_a_close_match(keyed, respx_mock):
    route = title_search(
        respx_mock,
        [work(location("https://www.cidrdb.org/lakehouse.pdf"), title="Lakehouse: A New Generation of Open Platforms", year=2021)],
    )
    reference = ref(title="Lakehouse: {A} new generation of open platforms", year="2021")
    result = resolve(keyed, reference)
    assert [item.url for item in result.candidates] == ["https://www.cidrdb.org/lakehouse.pdf"]
    assert route.calls.last.request.url.params["filter"] == "publication_year:2021"


@pytest.mark.parametrize(
    ("found_title", "found_year"),
    [
        ("Lakehouse: A New Generation of Closed Platforms for Warehousing", 2021),
        ("Lakehouse: A New Generation of Open Platforms", 2019),
        ("Completely Unrelated Paper", 2021),
    ],
)
def test_title_search_rejects_unrelated_works(keyed, respx_mock, found_title, found_year):
    route = title_search(respx_mock, [work(location("https://wrong.example/x.pdf"), title=found_title, year=found_year)])
    result = resolve(keyed, ref(title="Lakehouse: A New Generation of Open Platforms", year="2021"))
    assert route.called
    assert result.candidates == []
    assert result.found is False


def test_title_search_ignores_non_numeric_years(keyed, respx_mock):
    route = title_search(respx_mock, [work(location("https://oa.example/x.pdf"), title="Apache Hudi Indexes", year=2023)])
    result = resolve(keyed, ref(entry_type="article", title="Apache Hudi Indexes", year="n.d."))
    assert result.candidates
    assert "filter" not in route.calls.last.request.url.params


@pytest.mark.respx(assert_all_called=False)
@pytest.mark.parametrize(
    "fields",
    [
        {"arxiv_id": "2504.15247"},
        {"url": "https://authors.example/iceberg.pdf"},
        {"title": None},
    ],
)
def test_title_search_is_skipped_when_not_needed(keyed, respx_mock, fields):
    reference = ref(**fields)
    resolve(keyed, reference)
    assert not any(call.request.url.host == "api.openalex.org" for call in respx_mock.calls)


def test_openalex_can_be_disabled(project, respx_mock):
    settings = make_settings(project, resolution=ResolutionFile(use_openalex=False))
    result = resolve(settings, ref(doi=DOI))
    assert result.candidates == []
    assert len(respx_mock.calls) == 0
