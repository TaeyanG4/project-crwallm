"""Decoding, transforms and CSS extraction.

docs/06_EXTRACTION_ARCHITECTURE.md
"""

from __future__ import annotations

import pytest

from crwallm.crawler.contracts import FetchResponse
from crwallm.crawler.extraction.css import (
    CssExtractor,
    CssSpec,
    FieldSpec,
    extract_canonical,
    extract_links,
    parse,
)
from crwallm.crawler.extraction.decoding import decode_html
from crwallm.crawler.extraction.transforms import (
    TransformError,
    apply_chain,
    validate_chain,
)
from crwallm.policy.url import normalize
from crwallm.schemas.types import FetchMode


def response(
    html: str | bytes, *, url: str = "https://shop.test/list", ct: str = "text/html"
) -> FetchResponse:
    body = html.encode("utf-8") if isinstance(html, str) else html
    return FetchResponse(
        url=normalize(url),
        status=200,
        headers={"content-type": ct},
        body=body,
        elapsed_ms=1,
        fetch_mode=FetchMode.HTTP,
    )


# ---------------------------------------------------------------- decoding


class TestDecoding:
    def test_utf8_is_decoded(self) -> None:
        doc = decode_html("노트북".encode(), "text/html; charset=utf-8")
        assert doc.text == "노트북"
        assert doc.encoding == "utf-8"

    def test_euc_kr_declaration_uses_cp949(self) -> None:
        """Servers declare the subset and serve the superset. Honouring the
        declaration literally fails on perfectly good Korean text."""
        body = "노트북 ￦".encode("cp949")
        doc = decode_html(body, "text/html; charset=euc-kr")
        assert "노트북" in doc.text
        assert doc.encoding == "cp949"

    def test_meta_charset_is_used_when_header_is_silent(self) -> None:
        body = '<meta charset="cp949">노트북'.encode("cp949")
        doc = decode_html(body, "text/html")
        assert "노트북" in doc.text

    def test_header_beats_meta(self) -> None:
        body = '<meta charset="cp949">노트북'.encode()
        doc = decode_html(body, "text/html; charset=utf-8")
        assert "노트북" in doc.text

    def test_bom_beats_a_contradicting_declaration(self) -> None:
        import codecs

        body = codecs.BOM_UTF8 + "노트북".encode()
        doc = decode_html(body, "text/html; charset=euc-kr")
        assert "노트북" in doc.text

    def test_wrong_declaration_falls_through_to_trial(self) -> None:
        """A page that says cp949 and sends UTF-8 is common; losing it is not
        acceptable."""
        doc = decode_html("노트북".encode(), "text/html; charset=euc-kr")
        assert doc.text == "노트북"
        assert doc.encoding == "utf-8"

    def test_undecodable_bytes_never_raise(self) -> None:
        """A crawler that throws on a bad page loses the page."""
        doc = decode_html(b"\xff\xfe\x00broken\x81\x82", "text/html")
        assert isinstance(doc.text, str)

    def test_empty_body(self) -> None:
        assert decode_html(b"", None).text == ""


# -------------------------------------------------------------- transforms


class TestTransforms:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("1,290,000원", 1290000),
            ("₩1,290,000", 1290000),
            ("$1,299.00", 1299.0),
            ("월 12,900원~", 12900),
            ("1.234.567", 1234567),
            ("42", 42),
            ("-5", -5),
            ("품절", None),
            ("", None),
        ],
    )
    def test_to_number(self, raw: str, expected: object) -> None:
        assert apply_chain(raw, ["to_number"]) == expected

    def test_to_absolute_url(self) -> None:
        got = apply_chain("/product/1", ["to_absolute_url"], base_url="https://a.com/list")
        assert got == "https://a.com/product/1"

    def test_absolute_url_without_base_is_left_alone(self) -> None:
        assert apply_chain("/p", ["to_absolute_url"]) == "/p"

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("1:23:45", 5025), ("12:34", 754), ("0:07", 7), ("nonsense", None)],
    )
    def test_duration_to_seconds(self, raw: str, expected: object) -> None:
        assert apply_chain(raw, ["duration_to_seconds"]) == expected

    @pytest.mark.parametrize(
        "raw", ["2025-01-15", "2025/01/15", "2025.01.15", "2025-01-15T10:30:00"]
    )
    def test_parse_date_accepts_common_shapes(self, raw: str) -> None:
        got = apply_chain(raw, ["parse_date"])
        assert isinstance(got, str)
        assert got.startswith("2025-01-15")

    def test_parse_date_returns_text_not_datetime(self) -> None:
        """Records are JSONB; a round-trip must not change the value."""
        assert isinstance(apply_chain("2025-01-15", ["parse_date"]), str)

    def test_chain_runs_left_to_right(self) -> None:
        got = apply_chain("  <b>1,290,000</b>원  ", ["strip_html", "trim", "to_number"])
        assert got == 1290000

    def test_none_short_circuits_the_rest(self) -> None:
        """Running to_absolute_url on nothing only hides which step failed."""
        assert apply_chain("품절", ["to_number", "to_absolute_url"]) is None

    def test_default_recovers_from_none(self) -> None:
        assert apply_chain("품절", ["to_number", "default(0)"]) == "0"

    def test_regex_extract(self) -> None:
        assert apply_chain("SKU-8821-X", ["regex_extract(\\d+)"]) == "8821"

    def test_split(self) -> None:
        assert apply_chain("a|b|c", ["split(|, 1)"]) == "b"

    def test_normalize_ws(self) -> None:
        assert apply_chain("  a\n\n  b  ", ["normalize_ws"]) == "a b"

    def test_unknown_transform_is_rejected(self) -> None:
        with pytest.raises(TransformError, match="unknown transform"):
            apply_chain("x", ["exec_python"])

    def test_validation_catches_typos_before_the_crawl(self) -> None:
        """A typo should fail at recipe test, not three hundred pages in."""
        with pytest.raises(TransformError):
            validate_chain(["to_numbr"])
        validate_chain(["trim", "to_number", "default(0)"])


