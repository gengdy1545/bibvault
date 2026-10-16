"""Reading-order HTML extraction for publisher program pages."""

import logging
from difflib import SequenceMatcher
from html.parser import HTMLParser
from typing import NamedTuple
from urllib.parse import urljoin

import httpx

from bibvault.models import Reference
from bibvault.resolve.textutil import normalize_title

logger = logging.getLogger(__name__)


class Link(NamedTuple):
    url: str
    text: str


class Page(HTMLParser):
    """The text and links of an HTML page in reading order, with its meta tags and frames."""

    def __init__(self, base: str) -> None:
        super().__init__()
        self.base = base
        self.parts: list[str | Link] = []
        self.meta: dict[str, str] = {}
        self.frames: list[str] = []
        self._href: str | None = None
        self._label: list[str] = []
        self._hidden: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag in {"script", "style"}:
            self._hidden = tag
        elif tag == "a" and values.get("href"):
            self._href = urljoin(self.base, values["href"] or "")
            self._label = []
        elif tag == "meta" and values.get("name") and values.get("content"):
            self.meta.setdefault(values["name"] or "", values["content"] or "")
        elif tag in {"frame", "iframe"} and values.get("src"):
            self.frames.append(urljoin(self.base, values["src"] or ""))

    def handle_endtag(self, tag: str) -> None:
        if tag == self._hidden:
            self._hidden = None
        elif tag == "a" and self._href is not None:
            self.parts.append(Link(self._href, " ".join("".join(self._label).split())))
            self._href = None

    def handle_data(self, data: str) -> None:
        if self._hidden:
            return
        if self._href is None:
            self.parts.append(data)
        else:
            self._label.append(data)


async def fetch_page(client: httpx.AsyncClient, url: str) -> Page | None:
    try:
        response = await client.get(url)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        logger.warning("%s unavailable: %s", url, exc.__class__.__name__)
        return None
    page = Page(str(response.url))
    page.feed(response.text)
    page.close()
    return page


def pick_link(reference: Reference, links: list[Link], threshold: float) -> str | None:
    """The link whose text is the title, or contains it when titles share a block with authors."""

    wanted = normalize_title(reference.title)
    best, best_score = None, threshold
    for link in links:
        text = normalize_title(link.text)
        score = 1.0 if wanted and wanted in text else SequenceMatcher(None, wanted, text).ratio()
        if score >= best_score:
            best, best_score = link.url, score
    return best


def plain_text(parts: list[str | Link]) -> str:
    return " ".join(part for part in parts if isinstance(part, str))
