"""Does this build actually work?

A packaged application fails in a way a running one does not: a module that
was never bundled, a data file that did not come along, a compiled extension
that will not load. None of it shows up until the moment it is needed, and by
then the person is looking at a window that did nothing.

So the executable can prove itself. ``CRWALLM.exe --self-test`` runs the whole
path - fetch a page, find the repeating structure, extract from it, write a
CSV - and says what happened. It is how the build is verified before anyone
ships it, and it is the thing to run when the app misbehaves on a machine
nobody can debug from here.

Deliberately one known page rather than a URL the caller supplies: the answer
has to be checkable, and "10 rows from quotes.toscrape.com" is checkable.
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

__all__ = ["run_self_test"]

PAGE = "https://quotes.toscrape.com/"
EXPECT_ROWS = 10
"""What that page holds. If this ever changes the test says so rather than
quietly passing on one row."""


def run_self_test() -> tuple[int, str]:
    """Return an exit code and a report anyone can read."""
    from crwallm.desktop.bridge import Bridge

    lines: list[str] = []
    started = time.monotonic()
    bridge = Bridge()

    def say(text: str) -> None:
        lines.append(text)

    try:
        say(f"실행 파일  {Path(sys.executable).name}")
        say(f"묶인 위치  {getattr(sys, '_MEIPASS', '(묶이지 않음 - 소스에서 실행 중)')}")
        say("")

        from crwallm.desktop.app import ui_root

        index = ui_root() / "index.html"
        say(f"[{'OK' if index.exists() else '실패'}] 화면 파일  {index}")
        if not index.exists():
            return 1, "\n".join(lines)

        looked = bridge.look(PAGE)
        if not looked.get("ok"):
            say(f"[실패] 페이지 읽기  {looked.get('error')}")
            return 1, "\n".join(lines)
        say(f"[OK] 페이지 읽기  {looked['count']}개 반복, 항목 {len(looked['columns'])}종")

        picks = [
            {"index": column["index"], "name": f"c{n}"}
            for n, column in enumerate(looked["columns"][:2])
        ]
        collected = bridge.collect(PAGE, picks, {"max_pages": 1})
        if not collected.get("ok"):
            say(f"[실패] 모으기  {collected.get('error')}")
            return 1, "\n".join(lines)

        rows = collected["total"]
        ok = rows == EXPECT_ROWS
        say(f"[{'OK' if ok else '이상'}] 모으기  {rows}건 (예상 {EXPECT_ROWS}건)")

        target = Path(tempfile.gettempdir()) / "crwallm-self-test.csv"
        # The save dialog needs a window. This is the same writer underneath.
        bridge._ask_where = lambda fmt: str(target)  # type: ignore[method-assign]
        saved = bridge.save("csv")
        if not saved.get("ok"):
            say(f"[실패] 저장  {saved.get('error')}")
            return 1, "\n".join(lines)
        head = target.read_text(encoding="utf-8-sig").splitlines()[0]
        say(f"[OK] 저장  {saved['rows']}건 → {target}")
        say(f"      첫 줄  {head}")

        say("")
        say(f"{time.monotonic() - started:.1f}초. 이 빌드는 정상입니다.")
        return 0 if ok else 1, "\n".join(lines)
    except Exception as exc:
        import traceback

        say("")
        say(f"[실패] {type(exc).__name__}: {exc}")
        say("")
        say(traceback.format_exc())
        return 1, "\n".join(lines)
    finally:
        bridge._shutdown()
