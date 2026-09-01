"""URL normalisation - docs/05_SPIDER_ARCHITECTURE.md section 3.

The properties that matter for a spider are idempotence (or the same page
enters the frontier twice under different keys) and the separation of the
fetch URL from the dedupe key (or aggressive dedupe starts fetching the wrong
pages).
"""

from __future__ import annotations

import pytest

from crwallm.policy.url import (
    NormalizedUrl,
    UrlNormalizationError,
    normalize,
    url_pattern,
)


def n(raw: str, **kw: object) -> NormalizedUrl:
    return normalize(raw, **kw)  # type: ignore[arg-type]


class TestScheme:
    @pytest.mark.parametrize("raw", ["https://a.com/", "HTTPS://a.com/", "HtTp://a.com/"])
    def test_scheme_is_lowercased(self, raw: str) -> None:
        assert n(raw).scheme in ("http", "https")

    @pytest.mark.parametrize(
        "raw",
        [
            "file:///etc/passwd",
            "ftp://a.com/x",
            "javascript:alert(1)",
            "data:text/html,<h1>x",
            "gopher://a.com/",
            "mailto:a@b.com",
        ],
    )
    def test_non_http_schemes_are_refused(self, raw: str) -> None:
        with pytest.raises(UrlNormalizationError):
            n(raw)

    def test_empty_is_refused(self) -> None:
        with pytest.raises(UrlNormalizationError):
            n("   ")


class TestHost:
    def test_host_is_lowercased(self) -> None:
        assert n("https://EXAMPLE.COM/Path").host == "example.com"

    def test_path_case_is_preserved(self) -> None:
        # Paths are case-sensitive on most servers; lowercasing them loses pages.
        assert n("https://example.com/Path/To").path == "/Path/To"

    def test_default_port_is_dropped(self) -> None:
        assert n("https://example.com:443/x").url == "https://example.com/x"
        assert n("http://example.com:80/x").url == "http://example.com/x"

    def test_nonstandard_port_is_kept(self) -> None:
        assert n("http://example.com:8080/x").port == 8080

    def test_idn_is_punycoded(self) -> None:
        assert n("https://한국.kr/").host.startswith("xn--")

    def test_invalid_port_is_refused(self) -> None:
        with pytest.raises(UrlNormalizationError):
            n("http://example.com:99999999/")


class TestFragmentAndPath:
    def test_fragment_is_dropped(self) -> None:
        assert n("https://a.com/p#section").url == "https://a.com/p"

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("https://a.com/x/./y", "/x/y"),
            ("https://a.com/x/../y", "/y"),
            ("https://a.com/x//y", "/x/y"),
            ("https://a.com/x/y/..", "/x"),
            ("https://a.com", "/"),
            ("https://a.com/", "/"),
        ],
    )
    def test_dot_segments_resolve(self, raw: str, expected: str) -> None:
        assert n(raw).path == expected

    def test_trailing_slash_is_preserved_for_fetch(self) -> None:
        # Some servers redirect between the two; keep the caller's choice.
        assert n("https://a.com/dir/").path == "/dir/"

    def test_trailing_slash_is_ignored_for_dedupe(self) -> None:
        assert n("https://a.com/dir/").dedupe_key == n("https://a.com/dir").dedupe_key


class TestPercentEncoding:
    def test_unreserved_escapes_are_decoded(self) -> None:
        assert n("https://a.com/%7Euser").path == "/~user"

    def test_hex_case_is_normalised(self) -> None:
        assert n("https://a.com/a%2fb").path == n("https://a.com/a%2Fb").path

    def test_reserved_escapes_survive(self) -> None:
        # %2F is an encoded slash, not a path separator - decoding it would
        # change which resource we ask for.
        assert "%2F" in n("https://a.com/a%2fb").path


class TestTrackingParams:
    @pytest.mark.parametrize(
        "param",
        ["utm_source=x", "fbclid=abc", "gclid=abc", "_ga=1.2", "NaPm=ct%3D", "spm=a1z"],
    )
    def test_tracking_params_leave_the_fetch_url(self, param: str) -> None:
        assert n(f"https://a.com/p?{param}").url == "https://a.com/p"

    def test_session_ids_are_dropped(self) -> None:
        # Otherwise every request mints a brand new frontier entry.
        assert n("https://a.com/p?JSESSIONID=99AA").url == "https://a.com/p"

    def test_real_params_survive(self) -> None:
        assert n("https://a.com/p?id=88&utm_source=x").url == "https://a.com/p?id=88"


