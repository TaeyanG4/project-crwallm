"""Column names read off the page, with no model.

The picker opens with every box filled so that the first result costs one
click. That only works if the names are worth keeping - a page that arrives
labelled 항목1..항목5 has moved the typing, not removed it.

The source is the selector the structure detector already cleaned: layout and
utility classes are gone by then, so what is left is usually what the author
called the thing.
"""

from __future__ import annotations

import pytest

from crwallm.desktop.naming import FALLBACK, name_columns, name_for


class TestFromTheMarkup:
    @pytest.mark.parametrize(
        ("selector", "expected"),
        [
            ("span > small.author", "작성자"),
            ("span.price", "가격"),
            ("h3.name", "이름"),
            ("div.tags > a.tag", "태그"),
            ("span.text", "내용"),
            ("time.published", "날짜"),
            ("div.product-title", "제목"),
            ("div.product_title", "제목"),
            ("span.productTitle", "제목"),
        ],
    )
    def test_the_class_the_author_wrote_wins(self, selector: str, expected: str) -> None:
        assert name_for(selector, "text") == expected

    def test_only_the_last_step_describes_the_value(self) -> None:
        """``div.price > span.text`` sits inside a price and holds text.

        The leading steps say where it is, not what it is, and reading them
        would name half a page "가격"."""
        assert name_for("div.price > span.author", "text") == "작성자"

    def test_the_last_class_wins(self) -> None:
        """``a.btn.add-to-cart`` names itself at the end; the first class is
        what it looks like, the last is what it does."""
        assert name_for("a.btn.title", "text") == "제목"

    def test_inside_a_class_the_first_known_word_wins(self) -> None:
        """books.toscrape.com prints its prices in ``p.price_color``.

        English puts the head noun last, so "price color" ought to be a colour
        - and reading it that way labelled the price column 색상 on a real
        page. Whatever the grammar says, the author was naming a price.
        """
        assert name_for("p.price_color", "text") == "가격"
        assert name_for("div.product-title", "text") == "제목"

    def test_an_unknown_class_is_kept_rather_than_invented(self) -> None:
        """ "sku" tells the person which column it is. Turning it into
        "품목번호" would be a guess wearing a translation's clothes."""
        assert name_for("span.sku", "text") == "sku"
        assert name_for("span.item-code", "text") == "item code"

    def test_the_tag_answers_when_the_classes_do_not(self) -> None:
        assert name_for("h2", "text") == "제목"
        assert name_for("img", "src") == "이미지"
        assert name_for("time", "text") == "날짜"

    def test_the_kind_is_the_last_resort(self) -> None:
        assert name_for("span", "href") == "링크"
        assert name_for("span", "src") == "이미지"
        assert name_for("span", "text") == FALLBACK

    def test_a_link_inside_a_heading_is_a_title(self) -> None:
        """Reading the kind first would call this "링크" and lose the one
        column anybody actually wants."""
        assert name_for("h3.name > a", "href") == "링크"
        assert name_for("h3.title", "href") == "제목"


class TestTheWholePicker:
    def test_every_column_gets_a_name(self) -> None:
        """An empty box is a decision the person has to make before the button
        does anything, which is the thing this removed."""
        columns = [
            {"selector": "span.text", "kind": "text"},
            {"selector": "span > small.author", "kind": "text"},
            {"selector": "span > a", "kind": "href"},
            {"selector": "div.tags > a.tag", "kind": "text"},
            {"selector": "div.tags > a.tag", "kind": "href"},
        ]
        names = name_columns(columns)

        assert all(names), names
        assert len(names) == len(columns)

    def test_a_text_and_its_link_are_told_apart_by_what_they_are(self) -> None:
        """The same markup usually appears twice - the thing, and the URL
        under it. ``a.hnuser`` is the poster and the poster's profile. Naming
        those "hnuser" and "hnuser2" is unique and says nothing."""
        columns = [
            {"selector": "div.tags > a.tag", "kind": "text"},
            {"selector": "div.tags > a.tag", "kind": "href"},
        ]
        assert name_columns(columns) == ["태그", "태그 링크"]

    def test_repeats_are_numbered_rather_than_dropped(self) -> None:
        """A record is a dict: two columns called "링크" is one column, and the
        second silently overwrites the first. Blanking the repeat was the
        earlier fix and it put an empty box back on screen."""
        columns = [
            {"selector": "span > a", "kind": "href"},
            {"selector": "div.tags > a", "kind": "href"},
            {"selector": "footer > a", "kind": "href"},
        ]
        names = name_columns(columns)

        assert names == ["링크", "링크2", "링크3"]
        assert len(set(names)) == len(names)

    def test_a_missing_selector_still_produces_something(self) -> None:
        """The container itself can hold the value, and then there is no
        selector at all."""
        assert name_columns([{"selector": "", "kind": "text"}]) == [FALLBACK]
