"""Data a page states about itself, rather than data read off its layout.

A CSS recipe infers structure from where things sit. Everything here is
declared: JSON-LD that names a Product and its price, an OpenGraph tag that
names the video, a ``__NEXT_DATA__`` blob that holds the very array the page
rendered. When it is present it beats any selector, because it does not move
when the site is restyled.

**Measured, not assumed.** Surveying six real sites found JSON-LD on one
listing page and only as ``WebPage`` - it is a *detail page* format, and the
useful shape is "crawl the listing with a recipe, follow to detail pages, read
the JSON-LD there". Embedded JSON was on two of six and held the actual items.
OpenGraph was on four but describes the page, not the rows on it. That is why
this module returns all of them separately rather than one merged blob: they
answer different questions and conflating them would hide which one was
missing (docs/06_EXTRACTION_ARCHITECTURE.md).
"""

from __future__ import annotations

import contextlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

from selectolax.lexbor import LexborHTMLParser, LexborNode

from crwallm.crawler.contracts import ExtractionResult, FetchResponse
from crwallm.crawler.extraction.css import CssSpec, extract_canonical, extract_links, parse

__all__ = [
    "EMBEDDED_SCRIPT_IDS",
    "PageMetadata",
    "StructuredData",
    "StructuredExtractor",
    "StructuredSpec",
    "extract_microdata",
    "extract_structured",
    "find_types",
    "iter_jsonld_nodes",
    "json_path",
    "records_from",
]

MAX_JSON_BYTES = 4_000_000
"""Cap on one embedded blob.

``__NEXT_DATA__`` on a large listing is routinely megabytes, and parsing an
unbounded one on every page of a crawl is how a spider runs out of memory
somewhere around page four hundred."""

EMBEDDED_SCRIPT_IDS = (
    "__NEXT_DATA__",
    "__NUXT_DATA__",
    "__remixContext",
    "__APOLLO_STATE__",
    "__INITIAL_STATE__",
    "__PRELOADED_STATE__",
)
"""Script ids that frameworks use to ship the page's own data.

The page was rendered from this. Reading it is not scraping the render, it is
reading the source the render came from - which is why the values are clean
where the rendered text has currency symbols and thousands separators in it."""


@dataclass(frozen=True, slots=True)
class PageMetadata:
    """What the page says it is. OpenGraph, Twitter cards, and the basics.

    Page-level, not row-level: this describes the document, so on a listing it
    is the listing's own title and image, not the products'. Useful for
    collecting *pages* - which is exactly the video-link case - and misleading
    if mistaken for item data.
    """

    title: str | None = None
    description: str | None = None
    canonical: str | None = None
    site_name: str | None = None
    kind: str | None = None
    """``og:type`` - "article", "video.other", "product"."""

    image: str | None = None
    video: str | None = None
    """``og:video`` / ``og:video:url`` - a playable media URL when the page
    declares one, which is how a video page announces itself."""

    video_type: str | None = None
    duration_s: int | None = None
    published_at: str | None = None
    author: str | None = None

    extra: dict[str, str] = field(default_factory=dict)
    """Every other ``og:``/``twitter:`` property, unmapped."""

    def is_video_page(self) -> bool:
        """Whether this page is *about* a video.

        Deliberately generous: a page carrying ``og:video`` or declaring a
        video ``og:type`` is one, even when the file itself is behind a
        player. Collecting the page is usually what was wanted anyway.
        """
        return bool(self.video) or (self.kind or "").startswith("video")


