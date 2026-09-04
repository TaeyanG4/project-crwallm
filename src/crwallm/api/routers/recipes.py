"""Recipe endpoints.

**Writing** a recipe is not here. Making one means fetching a sample page,
detecting its structure, asking a model to name the columns and scoring
several candidates - minutes, with a model in the loop, which is what the job
queue exists for (docs/07_RECIPE_ARCHITECTURE.md).

**Testing** one is. It fetches a single page and scores what the recipe
already says, with no model and no queue: a couple of seconds, which is an
HTTP request. That distinction is the whole reason ``test`` and ``activate``
are here while ``adapt`` is not - and it matters, because a screen that can
list recipes but never prove one turns "active" into a word nobody can check.

The UI needs the list so it can offer recipes when submitting a crawl, and it
needs the quality numbers so a stale recipe is visible as stale rather than
just "active".
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException, status

from crwallm.api.deps import settings_dep, token_dep
from crwallm.api.schemas import RecipeDetail, RecipeSummary
from crwallm.config import Settings
from crwallm.schemas.recipe import Recipe
from crwallm.services.recipe import RecipeFileError, RecipeStore

router = APIRouter(prefix="/api/recipes", tags=["recipes"])

Config = Annotated[Settings, Depends(settings_dep)]


def _summary(recipe: Recipe) -> RecipeSummary:
    return RecipeSummary(
        name=recipe.name,
        version=recipe.version,
        status=recipe.status.value,
        source=recipe.source,
        source_url=recipe.source_url,
        allowed_domains=list(recipe.allowed_domains),
        container=recipe.container,
        field_names=[f.name for f in recipe.fields],
        record_count=recipe.quality.record_count,
        mean_fill=recipe.quality.mean_fill,
        measured_at=recipe.quality.measured_at,
    )


@router.get("", response_model=list[RecipeSummary])
def list_recipes(settings: Config) -> list[RecipeSummary]:
    """Every recipe on disk.

    Files that fail to load are skipped rather than raising: one recipe with a
    typo should not blank the list of the twenty that work. The CLI's
    ``recipe list`` is where the parse errors are shown.
    """
    loaded, _errors = RecipeStore(settings.recipes_dir).load_all()
    return [_summary(r) for r in loaded]


@router.get("/{name}", response_model=RecipeDetail)
def get_recipe(name: str, settings: Config) -> RecipeDetail:
    try:
        recipe = RecipeStore(settings.recipes_dir).load(name)
    except RecipeFileError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    return RecipeDetail(
        **_summary(recipe).model_dump(),
        fields=[f.model_dump(mode="json", exclude_none=True) for f in recipe.fields],
        fingerprint=recipe.fingerprint,
        notes=recipe.notes,
    )


# --------------------------------------------------------------- proving one


def _quality(result: object) -> dict[str, object]:
    """The measurement, flattened for a screen.

    Every number the activation gate looks at, so a refusal can be read
    against the same figures the gate used rather than taken on faith.
    """
    from typing import Any, cast

    r = cast(Any, result)
    return {
        "passes": r.passes,
        "summary": r.summary,
        "container_matched": r.quality.container_matched,
        "record_count": r.quality.record_count,
        "mean_fill": r.quality.mean_fill,
        "consistency": r.quality.consistency,
        "score": r.quality.score,
        "fill_rates": dict(sorted(r.quality.fill_rates.items())),
        "sample_url": r.quality.sample_url,
        "filtered_out": r.filtered_out,
        "filter_reasons": dict(r.filter_reasons),
        "records": [dict(row) for row in r.records[:5]],
    }


async def _measure(recipe: Recipe, url: str | None) -> object:
    from crwallm.crawler.contracts import FetchFailure, FetchRequest
    from crwallm.crawler.fetching.http import SafeHttpFetcher
    from crwallm.policy.local import build_guard
    from crwallm.policy.url import normalize
    from crwallm.schemas.types import FetchMode
    from crwallm.services.recipe import measure

    target = url or recipe.source_url
    fetcher = SafeHttpFetcher(build_guard())
    try:
        outcome = await fetcher.fetch(
            FetchRequest(
                url=normalize(target),
                depth=0,
                mode=FetchMode.HTTP,
                timeout_s=20.0,
                byte_limit=8_000_000,
            )
        )
    finally:
        await fetcher.aclose()

    if isinstance(outcome, FetchFailure):
        from crwallm.services.quick import fetch_error

        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=fetch_error(outcome))
    return measure(recipe, outcome)


@router.post("/{name}/test", dependencies=[Depends(token_dep)])
async def test_recipe(
    name: str,
    settings: Config,
    url: Annotated[str | None, Body(embed=True)] = None,
) -> dict[str, object]:
    """Run the recipe against one page and report what it got.

    Changes nothing on disk. This is the button to press before believing a
    recipe, and after a site has been restyled.
    """
    try:
        recipe = RecipeStore(settings.recipes_dir).load(name)
    except RecipeFileError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    return _quality(await _measure(recipe, url))


@router.post("/{name}/activate", dependencies=[Depends(token_dep)])
async def activate_recipe(
    name: str,
    settings: Config,
    url: Annotated[str | None, Body(embed=True)] = None,
) -> dict[str, object]:
    """Re-measure, then promote to ``active`` if the evidence supports it.

    Re-measured rather than trusting the file: ``active`` is a claim that this
    works now, and the numbers already written there were true whenever they
    were taken.
    """
    from crwallm.services.recipe import activate

    store = RecipeStore(settings.recipes_dir)
    try:
        recipe = store.load(name)
    except RecipeFileError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    result = await _measure(recipe, url)
    report = _quality(result)
    try:
        promoted = activate(result)  # type: ignore[arg-type]
    except ValueError as exc:
        # 409, not 422: the request was well formed and the recipe is real -
        # it simply did not earn the promotion, and the numbers above say why.
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail=f"활성화할 수 없습니다: {exc}"
        ) from None

    store.save(promoted)
    return {**report, "status": promoted.status.value, "version": promoted.version}
