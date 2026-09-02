"""Model management: what is installed, what fits, what to pull.

Ollama being the local backend is largely this module's fault. llama.cpp and
vLLM serve models perfectly well; neither offers pull, delete and list as a
first-class API, and without those "install a model" is a manual step outside
the tool (docs/15_TECH_STACK.md).

The catalogue lives in ``models.toml`` rather than in code. Model names and
sizes change every few months, and a release should not be required to say
that a newer one exists.
"""

from __future__ import annotations

import tomllib
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from crwallm.config import Settings
from crwallm.llm.gateway import ModelGateway, ModelUnavailableError
from crwallm.llm.hardware import HardwareProfile, detect_hardware

__all__ = ["CatalogEntry", "InstalledModel", "ModelCatalog", "ModelManager", "PullProgress"]

DEFAULT_CATALOG = Path("models.toml")


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    name: str
    tasks: tuple[str, ...]
    min_vram_gb: float
    size_gb: float
    quant: str = ""
    note: str = ""
    default: bool = False

    def fits(self, profile: HardwareProfile) -> bool:
        """Will this load on the available device?

        Compared against usable VRAM, not the nameplate figure: the KV cache
        and whatever else is resident are real and a model sized to the label
        does not load.
        """
        if not profile.has_gpu:
            # CPU inference is real, just slow. System RAM is the ceiling, and
            # weights plus overhead need roughly twice the file size.
            return profile.system_ram_gb >= self.size_gb * 2
        return profile.usable_vram_gb >= self.min_vram_gb


@dataclass(frozen=True, slots=True)
class InstalledModel:
    name: str
    size_gb: float
    modified: str = ""
    family: str = ""
    parameter_size: str = ""
    quantization: str = ""


@dataclass(frozen=True, slots=True)
class PullProgress:
    status: str
    completed: int = 0
    total: int = 0

    @property
    def percent(self) -> float:
        return round(100 * self.completed / self.total, 1) if self.total else 0.0


class ModelCatalog:
    """``models.toml``."""

    def __init__(self, entries: tuple[CatalogEntry, ...], defaults: dict[str, Any]) -> None:
        self.entries = entries
        self.defaults = defaults

    @classmethod
    def load(cls, path: Path | None = None) -> ModelCatalog:
        target = path or DEFAULT_CATALOG
        if not target.exists():
            return cls((), {})
        raw = tomllib.loads(target.read_text(encoding="utf-8"))
        entries = tuple(
            CatalogEntry(
                name=str(item["name"]),
                tasks=tuple(item.get("tasks", ())),
                min_vram_gb=float(item.get("min_vram_gb", 0)),
                size_gb=float(item.get("size_gb", 0)),
                quant=str(item.get("quant", "")),
                note=str(item.get("note", "")),
                default=bool(item.get("default", False)),
            )
            for item in raw.get("model", [])
        )
        return cls(entries, dict(raw.get("defaults", {})))

    def for_task(self, task: str) -> tuple[CatalogEntry, ...]:
        return tuple(e for e in self.entries if task in e.tasks)

    def recommend(
        self, profile: HardwareProfile, *, task: str = "adapt_selectors"
    ) -> CatalogEntry | None:
        """The largest model for ``task`` that this machine can actually run.

        Largest rather than the flagged default: the default in the file is
        the recommendation for a typical machine, and a machine with more VRAM
        should be told it can do better.
        """
        candidates = [e for e in self.for_task(task) if e.fits(profile)]
        if not candidates:
            return None
        return max(candidates, key=lambda e: (e.min_vram_gb, e.size_gb))

    @property
    def default_entry(self) -> CatalogEntry | None:
        return next((e for e in self.entries if e.default), None)