@dataclass(frozen=True, slots=True)
class StructuredData:
    jsonld: tuple[dict[str, Any], ...] = ()
    """Flattened: ``@graph`` unwrapped, arrays spread, one dict per entity."""

    microdata: tuple[dict[str, Any], ...] = ()
    """schema.org in attributes rather than in a script.

    Kept apart from ``jsonld`` even though both are schema.org, because a page
    can carry both and they can disagree. YouTube's JSON-LD names the video
    and its upload date; its microdata adds duration, channel and view count -
    the fields a video recipe returned as null. Merging them would hide which
    one a recipe is actually reading when one of them changes."""

    embedded: dict[str, Any] = field(default_factory=dict)
    """Framework state blobs, keyed by script id."""

    meta: PageMetadata = field(default_factory=PageMetadata)

    def types(self) -> tuple[str, ...]:
        """Every schema.org type present, in order of first appearance."""
        seen: list[str] = []
        for node in (*self.jsonld, *self.microdata):
            for value in _as_list(node.get("@type")):
                if isinstance(value, str) and value not in seen:
                    seen.append(value)
        return tuple(seen)

    @property
    def is_empty(self) -> bool:
        return (
            not self.jsonld
            and not self.microdata
            and not self.embedded
            and self.meta == PageMetadata()
        )


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


_CDATA = re.compile(r"^\s*(?://\s*)?<!\[CDATA\[(.*?)\]\]>\s*$", re.DOTALL)
_HTML_COMMENT = re.compile(r"^\s*<!--(.*?)-->\s*$", re.DOTALL)


def _clean_json_text(raw: str) -> str:
    """Undo the wrappers people put around inline JSON.

    CDATA sections and HTML comments were how you hid script content from
    ancient browsers, and CMS templates still emit them. Neither is valid JSON
    and both are trivially removable, so failing to parse over one would be
    throwing away good data for a decoration.
    """
    text = raw.strip()
    for pattern in (_CDATA, _HTML_COMMENT):
        match = pattern.match(text)
        if match:
            text = match.group(1).strip()
    return text


def _parse_json(raw: str) -> Any | None:
    if len(raw) > MAX_JSON_BYTES:
        return None
    try:
        return json.loads(_clean_json_text(raw))
    except (ValueError, RecursionError):
        # Malformed JSON-LD is common enough that raising would make this
        # module unusable on the open web. The CSS path still works.
        return None


def iter_jsonld_nodes(value: Any) -> list[dict[str, Any]]:
    """Every entity in a JSON-LD document, flattened.

    Three shapes have to collapse into one list. A document can be a single
    object, an array of them, or an object whose ``@graph`` holds the real
    entities - and a publisher can nest a second entity inside the first as
    the value of a property. Callers want "the Products on this page" without
    caring which of those the site chose.
    """
    out: list[dict[str, Any]] = []

    def walk(node: Any, depth: int = 0) -> None:
        if depth > 6:
            return
        if isinstance(node, list):
            for item in node:
                walk(item, depth + 1)
            return
        if not isinstance(node, dict):
            return

        if "@graph" in node:
            walk(node["@graph"], depth + 1)
            # The wrapper can still carry its own @type alongside the graph.
            if "@type" in node:
                out.append({k: v for k, v in node.items() if k != "@graph"})
            return

        if "@type" in node:
            out.append(node)

        # Nested entities: an Article whose `author` is a Person, a Product
        # whose `offers` is an Offer. Both are entities the caller may want.
        for key, child in node.items():
            if key.startswith("@"):
                continue
            if isinstance(child, dict | list):
                walk(child, depth + 1)

    walk(value)
    return out


def find_types(data: StructuredData, *wanted: str) -> tuple[dict[str, Any], ...]:
    """Entities whose ``@type`` matches any of ``wanted``, case-insensitively.

    ``@type`` is legitimately a list - a page can declare something both a
    ``Product`` and a ``Book`` - so membership, not equality.
    """
    targets = {w.casefold() for w in wanted}
    return tuple(
        node
        for node in (*data.jsonld, *data.microdata)
        if any(isinstance(t, str) and t.casefold() in targets for t in _as_list(node.get("@type")))
    )


_OG_MAP = {
    "og:title": "title",
    "og:description": "description",
    "og:url": "canonical",
    "og:site_name": "site_name",
    "og:type": "kind",
    "og:image": "image",
    "og:image:secure_url": "image",
    "og:video": "video",
    "og:video:url": "video",
    "og:video:secure_url": "video",
    "og:video:type": "video_type",
    "article:published_time": "published_at",
    "article:author": "author",
}

