"""The window's four verbs, over HTTP.

The same page runs in two places. In the desktop window it talks to
``crwallm.desktop.bridge`` through ``window.pywebview.api``; in a browser it
talks to these routes. Both call ``crwallm.services.quick``, so there is one
implementation of look/collect/save/stop and two ways to reach it - which is
the point, because this project's recurring bug is two copies of a thing
drifting until one of them silently produces nothing.

**No database.** Nothing here opens a connection. That is what lets the whole
window work with Docker switched off, and it is why these routes are separate
from ``jobs`` rather than a mode of it.

**Sessions.** A collect can be tens of thousands of rows and the screen is
shown five hundred, so the rest has to live somewhere until the person presses
save. It lives here, keyed by an id the page makes up, and it is capped -
otherwise a long-lived server accumulates every table anyone ever collected.
"""

from __future__ import annotations

import asyncio
import tempfile
from collections import OrderedDict
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from crwallm.api.deps import token_dep
from crwallm.policy.local import build_guard
from crwallm.services import quick

router = APIRouter(prefix="/api/ui", tags=["ui"], dependencies=[Depends(token_dep)])

MAX_SESSIONS = 8
"""How many results are kept at once.

One person with a browser is the whole audience, and they have a few tabs at
most. The cap exists so a server left running for a week does not hold every
table anyone collected in it."""


class _Live:
    """What a browser is currently working on, and its stop button."""

    __slots__ = ("cancel", "session")

    def __init__(self) -> None:
        self.session = quick.Session()
        self.cancel = asyncio.Event()


_sessions: OrderedDict[str, _Live] = OrderedDict()


def _live(sid: str) -> _Live:
    entry = _sessions.get(sid)
    if entry is None:
        entry = _Live()
        _sessions[sid] = entry
        while len(_sessions) > MAX_SESSIONS:
            _sessions.popitem(last=False)
    _sessions.move_to_end(sid)
    return entry


class LookIn(BaseModel):
    sid: str = Field(min_length=1, max_length=64)
    url: str = Field(max_length=2048)


class Pick(BaseModel):
    index: int
    name: str = Field(default="", max_length=100)


class CollectIn(BaseModel):
    """Everything the picker can ask for.

    Bounded here rather than trusted: these come from a page, and the engine's
    own ceilings are a long way above what a person should be able to start by
    accident from a browser. ``max_pages`` in particular - the CLI will happily
    take a million, and a screen should not.
    """

    sid: str = Field(min_length=1, max_length=64)
    url: str = Field(max_length=2048)
    picks: list[Pick] = Field(default_factory=list, max_length=64)

    max_pages: int = Field(default=1, ge=1, le=5000)
    max_depth: int | None = Field(default=None, ge=0, le=16)
    fetch_mode: Literal["http", "auto", "browser"] = "http"
    concurrency: int = Field(default=4, ge=1, le=64)
    per_host: int = Field(default=4, ge=1, le=32)
    interval_ms: int = Field(default=0, ge=0, le=60_000)
    scroll_rounds: int = Field(default=0, ge=0, le=100)
    include: list[str] = Field(default_factory=list, max_length=20)
    exclude: list[str] = Field(default_factory=list, max_length=20)

    def options(self) -> dict[str, Any]:
        chosen = self.model_dump(exclude={"sid", "url", "picks"})
        if chosen.get("max_depth") is None:
            # Absent means "whatever following implies", which build_plan
            # decides. Sending None through would override that with nothing.
            chosen.pop("max_depth")
        return chosen


class SidIn(BaseModel):
    sid: str = Field(min_length=1, max_length=64)


@router.post("/look")
async def look(body: LookIn) -> dict[str, Any]:
    url = quick.normalise_input(body.url)
    if not url:
        return {"ok": False, "error": "주소를 입력해주세요."}

    entry = _live(body.sid)
    try:
        found = await quick.look(url, guard=build_guard())
    except Exception as exc:
        return {"ok": False, "error": quick.humanise(exc)}

    if found.get("ok"):
        entry.session = quick.Session(url=url, container=found.get("container"))
    return found


@router.post("/collect")
async def collect(body: CollectIn) -> dict[str, Any]:
    entry = _live(body.sid)
    entry.cancel = asyncio.Event()

    chosen = quick.picks_from([p.model_dump() for p in body.picks])
    refusal = quick.check_names(chosen)
    if refusal:
        return {"ok": False, "error": refusal}

    url = quick.normalise_input(body.url)
    try:
        result = await quick.collect(
            url,
            chosen,
            body.options(),
            guard=build_guard(),
            cancel=entry.cancel,
        )
    except Exception as exc:
        return {"ok": False, "error": quick.humanise(exc)}

    entry.session.url = url
    entry.session.rows = result.rows
    entry.session.pages = result.pages
    entry.session.failed = result.failed
    return result.payload()


@router.post("/stop")
async def stop(body: SidIn) -> dict[str, Any]:
    _live(body.sid).cancel.set()
    return {"ok": True}


@router.post("/save")
async def save(body: SidIn) -> FileResponse:
    """Hand the browser the file.

    The desktop opens the system's save dialog; a browser cannot, so it gets a
    download and the browser's own "where do you want it". Written to a
    temporary file and deleted once it has been sent, rather than built in
    memory - the whole table can be tens of thousands of rows and there is no
    reason for all of it to exist twice.
    """
    session = _live(body.sid).session
    rows = session.rows
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="저장할 결과가 없습니다.")

    handle = tempfile.NamedTemporaryFile(  # noqa: SIM115 - closed below, removed after send
        prefix="crwallm-", suffix=".csv", delete=False
    )
    handle.close()
    path = Path(handle.name)
    quick.write(rows, path, "csv")

    return FileResponse(
        path,
        media_type="text/csv; charset=utf-8",
        filename=quick.suggested_filename(session.url),
        background=BackgroundTask(path.unlink, missing_ok=True),
    )
