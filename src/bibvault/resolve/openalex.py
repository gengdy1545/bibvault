"""OpenAlex lookups, cache, and daily budget limits."""

import asyncio
import hashlib
import json
import logging
from datetime import UTC, datetime, timedelta
from difflib import SequenceMatcher

import httpx
from pydantic import ValidationError

from bibvault.config import Settings
from bibvault.models import Candidate, Reference
from bibvault.network import make_client
from bibvault.resolve.models import Lookup, OpenAlexUnavailable
from bibvault.resolve.ranking import is_pdf_url, map_version
from bibvault.resolve.textutil import normalize_title
from bibvault.storage import Library

logger = logging.getLogger(__name__)

_STOPPED = "openalex:stopped"
_PAUSED = "openalex:paused"
_KEYLESS = "openalex:keyless"
_KEYLESS_NOTED = "openalex:keyless-noted"
_SEARCH_CACHE_DAYS = 7
# About a tenth of what OpenAlex gives an IP address without a key, leaving the rest
# for the user's browser and anyone sharing the address.
KEYLESS_DAILY_REQUESTS = 100
_LOGIN_HINT = "run `bibvault auth login openalex` (keys are free)"


async def openalex_doi(
    reference: Reference,
    client: httpx.AsyncClient,
    library: Library,
    settings: Settings,
    memory: dict[str, object],
) -> Lookup:
    cache_key = f"openalex:doi:{reference.doi}"
    known = memory.get(cache_key)
    if isinstance(known, Lookup):
        return known
    cached = read_cache(library, cache_key)
    if cached is not None and cache_is_enough(cached, settings):
        memory[cache_key] = cached
        return cached
    url = f"https://api.openalex.org/works/https://doi.org/{reference.doi}"
    payload = await openalex_get(client, url, mailto(settings), library, settings, memory)
    lookup = Lookup() if payload is None else lookup_from_work(payload)
    library.cache_put(cache_key, lookup.model_dump_json())
    memory[cache_key] = lookup
    return lookup


async def openalex_title(
    reference: Reference,
    client: httpx.AsyncClient,
    library: Library,
    settings: Settings,
    memory: dict[str, object],
) -> Lookup:
    # DOI lookups are free, but every search is billed against the daily budget.
    cache_key = f"openalex:title:{reference.year or ''}:{normalize_title(reference.title)}"
    known = memory.get(cache_key)
    if isinstance(known, Lookup):
        return known
    lookup = recent_search(library, cache_key)
    if lookup is None:
        if not key_id(settings):
            note_keyless(memory)
            raise OpenAlexUnavailable(
                f"finding a paper without a DOI by its title needs an OpenAlex API key; {_LOGIN_HINT}",
                rate_limited=True,
            )
        lookup = await search_title(reference, client, library, settings, memory)
        entry = {"searched_at": datetime.now(UTC).isoformat(), "lookup": lookup.model_dump(mode="json")}
        library.cache_put(cache_key, json.dumps(entry))
    memory[cache_key] = lookup
    return lookup


def recent_search(library: Library, cache_key: str) -> Lookup | None:
    raw = library.cache_get(cache_key)
    if raw is None:
        return None
    try:
        entry = json.loads(raw)
        searched_at = datetime.fromisoformat(entry["searched_at"])
        lookup = Lookup.model_validate(entry["lookup"])
    except (ValueError, KeyError, TypeError):
        return None
    if datetime.now(UTC) - searched_at > timedelta(days=_SEARCH_CACHE_DAYS):
        return None
    return lookup


async def search_title(
    reference: Reference,
    client: httpx.AsyncClient,
    library: Library,
    settings: Settings,
    memory: dict[str, object],
) -> Lookup:
    params: dict[str, str] = {"search": reference.title or "", "per-page": "1"}
    if reference.year and reference.year.isdigit():
        params["filter"] = f"publication_year:{reference.year}"
    params.update(mailto(settings))
    payload = await openalex_get(client, "https://api.openalex.org/works", params, library, settings, memory)
    results = (payload or {}).get("results") or []
    if not results:
        return Lookup()
    work = results[0]
    if not isinstance(work, dict):
        return Lookup()
    if not title_matches(reference, work, settings.resolution.title_match_threshold):
        return Lookup()
    return lookup_from_work(work)


def title_matches(reference: Reference, work: dict, threshold: float) -> bool:
    left = normalize_title(reference.title)
    right = normalize_title(str(work.get("display_name") or ""))
    if not left or not right:
        return False
    if SequenceMatcher(None, left, right).ratio() < threshold:
        return False
    ours = (reference.year or "").strip()
    theirs = str(work.get("publication_year") or "")
    if ours.isdigit() and theirs.isdigit():
        return ours == theirs
    return True


