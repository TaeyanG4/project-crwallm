"""Assembling a crawl from a spec.

One place decides which fetcher, frontier, gate, extractor and archive a spec
gets, so the CLI, the worker and the API all run identical crawls. Wiring this
at each call site is how two entry points quietly diverge - one gets the
archive, the other does not, and a bug reproduces in only one of them.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import dataclass, field, replace
from pathlib import Path

from crwallm.crawler.contracts import Extractor
from crwallm.crawler.extraction.css import CssExtractor, CssSpec, FieldSpec
from crwallm.crawler.extraction.documents import DocumentExtractor
from crwallm.crawler.extraction.structured import StructuredExtractor, StructuredSpec
from crwallm.crawler.fetching.http import SafeHttpFetcher
from crwallm.crawler.frontier.memory import MemoryFrontier
from crwallm.crawler.traversal import CrawlDeps, run_crawl
from crwallm.policy.gate import Scope, UrlGate
from crwallm.policy.ssrf import CachingResolver, SsrfGuard, SystemResolver
from crwallm.schemas.events import CrawlEvent
from crwallm.schemas.spec import CrawlSpec
from crwallm.services.recipe import (
    RecipeFileError,
    RecipeStore,
    to_css_spec,
    to_document_spec,
    to_sieve,
    to_structured_spec,
)
from crwallm.services.semantic import RecordSieve
from crwallm.storage.blob import BlobStore, NullBlobStore

__all__ = [
    "CrawlPlan",
    "RecipeNotApplicableError",
    "build_extractor",
    "open_crawl",
    "resolve_plan",
]

DEFAULT_RECIPE_DIR = Path("recipes")


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

    document: DocumentExtractor | None = None
    """Set when the recipe reads a shape that carries its own schema - a
    feed, a table, an article. Ready-built rather than a spec, because there
    is nothing to configure beyond an optional rename."""

    sieve: RecordSieve | None = None
    """The recipe's ``required`` fields and ``filters``, if it has any."""

    structured: StructuredSpec | None = None
    """Set when the recipe reads declared data instead of the DOM.

    Alongside ``extraction`` rather than replacing it: link discovery is
    always DOM-based, whatever the records were read from. A JSON-LD block
    does not list the site's navigation, and a crawl that stopped following
    links because its recipe changed source would silently become a one-page
    crawl."""


class RecipeNotApplicableError(ValueError):
    """The named recipe cannot be run against this spec."""


def _narrow_domains(spec: tuple[str, ...], recipe: tuple[str, ...]) -> tuple[str, ...]:
    """Intersect two scopes, never widening either.

    A recipe carries the domains it is known to work on, and a spec carries
    the domains this crawl is allowed to touch. Taking the union would let a
    recipe grant reach the operator did not ask for, which is the one
    direction that must never happen (docs/07_RECIPE_ARCHITECTURE.md).

    The two are not always the same shape - a spec may say
    ``quotes.toscrape.com`` where the recipe says ``toscrape.com`` - so the
    narrower of each overlapping pair wins rather than requiring equality.
    """
    if not recipe:
        return spec

    kept: list[str] = []
    for s in spec:
        for r in recipe:
            if s == r or s.endswith(f".{r}"):
                kept.append(s)
                break
            if r.endswith(f".{s}"):
                kept.append(r)
                break
    return tuple(dict.fromkeys(kept))


def resolve_plan(spec: CrawlSpec, *, recipes_dir: Path | None = None) -> CrawlPlan:
    """Turn a spec into something runnable, loading its recipe if it names one.

    Both the CLI and the worker come through here. They used to build the plan
    separately, and the worker's version simply never loaded a recipe - so
    every queued job crawled correctly and extracted nothing, with no error to
    show for it.
    """
    if spec.recipe is None:
        return CrawlPlan(spec=spec)

    store = RecipeStore(recipes_dir or DEFAULT_RECIPE_DIR)
    try:
        recipe = store.load(spec.recipe)
    except RecipeFileError as exc:
        raise RecipeNotApplicableError(str(exc)) from exc

    if spec.recipe_version is not None and recipe.version != spec.recipe_version:
        raise RecipeNotApplicableError(
            f"recipe {spec.recipe!r} is version {recipe.version}, "
            f"but the job pinned version {spec.recipe_version}"
        )

    scope = _narrow_domains(spec.allowed_domains, recipe.allowed_domains)
    if not scope:
        raise RecipeNotApplicableError(
            f"recipe {spec.recipe!r} works on {list(recipe.allowed_domains)}, "
            f"which does not overlap this crawl's {list(spec.allowed_domains)}"
        )
    if scope != spec.allowed_domains:
        spec = spec.model_copy(update={"allowed_domains": scope})

    return CrawlPlan(
        spec=spec,
        extraction=to_css_spec(recipe, follow_links=spec.follow_links),
        structured=to_structured_spec(recipe),
        document=to_document_spec(recipe),
        sieve=to_sieve(recipe),
    )


def build_extractor(plan: CrawlPlan) -> Extractor:
    """One extractor for the plan, chosen by where its records come from.

    Link discovery is configured identically for all of them: whatever the
    records were read from, the crawl still has to walk the site, and a recipe
    that changed source must not quietly become a one-page crawl.
    """
    css = CssSpec(
        container=plan.extraction.container,
        fields=plan.extraction.fields,
        link_selector=plan.extraction.link_selector,
        follow_links=plan.spec.follow_links,
    )
    if plan.document is not None:
        return replace(plan.document, css=css)
    if plan.structured is not None:
        return StructuredExtractor(spec=plan.structured, css=css)
    return CssExtractor(css)


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
        sieve=plan.sieve,
        archive=BlobStore(archive_dir) if archive_dir else NullBlobStore(),
    )
    try:
        yield run_crawl(plan.spec, deps)
    finally:
        await deps.fetcher.aclose()
