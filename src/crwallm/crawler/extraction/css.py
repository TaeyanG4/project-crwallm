"""CSS extraction over selectolax.

Pulls three things out of a page, all of which the crawl needs:

* **records** - a container selector repeated N times, fields selected within
* **links** - ``<a href>``, the frontier's input
* **canonical** - ``<link rel="canonical">``, the cheapest duplicate signal
  there is, and the one that costs a fetch to learn

**Why selectolax.** BeautifulSoup's wrapper overhead is 5-15x on CSS selection
(docs/12_PERFORMANCE.md). Over a million pages that is the difference between
a crawl that finishes overnight and one that does not. lxml stays available for
the XPath cases selectolax cannot express.

**Parsing blocks the loop.** It is CPU-bound, so with hundreds of fetches in
flight it will stall the event loop and make the concurrency notional. The
offload decision belongs to the caller - measure before reaching for a process
pool - so everything here is synchronous and free of I/O.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

from selectolax.lexbor import LexborHTMLParser, LexborNode

from crwallm.crawler.contracts import ExtractionResult, FetchResponse
from crwallm.crawler.extraction.decoding import decode_html
from crwallm.crawler.extraction.transforms import apply_chain

__all__ = ["CssExtractor", "FieldSpec", "extract_links", "parse"]

type FieldType = Literal["text", "html", "href", "src", "attr", "json"]

# Anything that is not a page. Following these wastes a fetch each and they are
# never HTML.
_NON_PAGE_SCHEMES = ("javascript:", "mailto:", "tel:", "data:", "sms:", "callto:")
_NON_PAGE_SUFFIXES = (
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".svg",
    ".ico",
    ".bmp",
    ".css",
    ".js",
    ".woff",
    ".woff2",
    ".ttf",
    ".eot",
    ".zip",
    ".gz",
    ".tar",
    ".rar",
    ".7z",
    ".exe",
    ".dmg",
    ".mp3",
    ".mp4",
    ".avi",
    ".mov",
    ".wmv",
    ".flv",
    ".webm",
)


@dataclass(frozen=True, slots=True)
class FieldSpec:
    name: str
    selector: str
    type: FieldType = "text"
    attr: str | None = None
    """Required when ``type`` is ``attr``."""
    transform: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CssSpec:
    """What to pull off one page."""

    container: str | None = None
    """When set, one record per match. When absent, the page yields a single
    record - the detail-page shape."""

    fields: tuple[FieldSpec, ...] = ()
    link_selector: str = "a[href]"
    follow_links: bool = True


def parse(response: FetchResponse) -> tuple[LexborHTMLParser, str]:
    """Decode and parse, returning the tree and the text it came from."""
    doc = decode_html(response.body, response.content_type)
    return LexborHTMLParser(doc.text), doc.text


def _node_value(node: LexborNode, spec: FieldSpec) -> Any:
    match spec.type:
        case "html":
            return node.html
        case "href":
            return node.attributes.get("href")
        case "src":
            # Lazy-loaded images keep the real URL in data-src and put a
            # placeholder in src, so prefer the former when both exist.
            attrs = node.attributes
            return attrs.get("data-src") or attrs.get("src")
        case "attr":
            return node.attributes.get(spec.attr or "") if spec.attr else None
        case _:
            # "text", and anything a hand-written recipe made up. Field types
            # arrive from YAML, so the fallback has to be real.
            return node.text(deep=True, strip=True)


def _extract_field(scope: LexborNode, spec: FieldSpec, base_url: str) -> Any:
    """One field, from the first node matching its selector.

    An unmatched selector produces ``None`` rather than an error: a missing
    field is data (it feeds the fill-rate metric that gates recipe activation
    in Phase 3), whereas an exception would lose the whole record.
    """
    # default=None, strict=False is load-bearing. selectolax's stub declares
    # its first overload with `strict: Literal[True] = ...`, so a bare call
    # matches it and appears to return a non-optional node - which makes the
    # None check below read as dead code. Passing strict explicitly selects
    # the overload that matches runtime behaviour.
    node = scope.css_first(spec.selector, default=None, strict=False) if spec.selector else scope
    raw = _node_value(node, spec) if node is not None else None
    if spec.transform:
        return apply_chain(raw, spec.transform, base_url=base_url)
    return raw.strip() if isinstance(raw, str) else raw


def extract_links(
    tree: LexborHTMLParser, base_url: str, selector: str = "a[href]"
) -> tuple[str, ...]:
    """Hrefs worth queueing.

    Only obvious non-pages are filtered here - scheme and file extension.
    Scope, traps and duplicates are the gate's business
    (docs/05_SPIDER_ARCHITECTURE.md); this is about not spending a normalise
    call on ``mailto:``.
    """
    out: list[str] = []
    seen: set[str] = set()
    for node in tree.css(selector):
        href = node.attributes.get("href")
        if not href:
            continue
        href = href.strip()
        if not href or href.startswith("#"):
            continue
        lowered = href.lower()
        if lowered.startswith(_NON_PAGE_SCHEMES):
            continue
        path = lowered.split("?", 1)[0].split("#", 1)[0]
        if path.endswith(_NON_PAGE_SUFFIXES):
            continue
        if href in seen:
            continue
        seen.add(href)
        out.append(href)
    return tuple(out)


def extract_canonical(tree: LexborHTMLParser) -> str | None:
    node = tree.css_first('link[rel="canonical"]', default=None, strict=False)
    if node is None:
        return None
    href = node.attributes.get("href")
    return href.strip() if href else None


@dataclass(slots=True)
class CssExtractor:
    """``Extractor`` for HTML with a declared CSS shape."""

    spec: CssSpec = field(default_factory=CssSpec)
    name: str = "css"

    def supports(self, response: FetchResponse) -> bool:
        content_type = response.content_type or ""
        return content_type.startswith("text/html") or content_type.endswith("+xml")

    def extract(self, response: FetchResponse) -> ExtractionResult:
        tree, _ = parse(response)
        base_url = response.final_url or response.url.url

        records: list[dict[str, Any]] = []
        if self.spec.fields:
            scopes = (
                tree.css(self.spec.container)
                if self.spec.container
                else ([tree.root] if tree.root is not None else [])
            )
            for scope in scopes:
                record = {f.name: _extract_field(scope, f, base_url) for f in self.spec.fields}
                # A record where nothing matched is noise, not a row. Dropping
                # it keeps the fill-rate metric meaningful.
                if any(v is not None and v != "" for v in record.values()):
                    records.append(record)

        links = (
            extract_links(tree, base_url, self.spec.link_selector) if self.spec.follow_links else ()
        )

        return ExtractionResult(
            extractor=self.name,
            records=tuple(records),
            links=links,
            canonical_url=extract_canonical(tree),
            text=_visible_text(tree),
            content_hash=None,
        )


_WS = re.compile(r"\s+")


def _visible_text(tree: LexborHTMLParser) -> str | None:
    """Rough page text.

    Enough for content-level duplicate detection and for a human skimming
    results. Proper article extraction is trafilatura's job in Phase 6.
    """
    body = tree.body
    if body is None:
        return None
    for tag in ("script", "style", "noscript", "template"):
        for node in body.css(tag):
            node.decompose()
    text = body.text(deep=True, separator=" ", strip=True)
    return _WS.sub(" ", text).strip() or None
