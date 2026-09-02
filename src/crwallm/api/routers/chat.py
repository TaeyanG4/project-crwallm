"""The conversation endpoint.

SSE rather than a single JSON response, because a turn can take a minute: the
model plans, a page is fetched, a recipe is built and scored, a crawl is
queued. A request that returns only at the end of all that is indistinguishable
from one that has hung, and the interesting part - what it decided and why - is
exactly what gets thrown away by waiting for the end.

Guarded by the token like every other mutating route. This one can fetch pages
and queue crawls, so it is the most powerful thing on the API and gets the same
protection as ``POST /api/jobs`` rather than less.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from collections.abc import AsyncIterator
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from crwallm.api.deps import sessionmaker_dep, settings_dep, token_dep
from crwallm.config import Settings
from crwallm.llm.agent import AgentEvent, Answer, Turn, run_agent
from crwallm.schemas.spec import CrawlSpec
from crwallm.services.agent_tools import build_agent_deps
from crwallm.services.job import JobService

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/chat", tags=["chat"])

Config = Annotated[Settings, Depends(settings_dep)]
SessionFactory = Annotated[async_sessionmaker[AsyncSession], Depends(sessionmaker_dep)]


class ChatTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=4000)
    history: list[ChatTurn] = Field(default_factory=list, max_length=40)
    """Sent by the client rather than kept here.

    A local single-user tool has no session to hang a conversation off, and
    persisting one would mean deciding when it expires. The browser already
    holds the transcript it is rendering."""


def _frame(event: AgentEvent) -> str:
    payload = dataclasses.asdict(event)
    body = json.dumps(payload, ensure_ascii=False, default=str)
    return f"event: {payload['type']}\ndata: {body}\n\n"


@router.post("", dependencies=[Depends(token_dep)])
async def chat(
    request: ChatRequest,
    settings: Config,
    sessionmaker: SessionFactory,
) -> StreamingResponse:
    """Run one turn and stream what happens."""

    async def stream() -> AsyncIterator[str]:
        from crwallm.llm.manager import build_gateway

        try:
            gateway = build_gateway(settings)
        except Exception as exc:
            log.exception("no model gateway")
            yield _frame(Answer(text=f"모델을 사용할 수 없습니다: {exc}"))
            return

        async def submit_job(spec: CrawlSpec) -> Any:
            # Its own session: this runs inside a stream that outlives the
            # handler, and the crawl has to be committed before the browser is
            # told the job id - otherwise it polls for a row that is not there.
            async with sessionmaker() as session:
                return await JobService(session).submit(spec)

        deps = build_agent_deps(
            gateway,
            recipes_dir=settings.recipes_dir,
            submit_job=submit_job,
        )

        try:
            async for event in run_agent(
                gateway,
                deps,
                message=request.message,
                history=[Turn(role=t.role, content=t.content) for t in request.history],
            ):
                yield _frame(event)
        except Exception as exc:
            log.exception("chat turn failed")
            yield _frame(Answer(text=f"오류가 났습니다: {type(exc).__name__}: {exc}"))
        finally:
            await gateway.aclose()

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
