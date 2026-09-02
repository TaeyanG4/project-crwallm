"""Finding the data behind the page, and walking it.

The discovery half is deliberately narrow. Scraping endpoint URLs out of
inline JavaScript is the obvious approach and it was measured finding nothing
on four real sites, so it is not here - only the two mechanisms that actually
worked are, and both rest on something the site published rather than on a
guess.

The pagination half is wide, because every API disagrees about how to say
"there is more". The shapes below are the ones in the wild: GitHub's Link
header, Stripe's cursor, the DRF envelope, and the plain page number that half
the internet uses.

docs/06_EXTRACTION_ARCHITECTURE.md
"""

from __future__ import annotations

from selectolax.lexbor import LexborHTMLParser

from crwallm.crawler.discovery.api import (
    detect_pagination,
    discover_endpoints,
    next_data_url,
    next_page_url,
)


def page(head: str) -> LexborHTMLParser:
    return LexborHTMLParser(f"<html><head>{head}</head><body></body></html>")


class TestNextData:
    """Measured on react.dev: the rendered page is 1.4MB and this route
    returns the same content as 30KB of JSON with values already typed."""

    def test_a_path_becomes_a_json_route(self) -> None:
        assert (
            next_data_url("https://react.dev/learn", "abc123")
            == "https://react.dev/_next/data/abc123/learn.json"
        )

    def test_the_site_root_becomes_index(self) -> None:
        """An empty segment is not a filename, and Next.js spells it this way."""
        assert (
            next_data_url("https://react.dev/", "abc123")
            == "https://react.dev/_next/data/abc123/index.json"
        )

    def test_a_nested_path_keeps_its_segments(self) -> None:
        assert (
            next_data_url("https://react.dev/learn/thinking-in-react", "b")
            == "https://react.dev/_next/data/b/learn/thinking-in-react.json"
        )

    def test_no_build_id_means_no_route(self) -> None:
        assert next_data_url("https://react.dev/learn", "") is None


class TestEndpointDiscovery:
    def test_a_declared_feed_is_found(self) -> None:
        tree = page('<link rel="alternate" type="application/rss+xml" href="/rss" title="News">')
        found = discover_endpoints(tree, "https://site.test/news")
        assert found[0].url == "https://site.test/rss"
        assert found[0].kind == "feed"
        assert found[0].confidence == "declared"
        assert found[0].title == "News"

    def test_atom_counts_as_a_feed(self) -> None:
        tree = page('<link rel="alternate" type="application/atom+xml" href="/atom.xml">')
        assert discover_endpoints(tree, "https://site.test/")[0].kind == "feed"

    def test_a_json_alternate_is_distinguished_from_a_feed(self) -> None:
        """Both are other ways in, but only one is a feed, and a caller that
        wanted articles should not be handed an arbitrary JSON document."""
        tree = page('<link rel="alternate" type="application/json" href="/index.json">')
        assert discover_endpoints(tree, "https://site.test/")[0].kind == "json_alternate"

    def test_an_alternate_without_a_known_type_is_ignored(self) -> None:
        """``rel=alternate`` also marks translations and mobile versions."""
        tree = page('<link rel="alternate" hreflang="ko" href="/ko/">')
        assert discover_endpoints(tree, "https://site.test/") == ()

    def test_a_build_id_derives_the_data_route(self) -> None:
        tree = page("")
        found = discover_endpoints(
            tree,
            "https://react.dev/learn",
            embedded={"__NEXT_DATA__": {"buildId": "xyz"}},
        )
        assert found[0].kind == "next_data"
        assert found[0].confidence == "derived"
        assert found[0].url.endswith("/_next/data/xyz/learn.json")

    def test_embedded_json_without_a_build_id_derives_nothing(self) -> None:
        tree = page("")
        assert discover_endpoints(tree, "https://x.test/", embedded={"__NEXT_DATA__": {}}) == ()

    def test_the_same_url_is_not_listed_twice(self) -> None:
        tree = page(
            '<link rel="alternate" type="application/rss+xml" href="/rss">'
            '<link rel="alternate" type="application/atom+xml" href="/rss">'
        )
        assert len(discover_endpoints(tree, "https://site.test/")) == 1

    def test_nothing_is_guessed(self) -> None:
        """A page that declares no endpoints yields none. Probing conventions
        would mean several 404s per host for one occasional hit."""
        assert discover_endpoints(page(""), "https://site.test/") == ()


