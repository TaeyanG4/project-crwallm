"""What a conversation is allowed to do.

Each of these wraps machinery that already exists and already has tests. The
value added here is the *shape*: a small JSON-ish dict the model can read back
and act on. A tool that returned a rich object would force the agent loop to
know about ``Recipe`` and ``FetchResponse``, and a tool that returned prose
would make the model parse English to find out whether it worked.

Deliberately not exposed: deleting recipes, cancelling other people's jobs,
anything that writes outside ``recipes/``. A conversation can start work and
look at results; the destructive verbs stay on the CLI where they are typed on
purpose (docs/17_NON_GOALS.md).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from crwallm.crawler.contracts import FetchFailure, FetchRequest, FetchResponse
from crwallm.crawler.extraction.css import parse
from crwallm.crawler.extraction.structured import extract_structured
from crwallm.llm.agent import AgentDeps
from crwallm.llm.gateway import ModelGateway
from crwallm.policy.domains import registrable_domain
from crwallm.schemas.spec import CrawlLimits, CrawlSpec
from crwallm.schemas.types import CrawlMode, FetchMode
from crwallm.services.recipe import RecipeStore, save_recipe_file
from crwallm.structure.detector import detect_containers

__all__ = ["build_agent_deps"]

MAX_SAMPLE_COLUMNS = 10


class FetchRefusedError(RuntimeError):
    """The page could not be fetched. Carries the crawler's own reason."""


async def _fetch(url: str, *, allow_local: bool = False) -> FetchResponse:
    """One page, through the same guarded fetcher a crawl uses.

    Not a bare httpx call: the SSRF guard and the byte cap are the reason a
    URL a model chose is safe to fetch at all (docs/11_SECURITY_MODEL.md). A
    model can be talked into naming any URL by the user, so the guard has to
    be in this path rather than only in the crawl path.
    """
    from crwallm.crawler.fetching.http import SafeHttpFetcher
    from crwallm.policy.local import build_guard
    from crwallm.policy.url import normalize

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
    finally:
        await fetcher.aclose()

    if isinstance(outcome, FetchFailure):
        raise FetchRefusedError(f"{outcome.error_kind.value}: {outcome.message}")
    return outcome


def _domains_for(urls: list[str]) -> tuple[str, ...]:
    """The scope a crawl gets when the model did not name one.

    Registrable domains, from the seeds. An unbounded crawl is refused
    outright, so something has to be chosen, and "wherever you were told to
    start" is the only choice that cannot silently widen.
    """
    found: list[str] = []
    for url in urls:
        host = urlsplit(url).hostname or ""
        try:
            domain = registrable_domain(host)
        except Exception:
            domain = host
        if domain and domain not in found:
            found.append(domain)
    return tuple(found)


