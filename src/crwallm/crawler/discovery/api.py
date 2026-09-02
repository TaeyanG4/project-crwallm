"""Other ways into a site than its HTML.

A modern page ships a megabyte of markup wrapped around data that came from
somewhere smaller and cleaner. Finding that source is worth more than any
parsing improvement: measured on react.dev, the rendered page is 1.4MB and the
JSON it was built from is 30KB, with values that are already numbers.

**Only what was measured works here.** Scraping API URLs out of inline
JavaScript is the obvious idea and it found nothing on any of the four sites
tried - modern bundles do not contain literal endpoint strings. What did work
was narrower and more reliable:

* Next.js publishes a ``buildId`` and serves every page's props at a derivable
  URL. Two of four sites, both hits.
* ``<link rel="alternate">`` names feeds and JSON representations. This is a
  standard, so it is right when it is present rather than a guess.

Framework conventions that can only be probed rather than derived are listed
but not requested speculatively: a crawl that fires eight 404s at every host it
meets is rude and slow, and this project's one concession to politeness is
rate limiting (docs/17_NON_GOALS.md).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlsplit

from selectolax.lexbor import LexborHTMLParser

__all__ = [
    "WELL_KNOWN_API_PATHS",
    "Endpoint",
    "PaginationStyle",
    "detect_pagination",
    "discover_endpoints",
    "next_data_url",
    "next_page_url",
]


@dataclass(frozen=True, slots=True)
class Endpoint:
    """A URL that returns data rather than a page."""

    url: str
    kind: str
    """``next_data``, ``feed``, ``json_alternate``."""

    confidence: str
    """``derived`` when the site published the information needed to build
    this URL, ``declared`` when the site named it outright. Never a guess -
    speculative URLs are listed separately and only fetched on request."""

    title: str | None = None


WELL_KNOWN_API_PATHS = (
    "/wp-json/wp/v2/posts",
    "/products.json",
    "/api/v1/",
    "/feed",
    "/rss",
)
"""Conventions worth *offering*, never fetched without being asked.

