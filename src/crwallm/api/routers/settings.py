"""What this machine is set up to do, and the models it can do it with.

Both existed only as CLI commands - ``crwallm config`` and ``crwallm model`` -
which meant the screen could not tell you which model it was about to use, let
alone change it. A setting you can only reach by typing is a setting a person
who opened a window does not have.

**Read, then narrow.** ``GET /api/settings`` reports the effective
configuration with the token and the database password removed. It is not
writable: the API's own host, port and token decide who may talk to it, and an
endpoint that could rewrite them is an endpoint that can lock you out of the
thing you are using to call it. What *is* writable is the model choice, which
is a preference rather than a boundary.
"""

from __future__ import annotations

import json
import re
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator

from crwallm.api.deps import settings_dep, token_dep
from crwallm.config import Settings

router = APIRouter(prefix="/api", tags=["settings"])

Config = Annotated[Settings, Depends(settings_dep)]


def _safe_database_url(url: str) -> str:
    """The database, without its password.

    A settings screen that prints a live credential is a settings screen you
    cannot screenshot, and screenshots are how people ask for help.
    """
    return re.sub(r"://([^:/@]+):([^@]*)@", r"://\1:***@", url)


class Effective(BaseModel):
    """Everything the screen can honestly show about this install."""

    env: str
    api: str
    api_token_set: bool
    allowed_hosts: list[str]
    database: str
    archive_dir: str
    recipes_dir: str
    ollama: str
    llm_model: str
    embed_model: str

    limits: dict[str, Any] = Field(default_factory=dict)
    """The engine's defaults, so the screen's own defaults can match them
    rather than being a second set of numbers that drift."""


@router.get("/settings", response_model=Effective)
def effective(settings: Config) -> Effective:
    from crwallm.schemas.spec import BrowserConfig, CrawlLimits, SpiderConfig

    limits = CrawlLimits()
    return Effective(
        env=settings.env,
        api=f"http://{settings.api_host}:{settings.api_port}",
        api_token_set=bool(settings.api_token),
        allowed_hosts=list(settings.allowed_hosts),
        database=_safe_database_url(settings.database_url),
        archive_dir=str(settings.archive_dir),
        recipes_dir=str(settings.recipes_dir),
        ollama=settings.ollama_base_url,
        llm_model=settings.llm_model,
        embed_model=settings.embed_model,
        limits={
            **limits.model_dump(),
            "spider": SpiderConfig().model_dump(mode="json"),
            "browser": BrowserConfig().model_dump(),
        },
    )


# ------------------------------------------------------------------- models


class ModelRow(BaseModel):
    name: str
    size_gb: float = 0.0
    installed: bool = True
    chosen: bool = False
    note: str = ""
    fits: bool = True
    parameter_size: str = ""
    quantization: str = ""


class Models(BaseModel):
    reachable: bool
    ollama: str
    chosen: str
    embed: str
    installed: list[ModelRow] = Field(default_factory=list)
    available: list[ModelRow] = Field(default_factory=list)
    hardware: str = ""
    recommended: str = ""
    """The largest model this machine can actually run, so the list is a
    choice rather than a quiz."""


@router.get("/models", response_model=Models)
async def models(settings: Config) -> Models:
    """What is installed, what could be, and which one is in use.

    Unreachable Ollama is an answer, not an error: the screen says "the model
    server is not running" and everything that does not need a model keeps
    working. A 503 here would make the settings tab look broken because an
    optional component is switched off.
    """
    from crwallm.llm.hardware import detect_hardware
    from crwallm.llm.manager import ModelCatalog, ModelManager

    manager = ModelManager(settings.ollama_base_url)
    try:
        found = await manager.installed()
        reachable = True
    except Exception:
        found, reachable = [], False
    finally:
        await manager.aclose()

    profile = detect_hardware()
    catalog = ModelCatalog.load()
    have = {m.name for m in found}

    return Models(
        reachable=reachable,
        ollama=settings.ollama_base_url,
        chosen=settings.llm_model,
        embed=settings.embed_model,
        hardware=_hardware_line(profile),
        recommended=getattr(catalog.recommend(profile), "name", "") or "",
        installed=[
            ModelRow(
                name=m.name,
                size_gb=m.size_gb,
                installed=True,
                chosen=m.name == settings.llm_model,
                parameter_size=m.parameter_size,
                quantization=m.quantization,
            )
            for m in sorted(found, key=lambda m: m.name)
        ],
        available=[
            ModelRow(
                name=entry.name,
                size_gb=entry.size_gb,
                installed=False,
                note=entry.note,
                fits=entry.fits(profile),
                quantization=entry.quant,
            )
            for entry in catalog.entries
            # Everything but the pure embedders. `for_task("chat")` was the
            # first guess and returned nothing at all - models.toml's tasks are
            # compile_spec / classify / adapt_selectors / repair_recipe / embed,
            # and a screen filtering on a task name that does not exist shows an
            # empty list with no way to tell that from "nothing left to get".
            if entry.name not in have and set(entry.tasks) - {"embed"}
        ],
    )


