"""What a page declares about itself.

The shapes here are the ones real sites emit, not the ones the specification
shows. JSON-LD in the wild arrives wrapped in ``@graph``, split across several
script tags, with ``@type`` as a list, and occasionally inside a CDATA section
left over from a template written in 2009. A parser that only handles the
canonical form finds nothing on most of the web.

docs/06_EXTRACTION_ARCHITECTURE.md
"""

from __future__ import annotations

import json

import pytest
from selectolax.lexbor import LexborHTMLParser

from crwallm.crawler.extraction.structured import (
    MAX_JSON_BYTES,
    PageMetadata,
    StructuredSpec,
    extract_structured,
    find_types,
    iter_jsonld_nodes,
    json_path,
    records_from,
)


def page(head: str = "", body: str = "") -> LexborHTMLParser:
    return LexborHTMLParser(f"<html><head>{head}</head><body>{body}</body></html>")


def ld(payload: object) -> str:
    return f'<script type="application/ld+json">{json.dumps(payload)}</script>'


class TestJsonLd:
    def test_a_single_entity(self) -> None:
        data = extract_structured(
            page(ld({"@context": "https://schema.org", "@type": "Product", "name": "Keyboard"}))
        )
        assert data.types() == ("Product",)
        assert data.jsonld[0]["name"] == "Keyboard"

    def test_an_array_of_entities(self) -> None:
        data = extract_structured(
            page(ld([{"@type": "Product", "name": "A"}, {"@type": "Product", "name": "B"}]))
        )
        assert [n["name"] for n in data.jsonld] == ["A", "B"]

    def test_a_graph_wrapper_is_unwrapped(self) -> None:
        """Yoast and most WordPress SEO plugins ship everything in ``@graph``.
        A parser that only reads the top level finds nothing on those sites."""
        data = extract_structured(
            page(
                ld(
                    {
                        "@context": "https://schema.org",
                        "@graph": [
                            {"@type": "WebSite", "name": "Shop"},
                            {"@type": "Product", "name": "Keyboard"},
                        ],
                    }
                )
            )
        )
        assert set(data.types()) == {"WebSite", "Product"}

    def test_several_script_tags_are_all_read(self) -> None:
        data = extract_structured(
            page(
                ld({"@type": "Organization", "name": "Shop"})
                + ld({"@type": "Product", "name": "K"})
            )
        )
        assert len(data.jsonld) == 2

    def test_nested_entities_surface(self) -> None:
        """An Article whose author is a Person: both are entities somebody may
        want, and only the outer one is at the top level."""
        data = extract_structured(
            page(
                ld(
                    {
                        "@type": "Article",
                        "headline": "Hello",
                        "author": {"@type": "Person", "name": "Kim"},
                    }
                )
            )
        )
        assert set(data.types()) == {"Article", "Person"}

    def test_a_type_list_matches_either_name(self) -> None:
        """``"@type": ["Product", "Book"]`` is legal and used."""
        data = extract_structured(page(ld({"@type": ["Product", "Book"], "name": "Novel"})))
        assert find_types(data, "book")
        assert find_types(data, "product")

    def test_types_are_matched_case_insensitively(self) -> None:
        data = extract_structured(page(ld({"@type": "VideoObject", "name": "Clip"})))
        assert find_types(data, "videoobject")

    def test_cdata_wrapping_still_parses(self) -> None:
        """Left over from hiding scripts from browsers that no longer exist,
        and still emitted by CMS templates."""
        payload = json.dumps({"@type": "Product", "name": "Keyboard"})
        tree = page(f'<script type="application/ld+json">//<![CDATA[{payload}]]></script>')
        assert extract_structured(tree).types() == ("Product",)

    def test_malformed_json_is_skipped_not_raised(self) -> None:
        """Broken JSON-LD is common on the open web. Raising would make every
        other extractor on the page unreachable for a decoration."""
        tree = page('<script type="application/ld+json">{"@type": "Product",}</script>')
        assert extract_structured(tree).jsonld == ()

    def test_an_oversized_blob_is_refused(self) -> None:
        """A crawl runs this on every page; an unbounded parse is how a spider
        dies four hundred pages in."""
        huge = "x" * (MAX_JSON_BYTES + 10)
        tree = page(f'<script type="application/ld+json">"{huge}"</script>')
        assert extract_structured(tree).jsonld == ()

    def test_a_page_without_any_reports_empty(self) -> None:
        assert extract_structured(page(body="<p>nothing</p>")).is_empty

    def test_recursion_is_bounded(self) -> None:
        """A self-referential document must not walk forever."""
        deep: dict[str, object] = {"@type": "Thing"}
        node = deep
        for _ in range(30):
            child: dict[str, object] = {"@type": "Thing"}
            node["child"] = child
            node = child
        assert len(iter_jsonld_nodes(deep)) < 30