Each is one platform's default: WordPress, Shopify, and the two feed paths
almost every CMS answers on. Probing all of them at every host would mean
several 404s per site for one occasional hit."""


def next_data_url(page_url: str, build_id: str) -> str | None:
    """Where Next.js serves the props for ``page_url``.

    The client-side router fetches these on navigation, so they exist for
    every page the site renders - and they hold the same data as the HTML
    without the markup around it.

    The path mapping is Next.js's own: ``/learn`` becomes ``learn.json``, and
    the site root becomes ``index.json`` because an empty segment is not a
    filename.
    """
    if not build_id:
        return None

    parts = urlsplit(page_url)
    path = parts.path.strip("/")
    leaf = f"{path}.json" if path else "index.json"
    return f"{parts.scheme}://{parts.netloc}/_next/data/{build_id}/{leaf}"


_FEED_TYPES = {
    "application/rss+xml": "feed",
    "application/atom+xml": "feed",
    "application/feed+json": "feed",
    "application/json": "json_alternate",
}


def discover_endpoints(
    tree: LexborHTMLParser,
    page_url: str,
    *,
    embedded: dict[str, Any] | None = None,
) -> tuple[Endpoint, ...]:
    """Every data URL this page tells us about.

    Declared first, derived second. Both are things the site published; what
    is deliberately absent is anything guessed from a URL's shape.
    """
    found: list[Endpoint] = []
    seen: set[str] = set()

    def add(endpoint: Endpoint) -> None:
        if endpoint.url not in seen:
            seen.add(endpoint.url)
            found.append(endpoint)

    for node in tree.css('link[rel="alternate"]'):
        attrs = node.attributes
        href = (attrs.get("href") or "").strip()
        kind = _FEED_TYPES.get((attrs.get("type") or "").strip().lower())
        if not href or kind is None:
            continue
        add(
            Endpoint(
                url=urljoin(page_url, href),
                kind=kind,
                confidence="declared",
                title=(attrs.get("title") or "").strip() or None,
            )
        )

    build_id = _build_id(embedded or {})
    if build_id:
        derived = next_data_url(page_url, build_id)
        if derived:
            add(Endpoint(url=derived, kind="next_data", confidence="derived"))

    return tuple(found)


def _build_id(embedded: dict[str, Any]) -> str | None:
    blob = embedded.get("__NEXT_DATA__")
    if isinstance(blob, dict):
        value = blob.get("buildId")
        if isinstance(value, str) and value:
            return value
    return None


# --------------------------------------------------------------- pagination


@dataclass(frozen=True, slots=True)
class PaginationStyle:
    """How a JSON endpoint says there is more.

    Named rather than inferred at each step, because the answer decides
    whether the crawl can stop: an offset scheme runs off the end and returns
    an empty page, while a cursor scheme simply stops issuing one.
    """

    kind: str
    """``link_header``, ``next_url``, ``cursor``, ``page``, ``offset``, or
    ``none``."""

    next_url: str | None = None
    """Ready to fetch, when the response gave one outright."""

    param: str | None = None
    """The query parameter to advance, for the styles that need building."""

    value: Any = None
    """What to set it to for the next page."""

    total: int | None = None


_NEXT_URL_KEYS = ("next", "next_page", "nextPage", "next_url", "nextUrl", "next_href")
_CURSOR_KEYS = ("next_cursor", "nextCursor", "cursor", "after", "endCursor", "continuation")
_PAGE_KEYS = ("page", "page_number", "pageNumber", "current_page", "currentPage")
_OFFSET_KEYS = ("offset", "start", "skip", "from")
_TOTAL_KEYS = ("total", "total_count", "totalCount", "totalResults", "count")

_LINK_NEXT = re.compile(r'<([^>]+)>\s*;\s*rel\s*=\s*"?next"?', re.IGNORECASE)


def detect_pagination(
    payload: Any,
    *,
    headers: dict[str, str] | None = None,
    request_url: str | None = None,
) -> PaginationStyle:
    """Read a JSON response and say how to get the next page.

    Checked in order of how much they can be trusted. A ``Link`` header is
    RFC 8288 and means exactly one thing. A ``next`` field holding a URL is
    the API telling us outright. A cursor is explicit but has to be placed in
    a parameter. Page and offset numbers are last because they are inferred
    from names, and a field called ``page`` might be describing the content.
    """
    link = (headers or {}).get("link") or (headers or {}).get("Link")
    if link:
        match = _LINK_NEXT.search(link)
        if match:
            url = match.group(1).strip()
            return PaginationStyle(
                kind="link_header",
                next_url=urljoin(request_url, url) if request_url else url,
            )

    envelope = _envelope(payload)
    if envelope is None:
        return PaginationStyle(kind="none")

    total = _first_int(envelope, _TOTAL_KEYS)

    for key in _NEXT_URL_KEYS:
        value = envelope.get(key)
        if isinstance(value, str) and value.strip():
            return PaginationStyle(
                kind="next_url",
                next_url=urljoin(request_url, value) if request_url else value,
                total=total,
            )
        # Some APIs nest it: {"links": {"next": "..."}}.
        if isinstance(value, dict):
            nested = value.get("href") or value.get("url")
            if isinstance(nested, str) and nested.strip():
                return PaginationStyle(
                    kind="next_url",
                    next_url=urljoin(request_url, nested) if request_url else nested,
                    total=total,
                )

    for key in _CURSOR_KEYS:
        value = envelope.get(key)
        if isinstance(value, str) and value.strip():
            return PaginationStyle(kind="cursor", param=key, value=value, total=total)

    for key in _PAGE_KEYS:
        value = envelope.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return PaginationStyle(kind="page", param=key, value=value + 1, total=total)

    for key in _OFFSET_KEYS:
        value = envelope.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            size = _page_size(envelope)
            if size:
                return PaginationStyle(kind="offset", param=key, value=value + size, total=total)

    return PaginationStyle(kind="none", total=total)


def _first_int(envelope: dict[str, Any], keys: tuple[str, ...]) -> int | None:
    """The first of ``keys`` holding a real integer.

    ``bool`` is excluded because it is an ``int`` in Python and a field called
    ``count`` holding ``True`` means something other than one.
    """
    for key in keys:
        value = envelope.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def _envelope(payload: Any) -> dict[str, Any] | None:
    """The object carrying the paging fields.

    Usually the top level, but a bare array is a complete answer with no
    envelope at all, and some APIs put everything under ``meta`` or
    ``pagination``. Merged shallowest-first so a top-level ``next`` wins over
    a nested one.
    """
    if not isinstance(payload, dict):
        return None

    merged: dict[str, Any] = {}
    for key in ("pagination", "meta", "paging", "page_info", "pageInfo"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            merged.update(nested)

    # JSON:API and HAL both hang paging off a `links` object rather than the
    # top level, so its members are lifted to where the lookups below expect
    # them. Only into empty slots: a top-level `next` is the more direct
    # statement and wins.
    links = payload.get("links")
    if isinstance(links, dict):
        for key, value in links.items():
            merged.setdefault(key, value)

    merged.update({k: v for k, v in payload.items() if not isinstance(v, dict | list)})

    next_value = payload.get("next")
    if isinstance(next_value, dict):
        merged["next"] = next_value
    return merged


def _page_size(envelope: dict[str, Any]) -> int | None:
    for key in ("limit", "per_page", "perPage", "page_size", "pageSize", "size"):
        value = envelope.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
    return None


def next_page_url(current_url: str, style: PaginationStyle) -> str | None:
    """Build the URL for the next page, or None when there is not one.

    A style that already carries a URL is used as it is; the rest are the
    current URL with one parameter replaced, which keeps every filter and sort
    the caller had set.
    """
    if style.next_url:
        return style.next_url
    if style.kind in {"none", "link_header"} or not style.param:
        return None

    from urllib.parse import parse_qsl, urlencode, urlunsplit

    parts = urlsplit(current_url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != style.param]
    query.append((style.param, str(style.value)))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))