_NAME_MAP = {
    "twitter:title": "title",
    "twitter:description": "description",
    "twitter:image": "image",
    "twitter:player": "video",
    "description": "description",
    "author": "author",
}


def _read_metadata(tree: LexborHTMLParser) -> PageMetadata:
    """OpenGraph and Twitter cards.

    Both are read because sites are inconsistent about which they fill in, and
    the first non-empty value wins rather than the last: a page that declares
    ``og:image`` and then a Twitter fallback means the first one.
    """
    fields: dict[str, str] = {}
    extra: dict[str, str] = {}
    duration: int | None = None

    for node in tree.css("meta"):
        attrs = node.attributes
        key = (attrs.get("property") or attrs.get("name") or "").strip().lower()
        content = (attrs.get("content") or "").strip()
        if not key or not content:
            continue

        mapped = _OG_MAP.get(key) or _NAME_MAP.get(key)
        if mapped:
            fields.setdefault(mapped, content)
        elif key in {"og:video:duration", "video:duration"}:
            with contextlib.suppress(ValueError):
                duration = int(float(content))
        elif key.startswith(("og:", "twitter:")):
            extra.setdefault(key, content)

    canonical = fields.get("canonical")
    if not canonical:
        link = tree.css_first('link[rel="canonical"]')
        if link is not None:
            canonical = (link.attributes.get("href") or "").strip() or None

    return PageMetadata(
        title=fields.get("title"),
        description=fields.get("description"),
        canonical=canonical,
        site_name=fields.get("site_name"),
        kind=fields.get("kind"),
        image=fields.get("image"),
        video=fields.get("video"),
        video_type=fields.get("video_type"),
        duration_s=duration,
        published_at=fields.get("published_at"),
        author=fields.get("author"),
        extra=extra,
    )


def extract_structured(tree: LexborHTMLParser) -> StructuredData:
    """Everything the page declares about itself, in one pass.

    One pass because all three live in ``<head>`` or in ``<script>`` tags, and
    a crawl runs this on every page - three separate walks of the same tree
    would be three times the cost for the same answer.
    """
    jsonld: list[dict[str, Any]] = []
    embedded: dict[str, Any] = {}

    for script in tree.css("script"):
        attrs = script.attributes
        script_type = (attrs.get("type") or "").strip().lower()
        script_id = (attrs.get("id") or "").strip()

        if script_type == "application/ld+json":
            parsed = _parse_json(script.text(deep=True, strip=False))
            if parsed is not None:
                jsonld.extend(iter_jsonld_nodes(parsed))

        elif script_id in EMBEDDED_SCRIPT_IDS:
            parsed = _parse_json(script.text(deep=True, strip=False))
            if parsed is not None:
                embedded[script_id] = parsed

    return StructuredData(
        jsonld=tuple(jsonld),
        microdata=extract_microdata(tree),
        embedded=embedded,
        meta=_read_metadata(tree),
    )


# ------------------------------------------------------- reading it as rows

_INDEX = re.compile(r"^\d+$")


def json_path(node: Any, path: str) -> Any:
    """Follow a dotted path into parsed JSON.

    ``offers.price``, ``author.name``, ``props.pageProps.items.0.title``. A
    numeric segment indexes a list; everything else is a key.

    JSON-LD needs two unwrappings that a plain path walk would get wrong. A
    value can be ``{"@value": x}`` - the expanded form, which means x - and a
    single-valued property is often written as a one-element list. Both are
    the publisher's choice about serialisation, not about the data, so a
    recipe should not have to know which one a given site picked.
    """
    current = node
    for segment in path.split("."):
        if current is None:
            return None

        if isinstance(current, dict) and "@value" in current and segment not in current:
            current = current["@value"]

        # A single value written as a list: take the first, which is what the
        # publisher meant by it.
        if isinstance(current, list) and not _INDEX.match(segment):
            if len(current) != 1:
                return None
            current = current[0]

        if isinstance(current, list):
            index = int(segment)
            current = current[index] if -len(current) <= index < len(current) else None
        elif isinstance(current, dict):
            current = current.get(segment)
        else:
            return None

    if isinstance(current, dict) and "@value" in current:
        current = current["@value"]
    return current