class TestEmbeddedJson:
    def test_next_data_is_captured(self) -> None:
        """Two of six real sites surveyed shipped their items this way, and
        the values are clean - no currency symbols, no thousands separators."""
        payload = {"props": {"pageProps": {"items": [{"id": 1}, {"id": 2}]}}}
        blob = json.dumps(payload)
        tree = page(body=f'<script id="__NEXT_DATA__" type="application/json">{blob}</script>')
        data = extract_structured(tree)
        assert data.embedded["__NEXT_DATA__"]["props"]["pageProps"]["items"][0]["id"] == 1

    def test_an_unknown_script_id_is_ignored(self) -> None:
        tree = page(body='<script id="analytics-config">{"a":1}</script>')
        assert extract_structured(tree).embedded == {}

    def test_a_broken_blob_does_not_break_the_page(self) -> None:
        tree = page(body='<script id="__NEXT_DATA__">{not json}</script>')
        assert extract_structured(tree).embedded == {}


class TestPageMetadata:
    def test_opengraph_is_read(self) -> None:
        tree = page(
            '<meta property="og:title" content="Keyboard">'
            '<meta property="og:type" content="product">'
            '<meta property="og:image" content="https://cdn.test/k.jpg">'
        )
        meta = extract_structured(tree).meta
        assert meta.title == "Keyboard"
        assert meta.kind == "product"
        assert meta.image == "https://cdn.test/k.jpg"

    def test_twitter_cards_fill_gaps(self) -> None:
        """Sites are inconsistent about which they populate, so both are read."""
        tree = page('<meta name="twitter:title" content="From Twitter">')
        assert extract_structured(tree).meta.title == "From Twitter"

    def test_opengraph_wins_over_twitter(self) -> None:
        """First non-empty wins: a page that declares og and then a fallback
        means the og one."""
        tree = page(
            '<meta property="og:title" content="Real">'
            '<meta name="twitter:title" content="Fallback">'
        )
        assert extract_structured(tree).meta.title == "Real"

    def test_canonical_falls_back_to_the_link_tag(self) -> None:
        tree = page('<link rel="canonical" href="https://shop.test/p/1">')
        assert extract_structured(tree).meta.canonical == "https://shop.test/p/1"

    def test_unmapped_properties_are_kept(self) -> None:
        tree = page('<meta property="og:locale" content="ko_KR">')
        assert extract_structured(tree).meta.extra["og:locale"] == "ko_KR"

    def test_empty_content_is_not_a_value(self) -> None:
        """An empty og:title must not shadow a Twitter one that has text."""
        tree = page(
            '<meta property="og:title" content=""><meta name="twitter:title" content="Real title">'
        )
        assert extract_structured(tree).meta.title == "Real title"


