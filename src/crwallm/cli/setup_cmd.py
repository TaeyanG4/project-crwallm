"""``crwallm setup`` - from a fresh checkout to a working install.

A clone gets source and config templates; everything heavy is built here. The
alternative is a README with eleven numbered steps, which is a README nobody
finishes.

Each step reports what it found and what it did, and any step that is already
satisfied says so and moves on - so running it twice is safe, and running it
after a change is how you check the machine is still set up.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import shutil
import subprocess
from pathlib import Path
from typing import Annotated

import typer

from crwallm.llm.hardware import detect_hardware
from crwallm.llm.manager import ModelCatalog, ModelManager

app = typer.Typer(help="Set this machine up.", no_args_is_help=False, invoke_without_command=True)

ENV_FILE = Path(".env")
ENV_TEMPLATE = Path(".env.example")


def _ok(message: str) -> None:
    typer.secho(f"  ok    {message}", fg=typer.colors.GREEN)


def _skip(message: str) -> None:
    typer.secho(f"  --    {message}", fg=typer.colors.BRIGHT_BLACK)


def _warn(message: str) -> None:
    typer.secho(f"  warn  {message}", fg=typer.colors.YELLOW)


def _fail(message: str) -> None:
    typer.secho(f"  fail  {message}", fg=typer.colors.RED)


def _step(title: str) -> None:
    typer.echo(f"\n{title}")


def _run(argv: list[str], timeout: int = 120) -> tuple[int, str]:
    exe = shutil.which(argv[0])
    if exe is None:
        return 127, f"{argv[0]} not found"
    try:
        done = subprocess.run(  # noqa: S603 - fixed argv, resolved via which
            [exe, *argv[1:]], capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)
    return done.returncode, (done.stdout + done.stderr).strip()


@app.callback()
def setup(
    ctx: typer.Context,
    llm: Annotated[
        bool,
        typer.Option("--llm/--no-llm", help="Set up the model server too"),
    ] = True,
    pull: Annotated[
        bool, typer.Option("--pull/--no-pull", help="Download the recommended model")
    ] = True,
) -> None:
    """Check the machine, write config, start services, install a model."""
    if ctx.invoked_subcommand is not None:
        return

    typer.secho("crwallm setup", bold=True)
    problems: list[str] = []

    _step("1. environment file")
    _setup_env()

    _step("2. docker")
    docker_ok = _check_docker(problems)

    _step("3. database")
    if docker_ok:
        _setup_database(problems)
    else:
        _skip("skipped - docker is not available")

    _step("4. hardware")
    profile = detect_hardware()
    typer.echo(f"        {profile.summary()}")
    if not profile.has_gpu:
        _warn("no GPU - inference will run on CPU and be slow")

    if llm and docker_ok:
        _step("5. model server")
        _setup_llm(pull, problems)
    elif llm:
        _step("5. model server")
        _skip("skipped - docker is not available")
    else:
        _step("5. model server")
        _skip("skipped by --no-llm; levels 0-1 work without a model")

    typer.echo("")
    if problems:
        typer.secho("setup finished with problems:", fg=typer.colors.YELLOW, bold=True)
        for problem in problems:
            typer.echo(f"  - {problem}")
        raise typer.Exit(1)

    typer.secho("ready", fg=typer.colors.GREEN, bold=True)
    typer.echo("\n  crwallm inspect https://example.com/")
    typer.echo("  crwallm model list")


def _setup_env() -> None:
    if not ENV_TEMPLATE.exists():
        _fail(f"{ENV_TEMPLATE} is missing - is this a full checkout?")
        return

    if not ENV_FILE.exists():
        shutil.copyfile(ENV_TEMPLATE, ENV_FILE)
        _ok(f"created {ENV_FILE} from the template")

    text = ENV_FILE.read_text(encoding="utf-8")
    if "change-me" in text:
        # The token is what stops a page the user is visiting from driving the
        # crawler (docs/11_SECURITY_MODEL.md); shipping a placeholder that
        # works would defeat it.
        text = text.replace("change-me-generate-a-random-token", secrets.token_urlsafe(32))
        ENV_FILE.write_text(text, encoding="utf-8")
        _ok("generated an API token")
    else:
        _skip("API token already set")


def _check_docker(problems: list[str]) -> bool:
    code, output = _run(["docker", "version", "--format", "{{.Server.Version}}"], timeout=30)
    if code != 0:
        _fail("docker is not running - start Docker Desktop")
        problems.append("docker is not running; the database and model server need it")
        return False
    _ok(f"docker {output.splitlines()[-1] if output else 'ok'}")
    return True


def _setup_database(problems: list[str]) -> None:
    code, output = _run(["docker", "compose", "up", "-d", "db"], timeout=300)
    if code != 0:
        _fail(output.splitlines()[-1][:160] if output else "compose failed")
        problems.append("could not start PostgreSQL")
        return
    _ok("PostgreSQL started")

    # The container reports healthy before it accepts connections for a moment;
    # migrating too early fails in a way that looks like a broken migration.
    for _ in range(30):
        code, _ = _run(
            ["docker", "compose", "exec", "-T", "db", "pg_isready", "-U", "crwallm"], timeout=20
        )
        if code == 0:
            break
        import time

        time.sleep(1)

    code, output = _run(["alembic", "upgrade", "head"], timeout=180)
    if code != 0:
        _fail(output.splitlines()[-1][:160] if output else "alembic failed")
        problems.append("migrations did not apply")
        return
    _ok("schema up to date")


def _setup_llm(pull: bool, problems: list[str]) -> None:
    code, output = _run(
        ["docker", "compose", "--profile", "llm", "up", "-d", "ollama"], timeout=600
    )
    if code != 0:
        _fail(output.splitlines()[-1][:160] if output else "compose failed")
        problems.append("could not start the model server")
        return
    _ok("model server started")

    try:
        asyncio.run(_ensure_model(pull, problems))
    except Exception as exc:
        _warn(f"model check failed: {type(exc).__name__}: {exc}")
        problems.append("could not verify the model")


async def _ensure_model(pull: bool, problems: list[str]) -> None:
    base = os.environ.get("CRWALLM_OLLAMA_BASE_URL", "http://127.0.0.1:11434")
    manager = ModelManager(base)
    profile = detect_hardware()
    catalog = ModelCatalog.load()

    try:
        # The server needs a moment after the container starts.
        for attempt in range(20):
            try:
                installed = await manager.installed()
                break
            except Exception:
                if attempt == 19:
                    raise
                await asyncio.sleep(1)

        recommended = catalog.recommend(profile)
        if recommended is None:
            _warn("no catalogue entry fits this machine - see models.toml")
            return

        names = {m.name for m in installed}
        if recommended.name in names or f"{recommended.name}:latest" in names:
            _skip(f"{recommended.name} already installed")
        elif not pull:
            _warn(f"{recommended.name} recommended but --no-pull was given")
        else:
            typer.echo(f"        pulling {recommended.name} ({recommended.size_gb} GB)")
            async for progress in manager.pull(recommended.name):
                if progress.total:
                    typer.echo(f"\r        {progress.percent:5.1f}%", nl=False)
            typer.echo("")
            _ok(f"{recommended.name} installed")

        embed = next((e for e in catalog.entries if "embed" in e.tasks), None)
        if embed and embed.name not in names and f"{embed.name}:latest" not in names:
            if pull:
                typer.echo(f"        pulling {embed.name} ({embed.size_gb} GB)")
                async for _ in manager.pull(embed.name):
                    pass
                _ok(f"{embed.name} installed")
            else:
                _warn(f"{embed.name} not installed; semantic filters will be skipped")
    finally:
        await manager.aclose()


@app.command("check")
def check() -> None:
    """Report what is set up, changing nothing."""
    typer.secho("crwallm check", bold=True)

    _step("environment")
    if ENV_FILE.exists():
        text = ENV_FILE.read_text(encoding="utf-8")
        (_ok if "change-me" not in text else _warn)(
            ".env present" if "change-me" not in text else ".env has an unset token"
        )
    else:
        _fail(".env missing - run `crwallm setup`")

    _step("docker")
    code, output = _run(["docker", "version", "--format", "{{.Server.Version}}"], timeout=30)
    (_ok if code == 0 else _fail)(
        f"docker {output.splitlines()[-1]}" if code == 0 else "not running"
    )

    _step("hardware")
    typer.echo(f"        {detect_hardware().summary()}")

    _step("models")
    asyncio.run(_check_models())


async def _check_models() -> None:
    manager = ModelManager(os.environ.get("CRWALLM_OLLAMA_BASE_URL", "http://127.0.0.1:11434"))
    try:
        installed = await manager.installed()
    except Exception:
        _warn("model server not reachable - `docker compose --profile llm up -d`")
        _skip("levels 0-1 work without it: inspect, recipe init/test, crawl")
        return
    finally:
        await manager.aclose()

    if not installed:
        _warn("no models installed - `crwallm model catalog`")
        return
    for model in installed:
        _ok(f"{model.name} ({model.size_gb} GB)")
