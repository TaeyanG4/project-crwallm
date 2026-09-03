"""What the window is allowed to ask the engine to do.

The whole surface, in four verbs: look at a page, collect from it, save the
result, stop. Small on purpose - this is the desktop app's contract, and every
verb here is one the person using it would recognise as a thing they wanted.
The vocabulary of the engine (recipe, container, frontier, spec) does not
appear.

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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import urlsplit

from crwallm.desktop.naming import name_columns

__all__ = ["Bridge", "Picked"]

T = TypeVar("T")

MAX_PREVIEW_ROWS = 500
"""How many rows the window is handed.

The table is read by a person, and a browser asked to lay out fifty thousand
rows stops being a window and becomes a progress bar. The file gets all of
them; the screen gets the first few hundred."""


@dataclass(frozen=True, slots=True)
class Picked:
    """One column the person chose, and what they called it."""

    index: int
    name: str


@dataclass(slots=True)
class Session:
    """What the window is currently working on.

    Held in memory because that is what it is - the thing on screen. Saving is
    an explicit act, and a tool that silently kept every experiment would be
    filling a database with pages nobody asked to keep.
    """

    url: str = ""
    container: str | None = None
    rows: list[dict[str, Any]] = field(default_factory=list)
    pages: int = 0
    failed: int = 0


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
        """Fetch one page and report what repeats on it.

        The columns come back with *samples*, because that is the whole
        interaction: reading "Albert Einstein" and typing "작가" is something
        anyone can do, and writing ``span > small.author`` is not.
        """
        url = (url or "").strip()
        if not url:
            return {"ok": False, "error": "주소를 입력해주세요."}
        if not urlsplit(url).scheme:
            url = f"https://{url}"

        try:
            return self._run(self._look(url))
        except Exception as exc:
            return {"ok": False, "error": _humanise(exc)}

    def collect(
        self, url: str, picks: list[dict[str, Any]], options: dict[str, Any]
    ) -> dict[str, Any]:
        """Walk the site with the chosen columns and return a table."""
        self._cancel.clear()
        chosen = [
            Picked(index=int(p["index"]), name=str(p["name"]).strip())
            for p in picks
            if str(p.get("name", "")).strip()
        ]
        if not chosen:
            return {"ok": False, "error": "모을 항목을 하나 이상 골라주세요."}

        # A record is a dict, so two columns with one name is not two columns:
        # the second overwrites the first and half of what was asked for
        # silently never appears. Better to say so than to lose it.
        names = [c.name for c in chosen]
        repeated = sorted({n for n in names if names.count(n) > 1})
        if repeated:
            return {
                "ok": False,
                "error": f"이름이 겹칩니다: {', '.join(repeated)}. 서로 다른 이름을 지어주세요.",
            }

        try:
            return self._run(self._collect(url, chosen, options or {}))
        except Exception as exc:
            return {"ok": False, "error": _humanise(exc)}

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
            written = _write(self._session.rows, Path(target), fmt)
        except OSError as exc:
            return {"ok": False, "error": f"저장하지 못했습니다: {exc}"}
        return {"ok": True, "path": str(target), "rows": written}

    def _ask_where(self, fmt: str) -> str | None:
        """The system's own save dialog.

        Not a path typed into the page: a file picker is the one piece of this
        that a person already knows how to use, and it is the difference
        between "where do you want it" and "type a path".
        """
        if self._window is None:
            return None
        import webview

        host = urlsplit(self._session.url).hostname or "crwallm"
        result = self._window.create_file_dialog(
            webview.SAVE_DIALOG,
            save_filename=f"{host.replace('.', '-')}.{fmt}",
            file_types=(f"{fmt.upper()} (*.{fmt})", "All files (*.*)"),
        )
        if not result:
            return None
        return result if isinstance(result, str) else result[0]

    # ----------------------------------------------------------- the engine

    async def _look(self, url: str) -> dict[str, Any]:
        from crwallm.crawler.contracts import FetchFailure, FetchRequest
        from crwallm.crawler.extraction.css import parse
        from crwallm.crawler.extraction.structured import extract_structured
        from crwallm.crawler.fetching.http import SafeHttpFetcher
        from crwallm.policy.url import normalize
        from crwallm.schemas.types import FetchMode
        from crwallm.structure.detector import detect_containers

        fetcher = SafeHttpFetcher(self._guard())
        try:
            outcome = await fetcher.fetch(
                FetchRequest(
                    url=normalize(url),
                    depth=0,
                    mode=FetchMode.HTTP,
                    timeout_s=20.0,
                    byte_limit=8_000_000,
                )
            )
        finally:
            await fetcher.aclose()

        if isinstance(outcome, FetchFailure):
            return {"ok": False, "error": _fetch_error(outcome)}

        tree, _ = parse(outcome)
        candidates = detect_containers(tree)
        declared = extract_structured(tree)

        title = tree.css_first("title", default=None, strict=False)
        self._session = Session(url=url)

        if not candidates:
            return {
                "ok": True,
                "url": url,
                "title": title.text(strip=True) if title is not None else "",
                "container": None,
                "count": 0,
                "columns": [],
                "hint": (
                    "이 페이지에서는 반복되는 목록을 찾지 못했습니다. "
                    "상품이나 글이 여러 개 나열된 페이지를 알려주시면 더 잘 찾습니다."
                ),
                "declared": list(declared.types())[:4],
            }

        best = candidates[0]
        self._session.container = best.selector
        return {
            "ok": True,
            "url": url,
            "title": title.text(strip=True) if title is not None else "",
            "container": best.selector,
            "count": best.count,
            # Empty rather than absent. The other branch of this function sets
            # it, and a key that exists on one path and not the other is how
            # the window ends up reading `undefined` and showing nothing.
            "hint": "",
            "columns": _columns(best.usable_columns),
            "declared": list(declared.types())[:4],
        }

    async def _collect(
        self, url: str, picks: list[Picked], options: dict[str, Any]
    ) -> dict[str, Any]:
        from crwallm.schemas.events import JobCompleted, PageFailed, PageFetched, RecordsExtracted
        from crwallm.services.crawl import CrawlPlan, open_crawl

        # The page is fetched again rather than remembered from `look`. It is
        # one request, and it means the columns are read from the page as it
        # is now - a listing that changed between looking and collecting would
        # otherwise produce a table of nulls with no explanation.
        looked = await self._look(url)
        if not looked.get("ok"):
            return looked

        spec, extraction = _build(url, looked, picks, options)
        plan = CrawlPlan(spec=spec, extraction=extraction)

        rows: list[dict[str, Any]] = []
        pages = failed = 0

        async with open_crawl(plan, guard=self._guard()) as events:
            async for event in events:
                if self._cancel.is_set():
                    break
                if isinstance(event, RecordsExtracted):
                    rows.extend(event.records)
                elif isinstance(event, PageFetched):
                    pages += 1
                elif isinstance(event, PageFailed):
                    failed += 1
                elif isinstance(event, JobCompleted):
                    break

                if isinstance(event, PageFetched | RecordsExtracted):
                    self._emit(
                        "progress",
                        {"pages": pages, "rows": len(rows), "failed": failed},
                    )

        self._session.rows = rows
        self._session.pages = pages
        self._session.failed = failed

        return {
            "ok": True,
            "rows": rows[:MAX_PREVIEW_ROWS],
            "total": len(rows),
            "shown": min(len(rows), MAX_PREVIEW_ROWS),
            "pages": pages,
            "failed": failed,
            "cancelled": self._cancel.is_set(),
            "hint": _empty_hint(rows, pages) if not rows else "",
        }


# ------------------------------------------------------------------ helpers


def _build(url: str, looked: dict[str, Any], picks: list[Picked], options: dict[str, Any]):  # type: ignore[no-untyped-def]
    """Turn "these columns, this deep" into a spec the engine understands."""
    from urllib.parse import urlsplit as _split

    from crwallm.crawler.extraction.plan import Extraction, Field
    from crwallm.policy.domains import registrable_domain
    from crwallm.schemas.spec import CrawlLimits, CrawlSpec
    from crwallm.schemas.types import CrawlMode

    by_index = {int(c["index"]): c for c in looked["columns"]}
    fields = tuple(
        Field(
            name=p.name,
            path=by_index[p.index]["selector"],
            kind=by_index[p.index]["kind"],
            # A link that only works from the page it was found on is not much
            # use in a spreadsheet.
            transform=("to_absolute_url",) if by_index[p.index]["kind"] == "href" else (),
        )
        for p in picks
        if p.index in by_index
    )

    host = _split(url).hostname or ""
    domain = registrable_domain(host) or host

    # "몇 페이지까지" is the only knob offered, and it maps to following links
    # at all. A person asking for one page means the page they pasted.
    pages = max(1, int(options.get("max_pages", 1)))
    follow = pages > 1

    spec = CrawlSpec(
        seed_urls=(url,),
        allowed_domains=(domain,) if domain else (),
        mode=CrawlMode.COLLECT,
        follow_links=follow,
        limits=CrawlLimits(
            max_pages=pages,
            max_depth=2 if follow else 0,
            global_concurrency=4,
        ),
    )
    return spec, Extraction(container=looked["container"], fields=fields, follow_links=follow)


def _columns(usable: Any) -> list[dict[str, Any]]:
    """The picker's rows, every one of them already named.

    Naming used to be the price of entry: empty boxes, and an empty box means
    "do not collect", so five columns was five decisions before the button did
    anything. Now the button works on arrival and clearing a box is how you
    drop a column - the same rule, without the toll.
    """
    out: list[dict[str, Any]] = [
        {
            "index": column.index,
            # The window never shows this - the whole point is that nobody
            # reads a selector - but `collect` builds the plan from what
            # `look` found, and it has to come from somewhere. Leaving it out
            # was a KeyError waiting for the first person who pressed the
            # button. It is also what the names are read off.
            "selector": column.selector,
            "samples": [s for s in column.samples[:2] if s],
            "kind": column.kind,
            "fill": round(column.fill_rate * 100),
        }
        for column in usable
    ]
    for row, name in zip(out, name_columns(out), strict=True):
        row["suggested"] = name
    return out


def _fetch_error(failure: Any) -> str:
    """The crawler's error taxonomy, in words a person can act on."""
    kind = getattr(failure.error_kind, "value", str(failure.error_kind))
    return {
        "dns_fail": "그 주소를 찾을 수 없습니다. 오타가 없는지 확인해주세요.",
        "conn_refused": "사이트가 응답하지 않습니다.",
        "conn_timeout": "사이트가 너무 느립니다. 잠시 후 다시 시도해주세요.",
        "read_timeout": "사이트가 너무 느립니다. 잠시 후 다시 시도해주세요.",
        "tls_error": "사이트의 보안 인증서에 문제가 있습니다.",
        "http_4xx": "그 페이지를 찾을 수 없습니다.",
        "http_5xx": "사이트에 문제가 생겼습니다. 잠시 후 다시 시도해주세요.",
        "blocked_403": "사이트가 접근을 막았습니다.",
        "blocked_429": "너무 자주 요청했습니다. 잠시 후 다시 시도해주세요.",
        "ssrf_reject": "그 주소로는 갈 수 없습니다.",
        "size_exceeded": "페이지가 너무 큽니다.",
    }.get(kind, f"가져오지 못했습니다 ({kind}).")


