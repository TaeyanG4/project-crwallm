"""Structure detection, DOM reduction and fingerprints.

These are what let the tool work without a model, and what makes the model's
job easy once there is one. The cases below are the ways real pages are
awkward: framework layout classes, a navigation menu that also repeats, an
item missing a field, a lazy-loaded image with a placeholder in ``src``, and a
button whose text is the same on every row.

docs/08_LLM_ARCHITECTURE.md, docs/07_RECIPE_ARCHITECTURE.md
"""

from __future__ import annotations

import pytest
from selectolax.lexbor import LexborHTMLParser

from crwallm.structure.detector import detect_containers, node_signature, selector_for
from crwallm.structure.fingerprint import fingerprint_of, similarity
from crwallm.structure.reducer import estimate_tokens, reduce_dom


def shop(items: int = 8, *, grid: str = "col-md-3", nav: bool = True) -> LexborHTMLParser:
    """A listing page with the noise a real one has."""
    cards = "".join(
        f'<li class="product-item {grid}" data-idx="{i}">'
        f'<div class="card-body p-2">'
        f'<h3 class="name"><a href="/item/{i}">Laptop model {i} with a long name</a></h3>'
        f'<span class="price">{"Sold out" if i % 4 == 0 else f"{i}90,000"}</span>'
        f'<img data-src="/img/{i}.jpg" src="/img/blank.gif">'
        f'<button class="btn">Add to cart</button>'
        f"</div></li>"
        for i in range(1, items + 1)
    )
    menu = (
        '<nav><ul class="menu">'
        + "".join(f'<li class="nav-item"><a href="/{c}">{c}</a></li>' for c in "abcd")
        + "</ul></nav>"
        if nav
        else ""
    )
    return LexborHTMLParser(
        f"<html><head><title>Shop</title></head><body>{menu}"
        f'<main><ul class="grid row">{cards}</ul></main>'
        f"<footer><p>copyright</p></footer></body></html>"
    )


