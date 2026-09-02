"""``crwallm model ...``

Deliberately four verbs: see what is installed, add one, remove one, choose
which to use. Anything more would be a package manager, and Ollama already is
one.

    crwallm model list                 what is installed, and what is loaded
    crwallm model catalog              what this machine can run
    crwallm model pull qwen3.5:9b      add
    crwallm model rm qwen3:14b         remove
    crwallm model use qwen3.5:9b       select
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Annotated, Any

import typer

from crwallm.llm.gateway import ModelUnavailableError
from crwallm.llm.hardware import detect_hardware
from crwallm.llm.manager import ModelCatalog, ModelManager

app = typer.Typer(help="Install, remove and choose local models.", no_args_is_help=True)

ENV_FILE = Path(".env")
MODEL_KEY = "CRWALLM_LLM_MODEL"


def _err(message: str) -> None:
    typer.secho(message, fg=typer.colors.RED, err=True)


def _base_url() -> str:
    return os.environ.get("CRWALLM_OLLAMA_BASE_URL", "http://127.0.0.1:11434")


def _manager() -> ModelManager:
    return ModelManager(_base_url())


def _run(coro: Any) -> Any:
    try:
        return asyncio.run(coro)
    except ModelUnavailableError as exc:
        _err(str(exc))
        raise typer.Exit(1) from None


@app.command("list")
def list_models() -> None:
    """What is installed, and what is loaded right now.

    The placement column is the one worth reading: a model that quietly fell
    back to CPU is a 20x slowdown that nothing else reports.
    """
    _run(_list())


async def _list() -> None:
    manager = _manager()
    try:
        installed = await manager.installed()
        loaded = {m.get("name"): m for m in await manager.running()}
    finally:
        await manager.aclose()

    if not installed:
        typer.echo("nothing installed - try `crwallm model catalog`")
        return

    selected = os.environ.get(MODEL_KEY, "")
    for model in installed:
        marker = "*" if model.name == selected else " "
        row = loaded.get(model.name)
        if row:
            total = max(int(row.get("size", 1)), 1)
            placement = f"loaded, {int(row.get('size_vram', 0)) / total:.0%} GPU"
        else:
            placement = ""
        typer.echo(
            f" {marker} {model.name:<20} {model.size_gb:>5.1f} GB  "
            f"{model.parameter_size:<6} {model.quantization:<8} {placement}"
        )
    if selected:
        typer.echo(f"\n* in use ({MODEL_KEY})")


@app.command("catalog")
def catalog(
    task: Annotated[str, typer.Option("--task")] = "adapt_selectors",
) -> None:
    """What this machine can run, measured rather than guessed."""
    _run(_catalog(task))


async def _catalog(task: str) -> None:
    profile = detect_hardware()
    cat = ModelCatalog.load()
    manager = _manager()
    try:
        installed = {m.name for m in await manager.installed()}
    except ModelUnavailableError:
        installed = set()
    finally:
        await manager.aclose()

    typer.echo(profile.summary())
    if profile.has_gpu:
        typer.echo(f"usable for weights: ~{profile.usable_vram_gb} GB\n")
    else:
        typer.secho("no GPU - inference will run on CPU and be slow\n", fg=typer.colors.YELLOW)

    entries = cat.for_task(task) or cat.entries
    if not entries:
        _err("models.toml has no entries")
        raise typer.Exit(1)

    recommended = cat.recommend(profile, task=task)
    for entry in entries:
        fits = entry.fits(profile)
        here = entry.name in installed or f"{entry.name}:latest" in installed
        marks = "".join(
            (
                "*" if recommended and entry.name == recommended.name else " ",
                "+" if here else " ",
            )
        )
        typer.secho(
            f" {marks} {entry.name:<16} {entry.size_gb:>5.1f} GB  "
            f"needs {entry.min_vram_gb:>4.1f} GB VRAM  "
            f"{'' if fits else '(too large for this machine)'}",
            fg=None if fits else typer.colors.BRIGHT_BLACK,
        )
    typer.echo("\n*  recommended here    +  installed")
    if recommended and recommended.name not in installed:
        typer.echo(f"\n  crwallm model pull {recommended.name}")


@app.command("pull")
def pull(name: Annotated[str, typer.Argument(help="e.g. qwen3.5:9b")]) -> None:
    """Download a model."""
    _run(_pull(name))


async def _pull(name: str) -> None:
    profile = detect_hardware()
    entry = next((e for e in ModelCatalog.load().entries if e.name == name), None)
    if entry and not entry.fits(profile):
        # A warning, not a refusal: it is their machine, and Ollama will
        # happily run it on the CPU. But a 17GB model on a 16GB card is worth
        # hearing about before the download rather than after.
        typer.secho(
            f"warning: {name} wants {entry.min_vram_gb} GB VRAM and this machine has "
            f"~{profile.usable_vram_gb} GB usable - expect CPU fallback",
            fg=typer.colors.YELLOW,
            err=True,
        )

    manager = _manager()
    last = ""
    try:
        async for progress in manager.pull(name):
            if progress.total and progress.status != last:
                last = progress.status
            if progress.total:
                typer.echo(
                    f"\r  {progress.status:<24} {progress.percent:5.1f}%  "
                    f"{progress.completed / 1e9:.1f}/{progress.total / 1e9:.1f} GB",
                    nl=False,
                )
        typer.echo("")
    finally:
        await manager.aclose()
    typer.secho(f"{name} installed", fg=typer.colors.GREEN)
    typer.echo(f"  crwallm model use {name}")


@app.command("rm")
def remove(
    name: Annotated[str, typer.Argument()],
    yes: Annotated[bool, typer.Option("--yes", "-y")] = False,
) -> None:
    """Remove a model.

    Confirms first: re-downloading is several gigabytes, and the mistake is
    easy to make and slow to undo.
    """
    if not yes and not typer.confirm(f"delete {name}? re-downloading takes a while"):
        raise typer.Abort
    if _run(_remove(name)):
        typer.secho(f"{name} deleted", fg=typer.colors.GREEN)
    else:
        _err(f"{name} is not installed")
        raise typer.Exit(1)


async def _remove(name: str) -> bool:
    manager = _manager()
    try:
        return await manager.delete(name)
    finally:
        await manager.aclose()


@app.command("use")
def use(name: Annotated[str, typer.Argument()]) -> None:
    """Select the model for future runs.

    Writes ``CRWALLM_LLM_MODEL`` to ``.env`` rather than to a config file of
    its own - the setting already exists there, and two places to look is one
    too many.
    """
    if not _run(_installed(name)):
        _err(f"{name} is not installed - `crwallm model pull {name}` first")
        raise typer.Exit(1)

    _write_env(MODEL_KEY, name)
    typer.secho(f"{name} selected", fg=typer.colors.GREEN)


async def _installed(name: str) -> bool:
    manager = _manager()
    try:
        return await manager.has(name)
    finally:
        await manager.aclose()


def _write_env(key: str, value: str) -> None:
    """Set one key in ``.env``, leaving the rest alone.

    Rewritten line by line rather than parsed and re-serialised: a config file
    someone hand-edited should come back with its comments and ordering
    intact.
    """
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines() if ENV_FILE.exists() else []
    for i, line in enumerate(lines):
        if line.split("=", 1)[0].strip() == key:
            lines[i] = f"{key}={value}"
            break
    else:
        lines.append(f"{key}={value}")
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


@app.command("status")
def status() -> None:
    """Is the model server reachable, and what does it have."""
    _run(_status())


async def _status() -> None:
    from crwallm.llm.routing import RoutedGateway, RoutingConfig

    model = os.environ.get(MODEL_KEY, "qwen3.5:9b")
    embed = os.environ.get("CRWALLM_EMBED_MODEL", "bge-m3")
    gateway = RoutedGateway(
        RoutingConfig.local_default(base_url=_base_url(), model=model, embed_model=embed)
    )
    try:
        for name, health in (await gateway.health_all()).items():
            colour = typer.colors.GREEN if health.reachable else typer.colors.RED
            typer.secho(f"{name:<10} {health.detail}", fg=colour)
            if health.reachable and health.models:
                typer.echo(f"           {len(health.models)} model(s): {', '.join(health.models)}")
    finally:
        await gateway.aclose()
