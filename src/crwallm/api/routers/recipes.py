"""Recipe endpoints.

Read-only. Writing a recipe means fetching a sample page, detecting its
structure, asking a model to name the columns and scoring the result - a
minutes-long job with a browser waiting on it, which is what the job queue
exists for. Exposing a POST that blocks an HTTP request for all of that would
be building the thing the queue was built to avoid (docs/07_RECIPE_ARCHITECTURE.md).

The UI needs the list so it can offer recipes when submitting a crawl, and it
needs the quality numbers so a stale recipe is visible as stale rather than
just "active".
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from crwallm.api.deps import settings_dep
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