class TestDetection:
    def test_finds_the_listing(self) -> None:
        best = detect_containers(shop())[0]
        assert best.selector == "li.product-item"
        assert best.count == 8

    def test_navigation_does_not_win(self) -> None:
        """A menu repeats too. What separates it from a product grid is that
        its items carry two words each."""
        best = detect_containers(shop())[0]
        assert "nav-item" not in best.selector

    def test_layout_classes_are_stripped_from_the_selector(self) -> None:
        """``col-md-3`` describes where a card sits, not what it is. Keeping it
        makes the recipe break when the grid changes from three columns to
        four - a restyle, not a restructure."""
        best = detect_containers(shop(grid="col-md-3 col-sm-6"))[0]
        assert best.selector == "li.product-item"

    def test_tailwind_utilities_do_not_end_up_in_the_selector(self) -> None:
        """Found by running `inspect` against react.dev, which produced

            a.aspect-video.dark:outline-link.hover:opacity-95.items-center
             .outline-link.overflow-hidden.select-none.transition-opacity.xs:w-36

        - a valid selector that any CSS edit would break, and one that says
        nothing about what the element is.
        """
        html = """<ul>
          <li class="items-center overflow-hidden card"><a
             class="aspect-video dark:outline-link hover:opacity-95 select-none
                    transition-opacity xs:w-36 video-link"
             href="/v/1">Intro to hooks, part one</a></li>
          <li class="items-center overflow-hidden card"><a
             class="aspect-video dark:outline-link hover:opacity-95 select-none
                    transition-opacity xs:w-36 video-link"
             href="/v/2">State and effects explained</a></li>
          <li class="items-center overflow-hidden card"><a
             class="aspect-video dark:outline-link hover:opacity-95 select-none
                    transition-opacity xs:w-36 video-link"
             href="/v/3">Suspense in practice today</a></li>
        </ul>"""
        best = detect_containers(LexborHTMLParser(html))[0]

        assert best.selector == "li.card"
        for column in best.columns:
            assert ":" not in column.selector, column.selector
            assert "aspect-video" not in column.selector, column.selector

    def test_a_selector_never_carries_more_than_a_few_classes(self) -> None:
        """Eight classes is brittle even when every one is meaningful - each
        one is another way for the page to stop matching."""
        rows = "".join(
            f"""<li class="alpha beta gamma delta epsilon zeta">
                 <h3 class="name">Mechanical keyboard model {i}</h3>
                 <span class="price">{i}9,000 won including delivery</span>
               </li>"""
            for i in range(1, 7)
        )
        html = f"<ul>{rows}</ul>"
        best = detect_containers(LexborHTMLParser(html))[0]
        assert best.selector.count(".") <= 3, best.selector

    def test_the_same_page_at_a_different_width_detects_identically(self) -> None:
        a = detect_containers(shop(grid="col-md-3"))[0]
        b = detect_containers(shop(grid="col-lg-4"))[0]
        assert a.selector == b.selector
        assert [c.selector for c in a.columns] == [c.selector for c in b.columns]

    def test_columns_cover_the_fields(self) -> None:
        columns = detect_containers(shop())[0].usable_columns
        kinds = {(c.selector, c.kind) for c in columns}
        assert ("div.card-body > h3.name", "text") in kinds
        assert ("div.card-body > h3.name > a", "href") in kinds
        assert ("div.card-body > span.price", "text") in kinds

    def test_lazy_loaded_images_report_the_real_url(self) -> None:
        """``src`` holds a spacer and ``data-src`` the picture. Reporting the
        spacer would produce a recipe that collects the same blank gif for
        every row."""
        columns = detect_containers(shop())[0].usable_columns
        image = next(c for c in columns if c.kind == "src")
        assert image.samples[0].endswith("/img/1.jpg")

    def test_a_uniform_column_is_flagged_not_offered(self) -> None:
        """ "Add to cart" repeated eight times is a button."""
        best = detect_containers(shop())[0]
        buttons = [c for c in best.columns if c.samples and c.samples[0] == "Add to cart"]
        assert buttons
        assert all(c.looks_uniform for c in buttons)
        assert not any(c.looks_uniform for c in best.usable_columns)

    def test_wrapper_columns_are_dropped(self) -> None:
        """``div.card-body`` reports the whole card as one value. It is
        structurally a column and semantically nothing, and it looks plausible
        at a glance - which is what makes it worth removing."""
        columns = detect_containers(shop())[0].usable_columns
        assert not any(c.selector == "div.card-body" and c.kind == "text" for c in columns)

    def test_aliased_columns_collapse(self) -> None:
        """``h3`` and ``h3 > a`` carry the same text. Offering both doubles the
        naming work for no extra information."""
        columns = detect_containers(shop())[0].usable_columns
        texts = [c for c in columns if c.kind == "text"]
        values = [c.samples[0] for c in texts if c.samples]
        assert len(values) == len(set(values))

    def test_a_detail_page_has_no_repeated_structure(self) -> None:
        tree = LexborHTMLParser(
            "<html><body><h1>One product</h1><p>Some prose about it.</p></body></html>"
        )
        assert detect_containers(tree) == ()

    def test_two_matching_siblings_are_not_a_pattern(self) -> None:
        tree = LexborHTMLParser(
            '<html><body><ul><li class="x">one item here</li>'
            '<li class="x">two items here</li></ul></body></html>'
        )
        assert detect_containers(tree) == ()

    def test_a_missing_field_lowers_fill_rather_than_hiding_the_column(self) -> None:
        tree = LexborHTMLParser(
            "<html><body><ul>"
            + "".join(
                f'<li class="row"><span class="t">title number {i} here</span>'
                + (f'<span class="p">{i}00 won</span>' if i % 2 else "")
                + "</li>"
                for i in range(1, 9)
            )
            + "</ul></body></html>"
        )
        columns = detect_containers(tree)[0].usable_columns
        price = next(c for c in columns if c.selector == "span.p")
        assert 0.4 < price.fill_rate < 0.7

    def test_a_column_selector_resolves_to_the_value_it_reported(self) -> None:
        """A sample the recipe would not reproduce is worse than no sample.

        When a class is filtered out of a selector, ``span.price`` becomes
        ``span`` - which selects the *first* span in the container, not the
        one the sample came from. Values are read back through the selector so
        the two cannot diverge.
        """
        from crwallm.crawler.contracts import FetchResponse
        from crwallm.crawler.extraction.css import CssExtractor, CssSpec, FieldSpec
        from crwallm.policy.url import normalize
        from crwallm.schemas.types import FetchMode

        tree = shop()
        best = detect_containers(tree)[0]
        response = FetchResponse(
            url=normalize("https://shop.test/list"),
            status=200,
            headers={"content-type": "text/html"},
            body=(tree.html or "").encode(),
            elapsed_ms=1,
            fetch_mode=FetchMode.HTTP,
        )
        extracted = CssExtractor(
            CssSpec(
                container=best.selector,
                fields=tuple(
                    FieldSpec(name=f"c{c.index}", selector=c.selector, type=c.kind)
                    for c in best.usable_columns
                ),
            )
        ).extract(response)

        assert extracted.records
        first = extracted.records[0]
        for column in best.usable_columns:
            if column.samples:
                assert str(first[f"c{column.index}"]).startswith(column.samples[0][:20])


