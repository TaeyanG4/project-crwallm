"""Assembling a crawl from a spec.

One place decides which fetcher, frontier, gate, extractor and archive a spec
gets, so the CLI, the worker and the API all run identical crawls. Wiring this
at each call site is how two entry points quietly diverge - one gets the
archive, the other does not, and a bug reproduces in only one of them.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

from crwallm.crawler.extraction.css import CssExtractor, CssSpec, FieldSpec
from crwallm.crawler.fetching.http import SafeHttpFetcher
from crwallm.crawler.frontier.memory import MemoryFrontier
from crwallm.crawler.traversal import CrawlDeps, run_crawl
from crwallm.policy.gate import Scope, UrlGate
from crwallm.policy.ssrf import CachingResolver, SsrfGuard, SystemResolver
from crwallm.schemas.events import CrawlEvent
from crwallm.schemas.spec import CrawlSpec
from crwallm.storage.blob import BlobStore, NullBlobStore

__all__ = ["CrawlPlan", "build_extractor", "open_crawl"]


@dataclass(frozen=True, slots=True)
class CrawlPlan:
    """A spec plus the extraction shape to run against it.

    Extraction lives beside the spec rather than inside it because Phase 3
    moves it into ``Recipe``, which owns "how to extract" while the spec owns
    "what to crawl and how far" (docs/07_RECIPE_ARCHITECTURE.md). Keeping them
    separate now means that move is a substitution.
    """

    spec: CrawlSpec
    extraction: CssSpec = field(default_factory=CssSpec)


def build_extractor(plan: CrawlPlan) -> CssExtractor:
    spec = plan.extraction
    return CssExtractor(
        CssSpec(
            container=spec.container,
            fields=spec.fields,
            link_selector=spec.link_selector,
            follow_links=plan.spec.follow_links,
        )
    )


def parse_field(raw: str) -> FieldSpec:
    """``name=selector`` or ``name=selector::type`` or ``...::type|t1|t2``.

    A terse form for the command line, where a YAML recipe would be more
    ceremony than the job deserves. Phase 3's recipe files are the real
    surface; this is for a quick look.
    """
    name, _, rest = raw.partition("=")
    if not name or not rest:
        raise ValueError(f"expected name=selector, got {raw!r}")

    selector, _, tail = rest.partition("::")
    field_type = "text"
    transforms: tuple[str, ...] = ()
    if tail:
        parts = tail.split("|")
        field_type = parts[0] or "text"
        transforms = tuple(p for p in parts[1:] if p)

    return FieldSpec(
        name=name.strip(),
        selector=selector.strip(),
        type=field_type.strip(),  # type: ignore[arg-type]
        transform=transforms,
    )


@contextlib.asynccontextmanager
async def open_crawl(
    plan: CrawlPlan,
    *,
    archive_dir: Path | None = None,
    scope: Scope | None = None,
    guard: SsrfGuard | None = None,
) -> AsyncIterator[AsyncGenerator[CrawlEvent, None]]:
    """Build the crawl, yield its event stream, and close the fetcher after.

    A context manager because the fetcher owns a connection pool: leaving it
    open leaks sockets, and closing it before the stream is drained truncates
    the crawl.

    ``guard`` and ``scope`` default to the safe production choices. They are
    parameters because both are things a caller legitimately decides for
    itself - a recipe reuse narrows the scope
    (docs/07_RECIPE_ARCHITECTURE.md), and an authenticated crawl (Phase 9)
    will carry a guard configured for its session. Tests use the same door
    rather than reaching through the wall.
    """
    guard = guard if guard is not None else SsrfGuard(CachingResolver(SystemResolver()))
    deps = CrawlDeps(
        fetcher=SafeHttpFetcher(guard),
        frontier=MemoryFrontier(),
        gate=UrlGate.build(plan.spec, guard, scope=scope),
        extractor=build_extractor(plan),
        archive=BlobStore(archive_dir) if archive_dir else NullBlobStore(),
    )
    try:
        yield run_crawl(plan.spec, deps)
    finally:
        await deps.fetcher.aclose()