class TestFetchVsDedupe:
    """The central design decision - docs/05 section 3."""

    def test_fetch_url_keeps_meaningful_params_unsorted(self) -> None:
        u = n("https://a.com/list?sort=price&id=88")
        assert "sort=price" in u.url
        assert "id=88" in u.url

    def test_dedupe_key_sorts_params(self) -> None:
        a = n("https://a.com/p?b=2&a=1").dedupe_key
        b = n("https://a.com/p?a=1&b=2").dedupe_key
        assert a == b

    def test_dedupe_whitelist_narrows_the_key_only(self) -> None:
        u = n("https://a.com/list?id=88&sort=price&color=red", dedupe_whitelist=frozenset({"id"}))
        # The key ignores facets so the frontier does not explode ...
        assert u.dedupe_key == "https://a.com/list?id=88"
        # ... but the fetch still asks for the page the user meant.
        assert "sort=price" in u.url
        assert "color=red" in u.url

    def test_dedupe_key_is_never_the_fetch_url_when_whitelisting(self) -> None:
        u = n("https://a.com/l?a=1&b=2", dedupe_whitelist=frozenset({"a"}))
        assert u.url != u.dedupe_key


class TestIdempotence:
    """norm(norm(u)) == norm(u).

    If this breaks, one page enters the frontier under two keys and the crawl
    silently doubles.
    """

    @pytest.mark.parametrize(
        "raw",
        [
            "https://EXAMPLE.com:443/a/./b/../c/?utm_source=x&b=2&a=1#frag",
            "http://a.com:80//x//y/",
            "https://a.com/%7Euser/%2Fweird",
            "https://a.com/p?JSESSIONID=1&id=2",
            "https://한국.kr/경로",
            "https://a.com",
        ],
    )
    def test_normalize_is_idempotent(self, raw: str) -> None:
        once = n(raw)
        twice = n(once.url)
        assert twice.url == once.url
        assert twice.dedupe_key == once.dedupe_key

    def test_dedupe_key_is_stable_under_renormalisation(self) -> None:
        wl = frozenset({"id"})
        once = normalize("https://a.com/p?id=1&x=2", dedupe_whitelist=wl)
        twice = normalize(once.url, dedupe_whitelist=wl)
        assert once.dedupe_key == twice.dedupe_key


class TestRelativeResolution:
    @pytest.mark.parametrize(
        ("base", "href", "expected"),
        [
            ("https://a.com/dir/page", "other", "https://a.com/dir/other"),
            ("https://a.com/dir/page", "/root", "https://a.com/root"),
            ("https://a.com/dir/page", "../up", "https://a.com/up"),
            ("https://a.com/dir/page", "//cdn.b.com/x", "https://cdn.b.com/x"),
            ("https://a.com/dir/", "?q=1", "https://a.com/dir/?q=1"),
        ],
    )
    def test_relative_links_resolve(self, base: str, href: str, expected: str) -> None:
        assert normalize(href, base=base).url == expected

    def test_empty_href_is_refused(self) -> None:
        with pytest.raises(UrlNormalizationError):
            normalize("", base="https://a.com/")


class TestControlCharacters:
    def test_embedded_newline_is_stripped(self) -> None:
        # Header smuggling via a link in crawled HTML.
        assert n("https://a.com/p\r\nX-Evil: 1").host == "a.com"


class TestUrlPattern:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("https://a.com/product/8821", "a.com/product/{n}"),
            ("https://a.com/calendar/2031/07", "a.com/calendar/{n}/{n}"),
            ("https://a.com/post/2024-05-01", "a.com/post/{date}"),
            ("https://a.com/u/a3f9b2c1d4e5", "a.com/u/{id}"),
            (
                "https://a.com/i/550e8400-e29b-41d4-a716-446655440000",
                "a.com/i/{uuid}",
            ),
            ("https://a.com/about/team", "a.com/about/team"),
        ],
    )
    def test_ids_collapse_to_placeholders(self, raw: str, expected: str) -> None:
        assert url_pattern(n(raw)) == expected

    def test_infinite_calendar_shares_one_budget(self) -> None:
        months = [
            f"https://a.com/calendar/{y}/{m:02d}" for y in range(2030, 2040) for m in range(1, 13)
        ]
        assert len({url_pattern(n(u)) for u in months}) == 1

    def test_distinct_sections_get_distinct_budgets(self) -> None:
        assert url_pattern(n("https://a.com/product/1")) != url_pattern(
            n("https://a.com/article/1")
        )

    def test_query_keys_shape_the_pattern(self) -> None:
        assert url_pattern(n("https://a.com/l?page=3")) == "a.com/l?page={v}"
        assert url_pattern(n("https://a.com/l?page=9999")) == "a.com/l?page={v}"