class TestVideoPages:
    """Collecting only videos was an explicit requirement, and a page that
    declares one is the cheapest possible way to recognise it."""

    def test_a_page_with_og_video_is_a_video_page(self) -> None:
        tree = page('<meta property="og:video" content="https://cdn.test/v.mp4">')
        meta = extract_structured(tree).meta
        assert meta.is_video_page()
        assert meta.video == "https://cdn.test/v.mp4"

    def test_a_video_og_type_counts_even_without_a_file(self) -> None:
        """The file is usually behind a player. The page is what gets
        collected anyway."""
        tree = page('<meta property="og:type" content="video.other">')
        assert extract_structured(tree).meta.is_video_page()

    def test_a_twitter_player_counts(self) -> None:
        tree = page('<meta name="twitter:player" content="https://cdn.test/embed">')
        assert extract_structured(tree).meta.is_video_page()

    def test_duration_is_a_number(self) -> None:
        tree = page('<meta property="og:video:duration" content="212">')
        assert extract_structured(tree).meta.duration_s == 212

    def test_a_float_duration_is_accepted(self) -> None:
        tree = page('<meta property="video:duration" content="212.5">')
        assert extract_structured(tree).meta.duration_s == 212

    def test_a_nonsense_duration_is_dropped_not_raised(self) -> None:
        tree = page('<meta property="og:video:duration" content="PT3M32S">')
        assert extract_structured(tree).meta.duration_s is None

    def test_an_article_is_not_a_video_page(self) -> None:
        tree = page('<meta property="og:type" content="article">')
        assert not extract_structured(tree).meta.is_video_page()

    def test_a_bare_page_is_not_a_video_page(self) -> None:
        assert not PageMetadata().is_video_page()


class TestSeparation:
    """The three sources answer different questions and must not be merged.

    OpenGraph describes the document; on a listing that is the listing's own
    title, not the products'. Reporting it as item data would be confidently
    wrong, and hiding which source was empty would make a failure undiagnosable.
    """

    def test_each_source_is_reported_separately(self) -> None:
        tree = page(
            '<meta property="og:title" content="Shop - all keyboards">'
            + ld({"@type": "Product", "name": "Keyboard model one"}),
            body='<script id="__NEXT_DATA__">{"items":[1,2]}</script>',
        )
        data = extract_structured(tree)
        assert data.meta.title == "Shop - all keyboards"
        assert data.jsonld[0]["name"] == "Keyboard model one"
        assert data.embedded["__NEXT_DATA__"]["items"] == [1, 2]


@pytest.mark.parametrize(
    "wanted",
    ["Product", "VideoObject", "Article", "JobPosting", "Recipe", "Event"],
)
def test_the_types_worth_naming_all_match(wanted: str) -> None:
    data = extract_structured(page(ld({"@type": wanted, "name": "x"})))
    assert find_types(data, wanted)


class TestJsonPath:
    """The two unwrappings that a plain path walk gets wrong.

    Both are the publisher's choice about serialisation rather than about the
    data, and a recipe should not have to know which one a given site picked.
    """

    def test_a_plain_path(self) -> None:
        assert json_path({"offers": {"price": 1290}}, "offers.price") == 1290

    def test_a_numeric_segment_indexes_a_list(self) -> None:
        assert json_path({"items": [{"n": "a"}, {"n": "b"}]}, "items.1.n") == "b"

    def test_a_negative_index_is_refused_rather_than_wrapping(self) -> None:
        """Silently returning the last element for "items.-1" would be a
        surprise; a recipe that meant it can say so another way."""
        assert json_path({"items": [1, 2, 3]}, "items.5") is None

    def test_an_expanded_value_is_unwrapped(self) -> None:
        """JSON-LD's expanded form: ``{"@value": x}`` means x."""
        assert json_path({"name": {"@value": "Keyboard"}}, "name") == "Keyboard"

    def test_a_single_valued_list_is_unwrapped(self) -> None:
        """Publishers write one-element arrays for single values constantly."""
        assert json_path({"author": [{"name": "Kim"}]}, "author.name") == "Kim"

    def test_a_multi_valued_list_is_not_guessed_at(self) -> None:
        """Two authors and a path that names one field: picking the first
        would be inventing an answer."""
        assert json_path({"author": [{"name": "Kim"}, {"name": "Lee"}]}, "author.name") is None

    def test_a_missing_key_is_none_not_an_error(self) -> None:
        assert json_path({"a": 1}, "b.c.d") is None

    def test_walking_into_a_scalar_stops(self) -> None:
        assert json_path({"a": 5}, "a.b") is None


