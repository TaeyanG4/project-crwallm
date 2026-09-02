"""The desktop bridge against a real server.

``tests/unit/test_desktop_ui.py`` checks that the two halves use the same
names. This checks that the values behind those names are the shape the window
draws: a missing key here renders an empty table with no error, which is the
failure mode this path is most prone to because nothing on either side is
typed.

The fixture is the same adversarial one the crawler tests use - a listing page
with framework classes, a repeating nav menu and a row with a missing field -
rather than a clean page written to make this pass.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from crwallm.desktop.bridge import MAX_PREVIEW_ROWS, Bridge
from tests.fixtures.malicious_server.server import MaliciousServer, RunningServer

pytestmark = pytest.mark.integration

# What app.js reads off each response. Written out rather than derived: this is
# the contract, and it should take an edit to change it.
LOOK_KEYS = {"ok", "url", "columns", "count", "hint"}
COLUMN_KEYS = {"index", "selector", "samples", "kind", "suggested"}
COLLECT_KEYS = {"ok", "rows", "total", "shown", "pages", "failed", "cancelled", "hint"}


@pytest.fixture(scope="module")
def server() -> Iterator[RunningServer]:
    s = MaliciousServer()
    try:
        yield s.start()
    finally:
        s.stop()


@pytest.fixture
def bridge() -> Iterator[Bridge]:
    b = Bridge(allow_local=True)
    try:
        yield b
    finally:
        b._shutdown()


class TestLook:
    def test_a_listing_page_comes_back_as_columns_with_samples(
        self, bridge: Bridge, server: RunningServer
    ) -> None:
        found = bridge.look(server.url("/shop"))

        assert found["ok"] is True, found.get("error")
        assert set(found) >= LOOK_KEYS
        assert found["count"] >= 8
        assert found["columns"], "a page of eight product cards produced no columns"

        for column in found["columns"]:
            assert set(column) >= COLUMN_KEYS, sorted(COLUMN_KEYS - set(column))
            # The samples are the entire interface. A column with none of them
            # is a row in the picker with nothing to read.
            assert column["samples"], column

    def test_the_samples_are_what_is_on_the_page(
        self, bridge: Bridge, server: RunningServer
    ) -> None:
        found = bridge.look(server.url("/shop"))
        shown = [s for column in found["columns"] for s in column["samples"]]
        assert any("Laptop model" in s for s in shown), shown

    def test_a_detail_page_says_so_instead_of_returning_nothing(
        self, bridge: Bridge, server: RunningServer
    ) -> None:
        """No list here is an answer, and it has to be a sentence.

        An empty picker with no explanation is the moment someone decides the
        program is broken.
        """
        found = bridge.look(server.url("/shop/item/1"))

        assert found["ok"] is True
        assert found["count"] == 0
        assert found["columns"] == []
        assert found["hint"], "a page with no repetition must explain itself"

    def test_a_refused_address_is_a_sentence_not_a_traceback(self, bridge: Bridge) -> None:
        failed = bridge.look("http://127.0.0.1:1/")

        assert failed["ok"] is False
        assert failed["error"]
        assert "Error" not in failed["error"] and "Traceback" not in failed["error"]

    def test_an_empty_address_asks_for_one(self, bridge: Bridge) -> None:
        assert bridge.look("  ")["ok"] is False


class TestCollect:
    def test_the_table_has_exactly_the_columns_that_were_named(
        self, bridge: Bridge, server: RunningServer
    ) -> None:
        url = server.url("/shop")
        found = bridge.look(url)
        picks = [{"index": found["columns"][0]["index"], "name": "제품"}]

        out = bridge.collect(url, picks, {"max_pages": 1})

        assert out["ok"] is True, out.get("error")
        assert set(out) >= COLLECT_KEYS
        assert out["rows"], "the page the picker was built from produced no rows"
        assert {key for row in out["rows"] for key in row} == {"제품"}

    def test_nothing_named_is_refused_before_any_fetching(
        self, bridge: Bridge, server: RunningServer
    ) -> None:
        out = bridge.collect(server.url("/shop"), [{"index": 0, "name": "   "}], {})
        assert out["ok"] is False
        assert out["error"]

    def test_two_columns_cannot_share_a_name(self, bridge: Bridge, server: RunningServer) -> None:
        """A record is a dict. Two columns called the same thing is one column,
        and the person who named them both gets half of what they asked for
        with nothing saying so."""
        url = server.url("/shop")
        found = bridge.look(url)
        picks = [
            {"index": found["columns"][0]["index"], "name": "값"},
            {"index": found["columns"][1]["index"], "name": "값"},
        ]

        out = bridge.collect(url, picks, {"max_pages": 1})

        assert out["ok"] is False
        assert "값" in out["error"]

    def test_a_suggested_name_is_handed_out_once(
        self, bridge: Bridge, server: RunningServer
    ) -> None:
        """The picker must not open with a collision already in it."""
        found = bridge.look(server.url("/shop"))
        suggested = [c["suggested"] for c in found["columns"] if c["suggested"]]

        assert len(suggested) == len(set(suggested)), suggested

    def test_following_pages_reaches_more_than_the_one_pasted(
        self, bridge: Bridge, server: RunningServer
    ) -> None:
        """The checkbox has to do something, and it is the only knob offered."""
        url = server.url("/shop")
        found = bridge.look(url)
        picks = [{"index": found["columns"][0]["index"], "name": "제품"}]

        one = bridge.collect(url, picks, {"max_pages": 1})
        many = bridge.collect(url, picks, {"max_pages": 20})

        assert many["pages"] > one["pages"]
        assert many["total"] > one["total"]

    def test_the_screen_gets_a_slice_and_the_count_stays_honest(
        self, bridge: Bridge, server: RunningServer
    ) -> None:
        url = server.url("/shop")
        found = bridge.look(url)
        picks = [{"index": found["columns"][0]["index"], "name": "제품"}]

        out = bridge.collect(url, picks, {"max_pages": 20})

        assert len(out["rows"]) <= MAX_PREVIEW_ROWS
        assert out["shown"] == len(out["rows"])
        assert out["total"] >= out["shown"]


class TestSave:
    def test_the_file_gets_every_row_not_the_preview(
        self, bridge: Bridge, server: RunningServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        url = server.url("/shop")
        found = bridge.look(url)
        picks = [{"index": found["columns"][0]["index"], "name": "제품"}]
        out = bridge.collect(url, picks, {"max_pages": 20})

        target = tmp_path / "out.csv"
        monkeypatch.setattr(bridge, "_ask_where", lambda fmt: str(target))

        saved = bridge.save("csv")

        assert saved["ok"] is True
        assert saved["rows"] == out["total"]
        assert len(target.read_text(encoding="utf-8-sig").strip().splitlines()) == out["total"] + 1

    def test_excel_gets_the_byte_order_mark_it_needs(
        self, bridge: Bridge, server: RunningServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without it Excel reads the file as the system codepage and every
        Korean column heading arrives as mojibake."""
        url = server.url("/shop")
        found = bridge.look(url)
        bridge.collect(url, [{"index": found["columns"][0]["index"], "name": "제품"}], {})

        target = tmp_path / "out.csv"
        monkeypatch.setattr(bridge, "_ask_where", lambda fmt: str(target))
        bridge.save("csv")

        raw = target.read_bytes()
        assert raw.startswith(b"\xef\xbb\xbf")
        assert "제품" in raw.decode("utf-8-sig").splitlines()[0]

    def test_saving_nothing_is_refused(self, bridge: Bridge) -> None:
        assert bridge.save("csv")["ok"] is False

    def test_a_cancelled_dialog_is_not_an_error(
        self, bridge: Bridge, server: RunningServer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Closing the file picker is a decision, not a failure, and the window
        must not show a red message for it."""
        url = server.url("/shop")
        found = bridge.look(url)
        bridge.collect(url, [{"index": found["columns"][0]["index"], "name": "제품"}], {})

        monkeypatch.setattr(bridge, "_ask_where", lambda fmt: None)
        saved = bridge.save("csv")

        assert saved["cancelled"] is True
        assert "error" not in saved
