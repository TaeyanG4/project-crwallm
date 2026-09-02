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
    extract_structured,
    find_types,
    iter_jsonld_nodes,
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
