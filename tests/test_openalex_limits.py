"""OpenAlex refusals stop further requests, searches are not repeated, and without
a key BibVault keeps well inside the small daily budget of the IP address, which
the user's own visits to openalex.org draw on too.
"""

import json
from datetime import UTC, datetime, timedelta

import httpx

from bibvault.config import ResolutionFile
from bibvault.resolve import KEYLESS_DAILY_REQUESTS, keyless_requests_today, openalex_paused_until
from tests.helpers import (
    PDF,
    fetch,
    location,
    make_pdf,
    make_settings,
    open_library,
    openalex_doi_pattern,
    refs,
    with_key,
    work,
)

OPENALEX = r"^https://api\.openalex\.org/"
SEARCH = r"^https://api\.openalex\.org/works\?"
BUDGET_SPENT = {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "3600"}
NO_DOI = "@inproceedings{lakehouse, title = {Lakehouse}, year = {2021}}\n"


def entries(count: int) -> str:
    return "".join(
        f"@article{{paper{index}, title = {{Paper {index}}}, year = {{2020}}, doi = {{10.1000/{index}}}}}\n"
        for index in range(count)
    )


def paused_until(settings) -> datetime | None:
    library = open_library(settings)
    try:
        return openalex_paused_until(library, settings)
    finally:
        library.close()


def test_spent_budget_stops_openalex_for_the_run(settings, respx_mock):
    route = respx_mock.get(url__regex=OPENALEX).respond(429, headers=BUDGET_SPENT)
    results = fetch(settings, refs(entries(3)))
    assert route.call_count == 1
    assert {item.status for item in results.values()} == {"rate_limited"}
    assert "used up until" in results["paper2"].error
    assert "bibvault auth login openalex" in results["paper2"].error


def test_spent_budget_is_remembered_until_it_refills(settings, respx_mock):
    route = respx_mock.get(url__regex=OPENALEX).respond(429, headers=BUDGET_SPENT)
    fetch(settings, refs(entries(1)))
    results = fetch(settings, refs(entries(2)))
    assert route.call_count == 1
    assert results["paper1"].status == "rate_limited"


def test_pause_ends_when_the_budget_refills(settings, respx_mock):
    route = respx_mock.get(url__regex=OPENALEX).respond(
        429, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "0"}
    )
    fetch(settings, refs(entries(1)))
    fetch(settings, refs(entries(1)))
    assert route.call_count == 2


def test_pause_without_a_reset_header_lasts_until_midnight_utc(settings, respx_mock):
    respx_mock.get(url__regex=OPENALEX).respond(429)
    fetch(settings, refs(entries(1)))
    now = datetime.now(UTC)
    midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    until = paused_until(settings)
    assert until is not None
    assert abs(until - midnight) < timedelta(seconds=5)


def test_a_new_key_is_not_held_back_by_the_old_pause(project, respx_mock):
    route = respx_mock.get(url__regex=OPENALEX).respond(429, headers=BUDGET_SPENT)
    fetch(make_settings(project), refs(entries(1)))
    route.respond(200, json=work())
    keyed = make_settings(project, resolution=ResolutionFile(openalex_api_key="fresh-key"))
    assert fetch(keyed, refs(entries(1)))["paper0"].status == "no_open_access_found"
    assert route.call_count == 2
    assert paused_until(make_settings(project)) is not None


def test_spent_budget_of_a_key_says_so(project, respx_mock):
    keyed = make_settings(project, resolution=ResolutionFile(openalex_api_key="my-key"))
    respx_mock.get(url__regex=OPENALEX).respond(429, headers=BUDGET_SPENT)
    error = fetch(keyed, refs(entries(1)))["paper0"].error
    assert "budget of your API key is used up" in error
    assert "auth login" not in error


def test_per_second_limit_is_waited_out(settings, respx_mock, sleeps):
    route = respx_mock.get(url__regex=openalex_doi_pattern("10.1000/0"))
    route.side_effect = [
        httpx.Response(429, headers={"x-ratelimit-remaining": "0.95"}),
        httpx.Response(200, json=work()),
    ]
    assert fetch(settings, refs(entries(1)))["paper0"].status == "no_open_access_found"
    assert route.call_count == 2
    assert sleeps
    assert paused_until(settings) is None


def test_rejected_key_stops_openalex_for_the_run(project, respx_mock):
    keyed = make_settings(project, resolution=ResolutionFile(openalex_api_key="revoked"))
    route = respx_mock.get(url__regex=OPENALEX).respond(401)
    results = fetch(keyed, refs(entries(3)))
    assert route.call_count == 1
    assert results["paper2"].status == "failed"
    assert "rejected the API key" in results["paper2"].error
    assert paused_until(keyed) is None


def test_other_sources_still_work_while_openalex_is_paused(settings, respx_mock):
    respx_mock.get(url__regex=OPENALEX).respond(429, headers=BUDGET_SPENT)
    respx_mock.get("https://oa.example/paper.pdf").respond(200, content=make_pdf("Paper"), headers=PDF)
    text = "@article{paper, title = {Paper}, year = {2020}, doi = {10.1000/p}, url = {https://oa.example/paper.pdf}}"
    assert fetch(settings, refs(text))["paper"].status == "downloaded"