# ------------------------------------------------------------- css extract

LISTING = """<html><head>
<link rel="canonical" href="/p?id=1">
</head><body>
<ul>
 <li class="product-item">
   <h3><a href="/product/1">노트북 A</a></h3>
   <span class="price">1,290,000원</span>
   <img data-src="/i/1.jpg" src="placeholder.gif">
 </li>
 <li class="product-item">
   <h3><a href="/product/2">노트북 B</a></h3>
   <span class="price">990,000원</span>
   <img src="/i/2.jpg">
 </li>
 <li class="product-item">
   <h3><a href="/product/3">노트북 C</a></h3>
   <span class="price">품절</span>
 </li>
</ul>
<div class="banner">광고</div>
<a href="mailto:x@y.com">mail</a>
<a href="/style.css">css</a>
<a href="#top">anchor</a>
<a href="/next">next</a>
<script>var x = 1;</script>
</body></html>"""

SPEC = CssSpec(
    container="li.product-item",
    fields=(
        FieldSpec("title", "h3 > a", "text"),
        FieldSpec("price", "span.price", "text", transform=("to_number",)),
        FieldSpec("url", "a", "href", transform=("to_absolute_url",)),
        FieldSpec("image", "img", "src", transform=("to_absolute_url",)),
    ),
)


class TestCssExtractor:
    @pytest.fixture
    def result(self):  # type: ignore[no-untyped-def]
        return CssExtractor(SPEC).extract(response(LISTING))

    def test_one_record_per_container(self, result) -> None:  # type: ignore[no-untyped-def]
        assert len(result.records) == 3

    def test_fields_are_transformed(self, result) -> None:  # type: ignore[no-untyped-def]
        assert result.records[0]["title"] == "노트북 A"
        assert result.records[0]["price"] == 1290000
        assert result.records[0]["url"] == "https://shop.test/product/1"

    def test_missing_field_is_none_not_an_error(self, result) -> None:  # type: ignore[no-untyped-def]
        """A missing field is data - it feeds the fill-rate metric that gates
        recipe activation in Phase 3."""
        assert result.records[2]["price"] is None
        assert result.records[2]["image"] is None
        assert result.records[2]["title"] == "노트북 C"

    def test_data_src_beats_placeholder_src(self, result) -> None:  # type: ignore[no-untyped-def]
        """Lazy loading puts the real URL in data-src and a spacer in src."""
        assert result.records[0]["image"] == "https://shop.test/i/1.jpg"

    def test_canonical_is_extracted(self, result) -> None:  # type: ignore[no-untyped-def]
        assert result.canonical_url == "/p?id=1"

    def test_visible_text_excludes_scripts(self, result) -> None:  # type: ignore[no-untyped-def]
        assert result.text is not None
        assert "노트북 A" in result.text
        assert "var x" not in result.text

    def test_extractor_name_is_recorded(self, result) -> None:  # type: ignore[no-untyped-def]
        assert result.extractor == "css"

    def test_empty_records_are_dropped(self) -> None:
        """A container that matches structure but no content is noise, and
        counting it would make the fill-rate metric lie."""
        html = '<html><body><li class="product-item"></li></body></html>'
        assert CssExtractor(SPEC).extract(response(html)).records == ()

    def test_page_without_a_container_yields_one_record(self) -> None:
        """The detail-page shape."""
        spec = CssSpec(fields=(FieldSpec("title", "h1", "text"),))
        html = "<html><body><h1>상품 상세</h1></body></html>"
        records = CssExtractor(spec).extract(response(html)).records
        assert records == ({"title": "상품 상세"},)

    def test_supports_html_only(self) -> None:
        e = CssExtractor(SPEC)
        assert e.supports(response("<html></html>", ct="text/html"))
        assert not e.supports(response(b"{}", ct="application/json"))


class TestLinkExtraction:
    def test_page_links_are_kept(self) -> None:
        tree, _ = parse(response(LISTING))
        links = extract_links(tree, "https://shop.test/list")
        assert "/product/1" in links
        assert "/next" in links

    @pytest.mark.parametrize("junk", ["mailto:x@y.com", "/style.css", "#top"])
    def test_non_pages_are_filtered(self, junk: str) -> None:
        """Not scope enforcement - that is the gate's job. This is about not
        spending a normalise call on a mailto link."""
        tree, _ = parse(response(LISTING))
        assert junk not in extract_links(tree, "https://shop.test/list")

    def test_duplicates_within_a_page_collapse(self) -> None:
        html = '<html><body><a href="/x">1</a><a href="/x">2</a></body></html>'
        tree, _ = parse(response(html))
        assert extract_links(tree, "https://a.test/") == ("/x",)

    def test_following_can_be_switched_off(self) -> None:
        """Collect mode extracts the seeds and stops."""
        spec = CssSpec(container="li.product-item", fields=SPEC.fields, follow_links=False)
        assert CssExtractor(spec).extract(response(LISTING)).links == ()

    def test_missing_canonical_is_none(self) -> None:
        tree, _ = parse(response("<html><body>x</body></html>"))
        assert extract_canonical(tree) is None