def build_agent_deps(
    gateway: ModelGateway,
    *,
    recipes_dir: Path,
    submit_job: Any,
    allow_local: bool = False,
) -> AgentDeps:
    """Wire the agent's four verbs to the real machinery.

    ``submit_job`` is passed in rather than imported because queueing needs a
    database session, and where that comes from is the caller's business.
    """

    async def inspect(url: str) -> dict[str, Any]:
        """Fetch one page and report what repeats on it.

        The same deterministic detector the recipe pipeline uses, so what the
        model is told here is what ``make_recipe`` will actually see - a
        summary produced by different code could disagree with it, and the
        model would have no way to tell which was lying.
        """
        response = await _fetch(url, allow_local=allow_local)
        tree, _ = parse(response)
        containers = detect_containers(tree)
        declared = extract_structured(tree)

        # What the page states about itself outranks anything read off its
        # layout, so the model is told about it first. A recipe written
        # against JSON-LD does not break when the site is restyled.
        declared_summary: dict[str, Any] = {}
        if declared.jsonld:
            declared_summary["jsonld_types"] = [t.rsplit("/", 1)[-1] for t in declared.types()]
            declared_summary["jsonld_paths"] = _sample_paths(declared.jsonld[0])
        if declared.embedded:
            declared_summary["embedded_scripts"] = list(declared.embedded)
        if declared.meta.is_video_page():
            declared_summary["is_video_page"] = True

        if not containers:
            return {
                "url": url,
                "status": response.status,
                "container": None,
                "records": 0,
                "columns": [],
                "declared": declared_summary,
                "note": (
                    "no repeated structure - a detail page, or rendered by JavaScript. "
                    + (
                        "It does declare JSON-LD, so make_recipe can read that instead."
                        if declared.jsonld
                        else ""
                    )
                ),
            }

        best = containers[0]
        return {
            "url": url,
            "status": response.status,
            "container": best.selector,
            "records": best.count,
            "declared": declared_summary,
            "columns": [
                {
                    "index": column.index,
                    "selector": column.selector,
                    "kind": column.kind,
                    "samples": list(column.samples[:2]),
                }
                for column in best.columns[:MAX_SAMPLE_COLUMNS]
            ],
        }

    async def make_recipe(url: str, name: str) -> dict[str, Any]:
        """Build a recipe from a page and measure it.

        Returns the score rather than just success, because the model's next
        decision depends on it: a recipe at 100% fill is worth crawling with,
        one at 20% means the page was misread and inspecting a different page
        is the better next step.
        """
        from crwallm.llm.tasks.adapt import adapt_page

        response = await _fetch(url, allow_local=allow_local)
        outcome = await adapt_page(
            gateway,
            response,
            name=name,
            allowed_domains=_domains_for([url]),
        )

        if outcome.recipe is None or outcome.result is None:
            return {
                "ok": False,
                "reason": "; ".join(outcome.attempts[-2:]) or "no recipe could be built",
            }

        recipe = outcome.recipe.with_quality(outcome.result.quality)
        save_recipe_file(recipe, recipes_dir)

        return {
            "ok": True,
            "name": recipe.name,
            "score": outcome.result.quality.score,
            "records": outcome.result.quality.record_count,
            "fill": f"{outcome.result.quality.mean_fill:.0%}",
            "fields": [f.name for f in recipe.fields],
            "container": recipe.container,
        }

    async def crawl(request: dict[str, Any]) -> dict[str, Any]:
        seeds = [str(u) for u in request.get("seed_urls", []) if u]
        spider = bool(request.get("spider"))
        depth = int(request.get("max_depth", 2))

        spec = CrawlSpec(
            seed_urls=tuple(seeds),
            allowed_domains=_domains_for(seeds),
            mode=CrawlMode.SPIDER if spider else CrawlMode.COLLECT,
            # Spider mode requires following; a collect run only needs it if
            # the model asked to go deeper than the seeds.
            follow_links=spider or depth > 0,
            recipe=request.get("recipe") or None,
            limits=CrawlLimits(max_pages=int(request.get("max_pages", 50)), max_depth=depth),
        )
        job = await submit_job(spec)
        return {
            "job_id": str(job.id),
            "status": job.status,
            "seeds": seeds,
            "recipe": spec.recipe,
        }

    async def list_recipes() -> list[dict[str, Any]]:
        loaded, _errors = RecipeStore(recipes_dir).load_all()
        return [
            {
                "name": r.name,
                "status": r.status.value,
                "domains": list(r.allowed_domains),
                "fields": [f.name for f in r.fields],
                "records": r.quality.record_count,
            }
            for r in loaded
        ]

    return AgentDeps(
        inspect=inspect,
        make_recipe=make_recipe,
        crawl=crawl,
        list_recipes=list_recipes,
    )


def _sample_paths(node: dict[str, Any], limit: int = 10) -> list[str]:
    """Dotted paths into one JSON-LD entity.

    The model writes a recipe's field paths from this, so it needs the paths
    themselves rather than a description of them - the same reason the CSS
    side reports column indices instead of prose.
    """
    out: list[str] = []

    def walk(value: Any, prefix: str, depth: int) -> None:
        if depth > 2 or len(out) >= limit or not isinstance(value, dict):
            return
        for key, child in value.items():
            if key in {"@context", "@type"} or len(out) >= limit:
                continue
            path = f"{prefix}{key}"
            if isinstance(child, str | int | float | bool):
                out.append(path)
            elif isinstance(child, dict):
                walk(child, f"{path}.", depth + 1)

    walk(node, "", 0)
    return out
