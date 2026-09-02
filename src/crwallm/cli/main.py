"""CRWALLM command line.

The web UI is Phase 13. Until then this is the tool - and it is also what
proves the layering holds: the CLI and the REST API call the same service
functions, so if a command has to reach around ``services`` the boundary was
drawn in the wrong place (docs/03_SYSTEM_ARCHITECTURE.md).

Everything here is deliberately usable without an LLM. Levels 0 and 1 of
docs/02_PRODUCT_MODEL.md - write the selectors yourself, or pick from what
``inspect`` found - are the whole surface for now.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Annotated, Any

import typer
from pydantic import ValidationError

from crwallm import __version__
from crwallm.cli.model_cmd import app as model_app
from crwallm.cli.recipe_cmd import app as recipe_app
from crwallm.cli.setup_cmd import app as setup_app

app = typer.Typer(
    name="crwallm",
    help="Local AI crawler.",
    no_args_is_help=True,
    add_completion=False,
)


def _err(message: str) -> None:
    typer.secho(message, fg=typer.colors.RED, err=True)


@contextlib.contextmanager
def _user_errors() -> Iterator[None]:
    """Turn policy rejections into a message instead of a traceback.

    A bad ``--domain`` or an unknown transform is user error. Printing a stack
    for it buries the one line that says what to fix.
    """
    from crwallm.crawler.extraction.transforms import TransformError
    from crwallm.policy.domains import InvalidDomainError
    from crwallm.policy.url import UrlNormalizationError

    try:
        yield
    except (InvalidDomainError, UrlNormalizationError, TransformError) as exc:
        _err(str(exc))
        raise typer.Exit(2) from None
    except ValidationError as exc:
        for error in exc.errors():
            location = ".".join(str(p) for p in error["loc"])
            _err(f"{location}: {error['msg']}")
        raise typer.Exit(2) from None


# ---------------------------------------------------------------- meta


@app.command()
def version() -> None:
    """Print the version."""
    typer.echo(f"crwallm {__version__}")


@app.command()
def config() -> None:
    """Show effective settings (secrets redacted)."""
    from crwallm.config import get_settings

    s = get_settings()
    for line in (
        f"env           {s.env}",
        f"api           http://{s.api_host}:{s.api_port}",
        f"api_token     {'set' if s.api_token else 'MISSING'}",
        f"allowed_hosts {', '.join(s.allowed_hosts)}",
        f"database_url  {s.database_url.split('@')[-1]}",
        f"archive_dir   {s.archive_dir}",
    ):
        typer.echo(line)


@app.command()
def serve() -> None:
    """Run the API server."""
    from crwallm.main import main

    main()


@app.command()
def worker() -> None:
    """Run the crawl worker.

    A separate process from the API on purpose - see docs/09_JOB_ARCHITECTURE.md.
    """
    from crwallm.jobs.worker import main

    main()


# ------------------------------------------------------------- crawling


def _build_spec(
    seeds: list[str],
    *,
    domains: list[str] | None,
    max_pages: int,
    max_depth: int,
    follow: bool,
    concurrency: int,
    include: list[str] | None,
    exclude: list[str] | None,
) -> Any:
    from urllib.parse import urlsplit

    from crwallm.policy.domains import registrable_domain
    from crwallm.schemas.spec import CrawlLimits, CrawlSpec, UrlFilters
    from crwallm.schemas.types import CrawlMode

    if not domains:
        # Default the scope to the seeds' own domains. Leaving it empty would
        # be an unbounded crawl, and making the user restate what they just
        # typed is friction with no safety benefit.
        derived = {registrable_domain(urlsplit(s).hostname or "") for s in seeds} - {None}
        if not derived:
            raise typer.BadParameter(
                "could not derive a domain from the seeds - pass --domain explicitly"
            )
        domains = sorted(d for d in derived if d)

    return CrawlSpec(
        seed_urls=tuple(seeds),
        allowed_domains=tuple(domains),
        mode=CrawlMode.SPIDER if follow else CrawlMode.COLLECT,
        follow_links=follow,
        limits=CrawlLimits(
            max_pages=max_pages,
            max_depth=max_depth,
            global_concurrency=concurrency,
        ),
        url_filters=UrlFilters(include=tuple(include or ()), exclude=tuple(exclude or ())),
    )


@app.command()
def crawl(
    seeds: Annotated[list[str], typer.Argument(help="Seed URLs")],
    field: Annotated[
        list[str] | None,
        typer.Option(
            "--field",
            "-f",
            help="name=selector[::type|transform|transform], repeatable",
        ),
    ] = None,
    container: Annotated[
        str | None, typer.Option("--container", help="Repeat this selector per record")
    ] = None,
    recipe: Annotated[
        str | None,
        typer.Option("--recipe", help="Use a saved recipe instead of --field/--container"),
    ] = None,
    recipe_dir: Annotated[Path | None, typer.Option("--recipe-dir")] = None,
    domain: Annotated[list[str] | None, typer.Option("--domain", help="Allowed domains")] = None,
    max_pages: Annotated[int, typer.Option("--max-pages")] = 20,
    max_depth: Annotated[int, typer.Option("--max-depth")] = 2,
    follow: Annotated[bool, typer.Option("--follow/--no-follow")] = False,
    concurrency: Annotated[int, typer.Option("--concurrency", "-c")] = 8,
    include: Annotated[list[str] | None, typer.Option("--include")] = None,
    exclude: Annotated[list[str] | None, typer.Option("--exclude")] = None,
    archive: Annotated[Path | None, typer.Option("--archive", help="Archive bodies here")] = None,
    output: Annotated[
        Path | None, typer.Option("--output", "-o", help="Write records as JSONL")
    ] = None,
    quiet: Annotated[bool, typer.Option("--quiet", "-q")] = False,
    allow_local: Annotated[
        bool,
        typer.Option("--allow-local", help="Permit loopback targets (your own dev server)"),
    ] = False,
) -> None:
    """Run a crawl in the foreground and print what it finds.

    For a crawl you want to walk away from, use ``jobs submit`` - that queues
    it for the worker instead of tying it to this terminal.
    """
    from crwallm.crawler.extraction.css import CssSpec
    from crwallm.services.crawl import CrawlPlan, parse_field

    if recipe and (field or container):
        raise typer.BadParameter(
            "--recipe already says how to extract; drop --field and --container"
        )

    loaded = _load_recipe(recipe, recipe_dir) if recipe else None

    try:
        fields = tuple(parse_field(f) for f in (field or []))
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc

    with _user_errors():
        spec = _build_spec(
            seeds,
            # A recipe is system-of-record for where it works, and reuse must
            # narrow the scope rather than widen it
            # (docs/07_RECIPE_ARCHITECTURE.md). Passing the recipe's domains as
            # the default keeps a crawl from wandering off the site it was
            # written against.
            domains=domain or (list(loaded.allowed_domains) if loaded else None),
            max_pages=max_pages,
            max_depth=max_depth,
            follow=follow,
            concurrency=concurrency,
            include=include,
            exclude=exclude,
        )

    if loaded is not None:
        from crwallm.services.recipe import to_css_spec

        extraction = to_css_spec(loaded, follow_links=follow)
    else:
        extraction = CssSpec(container=container, fields=fields)

    plan = CrawlPlan(spec=spec, extraction=extraction)
    records = asyncio.run(_run_crawl(plan, archive=archive, quiet=quiet, allow_local=allow_local))
    _emit_records(records, output)


def _load_recipe(name: str, directory: Path | None) -> Any:
    from crwallm.schemas.recipe import RecipeStatus
    from crwallm.services.recipe import RecipeFileError, RecipeStore

    try:
        loaded = RecipeStore(directory or Path("recipes")).load(name)
    except RecipeFileError as exc:
        _err(str(exc))
        raise typer.Exit(1) from None

    if loaded.status is not RecipeStatus.ACTIVE:
        # A warning, not a refusal. Running a candidate is exactly what you do
        # while developing one; being stopped would make the loop useless.
        typer.secho(
            f"note: {name} is {loaded.status.value}, not active "
            f"(run `crwallm recipe activate {name}` once it scores well)",
            fg=typer.colors.YELLOW,
            err=True,
        )
    return loaded


async def _run_crawl(
    plan: Any, *, archive: Path | None, quiet: bool, allow_local: bool = False
) -> list[dict[str, Any]]:
    from collections import Counter

    from crwallm.policy.local import build_guard
    from crwallm.schemas.events import (
        JobCompleted,
        PageFailed,
        PageFetched,
        RecordsExtracted,
        UrlRejected,
    )
    from crwallm.services.crawl import open_crawl

    records: list[dict[str, Any]] = []
    errors: Counter[str] = Counter()
    rejects: Counter[str] = Counter()

    async with open_crawl(
        plan, archive_dir=archive, guard=build_guard(allow_local=allow_local)
    ) as events:
        async for event in events:
            match event:
                case PageFetched():
                    if not quiet:
                        typer.echo(f"  {event.status}  {event.url}  {event.bytes}B")
                case PageFailed():
                    errors[event.error_kind.value] += 1
                    if not quiet:
                        typer.secho(
                            f"  ---  {event.url}  ({event.error_kind.value})",
                            fg=typer.colors.YELLOW,
                        )
                case RecordsExtracted():
                    records.extend(event.records)
                case UrlRejected():
                    rejects[event.reason.value] += 1
                case JobCompleted():
                    _summarise(event, records, errors, rejects)
                case _:
                    pass

    return records


def _emit_records(records: list[dict[str, Any]], output: Path | None) -> None:
    """Writing happens here rather than inside the coroutine.

    File I/O is blocking, and an async function is the wrong place to do it -
    even at the end of a CLI run, where nothing else is competing for the
    loop. Coroutines gather; synchronous code writes.
    """
    lines = [json.dumps(row, ensure_ascii=False) for row in records]
    if output:
        _write_lines(lines, output)
        return
    for line in lines[:20]:
        typer.echo(line)
    if len(lines) > 20:
        typer.echo(f"... and {len(lines) - 20} more (use --output to keep them)")


def _write_lines(lines: list[str], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(("\n".join(lines) + "\n") if lines else "", encoding="utf-8")
    typer.echo(f"wrote {len(lines)} records to {output}")


def _summarise(event: Any, records: list[Any], errors: Any, rejects: Any) -> None:
    """Print the tallies, not just the totals.

    "12 failed" is not actionable; "12 blocked_429" says to lower concurrency.
    docs/09_JOB_ARCHITECTURE.md
    """
    typer.echo("")
    typer.secho(
        f"{event.pages_fetched} pages, {len(records)} records, {event.elapsed_s}s",
        fg=typer.colors.GREEN,
    )
    if errors:
        typer.echo("  failures: " + ", ".join(f"{k}={v}" for k, v in errors.most_common()))
    if rejects:
        typer.echo("  rejected: " + ", ".join(f"{k}={v}" for k, v in rejects.most_common()))


@app.command()
def inspect(
    url: Annotated[str, typer.Argument(help="Page to look at")],
    links: Annotated[bool, typer.Option("--links/--no-links")] = True,
    allow_local: Annotated[
        bool,
        typer.Option("--allow-local", help="Permit loopback targets (your own dev server)"),
    ] = False,
) -> None:
    """Fetch one page and report what repeats on it.

    The column indices in the output are the interface to ``recipe init
    --pick``, which is how a listing becomes a recipe without anyone writing a
    selector - level 1 of docs/02_PRODUCT_MODEL.md, and no model involved.
    """
    asyncio.run(_inspect(url, links, allow_local))


async def _inspect(url: str, show_links: bool, allow_local: bool = False) -> None:
    from crwallm.crawler.contracts import FetchFailure, FetchRequest
    from crwallm.crawler.extraction.css import extract_canonical, extract_links, parse
    from crwallm.crawler.fetching.http import SafeHttpFetcher
    from crwallm.policy.local import build_guard
    from crwallm.policy.url import normalize
    from crwallm.schemas.types import FetchMode
    from crwallm.structure.fingerprint import fingerprint_of

    fetcher = SafeHttpFetcher(build_guard(allow_local=allow_local))
    try:
        outcome = await fetcher.fetch(
            FetchRequest(
                url=normalize(url),
                depth=0,
                mode=FetchMode.HTTP,
                timeout_s=15.0,
                byte_limit=5_000_000,
            )
        )
        if isinstance(outcome, FetchFailure):
            _err(f"{outcome.error_kind.value}: {outcome.message}")
            raise typer.Exit(1)

        tree, _ = parse(outcome)
        typer.echo(f"status       {outcome.status}")
        typer.echo(f"content-type {outcome.content_type}")
        typer.echo(f"bytes        {len(outcome.body)}")

        title = tree.css_first("title", default=None, strict=False)
        if title is not None:
            typer.echo(f"title        {title.text(strip=True)}")
        canonical = extract_canonical(tree)
        if canonical:
            typer.echo(f"canonical    {canonical}")
        typer.echo(f"fingerprint  {fingerprint_of(tree)}")

        _print_structure(tree)

        if show_links:
            found = extract_links(tree, outcome.url.url)
            typer.echo("")
            typer.echo(f"links ({len(found)}):")
            for href in found[:20]:
                typer.echo(f"  {href}")
            if len(found) > 20:
                typer.echo(f"  ... and {len(found) - 20} more")
    finally:
        await fetcher.aclose()


def _print_structure(tree: Any) -> None:
    """Report the repeated containers and their columns.

    Printing indices rather than only selectors is deliberate. Naming a column
    is a question anyone can answer; writing a selector is not, and it is the
    step that a language model finds hard too
    (docs/08_LLM_ARCHITECTURE.md).
    """
    from crwallm.structure.detector import detect_containers

    candidates = detect_containers(tree)
    typer.echo("")

    if not candidates:
        typer.echo("no repeated structure - this looks like a detail page")
        typer.echo("write the fields by hand: crwallm recipe init <name> --url <url>")
        return

    typer.echo("repeated structure:")
    for rank, candidate in enumerate(candidates):
        marker = "*" if rank == 0 else " "
        typer.secho(
            f" {marker} {candidate.selector}  x{candidate.count}  "
            f"(score {candidate.score}, {candidate.text_density} words each)",
            fg=typer.colors.GREEN if rank == 0 else None,
        )
        for column in candidate.usable_columns:
            sample = column.samples[0] if column.samples else ""
            if len(sample) > 46:
                sample = sample[:46] + "..."
            typer.echo(
                f"     [{column.index}] {column.selector:<24} {column.kind:<5} "
                f"{column.fill_rate:.0%}  {sample}"
            )

    best = candidates[0]
    if best.usable_columns:
        picks = ",".join(f"name{c.index}={c.index}" for c in best.usable_columns[:3])
        typer.echo("")
        typer.echo(f"  crwallm recipe init <name> --url <url> --pick {picks}")


# --------------------------------------------------------------- recipes

app.add_typer(recipe_app, name="recipe")
app.add_typer(model_app, name="model")
app.add_typer(setup_app, name="setup")


# ----------------------------------------------------------------- jobs

jobs_app = typer.Typer(help="Queue crawls for the worker.", no_args_is_help=True)
app.add_typer(jobs_app, name="jobs")


@jobs_app.command("submit")
def jobs_submit(
    seeds: Annotated[list[str], typer.Argument(help="Seed URLs")],
    domain: Annotated[list[str] | None, typer.Option("--domain")] = None,
    max_pages: Annotated[int, typer.Option("--max-pages")] = 100,
    max_depth: Annotated[int, typer.Option("--max-depth")] = 2,
    follow: Annotated[bool, typer.Option("--follow/--no-follow")] = False,
    concurrency: Annotated[int, typer.Option("--concurrency", "-c")] = 8,
) -> None:
    """Queue a crawl and return its id immediately."""
    with _user_errors():
        spec = _build_spec(
            seeds,
            domains=domain,
            max_pages=max_pages,
            max_depth=max_depth,
            follow=follow,
            concurrency=concurrency,
            include=None,
            exclude=None,
        )
    asyncio.run(_submit(spec))


async def _submit(spec: Any) -> None:
    from crwallm.db.session import dispose_engine, get_sessionmaker
    from crwallm.services.job import JobService

    try:
        async with get_sessionmaker()() as session:
            job = await JobService(session).submit(spec)
            typer.echo(str(job.id))
    finally:
        await dispose_engine()


@jobs_app.command("list")
def jobs_list(
    limit: Annotated[int, typer.Option("--limit", "-n")] = 20,
    status: Annotated[str | None, typer.Option("--status")] = None,
) -> None:
    """List recent jobs."""
    asyncio.run(_jobs_list(limit, status))


async def _jobs_list(limit: int, status: str | None) -> None:
    from crwallm.db.session import dispose_engine, get_sessionmaker
    from crwallm.services.job import JobService

    try:
        async with get_sessionmaker()() as session:
            for job in await JobService(session).list_recent(limit=limit, status=status):
                typer.echo(
                    f"{job.id}  {job.status:<10} "
                    f"pages={job.pages_crawled:<6} records={job.records_extracted:<6} "
                    f"{job.created_at:%Y-%m-%d %H:%M}"
                )
    finally:
        await dispose_engine()


@jobs_app.command("show")
def jobs_show(job_id: Annotated[str, typer.Argument()]) -> None:
    """Show one job, with its failure and rejection tallies."""
    asyncio.run(_jobs_show(job_id))


async def _jobs_show(job_id: str) -> None:
    from uuid import UUID

    from crwallm.db.session import dispose_engine, get_sessionmaker
    from crwallm.services.job import JobService

    try:
        async with get_sessionmaker()() as session:
            job = await JobService(session).get(UUID(job_id))
            if job is None:
                _err(f"no such job: {job_id}")
                raise typer.Exit(1)
            typer.echo(f"id          {job.id}")
            typer.echo(f"status      {job.status}")
            typer.echo(f"worker      {job.worker_id or '-'}")
            typer.echo(f"pages       {job.pages_crawled} ({job.pages_failed} failed)")
            typer.echo(f"records     {job.records_extracted}")
            if job.error_counts:
                typer.echo(
                    "failures    "
                    + ", ".join(f"{k}={v}" for k, v in sorted(job.error_counts.items()))
                )
            if job.reject_counts:
                typer.echo(
                    "rejected    "
                    + ", ".join(f"{k}={v}" for k, v in sorted(job.reject_counts.items()))
                )
            if job.error_message:
                typer.echo(f"error       {job.error_message}")
    finally:
        await dispose_engine()


@jobs_app.command("results")
def jobs_results(
    job_id: Annotated[str, typer.Argument()],
    limit: Annotated[int, typer.Option("--limit", "-n")] = 100,
    output: Annotated[Path | None, typer.Option("--output", "-o")] = None,
) -> None:
    """Print a job's extracted records as JSONL."""
    _emit_lines(asyncio.run(_jobs_results(job_id, limit)), output)


async def _jobs_results(job_id: str, limit: int) -> list[str]:
    from uuid import UUID

    from sqlalchemy import select

    from crwallm.db.models import ExtractedRecord
    from crwallm.db.session import dispose_engine, get_sessionmaker

    try:
        async with get_sessionmaker()() as session:
            rows = (
                await session.execute(
                    select(ExtractedRecord)
                    .where(ExtractedRecord.job_id == UUID(job_id))
                    .order_by(ExtractedRecord.created_at)
                    .limit(limit)
                )
            ).scalars()
            return [json.dumps(r.data, ensure_ascii=False) for r in rows]
    finally:
        await dispose_engine()


def _emit_lines(lines: list[str], output: Path | None) -> None:
    if output:
        _write_lines(lines, output)
        return
    for line in lines:
        typer.echo(line)


if __name__ == "__main__":
    app()
