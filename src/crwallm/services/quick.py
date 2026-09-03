"""Look at a page, collect from it, write it out.

The short path, with no job, no queue, no database and no recipe: paste a URL,
name what repeats, get a table. It is what the window does, and now what the
browser does too, because both of them call in here.

**Why it is not in the desktop package any more.** It was, and that made the
desktop window the only thing that could do this - a second front end meant a
second copy of the same four verbs, and two copies of anything in this project
have historically drifted until one of them silently produced nothing
(``tests/unit/test_extraction_plan.py`` is the monument to that). One core,
two hosts.

Nothing here knows what a window is. Cancellation arrives as an object with
``is_set``; progress as a callback; where to save as a path someone else
chose. The desktop passes a native file dialog's answer, the browser passes a
temporary file it is about to stream - and neither difference reaches this
module.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from crwallm.services.naming import name_columns

__all__ = [
    "MAX_PREVIEW_ROWS",
    "Cancel",
    "Collected",
    "Picked",
    "Session",
    "check_names",
    "collect",
    "humanise",
    "look",
    "never",
    "normalise_input",
    "picks_from",
    "suggested_filename",
    "write",
]

MAX_PREVIEW_ROWS = 500
"""How many rows a screen is handed.

The table is read by a person, and a browser asked to lay out fifty thousand
rows stops being a page and becomes a progress bar. The file gets all of them;
the screen gets the first few hundred."""

FETCH_TIMEOUT_S = 20.0
LOOK_BYTE_LIMIT = 8_000_000


class Cancel(Protocol):
    """Anything that can say "stop".

    ``threading.Event`` and ``asyncio.Event`` both satisfy this, which is the
    point: the window sets one from its UI thread and the server sets one from
    a request handler, and neither has to care which the other used.
    """

    def is_set(self) -> bool: ...


class _Never:
    def is_set(self) -> bool:
        return False


def never() -> Cancel:
    """A cancel that never fires, for callers with no stop button."""
    return _Never()


@dataclass(frozen=True, slots=True)
class Picked:
    """One column someone chose, and what they called it."""

    index: int
    name: str


@dataclass(slots=True)
class Session:
    """What a front end is currently working on.

    Held in memory because that is what it is - the thing on screen. Saving is
    an explicit act, and a tool that silently kept every experiment would be
    filling a database with pages nobody asked to keep.
    """

    url: str = ""
    container: str | None = None
    rows: list[dict[str, Any]] = field(default_factory=list)
    pages: int = 0
    failed: int = 0


async def look(url: str, *, guard: Any) -> dict[str, Any]:
    """Fetch one page and report what repeats on it.

    The columns come back with *samples* and a name, because that is the whole
    interaction: reading "Albert Einstein" and seeing it called 작성자 is
    something anyone can check, and reading ``span > small.author`` is not.
    """
    from crwallm.crawler.contracts import FetchFailure, FetchRequest
    from crwallm.crawler.extraction.css import parse
    from crwallm.crawler.extraction.structured import extract_structured
    from crwallm.crawler.fetching.http import SafeHttpFetcher
    from crwallm.policy.url import normalize
    from crwallm.schemas.types import FetchMode
    from crwallm.structure.detector import detect_containers

    fetcher = SafeHttpFetcher(guard)
    try:
        outcome = await fetcher.fetch(
            FetchRequest(
                url=normalize(url),
                depth=0,
                mode=FetchMode.HTTP,
                timeout_s=FETCH_TIMEOUT_S,
                byte_limit=LOOK_BYTE_LIMIT,
            )
        )
    finally:
        await fetcher.aclose()

    if isinstance(outcome, FetchFailure):
        return {"ok": False, "error": fetch_error(outcome)}

    tree, _ = parse(outcome)
    candidates = detect_containers(tree)
    declared = extract_structured(tree)
    title = tree.css_first("title", default=None, strict=False)

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
    return {
        "ok": True,
        "url": url,
        "title": title.text(strip=True) if title is not None else "",
        "container": best.selector,
        "count": best.count,
        # Empty rather than absent. The other branch of this function sets it,
        # and a key that exists on one path and not the other is how a front
        # end ends up reading `undefined` and showing nothing.
        "hint": "",
        "columns": columns_of(best.usable_columns),
        "declared": list(declared.types())[:4],
    }


@dataclass(slots=True)
class Collected:
    """Everything that was collected, and the part a screen is shown.

    Two fields rather than one dict with both in it. The full list and the
    preview travel to different places - the preview is serialised and sent
    over a wire, the full list is written to a file - and the moment they share
    a dict, the day comes when fifty thousand rows are JSON-encoded into a
    response because nobody remembered to remove a key.
    """

    rows: list[dict[str, Any]]
    pages: int = 0
    failed: int = 0
    cancelled: bool = False
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    def payload(self) -> dict[str, Any]:
        """What a front end draws. Never the whole table."""
        if self.error:
            return {"ok": False, "error": self.error}
        return {
            "ok": True,
            "rows": self.rows[:MAX_PREVIEW_ROWS],
            "total": len(self.rows),
            "shown": min(len(self.rows), MAX_PREVIEW_ROWS),
            "pages": self.pages,
            "failed": self.failed,
            "cancelled": self.cancelled,
            "hint": empty_hint(self.rows, self.pages) if not self.rows else "",
        }


async def collect(
    url: str,
    picks: list[Picked],
    options: dict[str, Any],
    *,
    guard: Any,
    cancel: Cancel | None = None,
    on_progress: Any = None,
) -> Collected:
    """Walk the site with the chosen columns and return a table."""
    from crwallm.schemas.events import JobCompleted, PageFailed, PageFetched, RecordsExtracted
    from crwallm.services.crawl import CrawlPlan, open_crawl

    stop = cancel if cancel is not None else never()

    # The page is fetched again rather than remembered from `look`. It is one
    # request, and it means the columns are read from the page as it is now - a
    # listing that changed between looking and collecting would otherwise
    # produce a table of nulls with no explanation.
    looked = await look(url, guard=guard)
    if not looked.get("ok"):
        return Collected(rows=[], error=str(looked.get("error", "")))

    spec, extraction = build_plan(url, looked, picks, options)
    plan = CrawlPlan(spec=spec, extraction=extraction)

    rows: list[dict[str, Any]] = []
    pages = failed = 0

    async with open_crawl(plan, guard=guard) as events:
        async for event in events:
            if stop.is_set():
                break
            if isinstance(event, RecordsExtracted):
                rows.extend(event.records)
            elif isinstance(event, PageFetched):
                pages += 1
            elif isinstance(event, PageFailed):
                failed += 1
            elif isinstance(event, JobCompleted):
                break

            if on_progress is not None and isinstance(event, PageFetched | RecordsExtracted):
                on_progress({"pages": pages, "rows": len(rows), "failed": failed})

    return Collected(rows=rows, pages=pages, failed=failed, cancelled=stop.is_set())


def normalise_input(url: str) -> str:
    """What someone typed, made into something fetchable.

    People paste ``shop.example.com``. Refusing that because it has no scheme
    is a correct error message and a bad program.
    """
    url = (url or "").strip()
    if url and not urlsplit(url).scheme:
        url = f"https://{url}"
    return url


def picks_from(raw: list[dict[str, Any]]) -> list[Picked]:
    """Chosen columns, as they arrive from a page.

    Blank names are dropped here rather than rejected: an empty box is how a
    front end says "not this one", and it is the same rule whether the box was
    in a window or in a browser.
    """
    return [
        Picked(index=int(item["index"]), name=str(item.get("name", "")).strip())
        for item in raw
        if str(item.get("name", "")).strip()
    ]


def check_names(picks: list[Picked]) -> str | None:
    """Why these picks cannot be collected, or None.

    A record is a dict, so two columns with one name is not two columns: the
    second overwrites the first and half of what was asked for silently never
    appears. Better to say so than to lose it.
    """
    if not picks:
        return "모을 항목을 하나 이상 골라주세요."
    names = [p.name for p in picks]
    repeated = sorted({n for n in names if names.count(n) > 1})
    if repeated:
        return f"이름이 겹칩니다: {', '.join(repeated)}. 서로 다른 이름을 지어주세요."
    return None


def suggested_filename(url: str, fmt: str = "csv") -> str:
    host = urlsplit(url).hostname or "crwallm"
    return f"{host.replace('.', '-')}.{fmt}"


def build_plan(
    url: str, looked: dict[str, Any], picks: list[Picked], options: dict[str, Any]
) -> tuple[Any, Any]:
    """Turn "these columns, this deep" into a spec the engine understands."""
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

    host = urlsplit(url).hostname or ""
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
        limits=CrawlLimits(max_pages=pages, max_depth=2 if follow else 0, global_concurrency=4),
    )
    return spec, Extraction(container=looked["container"], fields=fields, follow_links=follow)


def columns_of(usable: Any) -> list[dict[str, Any]]:
    """The picker's rows, every one of them already named.

    Naming used to be the price of entry: empty boxes, and an empty box means
    "do not collect", so five columns was five decisions before the button did
    anything. Now the button works on arrival and clearing a box is how you
    drop a column - the same rule, without the toll.
    """
    out: list[dict[str, Any]] = [
        {
            "index": column.index,
            # No front end shows this - the whole point is that nobody reads a
            # selector - but `collect` builds the plan from what `look` found,
            # and it has to come from somewhere. Leaving it out was a KeyError
            # waiting for the first person who pressed the button. It is also
            # what the names are read off.
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


def fetch_error(failure: Any) -> str:
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


def empty_hint(rows: list[Any], pages: int) -> str:
    if pages == 0:
        return "페이지를 하나도 가져오지 못했습니다."
    return (
        "페이지는 가져왔지만 고른 항목을 찾지 못했습니다. "
        "다른 항목을 골라보시거나, 목록이 있는 다른 페이지를 알려주세요."
    )


def humanise(exc: Exception) -> str:
    from crwallm.policy.ssrf import SsrfBlockedError
    from crwallm.policy.url import UrlNormalizationError

    if isinstance(exc, UrlNormalizationError):
        return "주소 형식이 올바르지 않습니다."
    if isinstance(exc, SsrfBlockedError):
        return "그 주소로는 갈 수 없습니다."
    return f"문제가 생겼습니다: {type(exc).__name__}"


def write(rows: list[dict[str, Any]], path: Path, fmt: str = "csv") -> int:
    """Every row, not just the ones on screen."""
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
