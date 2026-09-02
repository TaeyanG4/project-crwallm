"""Document shapes that are not a listing page.

Three of them, each already structured in its own way and each currently
falling through to the CSS detector, which is the wrong tool:

**Feeds** are a listing with the schema already written down. Hacker News
declares one, and reading it gives titles, links and timestamps for one
request instead of parsing thirty-one table rows.

**Tables** carry their own column names in ``<th>``. The CSS detector can find
a table's rows, but it has to guess at what the columns *are*, and the header
row is the answer sitting right there.

**Articles** have no repeating structure at all. The detector correctly finds
nothing, and "nothing" is the wrong answer for a page whose whole point is one
body of text - the unstructured third of "정형·반정형·비정형".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urljoin
from xml.etree.ElementTree import Element, ParseError, fromstring

from selectolax.lexbor import LexborHTMLParser, LexborNode

from crwallm.crawler.contracts import ExtractionResult, FetchResponse
from crwallm.crawler.extraction.css import CssSpec, extract_canonical, extract_links, parse
from crwallm.crawler.extraction.structured import FieldPath

__all__ = [
    "Article",
    "DocumentExtractor",
    "FeedEntry",
    "FeedParseError",
    "extract_article",
    "extract_tables",
    "parse_feed",
]


# ------------------------------------------------------------------- feeds


@dataclass(frozen=True, slots=True)
class FeedEntry:
    title: str | None = None
    url: str | None = None
    published_at: datetime | None = None
    summary: str | None = None
    author: str | None = None
    entry_id: str | None = None

    def as_record(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "url": self.url,
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "summary": self.summary,
            "author": self.author,
        }


_DATE_FORMATS = (
    "%a, %d %b %Y %H:%M:%S %z",  # RFC 822, what RSS uses
    "%a, %d %b %Y %H:%M:%S %Z",
    "%Y-%m-%dT%H:%M:%S%z",  # RFC 3339, what Atom uses
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d",
)


def _parse_date(raw: str | None) -> datetime | None:
    """RSS and Atom disagree about date formats, and both are in use.

    Timezone-aware or nothing: a naive datetime compared against an aware one
    raises, and these get compared against ``lastmod`` and against each other
    (docs/05_SPIDER_ARCHITECTURE.md).
    """
    if not raw:
        return None
    text = raw.strip()
    for fmt in _DATE_FORMATS:
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)

    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class FeedParseError(ValueError):
    """The document is not usable XML, or declines to be parsed safely."""


_DECLARATION = re.compile(rb"<!(?:DOCTYPE|ENTITY)", re.IGNORECASE)
"""A legitimate feed has neither, and both are how XML parsing goes wrong.