class TestPagination:
    def test_a_link_header_wins(self) -> None:
        """RFC 8288, and it means exactly one thing - unlike a field named
        ``next``, which might be describing the content."""
        style = detect_pagination(
            {"page": 1},
            headers={"link": '<https://api.test/items?page=2>; rel="next"'},
        )
        assert style.kind == "link_header"
        assert style.next_url == "https://api.test/items?page=2"

    def test_a_relative_link_header_is_resolved(self) -> None:
        style = detect_pagination(
            {},
            headers={"link": '</items?page=2>; rel="next"'},
            request_url="https://api.test/items",
        )
        assert style.next_url == "https://api.test/items?page=2"

    def test_a_next_url_field(self) -> None:
        style = detect_pagination({"results": [], "next": "https://api.test/items?page=3"})
        assert style.kind == "next_url"
        assert style.next_url == "https://api.test/items?page=3"

    def test_a_nested_next_link(self) -> None:
        """JSON:API and HAL both put it under ``links``."""
        style = detect_pagination({"links": {"next": {"href": "/items?page=2"}}})
        assert style.kind == "next_url"

    def test_a_cursor(self) -> None:
        style = detect_pagination({"data": [], "next_cursor": "eyJpZCI6NDJ9"})
        assert style.kind == "cursor"
        assert style.param == "next_cursor"
        assert style.value == "eyJpZCI6NDJ9"

    def test_a_page_number_advances(self) -> None:
        style = detect_pagination({"page": 2, "items": []})
        assert style.kind == "page"
        assert style.value == 3

    def test_an_offset_advances_by_the_page_size(self) -> None:
        style = detect_pagination({"offset": 40, "limit": 20, "items": []})
        assert style.kind == "offset"
        assert style.value == 60

    def test_an_offset_without_a_page_size_is_not_guessed(self) -> None:
        """Advancing by an invented amount silently skips or repeats rows."""
        assert detect_pagination({"offset": 40, "items": []}).kind == "none"

    def test_a_nested_envelope_is_read(self) -> None:
        """Django REST Framework and friends wrap it in ``meta``."""
        style = detect_pagination({"data": [], "meta": {"next_cursor": "abc"}})
        assert style.kind == "cursor"

    def test_totals_are_reported_even_when_paging_is_absent(self) -> None:
        """Knowing there are 4,000 rows is worth having even when the API did
        not say how to reach them."""
        style = detect_pagination({"items": [], "total": 4000})
        assert style.kind == "none"
        assert style.total == 4000

    def test_a_bare_array_has_no_paging(self) -> None:
        assert detect_pagination([{"id": 1}, {"id": 2}]).kind == "none"

    def test_a_boolean_is_not_a_page_number(self) -> None:
        """``bool`` is an ``int`` in Python, and a field called ``page``
        holding ``True`` means something other than page one."""
        assert detect_pagination({"page": True, "items": []}).kind == "none"

    def test_an_empty_response_has_no_paging(self) -> None:
        assert detect_pagination({}).kind == "none"


class TestNextPageUrl:
    def test_a_ready_made_url_is_used_as_is(self) -> None:
        style = detect_pagination({"next": "https://api.test/items?page=9"})
        assert next_page_url("https://api.test/items", style) == "https://api.test/items?page=9"

    def test_a_page_parameter_is_replaced_not_appended(self) -> None:
        """Appending would produce ``?page=1&page=2`` and let the server pick."""
        style = detect_pagination({"page": 1, "items": []})
        assert next_page_url("https://api.test/items?page=1", style) == (
            "https://api.test/items?page=2"
        )

    def test_other_query_parameters_survive(self) -> None:
        """The filters and sort the caller set are the reason this URL exists."""
        style = detect_pagination({"page": 1, "items": []})
        result = next_page_url("https://api.test/items?sort=new&q=laptop&page=1", style)
        assert "sort=new" in result and "q=laptop" in result and "page=2" in result

    def test_a_cursor_is_placed_in_its_parameter(self) -> None:
        style = detect_pagination({"next_cursor": "abc", "data": []})
        assert next_page_url("https://api.test/items", style) == (
            "https://api.test/items?next_cursor=abc"
        )

    def test_no_paging_means_no_url(self) -> None:
        assert next_page_url("https://api.test/items", detect_pagination({})) is None
