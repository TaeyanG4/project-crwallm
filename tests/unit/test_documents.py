"""Feeds, tables and article text.

Each of these is a shape the CSS detector handles badly and something else
handles well. The tests are about the cases that make the difference: an Atom
feed whose links are attributes, a table whose rows are ragged, an article page
whose navigation is denser than its prose.

docs/06_EXTRACTION_ARCHITECTURE.md
"""

from __future__ import annotations

from selectolax.lexbor import LexborHTMLParser

from crwallm.crawler.extraction.documents import (
    extract_article,
    extract_tables,
    parse_feed,
)

RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <title>Example News</title>
  <item>
    <title>First story</title>
    <link>https://news.test/1</link>
    <description>Something happened today</description>
    <pubDate>Tue, 02 Sep 2025 09:00:00 +0000</pubDate>
    <guid>tag:news.test,2025:1</guid>
  </item>
  <item>
    <title>Second story</title>
    <link>https://news.test/2</link>
    <pubDate>Tue, 02 Sep 2025 10:30:00 +0000</pubDate>
  </item>
</channel></rss>"""

ATOM = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Example Blog</title>
  <entry>
    <title>Hello world</title>
    <link rel="alternate" href="https://blog.test/hello"/>
    <link rel="replies" href="https://blog.test/hello/comments"/>
    <id>urn:uuid:1</id>
    <published>2025-09-02T09:00:00Z</published>
    <summary>A first post</summary>
    <author><name>Kim</name></author>
  </entry>
</feed>"""


class TestFeeds:
    def test_rss_items_are_read(self) -> None:
        entries = parse_feed(RSS)
        assert len(entries) == 2
        assert entries[0].title == "First story"
        assert entries[0].url == "https://news.test/1"

    def test_atom_entries_are_read_by_the_same_parser(self) -> None:
        """The formats differ only in which element names hold the same four
        facts. A reader that knew one of them would work on half the web."""
        entries = parse_feed(ATOM)
        assert len(entries) == 1
        assert entries[0].title == "Hello world"

    def test_an_atom_link_comes_from_the_attribute(self) -> None:
        """Atom puts the URL in ``href``; RSS puts it in the element text."""
        assert parse_feed(ATOM)[0].url == "https://blog.test/hello"

    def test_a_comments_link_is_not_mistaken_for_the_entry(self) -> None:
        """An entry can carry several links, and only the alternate one is
        the entry itself."""
        assert "comments" not in (parse_feed(ATOM)[0].url or "")

    def test_dates_are_timezone_aware(self) -> None:
        """A naive datetime raises when compared with an aware one, and these
        get compared against sitemap lastmod values."""
        published = parse_feed(RSS)[0].published_at
        assert published is not None
        assert published.tzinfo is not None

    def test_rfc_822_and_rfc_3339_both_parse(self) -> None:
        assert parse_feed(RSS)[0].published_at is not None
        assert parse_feed(ATOM)[0].published_at is not None

    def test_an_unparseable_date_does_not_lose_the_entry(self) -> None:
        feed = RSS.replace("Tue, 02 Sep 2025 09:00:00 +0000", "sometime last week")
        entries = parse_feed(feed)
        assert entries[0].title == "First story"
        assert entries[0].published_at is None

    def test_the_author_is_read_from_atom_nesting(self) -> None:
        assert parse_feed(ATOM)[0].author == "Kim"

    def test_relative_links_resolve_against_the_feed_url(self) -> None:
        feed = RSS.replace("https://news.test/1", "/1")
        assert parse_feed(feed, "https://news.test/rss")[0].url == "https://news.test/1"

    def test_a_document_that_is_not_a_feed_yields_nothing(self) -> None:
        assert parse_feed("<html><body><p>hello</p></body></html>") == ()

    def test_bytes_are_accepted(self) -> None:
        assert len(parse_feed(RSS.encode())) == 2


