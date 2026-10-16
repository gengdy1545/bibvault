"""Version ranking and direct-PDF checks used while choosing a candidate."""

from urllib.parse import urlparse

from bibvault.config import Settings
from bibvault.models import Candidate


def is_pdf_url(url: str | None) -> bool:
    if not url:
        return False
    return urlparse(url).path.lower().endswith(".pdf")


def map_version(value: str | None) -> str:
    token = (value or "").lower()
    if "published" in token:
        return "published"
    if "accept" in token:
        return "accepted"
    if "submit" in token or "preprint" in token:
        return "preprint"
    return "unknown"


def version_ranks(settings: Settings) -> dict[str, int]:
    """Lower rank is the better version. ``fetch --upgrade`` replaces only a strictly better rank."""

    ranks = {
        "published": 0,
        "provided": 0,
        "unknown": 1,
        "accepted": 2,
        "preprint": 3,
        "archived": 4,
    }
    if not settings.resolution.prefer_published_version:
        ranks["preprint"] = 0
        ranks["published"] = 3
    return ranks


def sort_candidates(candidates: list[Candidate], settings: Settings) -> list[Candidate]:
    ranks = version_ranks(settings)
    selected: list[Candidate] = []
    seen: set[str] = set()
    for candidate in candidates:
        if (
            candidate.version == "preprint"
            and not settings.resolution.allow_preprints
            and candidate.provider != "direct"
        ):
            continue
        if candidate.url in seen:
            continue
        seen.add(candidate.url)
        selected.append(candidate)
    selected.sort(key=lambda item: (item.source_rank, ranks.get(item.version, 9), item.url))
    return selected