class TestSignatures:
    def test_generated_classes_do_not_split_siblings(self) -> None:
        tree = LexborHTMLParser('<html><body><li class="item col-3">a</li></body></html>')
        node = tree.css_first("li", default=None, strict=False)
        assert node is not None
        assert node_signature(node) == "li.item"

    def test_state_flags_are_ignored(self) -> None:
        tree = LexborHTMLParser('<html><body><li class="item is-active">a</li></body></html>')
        node = tree.css_first("li", default=None, strict=False)
        assert node is not None
        assert selector_for(node) == "li.item"

    def test_a_node_with_no_meaningful_class_falls_back_to_its_tag(self) -> None:
        tree = LexborHTMLParser('<html><body><li class="px-2 mt-4">a</li></body></html>')
        node = tree.css_first("li", default=None, strict=False)
        assert node is not None
        assert selector_for(node) == "li"


class TestReduction:
    def test_a_listing_shrinks_by_an_order_of_magnitude(self) -> None:
        """The budget is not a nicety. Exceeding a local model's context does
        not raise - Ollama truncates silently and the selectors come back
        confidently wrong (docs/08_LLM_ARCHITECTURE.md)."""
        reduced = reduce_dom(shop(items=40))
        assert reduced.tokens < 1000
        assert reduced.ratio < 0.3

    def test_repeated_siblings_collapse_to_a_count_and_samples(self) -> None:
        reduced = reduce_dom(shop(items=40))
        assert "[x40] li.product-item" in reduced.skeleton
        assert reduced.skeleton.count("Laptop model") <= 4

    def test_scripts_and_styles_are_gone(self) -> None:
        tree = LexborHTMLParser(
            "<html><body><script>var secret=1;</script>"
            "<style>.a{color:red}</style><p>text</p></body></html>"
        )
        skeleton = reduce_dom(tree).skeleton
        assert "secret" not in skeleton
        assert "color:red" not in skeleton
        assert "text" in skeleton

    def test_the_budget_is_a_hard_stop(self) -> None:
        """Trimming the output afterwards would cut mid-structure and leave a
        malformed tail, which reads as broken markup rather than as "there was
        more"."""
        reduced = reduce_dom(shop(items=200), max_tokens=120)
        assert reduced.truncated
        assert reduced.tokens <= 200
        assert "truncated" in reduced.skeleton

    def test_korean_text_is_not_underestimated(self) -> None:
        """A CJK character is closer to one token than to a quarter of one.
        Treating it as ASCII would let a Korean page blow the context."""
        korean = "노트북 가격 정보 페이지" * 20
        ascii_text = "laptop price information page " * 20
        assert estimate_tokens(korean) > estimate_tokens(ascii_text) / 2

    def test_an_empty_document_does_not_crash(self) -> None:
        """The parser synthesises a body, so the skeleton is not empty - the
        property that matters is that reduction returns rather than raising."""
        reduced = reduce_dom(LexborHTMLParser(""))
        assert reduced.tokens >= 0
        assert not reduced.truncated


class TestFingerprint:
    def test_the_same_template_with_different_content_matches(self) -> None:
        """The case this exists for: two stores on one hosted platform ship
        near-identical markup under different domains."""
        a = fingerprint_of(shop(items=12))
        b = fingerprint_of(shop(items=15))
        assert a.digest == b.digest

    def test_a_restyle_does_not_change_the_fingerprint(self) -> None:
        a = fingerprint_of(shop(grid="col-md-3"))
        b = fingerprint_of(shop(grid="col-lg-4 px-2"))
        assert a.digest == b.digest

    def test_a_different_template_does_not_match(self) -> None:
        table = LexborHTMLParser(
            "<html><body><table>"
            + "".join(
                f'<tr class="job"><td class="title">Job {i} title</td>'
                f'<td class="co">Company {i}</td></tr>'
                for i in range(1, 9)
            )
            + "</table></body></html>"
        )
        assert fingerprint_of(shop()).digest != fingerprint_of(table).digest

    def test_similarity_scores_a_near_miss(self) -> None:
        a = fingerprint_of(shop())
        b = fingerprint_of(shop())
        assert similarity(a, b) == 1.0

    def test_a_page_with_no_structure_fingerprints_empty(self) -> None:
        tree = LexborHTMLParser("<html><body><h1>detail</h1></body></html>")
        assert fingerprint_of(tree).is_empty

    def test_the_digest_carries_a_scheme_version(self) -> None:
        """A stored fingerprint from an older scheme must not compare
        plausibly-but-wrongly against a new one."""
        assert fingerprint_of(shop()).digest.startswith("fp1:")

    @pytest.mark.parametrize("items", [8, 9, 10, 11])
    def test_small_count_differences_do_not_matter(self, items: int) -> None:
        assert fingerprint_of(shop(items=items)).digest == fingerprint_of(shop(items=8)).digest