Scanned over the whole document rather than its prolog: it costs one pass
and removes the question of whether a long comment could push a declaration
past whatever prefix was checked."""


def _local(tag: str) -> str:
    """An element's name without its namespace.

    Atom elements arrive as ``{http://www.w3.org/2005/Atom}entry`` and RSS
    extensions add several more namespaces. The local name is the part that
    distinguishes them, and matching on it is what lets one pass read both.
    """
    return tag.rsplit("}", 1)[-1].lower()


def _child_text(parent: Element, *names: str) -> str | None:
    for name in names:
        for child in parent:
            if _local(child.tag) == name:
                text = "".join(child.itertext()).strip()
                if text:
                    return text
    return None


def parse_feed(body: bytes | str, base_url: str = "") -> tuple[FeedEntry, ...]:
    """Entries from an RSS or Atom document.

    Parsed as XML, which sounds obvious and is the second attempt. The first
    used the HTML parser already in the project, and HTML treats ``<link>`` as
    a void element - so every RSS entry's URL ended up as a text node beside
    the empty link rather than inside it, and every entry came back with no
    address. Found by testing rather than by reading.

    Both formats are handled in one pass, because the difference between them
    is only which element names hold the same four facts: ``<item>`` or
    ``<entry>``, ``<link>`` as text or as an ``href``, ``pubDate`` or
    ``published`` or ``updated``.

    **Entity declarations are refused rather than parsed.** Three things go
    wrong when parsing XML from arbitrary hosts, and the standard library
    already handles two: it does not resolve external entities, so XXE and
    remote DTD retrieval are not available. The third - a document that
    defines an entity in terms of itself and expands until the process dies -
    needs a declaration to set up, and refusing the declaration removes the
    whole class without a second XML library to keep current.
    """
    raw = body.encode("utf-8", errors="replace") if isinstance(body, str) else body
    if _DECLARATION.search(raw):
        raise FeedParseError("feed declares a DOCTYPE or ENTITY; refusing to expand it")

    try:
        root = fromstring(raw)  # noqa: S314 - guarded above
    except ParseError:
        return ()

    entries: list[FeedEntry] = []
    for node in root.iter():
        if _local(node.tag) not in {"item", "entry"}:
            continue

        # Atom puts the URL in an attribute and RSS in the element text. An
        # entry can carry several links, and only the one without a `rel` (or
        # with rel="alternate") is the entry itself - the others are comment
        # feeds and enclosures.
        url: str | None = None
        for child in node:
            if _local(child.tag) != "link":
                continue
            if (child.get("rel") or "alternate").strip().lower() != "alternate":
                continue
            href = (child.get("href") or "").strip() or (child.text or "").strip()
            if href:
                url = href
                break
        if not url:
            url = _child_text(node, "guid", "id")

        author = _child_text(node, "author", "creator")
        for child in node:
            if _local(child.tag) == "author":
                nested = _child_text(child, "name")
                if nested:
                    author = nested
                break

        entries.append(
            FeedEntry(
                title=_child_text(node, "title"),
                url=urljoin(base_url, url) if url and base_url else url,
                published_at=_parse_date(
                    _child_text(node, "pubdate", "published", "updated", "date")
                ),
                summary=_child_text(node, "description", "summary", "content"),
                author=author,
                entry_id=_child_text(node, "guid", "id"),
            )
        )
    return tuple(entries)


# ------------------------------------------------------------------ tables


def extract_tables(
    scope: LexborHTMLParser | LexborNode,
    *,
    min_rows: int = 2,
    min_columns: int = 2,
) -> tuple[tuple[dict[str, str], ...], ...]:
    """Every data table on the page, as records keyed by column name.

    The header row is the point. A CSS recipe over the same table produces
    ``col_0``, ``col_1``, ``col_2`` and leaves a human to say what they are;
    the table already said.

    Layout tables - the 1990s kind, used for positioning - are excluded by
    requiring a header row and more than one column, which is what separates
    a table of data from a table used as a grid.
    """
    out: list[tuple[dict[str, str], ...]] = []

    for table in scope.css("table"):
        # `tr` alone, not `thead tr, tr` or `tbody tr, tr`: selectolax returns
        # a node once per matching selector, so the pair matched every row in
        # a table that had a tbody twice and doubled every result.
        rows_raw = table.css("tr")
        headers: list[str] = []
        for row in rows_raw:
            cells = row.css("th")
            if cells:
                headers = [
                    cell.text(deep=True, strip=True) or f"col_{i}" for i, cell in enumerate(cells)
                ]
                break

        if len(headers) < min_columns:
            continue

        rows: list[dict[str, str]] = []
        for row in rows_raw:
            cells = row.css("td")
            if not cells:
                continue  # the header row itself
            values = [cell.text(deep=True, strip=True) for cell in cells]
            # Ragged rows are normal - a colspan'd "no results" line, a footer
            # total. Zipping to the shorter of the two keeps a short row from
            # shifting every column after it.
            rows.append(dict(zip(headers, values, strict=False)))

        if len(rows) >= min_rows:
            out.append(tuple(rows))

    return tuple(out)


# ---------------------------------------------------------------- articles


@dataclass(frozen=True, slots=True)
class Article:
    """One body of text, with what could be learned about it."""

    text: str
    title: str | None = None
    byline: str | None = None
    published_at: str | None = None
    word_count: int = 0
    extra: dict[str, str] = field(default_factory=dict)

    def as_record(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "byline": self.byline,
            "published_at": self.published_at,
            "text": self.text,
            "word_count": self.word_count,
        }


_STRIP_TAGS = (
    "script",
    "style",
    "noscript",
    "template",
    "nav",
    "header",
    "footer",
    "aside",
    "form",
    "iframe",
    "svg",
)

_BOILERPLATE_HINT = re.compile(
    r"(?:^|[-_\s])(?:nav|menu|footer|header|sidebar|comment|share|social|related|promo"
    r"|advert|banner|cookie|newsletter|subscribe|breadcrumb)",
    re.IGNORECASE,
)

_CONTENT_HINT = re.compile(
    r"(?:^|[-_\s])(?:article|post|story|content|entry|main|body|prose)",
    re.IGNORECASE,
)

_WORD = re.compile(r"[0-9a-zA-Z가-힣一-鿿ぁ-ヿ]+")


def _word_count(text: str) -> int:
    """CJK-aware, for the same reason the deduper is.

    Splitting on whitespace makes a Korean article one word long, and every
    threshold measured in words then does nothing.
    """
    return len(_WORD.findall(text))


def _density(node: LexborNode) -> float:
    """Text per link, roughly.

    A navigation block is almost entirely links; an article is mostly not.
    This is the one signal that separates them without knowing the site, and
    it is why a link-heavy ``<div class="content">`` still loses.
    """
    text = node.text(deep=True, separator=" ", strip=True)
    words = _word_count(text)
    if words == 0:
        return 0.0
    link_words = sum(_word_count(a.text(deep=True, strip=True)) for a in node.css("a"))
    return words * (1.0 - min(link_words / words, 1.0))


def extract_article(tree: LexborHTMLParser, *, min_words: int = 40) -> Article | None:
    """The main body of a page that has one, or None.

    Scored rather than matched. ``<article>`` is the right answer when a page
    uses it, but most do not, and a site that puts its comments in a second
    ``<article>`` would win on document order alone. So every candidate block
    is scored on how much text it holds that is not inside a link, with names
    that announce themselves as content or as furniture nudging it either way.

    Returning None is a real answer: a listing page has no article, and
    inventing one out of its densest column would be worse than saying so.
    """
    body = tree.body or tree.root
    if body is None:
        return None

    for tag in _STRIP_TAGS:
        for node in body.css(tag):
            node.decompose()

    best: LexborNode | None = None
    best_score = 0.0

    for node in body.css("article, main, section, div, td"):
        score = _density(node)
        if score <= 0:
            continue

        marker = f"{node.attributes.get('class') or ''} {node.attributes.get('id') or ''}"
        if _BOILERPLATE_HINT.search(marker):
            score *= 0.25
        if _CONTENT_HINT.search(marker):
            score *= 1.6
        if node.tag == "article":
            score *= 1.5

        if score > best_score:
            best, best_score = node, score

    if best is None:
        return None

    text = re.sub(r"\n{3,}", "\n\n", best.text(deep=True, separator="\n", strip=True)).strip()
    words = _word_count(text)
    if words < min_words:
        return None

    title_node = tree.css_first("h1") or tree.css_first("title")
    return Article(
        text=text,
        title=title_node.text(deep=True, strip=True) if title_node is not None else None,
        word_count=words,
    )


# --------------------------------------------------------------- as records


@dataclass(slots=True)
class DocumentExtractor:
    """``Extractor`` for the three shapes whose schema is already known.

    Unlike a CSS or JSON-LD recipe, these need no field list. A feed entry has
    a title, a link and a date because that is what a feed entry *is*; a
    table's field names are its header row; an article is one body of text.
    Making a recipe restate any of that would be ceremony, so ``fields`` here
    only ever renames or narrows what the shape already provides.
    """

    kind: str
    """``feed``, ``table`` or ``article``."""

    container: str | None = None
    """For ``table``: a CSS selector picking which table. Absent means the
    first one that looks like data."""

    fields: tuple[FieldPath, ...] = ()
    """Rename, narrow, and transform. Empty keeps every key as it is."""

    css: CssSpec = field(default_factory=lambda: CssSpec())
    name: str = "document"

    def supports(self, response: FetchResponse) -> bool:
        content_type = (response.content_type or "").lower()
        if self.kind == "feed":
            # A feed can be served as any of these, and plenty of sites get it
            # wrong and send text/html; the parser refuses non-XML anyway.
            return True
        return content_type.startswith("text/html") or content_type.endswith("+xml")

    def extract(self, response: FetchResponse) -> ExtractionResult:
        base_url = response.final_url or response.url.url

        if self.kind == "feed":
            try:
                entries = parse_feed(response.body, base_url)
            except FeedParseError:
                entries = ()
            records = tuple(self._rename(e.as_record()) for e in entries)
            # A feed's entries *are* the links worth following, and they were
            # just parsed - re-reading the document as HTML to find them again
            # would produce the channel's own navigation instead.
            links = tuple(e.url for e in entries if e.url) if self.css.follow_links else ()
            return ExtractionResult(
                extractor=self.name, records=records, links=links, canonical_url=None, text=None
            )

        tree, _ = parse(response)
        links = (
            extract_links(tree, base_url, self.css.link_selector) if self.css.follow_links else ()
        )
        canonical = extract_canonical(tree)

        if self.kind == "table":
            scope: LexborHTMLParser | LexborNode = tree
            if self.container:
                # `default=None` is what makes the return type optional;
                # without it selectolax is typed as always finding one.
                picked = tree.css_first(self.container, default=None, strict=False)
                if picked is None:
                    return ExtractionResult(
                        extractor=self.name, records=(), links=links, canonical_url=canonical
                    )
                scope = picked
            tables = extract_tables(scope)
            rows = tables[0] if tables else ()
            return ExtractionResult(
                extractor=self.name,
                records=tuple(self._rename(dict(r)) for r in rows),
                links=links,
                canonical_url=canonical,
            )

        article = extract_article(tree)
        return ExtractionResult(
            extractor=self.name,
            records=(self._rename(article.as_record()),) if article else (),
            links=links,
            canonical_url=canonical,
            text=article.text if article else None,
        )

    def _rename(self, record: dict[str, Any]) -> dict[str, Any]:
        if not self.fields:
            return record

        from crwallm.crawler.extraction.transforms import apply_chain

        out: dict[str, Any] = {}
        for field_path in self.fields:
            value = record.get(field_path.path)
            out[field_path.name] = (
                apply_chain(value, list(field_path.transform)) if field_path.transform else value
            )
        return out
