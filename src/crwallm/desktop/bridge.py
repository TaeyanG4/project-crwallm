"""What the window is allowed to ask the engine to do.

The whole surface, in four verbs: look at a page, collect from it, save the
result, stop. Small on purpose - this is the desktop app's contract, and every
verb here is one the person using it would recognise as a thing they wanted.
The vocabulary of the engine (recipe, container, frontier, spec) does not
appear.

**The work is not here.** It is in ``crwallm.services.quick``, which the
browser calls too. This file is what makes that core usable from a pywebview
window and nothing else: a thread to run it on, a native save dialog, and a
way to push progress into the page. The same four verbs over HTTP live in
``crwallm.api.routers.ui``, and neither copy owns the logic.

**No database in this path.** Paste a URL, name the columns, get a table, save
it. That is a session rather than a system, and a job queue with a schema and
a worker is machinery for questions nobody asked here - history, resume, many
workers. Those earn their way in later if they are missed.

**Threading.** pywebview calls these from its own UI thread and the engine is
async, so a loop runs on a thread of its own and every call is marshalled onto
it. Blocking the UI thread on a crawl would freeze the window - including the
stop button, which is the one control that has to keep working.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import threading
from collections.abc import Coroutine
from pathlib import Path
from typing import Any, TypeVar

from crwallm.services import quick
from crwallm.services.quick import MAX_PREVIEW_ROWS, Picked, Session

__all__ = ["MAX_PREVIEW_ROWS", "Bridge", "Picked"]

T = TypeVar("T")


class Bridge:
    """The object JavaScript sees as ``window.pywebview.api``."""

    def __init__(self, *, allow_local: bool = False) -> None:
        self._allow_local = allow_local
        self._window: Any = None
        self._session = Session()
        self._cancel = threading.Event()

        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, name="crwallm-engine", daemon=True
        )
        self._thread.start()

    # Underscored, and not for style: pywebview exposes every public method of
    # this object to the page, so `attach` and `close` were two more verbs the
    # JavaScript could call. `close` stops the engine loop, after which every
    # later call from the window blocks forever. The docstring above says four
    # verbs; the leading underscore is what makes that true.

    def _attach(self, window: Any) -> None:
        """Given the window once it exists, so progress can be pushed to it."""
        self._window = window

    # ------------------------------------------------------------- plumbing

    def _run(self, coro: Coroutine[Any, Any, T]) -> T:
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result()

    def _emit(self, event: str, payload: dict[str, Any]) -> None:
        """Tell the page something happened.

        One-way and best-effort: a window that has been closed mid-crawl is a
        normal way for this to end, not an error to report.
        """
        if self._window is None:
            return
        with contextlib.suppress(Exception):
            self._window.evaluate_js(
                f"window.crwallm && window.crwallm.on("
                f"{json.dumps(event)}, {json.dumps(payload, default=str)})"
            )

    def _guard(self) -> Any:
        """The SSRF policy for everything this window does.

        Both halves get the same one. They did not: `look` was built with the
        session's guard and `collect` let ``open_crawl`` default to its own, so
        ``--allow-local`` inspected a dev server and then collected nothing
        from it - a crawl that ran, fetched zero pages, and blamed the page.
        """
        from crwallm.policy.local import build_guard

        return build_guard(allow_local=self._allow_local)

    def _shutdown(self) -> None:
        self._cancel.set()
        self._loop.call_soon_threadsafe(self._loop.stop)

    # ---------------------------------------------------------------- verbs

    def look(self, url: str) -> dict[str, Any]:
        """Fetch one page and report what repeats on it."""
        url = quick.normalise_input(url)
        if not url:
            return {"ok": False, "error": "주소를 입력해주세요."}

        try:
            found = self._run(quick.look(url, guard=self._guard()))
        except Exception as exc:
            return {"ok": False, "error": quick.humanise(exc)}

        if found.get("ok"):
            self._session = Session(url=url, container=found.get("container"))
        return found

    def collect(
        self, url: str, picks: list[dict[str, Any]], options: dict[str, Any]
    ) -> dict[str, Any]:
        """Walk the site with the chosen columns and return a table."""
        self._cancel.clear()
        chosen = quick.picks_from(picks)
        refusal = quick.check_names(chosen)
        if refusal:
            return {"ok": False, "error": refusal}

        try:
            result = self._run(
                quick.collect(
                    quick.normalise_input(url),
                    chosen,
                    options or {},
                    guard=self._guard(),
                    cancel=self._cancel,
                    on_progress=lambda payload: self._emit("progress", payload),
                )
            )
        except Exception as exc:
            return {"ok": False, "error": quick.humanise(exc)}

        self._session.rows = result.rows
        self._session.pages = result.pages
        self._session.failed = result.failed
        return result.payload()

    def stop(self) -> dict[str, Any]:
        """Ask the crawl to stop. It finishes the page it is on."""
        self._cancel.set()
        return {"ok": True}

    def save(self, fmt: str = "csv") -> dict[str, Any]:
        """Write what is on screen to a file the person chooses."""
        if not self._session.rows:
            return {"ok": False, "error": "저장할 결과가 없습니다."}

        target = self._ask_where(fmt)
        if target is None:
            return {"ok": False, "cancelled": True}

        try:
            written = quick.write(self._session.rows, Path(target), fmt)
        except OSError as exc:
            return {"ok": False, "error": f"저장하지 못했습니다: {exc}"}
        return {"ok": True, "path": str(target), "rows": written}

    def _ask_where(self, fmt: str) -> str | None:
        """The system's own save dialog.

        Not a path typed into the page: a file picker is the one piece of this
        that a person already knows how to use, and it is the difference
        between "where do you want it" and "type a path". The browser has no
        equivalent, which is why saving is the one verb whose two hosts differ
        below the surface.
        """
        if self._window is None:
            return None
        import webview

        result = self._window.create_file_dialog(
            webview.SAVE_DIALOG,
            save_filename=quick.suggested_filename(self._session.url, fmt),
            file_types=(f"{fmt.upper()} (*.{fmt})", "All files (*.*)"),
        )
        if not result:
            return None
        return result if isinstance(result, str) else result[0]