def lookup_from_work(work: dict) -> Lookup:
    locations: list[dict] = []
    best = work.get("best_oa_location")
    if isinstance(best, dict):
        locations.append(best)
    for location in work.get("locations") or []:
        if isinstance(location, dict):
            locations.append(location)
    found: list[Candidate] = []
    seen: set[str] = set()
    for location in locations:
        pdf_url = location.get("pdf_url")
        if not isinstance(pdf_url, str) or not pdf_url or pdf_url in seen:
            continue
        seen.add(pdf_url)
        version = location.get("version")
        found.append(
            Candidate(
                url=pdf_url,
                provider="openalex",
                version=map_version(version if isinstance(version, str) else None),
                source_rank=1,
            )
        )
    open_access = work.get("open_access")
    if not isinstance(open_access, dict):
        open_access = {}
    oa_url = open_access.get("oa_url")
    if isinstance(oa_url, str) and oa_url not in seen and is_pdf_url(oa_url):
        found.append(Candidate(url=oa_url, provider="openalex", version="unknown", source_rank=1))

    landing_url = None
    if not found and open_access.get("is_oa"):
        landing = best.get("landing_page_url") if isinstance(best, dict) else None
        landing_url = landing if isinstance(landing, str) and landing else oa_url
        if not isinstance(landing_url, str):
            landing_url = None
    return Lookup(found=True, candidates=found, landing_url=landing_url)


def cache_is_enough(lookup: Lookup, settings: Settings) -> bool:
    if not lookup.candidates:
        return False
    if not settings.resolution.prefer_published_version:
        return True
    return any(item.version == "published" for item in lookup.candidates)


def read_cache(library: Library, cache_key: str) -> Lookup | None:
    raw = library.cache_get(cache_key)
    if raw is None:
        return None
    try:
        return Lookup.model_validate_json(raw)
    except ValidationError:
        return None


async def openalex_get(
    client: httpx.AsyncClient,
    url: str,
    params: dict[str, str],
    library: Library,
    settings: Settings,
    memory: dict[str, object],
) -> dict | None:
    """GET from OpenAlex unless it refused earlier in this run or its daily budget is still spent."""

    stopped = memory.get(_STOPPED)
    if stopped is None:
        until = openalex_paused_until(library, settings)
        if until is not None:
            stopped = memory[_STOPPED] = (budget_message(until, settings), True)
            tell(memory, stopped[0])
    if isinstance(stopped, tuple):
        raise OpenAlexUnavailable(stopped[0], rate_limited=stopped[1])
    if not key_id(settings):
        note_keyless(memory)
        spend_keyless_request(library, memory)
    try:
        return await get_json(client, url, params, api_key_header(settings))
    except httpx.HTTPStatusError as exc:
        refusal = refused(exc.response, settings)
        if refusal is None:
            raise
        message, rate_limited, until = refusal
        if _STOPPED not in memory:
            memory[_STOPPED] = (message, rate_limited)
            if until is not None:
                pause = {"until": until.isoformat(), "key": key_id(settings)}
                library.cache_put(_PAUSED, json.dumps(pause))
            tell(memory, message)
        raise OpenAlexUnavailable(message, rate_limited=rate_limited) from exc


def tell(memory: dict[str, object], message: str) -> None:
    notify = memory.get("notify")
    if callable(notify):
        notify(message)
    else:
        logger.warning("%s", message)


def note_keyless(memory: dict[str, object]) -> None:
    if _KEYLESS_NOTED in memory:
        return
    memory[_KEYLESS_NOTED] = True
    tell(
        memory,
        "no OpenAlex API key: BibVault sends OpenAlex at most "
        f"{KEYLESS_DAILY_REQUESTS} requests a day, only for papers "
        f"without a local PDF, and no title searches; {_LOGIN_HINT}",
    )


def spend_keyless_request(library: Library, memory: dict[str, object]) -> None:
    used = keyless_requests_today(library)
    if used >= KEYLESS_DAILY_REQUESTS:
        at = next_utc_midnight().astimezone().strftime("%H:%M")
        message = (
            f"BibVault's {KEYLESS_DAILY_REQUESTS} OpenAlex requests a day without an API key "
            f"are used up until {at}; {_LOGIN_HINT}"
        )
        memory[_STOPPED] = (message, True)
        tell(memory, message)
        raise OpenAlexUnavailable(message, rate_limited=True)
    today = datetime.now(UTC).date().isoformat()
    library.cache_put(_KEYLESS, json.dumps({"day": today, "used": used + 1}))