@dataclass(frozen=True, slots=True)
class StructuredSpec:
    """How to read records out of declared data.

    Same two questions as a CSS spec, asked of JSON instead of a DOM:
    ``container`` finds the repeating unit and ``fields`` find values inside
    it. Keeping the shape identical is what lets recipes, scoring and the
    activation gate stay one code path rather than two.
    """

    kind: str = "jsonld"
    """``jsonld`` or ``embedded``."""

    container: str | None = None
    """For ``jsonld``: the ``@type`` to collect - "Product", "VideoObject".
    For ``embedded``: a dotted path to the array, and the script id is the
    first segment (``__NEXT_DATA__.props.pageProps.items``)."""

    fields: tuple[tuple[str, str], ...] = ()
    """``(name, path)`` pairs, each path relative to one record."""


def records_from(data: StructuredData, spec: StructuredSpec) -> tuple[dict[str, Any], ...]:
    """Apply a spec to what a page declared.

    Records where every field came back empty are dropped, the same rule the
    CSS extractor uses: a row of nulls is a selector that missed, and counting
    it would make the fill rate - which is what activation is scored on - a
    lie (docs/07_RECIPE_ARCHITECTURE.md).
    """
    if not spec.fields:
        return ()

    items: list[Any]
    if spec.kind == "jsonld":
        items = list(find_types(data, spec.container)) if spec.container else list(data.jsonld)
    elif spec.kind == "microdata":
        items = (
            [n for n in data.microdata if _has_type(n, spec.container)]
            if spec.container
            else list(data.microdata)
        )
    elif spec.kind == "embedded":
        if not spec.container:
            return ()
        script_id, _, rest = spec.container.partition(".")
        found = (
            json_path(data.embedded.get(script_id), rest) if rest else data.embedded.get(script_id)
        )
        items = found if isinstance(found, list) else ([found] if found is not None else [])
    else:
        return ()

    out: list[dict[str, Any]] = []
    for item in items:
        record = {name: json_path(item, path) for name, path in spec.fields}
        if any(v is not None and v != "" and v != [] for v in record.values()):
            out.append(record)
    return tuple(out)


@dataclass(slots=True)
class StructuredExtractor:
    """``Extractor`` for pages that declare their own data.

    Links, canonical and text still come from the DOM. Only the *records*
    change source: a crawl has to keep walking whatever the rows were read
    from, and a JSON-LD block does not list the site's navigation.

    An empty result is not an error here. A listing page whose detail pages
    carry the JSON-LD will legitimately produce nothing, and the operator sees
    that as "0 records over 200 pages" with the pages tab to explain it.
    """

    spec: StructuredSpec
    css: CssSpec = field(default_factory=lambda: CssSpec())
    name: str = "structured"

    def supports(self, response: FetchResponse) -> bool:
        content_type = response.content_type or ""
        return content_type.startswith("text/html") or content_type.endswith("+xml")

    def extract(self, response: FetchResponse) -> ExtractionResult:
        tree, _ = parse(response)
        base_url = response.final_url or response.url.url
        data = extract_structured(tree)

        return ExtractionResult(
            extractor=self.name,
            records=records_from(data, self.spec),
            links=extract_links(tree, base_url, self.css.link_selector)
            if self.css.follow_links
            else (),
            canonical_url=data.meta.canonical or extract_canonical(tree),
            text=None,
            content_hash=None,
        )


# ---------------------------------------------------------------- microdata

_URL_ATTR_TAGS = {
    "a": "href",
    "area": "href",
    "link": "href",
    "audio": "src",
    "embed": "src",
    "iframe": "src",
    "img": "src",
    "source": "src",
    "track": "src",
    "video": "src",
    "object": "data",
}