def age_searches(settings, days: int) -> None:
    library = open_library(settings)
    try:
        rows = library.conn.execute(
            "SELECT cache_key, payload FROM resolution_cache WHERE cache_key LIKE 'openalex:title:%'"
        ).fetchall()
        for row in rows:
            entry = json.loads(row["payload"])
            entry["searched_at"] = (datetime.now(UTC) - timedelta(days=days)).isoformat()
            library.cache_put(row["cache_key"], json.dumps(entry))
    finally:
        library.close()


def test_title_search_is_reused_for_a_week(project, respx_mock):
    keyed = make_settings(project, resolution=with_key())
    route = respx_mock.get(url__regex=SEARCH).respond(200, json={"results": []})
    assert fetch(keyed, refs(NO_DOI))["lakehouse"].status == "not_found"
    assert fetch(keyed, refs(NO_DOI))["lakehouse"].status == "not_found"
    assert route.call_count == 1

    age_searches(keyed, days=8)
    fetch(keyed, refs(NO_DOI))
    assert route.call_count == 2


def test_refused_search_is_not_remembered(project, respx_mock):
    keyed = make_settings(project, resolution=with_key())
    route = respx_mock.get(url__regex=SEARCH)
    route.side_effect = [
        httpx.Response(429, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "0"}),
        httpx.Response(200, json={"results": []}),
    ]
    assert fetch(keyed, refs(NO_DOI))["lakehouse"].status == "rate_limited"
    assert fetch(keyed, refs(NO_DOI))["lakehouse"].status == "not_found"
    assert route.call_count == 2


def set_keyless_used(settings, used: int, day: str | None = None) -> None:
    library = open_library(settings)
    try:
        day = day or datetime.now(UTC).date().isoformat()
        library.cache_put("openalex:keyless", json.dumps({"day": day, "used": used}))
    finally:
        library.close()


def keyless_used(settings) -> int:
    library = open_library(settings)
    try:
        return keyless_requests_today(library)
    finally:
        library.close()


def test_without_a_key_titles_are_not_searched(settings, respx_mock):
    result = fetch(settings, refs(NO_DOI))["lakehouse"]
    assert not respx_mock.calls
    assert result.status == "rate_limited"
    assert "needs an OpenAlex API key" in result.error
    assert "bibvault auth login openalex" in result.error


def preprint_only(respx_mock):
    """OpenAlex knows only a preprint of the paper."""

    route = respx_mock.get(url__regex=OPENALEX).respond(
        200, json=work(location("https://oa.example/0.pdf", "submittedVersion"))
    )
    respx_mock.get("https://oa.example/0.pdf").respond(200, content=make_pdf("Paper 0"), headers=PDF)
    return route


def test_without_a_key_papers_already_here_are_not_looked_up(settings, respx_mock):
    route = preprint_only(respx_mock)
    assert fetch(settings, refs(entries(1)))["paper0"].status == "downloaded"
    assert fetch(settings, refs(entries(1)))["paper0"].status == "already_exists"
    assert route.call_count == 1


def test_with_a_key_papers_already_here_are_not_looked_up_either(project, respx_mock):
    keyed = make_settings(project, resolution=with_key())
    route = preprint_only(respx_mock)
    fetch(keyed, refs(entries(1)))
    assert fetch(keyed, refs(entries(1)))["paper0"].status == "already_exists"
    assert route.call_count == 1


def test_without_a_key_requests_are_counted(settings, respx_mock):
    respx_mock.get(url__regex=OPENALEX).respond(200, json=work())
    fetch(settings, refs(entries(3)))
    assert keyless_used(settings) == 3


def test_with_a_key_requests_are_not_counted(project, respx_mock):
    keyed = make_settings(project, resolution=with_key())
    respx_mock.get(url__regex=OPENALEX).respond(200, json=work())
    fetch(keyed, refs(entries(3)))
    assert keyless_used(keyed) == 0


def test_daily_allowance_without_a_key(settings, respx_mock):
    set_keyless_used(settings, KEYLESS_DAILY_REQUESTS - 1)
    route = respx_mock.get(url__regex=OPENALEX).respond(200, json=work())
    results = fetch(settings, refs(entries(3)))
    assert route.call_count == 1
    assert results["paper0"].status == "no_open_access_found"
    assert results["paper2"].status == "rate_limited"
    assert f"{KEYLESS_DAILY_REQUESTS} OpenAlex requests a day without an API key" in results["paper2"].error

    fetch(settings, refs(entries(4)))
    assert route.call_count == 1


def test_daily_allowance_starts_over_each_day(settings, respx_mock):
    yesterday = (datetime.now(UTC) - timedelta(days=1)).date().isoformat()
    set_keyless_used(settings, KEYLESS_DAILY_REQUESTS, day=yesterday)
    route = respx_mock.get(url__regex=OPENALEX).respond(200, json=work())
    fetch(settings, refs(entries(1)))
    assert route.call_count == 1
    assert keyless_used(settings) == 1


def test_keyless_limits_are_explained_once_per_run(settings, respx_mock, caplog):
    respx_mock.get(url__regex=OPENALEX).respond(200, json=work())
    fetch(settings, refs(entries(3) + NO_DOI))
    notes = [record for record in caplog.records if "no OpenAlex API key" in record.getMessage()]
    assert len(notes) == 1
    assert "bibvault auth login openalex" in notes[0].getMessage()
