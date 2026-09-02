"""Sitemaps, host scheduling, content dedup and soft 404s.

Each of these exists because a spider wastes its budget in a way that a
targeted collect never does. The tests are written against those failure
modes rather than against the happy path.

docs/05_SPIDER_ARCHITECTURE.md
"""

from __future__ import annotations

import pytest

from crwallm.crawler.contracts import FrontierItem
from crwallm.crawler.dedupe import (
    ContentDeduper,
    SoftNotFoundDetector,
    hamming,
    simhash,
)
from crwallm.crawler.discovery.sitemap import (
    candidate_sitemap_urls,
    parse_robots_txt,
    parse_sitemap,
)
from crwallm.crawler.frontier.scheduler import HostFrontier, score_url
from crwallm.policy.url import normalize

SITEMAP = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://shop.test/p/1</loc><lastmod>2026-01-15</lastmod><priority>0.9</priority></url>
  <url><loc>https://shop.test/p/2</loc><lastmod>2026-02-01T10:30:00Z</lastmod></url>
  <url><loc>/p/3</loc></url>
</urlset>"""

INDEX = """<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://shop.test/sitemap-1.xml</loc></sitemap>
  <sitemap><loc>https://shop.test/sitemap-2.xml</loc></sitemap>
</sitemapindex>"""


def item(url: str, depth: int = 0, priority: int = 0) -> FrontierItem:
    return FrontierItem(url=normalize(url), depth=depth, priority=priority)


# --------------------------------------------------------------- sitemaps


class TestRobots:
    def test_sitemap_directives_are_extracted(self) -> None:
        text = (
            "User-agent: *\nDisallow: /admin\n"
            "Sitemap: https://shop.test/sitemap.xml\n"
            "Sitemap: https://shop.test/news.xml\n"
        )
        assert parse_robots_txt(text, base_url="https://shop.test/") == (
            "https://shop.test/sitemap.xml",
            "https://shop.test/news.xml",
        )

    def test_rules_are_not_returned(self) -> None:
        """The file is read for its pointers, not its instructions
        (docs/17_NON_GOALS.md). Nothing here should surface Disallow."""
        text = "User-agent: *\nDisallow: /\nCrawl-delay: 10\n"
        assert parse_robots_txt(text, base_url="https://shop.test/") == ()

    def test_relative_directives_resolve(self) -> None:
        assert parse_robots_txt("Sitemap: /sitemap.xml", base_url="https://shop.test/") == (
            "https://shop.test/sitemap.xml",
        )

    def test_case_is_ignored(self) -> None:
        assert parse_robots_txt("SITEMAP: https://a.test/s.xml", base_url="https://a.test/")

    def test_conventional_paths_are_offered_as_a_fallback(self) -> None:
        candidates = candidate_sitemap_urls("https://shop.test/some/page")
        assert candidates[0] == "https://shop.test/sitemap.xml"
        assert len(candidates) <= 4, "every miss costs a request"


class TestSitemapParsing:
    def test_urls_and_metadata_are_read(self) -> None:
        result = parse_sitemap(SITEMAP.encode(), base_url="https://shop.test/sitemap.xml")
        assert len(result.entries) == 3
        assert result.entries[0].priority == 0.9
        assert result.entries[0].lastmod is not None

    def test_lastmod_is_timezone_aware(self) -> None:
        """A naive datetime compared against an aware one raises, and both
        spellings appear in real sitemaps."""
        result = parse_sitemap(SITEMAP.encode(), base_url="https://shop.test/sitemap.xml")
        for entry in result.entries:
            if entry.lastmod is not None:
                assert entry.lastmod.tzinfo is not None

    def test_relative_locations_resolve(self) -> None:
        result = parse_sitemap(SITEMAP.encode(), base_url="https://shop.test/sitemap.xml")
        assert result.entries[2].url.url == "https://shop.test/p/3"

    def test_an_index_yields_nested_sitemaps(self) -> None:
        result = parse_sitemap(INDEX.encode(), base_url="https://shop.test/sitemap.xml")
        assert result.is_index
        assert len(result.nested) == 2
        assert not result.entries

    def test_gzip_is_transparent(self) -> None:
        import gzip

        result = parse_sitemap(
            gzip.compress(SITEMAP.encode()), base_url="https://shop.test/s.xml.gz"
        )
        assert len(result.entries) == 3

    def test_a_broken_sitemap_costs_the_sitemap_not_the_crawl(self) -> None:
        result = parse_sitemap(b"<urlset><url><loc>unclosed", base_url="https://shop.test/")
        assert result.error is not None
        assert result.entries == ()

    def test_an_empty_document_is_reported(self) -> None:
        assert parse_sitemap(b"", base_url="https://shop.test/").error

    def test_namespaces_do_not_matter(self) -> None:
        """Generators pick their own namespace URIs, and some emit none."""
        plain = "<urlset><url><loc>https://a.test/x</loc></url></urlset>"
        assert parse_sitemap(plain.encode(), base_url="https://a.test/").entries

    def test_entry_count_is_capped(self) -> None:
        body = (
            "<urlset>"
            + "".join(f"<url><loc>https://a.test/{i}</loc></url>" for i in range(200))
            + "</urlset>"
        )
        result = parse_sitemap(body.encode(), base_url="https://a.test/", max_entries=50)
        assert len(result.entries) == 50

    def test_external_entities_are_inert(self) -> None:
        """A sitemap is untrusted input. ElementTree resolves no external
        entities, so an XXE payload parses as nothing."""
        payload = (
            '<?xml version="1.0"?>'
            '<!DOCTYPE r [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
            "<urlset><url><loc>&xxe;</loc></url></urlset>"
        )
        result = parse_sitemap(payload.encode(), base_url="https://a.test/")
        assert result.entries == ()


# ------------------------------------------------------------- scheduling


class TestPriorityScoring:
    def test_shallower_is_preferred(self) -> None:
        assert score_url("https://a.test/x", 0) > score_url("https://a.test/x", 3)

    def test_content_paths_beat_navigation(self) -> None:
        assert score_url("https://a.test/product/1", 1) > score_url("https://a.test/tag/sale", 1)

    def test_sitemap_entries_outrank_discovered_links(self) -> None:
        """The site listing a URL is the strongest signal there is: it is
        saying this is a page, not furniture."""
        assert score_url("https://a.test/x", 0, from_sitemap=True) > score_url(
            "https://a.test/x", 0
        )

    def test_the_sites_own_priority_hint_is_used(self) -> None:
        high = score_url("https://a.test/x", 0, from_sitemap=True, hint=1.0)
        low = score_url("https://a.test/x", 0, from_sitemap=True, hint=0.1)
        assert high > low

    def test_long_query_strings_are_penalised(self) -> None:
        plain = score_url("https://a.test/list", 1)
        faceted = score_url("https://a.test/list?a=1&b=2&c=3&d=4", 1)
        assert faceted < plain


class TestHostFrontier:
    async def test_hosts_are_served_round_robin(self) -> None:
        """The whole point. A FIFO hands one worker after another the same
        host - the fastest way to get blocked and the slowest way to crawl."""
        frontier = HostFrontier()
        for i in range(5):
            for host in ("a.test", "b.test", "c.test"):
                await frontier.add(item(f"https://{host}/{i}"))

        first_three = [await frontier.next() for _ in range(3)]
        hosts = {i.url.host for i in first_three if i}
        assert hosts == {"a.test", "b.test", "c.test"}

    async def test_higher_priority_comes_first_within_a_host(self) -> None:
        frontier = HostFrontier()
        await frontier.add(item("https://a.test/low", priority=10))
        await frontier.add(item("https://a.test/high", priority=900))

        first = await frontier.next()
        assert first is not None
        assert first.url.url.endswith("/high")

    async def test_equal_priority_keeps_insertion_order(self) -> None:
        frontier = HostFrontier()
        for i in range(4):
            await frontier.add(item(f"https://a.test/{i}", priority=100))
        order = []
        while (nxt := await frontier.next()) is not None:
            order.append(nxt.url.url)
        assert order == [f"https://a.test/{i}" for i in range(4)]

    async def test_per_host_concurrency_is_capped(self) -> None:
        frontier = HostFrontier(per_host_concurrency=2)
        for i in range(5):
            await frontier.add(item(f"https://a.test/{i}"))

        claimed = [await frontier.next() for _ in range(4)]
        assert sum(1 for c in claimed if c is not None) == 2, "one host, two slots"

    async def test_finishing_a_page_frees_its_slot(self) -> None:
        frontier = HostFrontier(per_host_concurrency=1)
        for i in range(3):
            await frontier.add(item(f"https://a.test/{i}"))

        first = await frontier.next()
        assert first is not None
        assert await frontier.next() is None
        await frontier.done(first)
        assert await frontier.next() is not None

    async def test_duplicates_are_refused(self) -> None:
        frontier = HostFrontier()
        assert await frontier.add(item("https://a.test/x"))
        assert not await frontier.add(item("https://a.test/x"))

    async def test_dedupe_uses_the_dedupe_key_not_the_url(self) -> None:
        """Two links differing only by a tracking parameter are one page."""
        frontier = HostFrontier()
        assert await frontier.add(item("https://a.test/x?utm_source=a"))
        assert not await frontier.add(item("https://a.test/x?utm_source=b"))

    async def test_exhaustion_waits_for_work_in_flight(self) -> None:
        """An empty queue is not the end: the worker holding that page is
        about to discover a hundred links."""
        frontier = HostFrontier()
        await frontier.add(item("https://a.test/x"))
        claimed = await frontier.next()
        assert claimed is not None
        assert not frontier.exhausted
        await frontier.done(claimed)
        assert frontier.exhausted

    async def test_a_penalised_host_is_skipped_while_others_run(self) -> None:
        """A blocked host produces nothing, so standing off is the faster
        route to the data - and the rest of the crawl keeps moving."""
        frontier = HostFrontier()
        await frontier.add(item("https://slow.test/x"))
        await frontier.add(item("https://fast.test/x"))

        frontier.penalise("slow.test", 60.0)
        nxt = await frontier.next()
        assert nxt is not None
        assert nxt.url.host == "fast.test"

    async def test_new_hosts_stop_being_accepted_past_the_cap(self) -> None:
        """A spider that has found ten thousand hosts has left the site it was
        pointed at."""
        frontier = HostFrontier(max_hosts=3)
        added = [await frontier.add(item(f"https://h{i}.test/x")) for i in range(6)]
        assert sum(added) == 3

    async def test_hosts_active_counts_work_not_hosts(self) -> None:
        frontier = HostFrontier()
        await frontier.add(item("https://a.test/x"))
        await frontier.add(item("https://b.test/x"))
        assert frontier.hosts_active == 2


# ---------------------------------------------------------------- dedupe


class TestSimhash:
    def test_identical_text_has_distance_zero(self) -> None:
        text = "the quick brown fox jumps over the lazy dog repeatedly"
        assert hamming(simhash(text), simhash(text)) == 0

    def test_a_small_edit_stays_close(self) -> None:
        base = "A long article about laptops with several sentences of body text here"
        edited = base + " Published at 10:32."
        assert hamming(simhash(base), simhash(edited)) < 12

    def test_different_documents_are_far_apart(self) -> None:
        a = "A long article about laptops with several sentences of body text here"
        b = "Completely unrelated content concerning maritime insurance regulation"
        assert hamming(simhash(a), simhash(b)) > 15

    def test_korean_is_tokenised_not_treated_as_one_word(self) -> None:
        """Korean does not delimit words with spaces. A whitespace split would
        make every Korean page a single token and therefore unique."""
        a = simhash("노트북 가격 정보 페이지 상품 목록 안내")
        b = simhash("노트북 가격 정보 페이지 상품 목록 안내")
        c = simhash("자동차 보험 약관 해설 문서 전문 자료")
        assert hamming(a, b) == 0
        assert hamming(a, c) > 10


class TestContentDeduper:
    def test_the_same_page_under_two_urls_is_caught(self) -> None:
        text = (
            "An article with enough words in it that the deduper will consider the "
            "comparison meaningful, running to several distinct sentences so that "
            "the shingles actually differ from one another rather than collapsing "
            "into a single indistinguishable blur of common vocabulary."
        )
        deduper = ContentDeduper()
        assert not deduper.check("https://a.test/article", text).is_duplicate
        assert deduper.check("https://a.test/print/article", text).is_duplicate

    def test_a_rotating_footer_does_not_make_a_new_page(self) -> None:
        """Exact hashing misses this, which is why simhash is here."""
        body = (
            "An article with enough words in it that the deduper will consider the "
            "comparison meaningful, running to several distinct sentences so that "
            "the shingles actually differ from one another in a useful way."
        )
        deduper = ContentDeduper()
        deduper.check("https://a.test/1", body + " Rendered at 10:31.")
        assert deduper.check("https://a.test/2", body + " Rendered at 10:32.").is_duplicate

    def test_different_articles_are_kept(self) -> None:
        deduper = ContentDeduper()
        deduper.check(
            "https://a.test/1",
            "A detailed review of a gaming laptop covering its display, keyboard, "
            "thermals and battery life across a week of ordinary use.",
        )
        verdict = deduper.check(
            "https://a.test/2",
            "Guidance on filing quarterly tax returns for sole traders, including "
            "the deadlines and the penalties for missing them.",
        )
        assert not verdict.is_duplicate

    def test_short_pages_are_not_compared(self) -> None:
        """Product detail pages share a template and differ by a few words.
        Comparing them would collapse a catalogue into one row."""
        deduper = ContentDeduper()
        deduper.check("https://a.test/p/1", "Laptop model 1 190,000")
        assert not deduper.check("https://a.test/p/2", "Laptop model 2 290,000").is_duplicate

    def test_an_exact_repeat_is_reported_as_exact(self) -> None:
        text = (
            "The same body text repeated verbatim across two different addresses, "
            "long enough that the deduper considers it at all, with several "
            "clauses so the comparison has something to work with."
        )
        deduper = ContentDeduper()
        deduper.check("https://a.test/1", text)
        assert deduper.check("https://a.test/2", text).via == "exact"

    def test_no_text_is_not_a_duplicate(self) -> None:
        assert not ContentDeduper().check("https://a.test/x", None).is_duplicate


class TestSoftNotFound:
    def test_a_not_found_phrase_is_caught(self) -> None:
        assert SoftNotFoundDetector().check("Page not found. The page does not exist.")

    def test_korean_phrasing_is_caught(self) -> None:
        assert SoftNotFoundDetector().check("페이지를 찾을 수 없습니다")

    def test_a_page_that_produced_records_is_never_a_soft_404(self) -> None:
        """Whatever it says, a page that yielded data is a real page."""
        assert not SoftNotFoundDetector().check("Page not found", records_found=5)

    def test_a_real_article_is_not_flagged(self) -> None:
        text = " ".join(f"sentence number {i} of real content" for i in range(20))
        assert not SoftNotFoundDetector().check(text)

    def test_a_one_word_page_is_not_flagged(self) -> None:
        """The bug this threshold exists for: a page reading "b" was flagged
        because it resembled another one-word page, and that took its whole
        URL pattern's budget with it."""
        detector = SoftNotFoundDetector()
        assert not detector.check("b")
        assert not detector.check("canonical")
        assert not detector.check("a b self")

    def test_two_similar_stubs_are_not_enough(self) -> None:
        """Two is a coincidence on any site with a couple of stub pages, and
        the cost of being wrong is a whole URL pattern."""
        detector = SoftNotFoundDetector()
        stub = "This section has no entries at the moment please check back"
        assert not detector.check(stub)
        assert not detector.check(stub + " again")

    def test_a_repeated_empty_template_is_eventually_caught(self) -> None:
        detector = SoftNotFoundDetector()
        stub = "This section has no entries at the moment please check back"
        results = [detector.check(f"{stub} {i}") for i in range(5)]
        assert any(results), "a template's empty state should be recognised"

    def test_an_empty_body_is_a_soft_404(self) -> None:
        assert SoftNotFoundDetector().check("")
        assert SoftNotFoundDetector().check("   ")


@pytest.mark.parametrize(
    "text",
    [
        "Sorry, we can't find that page",
        "This content no longer exists on our servers",
        "찾을 수 없는 페이지입니다",
    ],
)
def test_common_not_found_wordings(text: str) -> None:
    assert SoftNotFoundDetector().check(text)