class TestRecordsFrom:
    def test_jsonld_products_become_rows(self) -> None:
        tree = page(
            ld(
                [
                    {"@type": "Product", "name": "Keyboard", "offers": {"price": 129000}},
                    {"@type": "Product", "name": "Mouse", "offers": {"price": 39000}},
                    {"@type": "Organization", "name": "Shop"},
                ]
            )
        )
        rows = records_from(
            extract_structured(tree),
            StructuredSpec(
                kind="jsonld",
                container="Product",
                fields=(("title", "name"), ("price", "offers.price")),
            ),
        )
        assert rows == (
            {"title": "Keyboard", "price": 129000},
            {"title": "Mouse", "price": 39000},
        )

    def test_the_container_type_filters(self) -> None:
        """The Organization above must not become a product row."""
        tree = page(ld([{"@type": "Product", "name": "K"}, {"@type": "Organization", "name": "S"}]))
        rows = records_from(
            extract_structured(tree),
            StructuredSpec(kind="jsonld", container="Product", fields=(("title", "name"),)),
        )
        assert rows == ({"title": "K"},)

    def test_embedded_arrays_become_rows(self) -> None:
        """The values here are clean - this is the source the page rendered
        from, so prices are numbers rather than "129,000원"."""
        blob = json.dumps(
            {"props": {"pageProps": {"items": [{"t": "A", "p": 1}, {"t": "B", "p": 2}]}}}
        )
        tree = page(body=f'<script id="__NEXT_DATA__">{blob}</script>')
        rows = records_from(
            extract_structured(tree),
            StructuredSpec(
                kind="embedded",
                container="__NEXT_DATA__.props.pageProps.items",
                fields=(("title", "t"), ("price", "p")),
            ),
        )
        assert rows == ({"title": "A", "price": 1}, {"title": "B", "price": 2})

    def test_rows_where_nothing_matched_are_dropped(self) -> None:
        """A row of nulls is a path that missed. Counting it would make the
        fill rate - which activation is scored on - a lie."""
        tree = page(ld([{"@type": "Product", "name": "K"}, {"@type": "Product", "sku": "x"}]))
        rows = records_from(
            extract_structured(tree),
            StructuredSpec(kind="jsonld", container="Product", fields=(("title", "name"),)),
        )
        assert rows == ({"title": "K"},)

    def test_a_spec_without_fields_yields_nothing(self) -> None:
        tree = page(ld({"@type": "Product", "name": "K"}))
        assert records_from(extract_structured(tree), StructuredSpec(kind="jsonld")) == ()

    def test_an_unknown_kind_yields_nothing_rather_than_raising(self) -> None:
        """Recipes are YAML written by people and models; an unknown source
        must not take the crawl down."""
        tree = page(ld({"@type": "Product", "name": "K"}))
        rows = records_from(
            extract_structured(tree),
            StructuredSpec(kind="microdata", container="Product", fields=(("t", "name"),)),
        )
        assert rows == ()

    def test_a_missing_embedded_script_yields_nothing(self) -> None:
        tree = page(body="<p>no blob</p>")
        rows = records_from(
            extract_structured(tree),
            StructuredSpec(kind="embedded", container="__NEXT_DATA__.items", fields=(("t", "t"),)),
        )
        assert rows == ()

    def test_video_objects_read_as_rows(self) -> None:
        """The requirement stated at the outset: collect only the videos, and
        the fields that decide whether one is wanted."""
        tree = page(
            ld(
                {
                    "@type": "VideoObject",
                    "name": "Never Gonna Give You Up",
                    "duration": "PT3M33S",
                    "contentUrl": "https://cdn.test/v.mp4",
                    "uploadDate": "2009-10-25",
                }
            )
        )
        rows = records_from(
            extract_structured(tree),
            StructuredSpec(
                kind="jsonld",
                container="VideoObject",
                fields=(("title", "name"), ("length", "duration"), ("url", "contentUrl")),
            ),
        )
        assert rows[0]["title"] == "Never Gonna Give You Up"
        assert rows[0]["url"] == "https://cdn.test/v.mp4"