class ModelManager:
    """Ollama's model API.

    Separate from the gateway on purpose: one asks questions of a model, the
    other manages which models exist. Mixing them would put a download
    progress stream inside the thing that compiles a CrawlSpec.
    """

    def __init__(self, base_url: str = "http://127.0.0.1:11434", timeout_s: float = 30.0) -> None:
        root = base_url.rstrip("/")
        if root.endswith("/v1"):
            root = root[: -len("/v1")]
        self._root = root
        self._client = httpx.AsyncClient(base_url=root, timeout=httpx.Timeout(timeout_s))

    async def aclose(self) -> None:
        await self._client.aclose()

    async def installed(self) -> list[InstalledModel]:
        try:
            response = await self._client.get("/api/tags")
        except httpx.HTTPError as exc:
            raise ModelUnavailableError(
                f"cannot reach the model server at {self._root} - "
                "start it with `docker compose --profile llm up -d`"
            ) from exc
        response.raise_for_status()

        out: list[InstalledModel] = []
        for row in response.json().get("models", []):
            details = row.get("details") or {}
            out.append(
                InstalledModel(
                    name=str(row.get("name", "")),
                    size_gb=round(int(row.get("size", 0)) / 1e9, 2),
                    modified=str(row.get("modified_at", ""))[:19],
                    family=str(details.get("family", "")),
                    parameter_size=str(details.get("parameter_size", "")),
                    quantization=str(details.get("quantization_level", "")),
                )
            )
        return sorted(out, key=lambda m: m.name)

    async def has(self, name: str) -> bool:
        installed = {m.name for m in await self.installed()}
        # Ollama reports "qwen3:14b" but also answers to it without the tag
        # when the tag is "latest"; accept either spelling.
        return name in installed or f"{name}:latest" in installed

    async def pull(self, name: str) -> AsyncIterator[PullProgress]:
        """Download a model, yielding progress.

        Streamed rather than awaited: nine gigabytes with no output looks
        indistinguishable from a hang.
        """
        async with self._client.stream(
            "POST",
            "/api/pull",
            json={"model": name, "stream": True},
            timeout=httpx.Timeout(None, connect=10.0),
        ) as response:
            if response.status_code >= 400:
                await response.aread()
                raise ModelUnavailableError(f"pull {name!r} failed: HTTP {response.status_code}")

            import json

            async for line in response.aiter_lines():
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if "error" in row:
                    raise ModelUnavailableError(f"pull {name!r} failed: {row['error']}")
                yield PullProgress(
                    status=str(row.get("status", "")),
                    completed=int(row.get("completed", 0)),
                    total=int(row.get("total", 0)),
                )

    async def delete(self, name: str) -> bool:
        response = await self._client.request("DELETE", "/api/delete", json={"model": name})
        if response.status_code == 404:
            return False
        response.raise_for_status()
        return True

    async def show(self, name: str) -> dict[str, Any]:
        response = await self._client.post("/api/show", json={"model": name})
        if response.status_code == 404:
            raise ModelUnavailableError(f"{name!r} is not installed")
        response.raise_for_status()
        return dict(response.json())

    async def context_length(self, name: str) -> int | None:
        """The context the model was actually built for.

        Worth knowing before asking for 16k: a model trained for 8k does not
        refuse the request, it just answers worse.
        """
        try:
            info = await self.show(name)
        except (ModelUnavailableError, httpx.HTTPError):
            return None
        for key, value in (info.get("model_info") or {}).items():
            if key.endswith(".context_length"):
                try:
                    return int(value)
                except (TypeError, ValueError):
                    return None
        return None

    async def running(self) -> list[dict[str, Any]]:
        """What is loaded right now, and on what.

        The one place that says whether a model landed on the GPU or quietly
        fell back to CPU - which is a 20x difference nothing else reports.
        """
        response = await self._client.get("/api/ps")
        response.raise_for_status()
        return list(response.json().get("models", []))


async def describe_environment(
    manager: ModelManager, catalog: ModelCatalog | None = None
) -> dict[str, Any]:
    """Everything the onboarding flow needs in one call."""
    profile = detect_hardware()
    cat = catalog or ModelCatalog.load()
    recommended = cat.recommend(profile)

    try:
        installed = await manager.installed()
        reachable = True
    except ModelUnavailableError:
        installed = []
        reachable = False

    return {
        "hardware": profile,
        "reachable": reachable,
        "installed": installed,
        "recommended": recommended,
        "catalog": cat,
    }


def build_gateway(settings: Settings) -> ModelGateway:
    """The one place a gateway is constructed.

    It was being assembled inline at each call site from ``os.environ``, which
    is how two entry points end up talking to different models without anyone
    noticing - the same divergence that let the worker run crawls without a
    recipe. Routing config comes from ``Settings`` so ``.env``, the CLI and the
    API agree by construction.
    """
    from crwallm.llm.routing import RoutedGateway, RoutingConfig

    return RoutedGateway(
        RoutingConfig.local_default(
            base_url=settings.ollama_base_url,
            model=settings.llm_model,
            embed_model=settings.embed_model,
        )
    )
