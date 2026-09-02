"""``crwallm recipe ...``

The workflow this exists for is level 0 and 1 of docs/02_PRODUCT_MODEL.md -
write the selectors yourself, or pick them from what ``inspect`` found - and
it works with no model and no API key::

    crwallm inspect <url>                    what repeats on this page
    crwallm recipe init <name> --url <url>   write a draft from what was found
    <edit recipes/<name>.yaml>
    crwallm recipe test <name>               run it, see the rows and the score
    crwallm recipe activate <name>           promote once it scores well enough
    crwallm crawl --recipe <name>            use it

``test`` reads the archive when it can, so the middle three steps are a local
loop rather than a request per attempt.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Annotated, Any

import typer

from crwallm.services.recipe import (
    RecipeFileError,
    RecipeStore,
    RecipeTestResult,
    activate,
    measure,
    status_line,
)

app = typer.Typer(help="Write, test and activate extraction recipes.", no_args_is_help=True)

DEFAULT_DIR = Path("recipes")


def _err(message: str) -> None:
    typer.secho(message, fg=typer.colors.RED, err=True)


def _store(directory: Path | None) -> RecipeStore:
    return RecipeStore(directory or DEFAULT_DIR)


@app.command("list")
def list_recipes(
    directory: Annotated[Path | None, typer.Option("--dir")] = None,
) -> None:
    """List the recipes on disk."""
    recipes, errors = _store(directory).load_all()
    for recipe in recipes:
        typer.echo(status_line(recipe))
    for path, message in errors:
        _err(f"{path.name}: {message}")
    if not recipes and not errors:
        typer.echo("no recipes yet - try `crwallm recipe init <name> --url <url>`")


@app.command("show")
def show(
    name: Annotated[str, typer.Argument()],
    directory: Annotated[Path | None, typer.Option("--dir")] = None,
) -> None:
    """Print a recipe as it is stored."""
    import yaml

    try:
        recipe = _store(directory).load(name)
    except RecipeFileError as exc:
        _err(str(exc))
        raise typer.Exit(1) from None
    typer.echo(yaml.safe_dump(recipe.to_yaml_dict(), sort_keys=False, allow_unicode=True))


@app.command("init")
def init(
    name: Annotated[str, typer.Argument(help="Recipe name; also the filename")],
    url: Annotated[str, typer.Option("--url", help="Page to learn the structure from")],
    container: Annotated[
        str | None, typer.Option("--container", help="Override the detected container")
    ] = None,
    pick: Annotated[
        str | None,
        typer.Option(
            "--pick",
            help="Name the columns: 'title=0,price=2,url=1' using indices from `inspect`",
        ),
    ] = None,
    directory: Annotated[Path | None, typer.Option("--dir")] = None,
    force: Annotated[bool, typer.Option("--force")] = False,
    allow_local: Annotated[
        bool,
        typer.Option("--allow-local", help="Permit loopback targets (your own dev server)"),
    ] = False,
) -> None:
    """Draft a recipe from a page's detected structure.

    With ``--pick`` this is the whole of level 1: the detector found the
    columns, and naming them is the only decision left. Without it the draft
    uses placeholder names for you to edit.
    """
    store = _store(directory)
    target = store.directory / f"{name}.yaml"
    if target.exists() and not force:
        _err(f"{target} already exists; pass --force to overwrite")
        raise typer.Exit(1)

    path = asyncio.run(_init(store, name, url, container, pick, allow_local))
    typer.secho(f"wrote {path}", fg=typer.colors.GREEN)
    typer.echo(f"edit it, then: crwallm recipe test {name}")


async def _init(
    store: RecipeStore,
    name: str,
    url: str,
    container: str | None,
    pick: str | None,
    allow_local: bool = False,
) -> Path:
    from crwallm.crawler.extraction.css import parse
    from crwallm.policy.domains import registrable_domain
    from crwallm.schemas.recipe import FieldRule, Recipe
    from crwallm.structure.detector import Candidate, Column, detect_containers
    from crwallm.structure.fingerprint import fingerprint_of

    response = await _fetch_one(url, allow_local=allow_local)
    tree, _ = parse(response)
    candidates = detect_containers(tree)

    chosen: Candidate | None = None
    chosen_container: str | None = None
    columns: tuple[Column, ...] = ()

    if container:
        # An explicit container still gets its columns from the detector when
        # one of the candidates matches, so --pick keeps working.
        chosen_container = container
        chosen = next((c for c in candidates if c.selector == container), None)
        columns = chosen.usable_columns if chosen else ()
    elif candidates:
        chosen = candidates[0]
        chosen_container = chosen.selector
        columns = chosen.usable_columns

    fields = _fields_from(columns, pick)
    if not fields:
        fields = (FieldRule(name="title", selector="h1", type="text"),)

    from urllib.parse import urlsplit

    host = urlsplit(url).hostname or ""
    domain = registrable_domain(host) or host

    recipe = Recipe(
        name=name,
        source_url=url,
        allowed_domains=(domain,) if domain else (),
        container=chosen_container,
        fields=fields,
        fingerprint=str(fingerprint_of(tree)),
        notes=(
            f"drafted from {url} - detected {chosen.count} x {chosen.selector}"
            if chosen
            else f"drafted from {url} - no repeated structure detected"
        ),
    )
    return store.save(recipe)


def _fields_from(columns: Any, pick: str | None) -> tuple[Any, ...]:
    from crwallm.schemas.recipe import FieldRule

    by_index = {c.index: c for c in columns}

    if pick:
        chosen: list[FieldRule] = []
        for part in pick.split(","):
            label, _, index = part.partition("=")
            if not index.strip().isdigit():
                raise typer.BadParameter(f"expected name=index, got {part!r}")
            column = by_index.get(int(index))
            if column is None:
                raise typer.BadParameter(
                    f"no column {index} - run `crwallm inspect` to see the indices"
                )
            chosen.append(_rule_from(label.strip(), column))
        return tuple(chosen)

    # No names given: emit every column with a placeholder name, so the file
    # is a starting point rather than a blank.
    return tuple(_rule_from(f"field_{c.index}", c) for c in columns)


def _rule_from(name: str, column: Any) -> Any:
    from crwallm.schemas.recipe import FieldRule

    transform: tuple[str, ...] = ()
    if column.kind in ("href", "src"):
        transform = ("to_absolute_url",)

    return FieldRule(
        name=name,
        selector=column.selector,
        type=column.kind,
        transform=transform,
    )


@app.command("adapt")
def adapt(
    name: Annotated[str, typer.Argument(help="Recipe name to create")],
    url: Annotated[str, typer.Option("--url", help="Page to learn from")],
    rounds: Annotated[int, typer.Option("--rounds", help="Retry rounds")] = 3,
    candidates: Annotated[int, typer.Option("--candidates", help="Proposals per round")] = 3,
    directory: Annotated[Path | None, typer.Option("--dir")] = None,
    force: Annotated[bool, typer.Option("--force")] = False,
    allow_local: Annotated[bool, typer.Option("--allow-local")] = False,
) -> None:
    """Let a model name the detected columns, then keep what scores best.

    The model never writes a selector - Phase 3's detector already found
    those. It only says what each column holds, and every proposal is run
    against the real page before one is chosen
    (docs/08_LLM_ARCHITECTURE.md).

    ``recipe init --pick`` does the same thing with you doing the naming, and
    needs no model at all.
    """
    store = _store(directory)
    target = store.directory / f"{name}.yaml"
    if target.exists() and not force:
        _err(f"{target} already exists; pass --force to overwrite")
        raise typer.Exit(1)

    outcome = asyncio.run(_adapt(store, name, url, rounds, candidates, allow_local))
    if outcome is None:
        raise typer.Exit(1)


async def _adapt(
    store: RecipeStore,
    name: str,
    url: str,
    rounds: int,
    candidates: int,
    allow_local: bool,
) -> Path | None:
    from urllib.parse import urlsplit

    from crwallm.llm.gateway import ModelUnavailableError
    from crwallm.llm.routing import RoutedGateway, RoutingConfig
    from crwallm.llm.tasks.adapt import adapt_page
    from crwallm.policy.domains import registrable_domain

    response = await _fetch_one(url, allow_local=allow_local)

    host = urlsplit(url).hostname or ""
    domain = registrable_domain(host) or host

    import os

    gateway = RoutedGateway(
        RoutingConfig.local_default(
            base_url=os.environ.get("CRWALLM_OLLAMA_BASE_URL", "http://127.0.0.1:11434"),
            model=os.environ.get("CRWALLM_LLM_MODEL", "qwen3.5:9b"),
            embed_model=os.environ.get("CRWALLM_EMBED_MODEL", "bge-m3"),
        )
    )
    try:
        outcome = await adapt_page(
            gateway,
            response,
            name=name,
            allowed_domains=(domain,) if domain else (),
            rounds=rounds,
            candidates=candidates,
        )
    except ModelUnavailableError as exc:
        _err(str(exc))
        typer.echo("  no model? `crwallm recipe init --pick` does this by hand.")
        return None
    finally:
        await gateway.aclose()

    for line in outcome.attempts:
        typer.echo(f"  {line}")
    typer.echo(
        f"  {outcome.rounds} round(s), {outcome.total_elapsed_ms / 1000:.1f}s, "
        f"{outcome.total_tokens} tokens"
    )

    if outcome.recipe is None:
        _err("no usable recipe - try `crwallm inspect` to see what is on the page")
        return None

    path = store.save(outcome.recipe)
    colour = typer.colors.GREEN if outcome.succeeded else typer.colors.YELLOW
    typer.secho(f"wrote {path}", fg=colour)
    if not outcome.succeeded:
        typer.secho("  it did not reach the activation threshold - edit and re-test", fg=colour)
    else:
        typer.echo(f"  crwallm recipe activate {name}")
    return path


@app.command("test")
def test(
    name: Annotated[str, typer.Argument()],
    url: Annotated[str | None, typer.Option("--url", help="Override the sample URL")] = None,
    archive: Annotated[
        Path | None, typer.Option("--archive", help="Read the body from here instead")
    ] = None,
    show_records: Annotated[int, typer.Option("--show", "-n")] = 5,
    directory: Annotated[Path | None, typer.Option("--dir")] = None,
    allow_local: Annotated[bool, typer.Option("--allow-local")] = False,
) -> None:
    """Run a recipe against its sample page and score the result.

    No model involved, and no network when the page is already archived.
    """
    try:
        recipe = _store(directory).load(name)
    except RecipeFileError as exc:
        _err(str(exc))
        raise typer.Exit(1) from None

    result = asyncio.run(_test(recipe, url or recipe.source_url, archive, allow_local))
    _report(result, show_records)
    if not result.passes:
        raise typer.Exit(1)


async def _test(
    recipe: Any, url: str, archive: Path | None, allow_local: bool = False
) -> RecipeTestResult:
    response = await _fetch_one(url, archive=archive, allow_local=allow_local)
    return measure(recipe, response)


def _report(result: RecipeTestResult, show_records: int) -> None:
    colour = typer.colors.GREEN if result.passes else typer.colors.YELLOW
    typer.secho(result.summary, fg=colour)

    if not result.quality.container_matched:
        _err(f"container {result.recipe.container!r} matched nothing")

    for field_name, rate in sorted(result.quality.fill_rates.items()):
        marker = " " if rate >= 0.5 else "!"
        typer.echo(f"  {marker} {field_name:<20} {rate:.0%}")

    if result.filtered_out:
        reasons = ", ".join(f"{k}={v}" for k, v in result.filter_reasons.items())
        typer.echo(f"  filtered out {result.filtered_out} ({reasons})")

    for record in result.records[:show_records]:
        typer.echo("  " + json.dumps(record, ensure_ascii=False))
    if len(result.records) > show_records:
        typer.echo(f"  ... and {len(result.records) - show_records} more")


@app.command("activate")
def activate_cmd(
    name: Annotated[str, typer.Argument()],
    url: Annotated[str | None, typer.Option("--url")] = None,
    archive: Annotated[Path | None, typer.Option("--archive")] = None,
    directory: Annotated[Path | None, typer.Option("--dir")] = None,
    allow_local: Annotated[bool, typer.Option("--allow-local")] = False,
) -> None:
    """Promote a recipe to ``active``, after re-testing it.

    Activation is a claim that this works, so it is re-measured here rather
    than trusting whatever the file last recorded.
    """
    store = _store(directory)
    try:
        recipe = store.load(name)
    except RecipeFileError as exc:
        _err(str(exc))
        raise typer.Exit(1) from None

    result = asyncio.run(_test(recipe, url or recipe.source_url, archive, allow_local))
    _report(result, 3)

    try:
        promoted = activate(result)
    except ValueError as exc:
        _err(f"cannot activate: {exc}")
        raise typer.Exit(1) from None

    store.save(promoted)
    typer.secho(f"{name} is now active (score {promoted.quality.score})", fg=typer.colors.GREEN)


async def _fetch_one(url: str, *, archive: Path | None = None, allow_local: bool = False) -> Any:
    """One page, from the archive when possible.

    The archive lookup is what makes the edit loop local: a recipe is tested
    dozens of times against the same page, and paying a request each time is
    both slow and rude (docs/12_PERFORMANCE.md).
    """
    from crwallm.crawler.contracts import FetchFailure, FetchRequest, FetchResponse
    from crwallm.crawler.fetching.http import SafeHttpFetcher
    from crwallm.policy.local import build_guard
    from crwallm.policy.url import normalize
    from crwallm.schemas.types import FetchMode
    from crwallm.storage.blob import BlobStore

    normalized = normalize(url)

    if archive is not None:
        store = BlobStore(archive)
        index = archive / "by-url.json"
        if index.exists():
            mapping = json.loads(index.read_text(encoding="utf-8"))
            digest = mapping.get(normalized.url)
            body = store.get(digest) if digest else None
            if body is not None:
                return FetchResponse(
                    url=normalized,
                    status=200,
                    headers={"content-type": "text/html"},
                    body=body,
                    elapsed_ms=0,
                    fetch_mode=FetchMode.HTTP,
                    from_cache=True,
                )

    fetcher = SafeHttpFetcher(build_guard(allow_local=allow_local))
    try:
        outcome = await fetcher.fetch(
            FetchRequest(
                url=normalized,
                depth=0,
                mode=FetchMode.HTTP,
                timeout_s=15.0,
                byte_limit=5_000_000,
            )
        )
    finally:
        await fetcher.aclose()

    if isinstance(outcome, FetchFailure):
        _err(f"{outcome.error_kind.value}: {outcome.message}")
        raise typer.Exit(1)
    return outcome