VIDEO_MICRODATA = """
<div itemscope itemtype="https://schema.org/VideoObject">
  <meta itemprop="name" content="Never Gonna Give You Up">
  <meta itemprop="duration" content="PT3M34S">
  <meta itemprop="genre" content="Music">
  <link itemprop="url" href="https://v.test/watch?v=1">
  <span itemprop="author" itemscope itemtype="https://schema.org/Person">
    <link itemprop="url" href="https://v.test/@rick">
    <link itemprop="name" content="Rick Astley">
  </span>
  <meta itemprop="regionsAllowed" content="KR">
  <meta itemprop="regionsAllowed" content="JP">
  <time itemprop="uploadDate" datetime="2009-10-24T23:57:33-07:00">2009</time>
</div>
"""


class TestMicrodata:
    """Added because it is *not* redundant with JSON-LD.

    Measured across five sites it appeared on one - and on that one it carried
    the duration and the channel, the two fields the same page's JSON-LD left
    out and a video recipe returned as null.
    """

    def data(self, html: str = VIDEO_MICRODATA):
        return extract_structured(page(body=html))

    def test_an_item_and_its_type(self) -> None:
        items = self.data().microdata
        assert len(items) == 1
        assert items[0]["@type"] == "VideoObject"

    def test_meta_reads_its_content(self) -> None:
        assert self.data().microdata[0]["duration"] == "PT3M34S"

    def test_a_link_reads_its_href(self) -> None:
        assert self.data().microdata[0]["url"] == "https://v.test/watch?v=1"

    def test_content_wins_over_href_on_a_link(self) -> None:
        """The specification reserves ``content`` for ``<meta>``. YouTube
        writes ``<link itemprop="name" content="Rick Astley">``, and reading
        href there returned an empty channel for a page that states it."""
        assert self.data().microdata[0]["author"]["name"] == "Rick Astley"

    def test_a_time_reads_its_datetime(self) -> None:
        assert self.data().microdata[0]["uploadDate"].startswith("2009-10-24T")

    def test_a_nested_scope_becomes_an_object(self) -> None:
        author = self.data().microdata[0]["author"]
        assert author["@type"] == "Person"
        assert author["url"] == "https://v.test/@rick"

    def test_a_nested_scope_is_not_also_top_level(self) -> None:
        """It is reached as its parent's property; returning it twice would
        double every count."""
        assert len(self.data().microdata) == 1

    def test_a_repeated_property_becomes_a_list(self) -> None:
        assert self.data().microdata[0]["regionsAllowed"] == ["KR", "JP"]

    def test_types_include_microdata(self) -> None:
        assert "VideoObject" in self.data().types()

    def test_find_types_matches_microdata_too(self) -> None:
        assert find_types(self.data(), "videoobject")

    def test_records_can_be_read_from_microdata(self) -> None:
        rows = records_from(
            self.data(),
            StructuredSpec(
                kind="microdata",
                container="VideoObject",
                fields=(("title", "name"), ("channel", "author.name"), ("length", "duration")),
            ),
        )
        assert rows == (
            {"title": "Never Gonna Give You Up", "channel": "Rick Astley", "length": "PT3M34S"},
        )

    def test_a_full_schema_url_matches_a_bare_type_name(self) -> None:
        """``itemtype`` is a URL; a recipe says "VideoObject"."""
        rows = records_from(
            self.data(),
            StructuredSpec(kind="microdata", container="videoobject", fields=(("t", "name"),)),
        )
        assert rows

    def test_a_scope_with_only_a_type_is_not_an_item(self) -> None:
        assert (
            extract_structured(page(body='<div itemscope itemtype="x/Thing"></div>')).microdata
            == ()
        )

    def test_a_page_without_microdata_reports_none(self) -> None:
        assert extract_structured(page(body="<p>plain</p>")).microdata == ()

    def test_jsonld_and_microdata_stay_separate(self) -> None:
        """A page can carry both and they can disagree. Merging them would
        hide which one a recipe is actually reading when one of them changes."""
        tree = page(ld({"@type": "VideoObject", "name": "From JSON-LD"}), VIDEO_MICRODATA)
        data = extract_structured(tree)
        assert data.jsonld[0]["name"] == "From JSON-LD"
        assert data.microdata[0]["name"] == "Never Gonna Give You Up"