def _hardware_line(profile: Any) -> str:
    """One line a person can compare against a model's size."""
    if profile.has_gpu:
        card = profile.gpus[0]
        return f"{card.name}  VRAM {profile.vram_gb:.0f}GB  ·  RAM {profile.system_ram_gb:.0f}GB"
    return f"GPU 없음  ·  RAM {profile.system_ram_gb:.0f}GB"


class ModelName(BaseModel):
    """A model name, as Ollama spells them: ``family:tag`` or ``ns/family:tag``.

    ``..`` is excluded even though nothing here opens a file by this name. It
    is meaningless to Ollama, so allowing it buys nothing and costs the next
    person who does reach for the filesystem with it.
    """

    name: str = Field(
        min_length=1,
        max_length=200,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$",
    )

    @field_validator("name")
    @classmethod
    def _no_parent_segments(cls, value: str) -> str:
        # Not in the pattern: pydantic compiles these with Rust's regex crate,
        # which has no look-ahead, and a validator says it more plainly than a
        # character class contorted to exclude one pair.
        if ".." in value:
            raise ValueError("모델 이름에 '..'은 쓸 수 없습니다")
        return value


def _unreachable(settings: Settings) -> HTTPException:
    """The one failure that is not a mistake: the optional part is switched off.

    503 rather than 500, and a sentence rather than a class name - a model
    server that is not running is a thing a person can start.
    """
    return HTTPException(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=(
            f"모델 서버(Ollama)가 {settings.ollama_base_url} 에서 응답하지 않습니다. "
            "실행 중인지 확인해주세요."
        ),
    )


@router.post("/models/pull", dependencies=[Depends(token_dep)])
async def pull(body: ModelName, settings: Config) -> StreamingResponse:
    """Download a model, reporting progress as it goes.

    A stream because this is gigabytes: a request that answers only when the
    download finishes is indistinguishable from one that has hung, and this is
    the longest thing the screen can ask for.
    """
    from crwallm.llm.manager import ModelManager

    async def frames() -> Any:
        manager = ModelManager(settings.ollama_base_url, timeout_s=3600.0)
        try:
            async for progress in manager.pull(body.name):
                payload = {
                    "status": progress.status,
                    "completed": progress.completed,
                    "total": progress.total,
                    "percent": round(progress.percent, 1),
                }
                yield f"event: progress\ndata: {json.dumps(payload)}\n\n"
            yield 'event: done\ndata: {"ok": true}\n\n'
        except Exception as exc:
            body_json = json.dumps({"error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False)
            yield f"event: failed\ndata: {body_json}\n\n"
        finally:
            await manager.aclose()

    return StreamingResponse(
        frames(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.delete("/models/{name:path}", dependencies=[Depends(token_dep)])
async def remove(name: str, settings: Config) -> dict[str, bool]:
    from crwallm.llm.manager import ModelManager

    if name == settings.llm_model:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=f"{name}은(는) 현재 사용 중인 모델입니다. 다른 모델을 먼저 고르세요.",
        )

    manager = ModelManager(settings.ollama_base_url)
    try:
        gone = await manager.delete(name)
    except HTTPException:
        raise
    except Exception as exc:
        raise _unreachable(settings) from exc
    finally:
        await manager.aclose()
    if not gone:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail=f"{name}이(가) 설치되어 있지 않습니다."
        )
    return {"ok": True}


@router.post("/models/use", dependencies=[Depends(token_dep)])
async def use(body: ModelName, settings: Config) -> dict[str, str]:
    """Choose the model, and write it where the next process will read it.

    ``.env`` rather than memory: the worker and the CLI are separate
    processes, and a choice that lived only in this one would mean the screen
    and the crawl disagreed about which model was in use.
    """
    from crwallm.llm.manager import ModelManager

    manager = ModelManager(settings.ollama_base_url)
    try:
        known = await manager.has(body.name)
    except Exception as exc:
        raise _unreachable(settings) from exc
    finally:
        await manager.aclose()

    if not known:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail=f"{body.name}이(가) 설치되어 있지 않습니다. 먼저 내려받으세요.",
        )

    _write_env("CRWALLM_LLM_MODEL", body.name)
    settings.llm_model = body.name
    return {"chosen": body.name, "note": "이 창에는 바로 반영되고, 워커는 다시 시작해야 합니다."}


def _write_env(key: str, value: str) -> None:
    """Set one key in ``.env``, leaving every other line as it was.

    Rewriting the file from parsed settings would drop comments and any key
    this build does not know about - a config file is also somebody's notes.
    """
    from pathlib import Path

    path = Path(".env")
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    replaced = False
    for i, line in enumerate(lines):
        if line.strip().startswith(f"{key}="):
            lines[i] = f"{key}={value}"
            replaced = True
            break
    if not replaced:
        lines.append(f"{key}={value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