def keyless_requests_today(library: Library) -> int:
    """OpenAlex requests sent without a key since midnight UTC."""

    raw = library.cache_get(_KEYLESS)
    if raw is None:
        return 0
    try:
        entry = json.loads(raw)
        if entry["day"] != datetime.now(UTC).date().isoformat():
            return 0
        return int(entry["used"])
    except (ValueError, KeyError, TypeError):
        return 0


def openalex_paused_until(library: Library, settings: Settings) -> datetime | None:
    """When the daily budget that the current key (or no key) used up refills."""

    raw = library.cache_get(_PAUSED)
    if raw is None:
        return None
    try:
        pause = json.loads(raw)
        until = datetime.fromisoformat(pause["until"])
        key = pause["key"]
    except (ValueError, KeyError, TypeError):
        return None
    if key != key_id(settings) or until <= datetime.now(UTC):
        return None
    return until


def refused(response: httpx.Response, settings: Settings) -> tuple[str, bool, datetime | None] | None:
    """Message, rate-limited flag, and pause end for answers that will repeat for every request."""

    if response.status_code == 401:
        if key_id(settings):
            return "OpenAlex rejected the API key; run `bibvault auth login openalex` to replace it", False, None
        return "OpenAlex asks for an API key; run `bibvault auth login openalex` (keys are free)", False, None
    if response.status_code != 429:
        return None
    if per_second_limit(response):
        return "OpenAlex kept answering HTTP 429 (too many requests per second)", True, None
    until = datetime.now(UTC) + timedelta(seconds=reset_seconds(response))
    return budget_message(until, settings), True, until


def per_second_limit(response: httpx.Response) -> bool:
    # A 429 with budget left means too many requests per second, which a short wait cures.
    try:
        return float(response.headers["x-ratelimit-remaining"]) > 0
    except (KeyError, ValueError):
        return False


def reset_seconds(response: httpx.Response) -> float:
    try:
        return max(0.0, float(response.headers["x-ratelimit-reset"]))
    except (KeyError, ValueError):
        return (next_utc_midnight() - datetime.now(UTC)).total_seconds()


def next_utc_midnight() -> datetime:
    # OpenAlex budgets refill at midnight UTC.
    tomorrow = datetime.now(UTC) + timedelta(days=1)
    return tomorrow.replace(hour=0, minute=0, second=0, microsecond=0)


def budget_message(until: datetime, settings: Settings) -> str:
    at = until.astimezone().strftime("%H:%M")
    if key_id(settings):
        return f"OpenAlex daily budget of your API key is used up until {at}"
    return (
        f"OpenAlex daily budget for requests without an API key is used up until {at}; "
        "run `bibvault auth login openalex` for a budget of your own (free)"
    )


def key_id(settings: Settings) -> str:
    # Tells keys apart without storing one.
    key = (settings.resolution.openalex_api_key or "").strip()
    return hashlib.sha256(key.encode()).hexdigest()[:12] if key else ""


async def check_openalex_key(key: str, settings: Settings) -> tuple[bool, str]:
    """Whether OpenAlex accepts the key, and what is left of today's budget if it says."""

    async with make_client(settings) as client:
        response = await client.get(
            "https://api.openalex.org/rate-limit", headers={"Authorization": f"Bearer {key}"}
        )
    if response.status_code in (401, 403):
        return False, ""
    response.raise_for_status()
    try:
        limits = response.json()["rate_limit"]
        left, total = float(limits["daily_remaining_usd"]), float(limits["daily_budget_usd"])
    except (ValueError, KeyError, TypeError):
        return True, ""
    return True, f"today's OpenAlex budget: ${left:.2f} of ${total:.2f} left"


async def get_json(
    client: httpx.AsyncClient, url: str, params: dict[str, str], headers: dict[str, str]
) -> dict | None:
    delay = 0.5
    for attempt in range(3):
        response = await client.get(url, params=params, headers=headers)
        if response.status_code == 404:
            return None
        retry = response.status_code >= 500 or (
            response.status_code == 429 and per_second_limit(response)
        )
        if retry and attempt < 2:
            await asyncio.sleep(delay)
            delay *= 2
            continue
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            return None
        return payload
    return None


def mailto(settings: Settings) -> dict[str, str]:
    email = (settings.resolution.email or "").strip()
    if not email:
        return {}
    return {"mailto": email}


def api_key_header(settings: Settings) -> dict[str, str]:
    # A header, not ?api_key=: httpx logs request URLs, and the key must stay out of logs.
    key = (settings.resolution.openalex_api_key or "").strip()
    if not key:
        return {}
    return {"Authorization": f"Bearer {key}"}