def _microdata_value(node: LexborNode) -> Any:
    """One property's value, by the rules the spec gives for each element.

    ``<meta content>``, ``<a href>``, ``<time datetime>`` and text everywhere
    else. Reading text from a ``<meta>`` would give an empty string, and
    reading text from a ``<link>`` would give whatever followed it - the same
    void-element trap that broke the first feed parser.
    """
    attrs = node.attributes
    tag = str(node.tag).lower()

    # `content` first, whatever the tag. The specification reserves it for
    # `<meta>`, and YouTube writes `<link itemprop="name" content="Rick
    # Astley">` - reading `href` there returned an empty channel name for a
    # page that plainly states it. Parsing the web as it is, not as specified.
    content = (attrs.get("content") or "").strip()
    if content:
        return content

    if tag == "meta":
        return ""
    if tag in _URL_ATTR_TAGS:
        return (attrs.get(_URL_ATTR_TAGS[tag]) or "").strip()
    if tag in {"time", "data"}:
        return (attrs.get("datetime") or attrs.get("value") or "").strip() or node.text(
            deep=True, strip=True
        )
    if tag == "meter":
        return (attrs.get("value") or "").strip()
    return node.text(deep=True, strip=True)


def _read_item(scope: LexborNode, depth: int = 0) -> dict[str, Any]:
    """One ``itemscope`` and the properties belonging to it.

    Walked downward from the scope rather than upward from each property.
    selectolax hands back a fresh wrapper on every ``.parent`` access, so
    "which scope owns this property" cannot be answered by walking up - the
    same constraint that shaped the structure detector.

    A nested ``itemscope`` ends the walk for that branch: its properties
    belong to it, not to this one.
    """
    item: dict[str, Any] = {}
    item_type = (scope.attributes.get("itemtype") or "").strip()
    if item_type:
        item["@type"] = item_type.rsplit("/", 1)[-1]

    if depth > 5:
        return item

    def visit(node: LexborNode) -> None:
        for child in node.iter(include_text=False):
            name = (child.attributes.get("itemprop") or "").strip()
            nested = "itemscope" in child.attributes

            if name and nested:
                value: Any = _read_item(child, depth + 1)
            elif name:
                value = _microdata_value(child)
            else:
                if not nested:
                    visit(child)
                continue

            if name in item:
                # Repeated properties are legal and mean a list - `regionsAllowed`
                # on a video is dozens of them.
                existing = item[name]
                item[name] = [*existing, value] if isinstance(existing, list) else [existing, value]
            else:
                item[name] = value

            if not nested:
                visit(child)

    visit(scope)
    return item


def extract_microdata(tree: LexborHTMLParser) -> tuple[dict[str, Any], ...]:
    """schema.org microdata, as flat entities like the JSON-LD ones.

    Worth having because it is not redundant. Measured across five sites it
    appeared on one - and on that one it carried ``duration`` and ``author``,
    the two fields the same page's JSON-LD left out and a video recipe
    returned as null. Complementary rather than an older spelling.

    Top-level scopes only: a nested one is reached as its parent's property
    value, and returning it twice would double every count.
    """
    # Descended from the root rather than filtered from a flat list.
    # `node.css(...)` in selectolax includes the node itself, unlike the DOM's
    # querySelectorAll - so "scopes inside this scope" quietly meant "this
    # scope and the ones inside it", every scope marked itself nested, and the
    # whole function returned nothing.
    top: list[dict[str, Any]] = []

    def descend(node: LexborNode, depth: int = 0) -> None:
        if depth > 12:
            return
        for child in node.iter(include_text=False):
            if "itemscope" in child.attributes:
                item = _read_item(child)
                # A scope carrying only its type describes nothing.
                if len(item) > 1 or (item and "@type" not in item):
                    top.append(item)
                continue  # its own scopes are its properties, not top level
            descend(child, depth + 1)

    root = tree.body or tree.root
    if root is not None:
        descend(root)
    return tuple(top)


def _has_type(node: dict[str, Any], wanted: str) -> bool:
    target = wanted.casefold()
    return any(
        isinstance(t, str) and t.rsplit("/", 1)[-1].casefold() == target
        for t in _as_list(node.get("@type"))
    )