class TestTables:
    def test_headers_become_field_names(self) -> None:
        """The point of the whole module: a CSS recipe over this produces
        col_0, col_1 and leaves a human to name them. The table already did."""
        html = """<table>
          <thead><tr><th>Model</th><th>Price</th></tr></thead>
          <tbody>
            <tr><td>K1</td><td>129000</td></tr>
            <tr><td>K2</td><td>89000</td></tr>
          </tbody>
        </table>"""
        rows = extract_tables(LexborHTMLParser(html))[0]
        assert rows == ({"Model": "K1", "Price": "129000"}, {"Model": "K2", "Price": "89000"})

    def test_a_table_without_a_tbody_still_works(self) -> None:
        html = """<table>
          <tr><th>A</th><th>B</th></tr>
          <tr><td>1</td><td>2</td></tr>
          <tr><td>3</td><td>4</td></tr>
        </table>"""
        assert len(extract_tables(LexborHTMLParser(html))[0]) == 2

    def test_a_ragged_row_does_not_shift_the_columns(self) -> None:
        """A colspan'd footer or a "no results" line is normal. Padding it out
        would put the wrong value under every header after it."""
        html = """<table>
          <tr><th>A</th><th>B</th><th>C</th></tr>
          <tr><td>1</td><td>2</td><td>3</td></tr>
          <tr><td>only one</td></tr>
        </table>"""
        rows = extract_tables(LexborHTMLParser(html))[0]
        assert rows[1] == {"A": "only one"}

    def test_a_layout_table_is_not_data(self) -> None:
        """The 1990s kind, used for positioning. No header row, so no data."""
        html = "<table><tr><td>left</td><td>right</td></tr></table>"
        assert extract_tables(LexborHTMLParser(html)) == ()

    def test_a_single_column_table_is_refused(self) -> None:
        html = "<table><tr><th>A</th></tr><tr><td>1</td></tr><tr><td>2</td></tr></table>"
        assert extract_tables(LexborHTMLParser(html)) == ()

    def test_a_table_with_too_few_rows_is_refused(self) -> None:
        html = "<table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr></table>"
        assert extract_tables(LexborHTMLParser(html), min_rows=2) == ()

    def test_several_tables_are_returned_separately(self) -> None:
        one = "<table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr>"
        one += "<tr><td>3</td><td>4</td></tr></table>"
        assert len(extract_tables(LexborHTMLParser(one + one))) == 2


ARTICLE_PAGE = """<html><body>
  <nav class="site-nav">
    <a href="/a">News</a><a href="/b">Sport</a><a href="/c">Culture</a>
    <a href="/d">Business</a><a href="/e">Travel</a><a href="/f">Opinion</a>
  </nav>
  <h1>The quiet rise of the local crawler</h1>
  <div class="article-body">
    <p>For most of the last decade the assumption was that collecting data at
    any scale meant renting somebody else's machines and paying per page.</p>
    <p>That assumption is quietly failing. A single desktop can now hold a
    model good enough to name the columns of a listing page, and the crawler
    that feeds it does not need a cluster to keep up with one operator.</p>
    <p>The interesting constraint turns out not to be throughput at all. It is
    the number of different site shapes one person can afford to describe by
    hand, which is exactly the part a model can take over.</p>
  </div>
  <aside class="related"><a href="/x">More like this</a><a href="/y">Also</a></aside>
</body></html>"""


class TestArticles:
    def test_the_body_is_found(self) -> None:
        article = extract_article(LexborHTMLParser(ARTICLE_PAGE))
        assert article is not None
        assert "quietly failing" in article.text

    def test_navigation_is_left_out(self) -> None:
        """Density is what separates them: a nav block is almost all links."""
        article = extract_article(LexborHTMLParser(ARTICLE_PAGE))
        assert article is not None
        assert "Business" not in article.text
        assert "More like this" not in article.text

    def test_the_headline_is_picked_up(self) -> None:
        article = extract_article(LexborHTMLParser(ARTICLE_PAGE))
        assert article is not None
        assert article.title == "The quiet rise of the local crawler"

    def test_a_listing_page_has_no_article(self) -> None:
        """None is a real answer. Inventing an article out of the densest
        column of a listing would be worse than saying there is not one."""
        html = (
            "<html><body><ul>"
            + "".join(f'<li><a href="/p/{i}">Item {i}</a></li>' for i in range(20))
            + "</ul></body></html>"
        )
        assert extract_article(LexborHTMLParser(html)) is None

    def test_a_short_page_is_not_an_article(self) -> None:
        html = "<html><body><div><p>Too short to be an article.</p></div></body></html>"
        assert extract_article(LexborHTMLParser(html)) is None

    def test_korean_text_is_counted_by_word_not_by_space(self) -> None:
        """Splitting on whitespace makes a Korean article one word long, and
        every threshold measured in words then does nothing."""
        body = "".join(
            f"<p>지역별 채용정보를 수집하는 크롤러는 문서 구조를 읽어야 한다 {i}.</p>"
            for i in range(6)
        )
        article = extract_article(LexborHTMLParser(f"<html><body><div>{body}</div></body></html>"))
        assert article is not None
        assert article.word_count > 40

    def test_scripts_and_styles_never_reach_the_text(self) -> None:
        html = ARTICLE_PAGE.replace("<h1>", "<script>var tracking = 'x'.repeat(500);</script><h1>")
        article = extract_article(LexborHTMLParser(html))
        assert article is not None
        assert "tracking" not in article.text

    def test_an_empty_document_yields_nothing(self) -> None:
        assert extract_article(LexborHTMLParser("<html><body></body></html>")) is None