def _empty_hint(rows: list[Any], pages: int) -> str:
    if pages == 0:
        return "페이지를 하나도 가져오지 못했습니다."
    return (
        "페이지는 가져왔지만 고른 항목을 찾지 못했습니다. "
        "다른 항목을 골라보시거나, 목록이 있는 다른 페이지를 알려주세요."
    )


def _humanise(exc: Exception) -> str:
    from crwallm.policy.ssrf import SsrfBlockedError
    from crwallm.policy.url import UrlNormalizationError

    if isinstance(exc, UrlNormalizationError):
        return "주소 형식이 올바르지 않습니다."
    if isinstance(exc, SsrfBlockedError):
        return "그 주소로는 갈 수 없습니다."
    return f"문제가 생겼습니다: {type(exc).__name__}"


def _write(rows: list[dict[str, Any]], path: Path, fmt: str) -> int:
    """Every row, not just the ones on screen."""
    import csv

    if fmt == "csv":
        # Column order from the data, in first-seen order. There is no shared
        # helper for this in Python - the web UI has one in TypeScript, which
        # is exactly the kind of thing that gets imported from memory and is
        # not there.
        columns: list[str] = []
        for row in rows:
            for key in row:
                if key not in columns:
                    columns.append(key)
        # utf-8-sig: Excel reads a plain UTF-8 CSV as the system codepage and
        # turns every Korean column into mojibake. The BOM is what tells it.
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        return len(rows)

    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return len(rows)
