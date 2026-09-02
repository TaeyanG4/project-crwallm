"""Turning a queued spec into something that actually extracts.

The gap these cover was found by running the thing rather than by reading it:
the CLI loaded a job's recipe and the worker did not, so every crawl submitted
through the API fetched pages correctly and produced zero records, with
nothing in the logs to say why.

docs/07_RECIPE_ARCHITECTURE.md
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from crwallm.schemas.recipe import FieldRule, Recipe, RecipeQuality, RecipeStatus
from crwallm.schemas.spec import CrawlSpec
from crwallm.services.crawl import RecipeNotApplicableError, resolve_plan
from crwallm.services.recipe import save_recipe_file


def recipe(**overrides: object) -> Recipe:
    base: dict[str, object] = {
        "name": "laptops",
        "source_url": "https://shop.test/list",
        "allowed_domains": ("shop.test",),
        "container": "li.item",
        "fields": (FieldRule(name="title", selector="h3", type="text"),),
    }
    base.update(overrides)
    return Recipe(**base)  # type: ignore[arg-type]


def spec(**overrides: object) -> CrawlSpec:
    base: dict[str, object] = {
        "seed_urls": ("https://shop.test/list",),
        "allowed_domains": ("shop.test",),
    }
    base.update(overrides)
    return CrawlSpec(**base)  # type: ignore[arg-type]


@pytest.fixture
def store(tmp_path: Path) -> Path:
    save_recipe_file(recipe(), tmp_path)
    return tmp_path


class TestLoading:
    def test_a_spec_without_a_recipe_is_left_alone(self, store: Path) -> None:
        plan = resolve_plan(spec(), recipes_dir=store)
        assert plan.extraction.container is None
        assert plan.extraction.fields == ()

    def test_a_named_recipe_becomes_the_extraction(self, store: Path) -> None:
        """The whole point: the plan the worker runs now knows how to extract."""
        plan = resolve_plan(spec(recipe="laptops"), recipes_dir=store)
        assert plan.extraction.container == "li.item"
        assert [f.name for f in plan.extraction.fields] == ["title"]

    def test_follow_links_comes_from_the_spec_not_the_recipe(self, store: Path) -> None:
        """A recipe says how to extract; how far to crawl stays the spec's."""
        plan = resolve_plan(spec(recipe="laptops", follow_links=True), recipes_dir=store)
        assert plan.extraction.follow_links is True

    def test_a_missing_recipe_is_a_config_error_not_a_crash(self, store: Path) -> None:
        with pytest.raises(RecipeNotApplicableError, match="no recipe named"):
            resolve_plan(spec(recipe="nope"), recipes_dir=store)


class TestVersionPinning:
    def test_a_matching_version_runs(self, store: Path) -> None:
        plan = resolve_plan(spec(recipe="laptops", recipe_version=1), recipes_dir=store)
        assert plan.extraction.container == "li.item"

    def test_an_edited_recipe_fails_the_pinned_job(self, tmp_path: Path) -> None:
        """A job can sit in the queue while the recipe is rewritten. Extracting
        with rules the submitter never saw is worse than refusing."""
        save_recipe_file(recipe(version=3), tmp_path)
        with pytest.raises(RecipeNotApplicableError, match="version 3"):
            resolve_plan(spec(recipe="laptops", recipe_version=1), recipes_dir=tmp_path)

    def test_a_version_cannot_be_pinned_without_a_recipe(self) -> None:
        with pytest.raises(ValueError, match="recipe_version given without recipe"):
            spec(recipe_version=2)


class TestScopeNarrowing:
    """A recipe must never grant reach the operator did not ask for."""

    def test_the_specs_narrower_scope_wins(self, tmp_path: Path) -> None:
        save_recipe_file(recipe(allowed_domains=("shop.test",)), tmp_path)
        plan = resolve_plan(
            spec(recipe="laptops", allowed_domains=("eu.shop.test",)), recipes_dir=tmp_path
        )
        assert plan.spec.allowed_domains == ("eu.shop.test",)

    def test_the_recipes_narrower_scope_also_wins(self, tmp_path: Path) -> None:
        save_recipe_file(recipe(allowed_domains=("eu.shop.test",)), tmp_path)
        plan = resolve_plan(
            spec(recipe="laptops", allowed_domains=("shop.test",)), recipes_dir=tmp_path
        )
        assert plan.spec.allowed_domains == ("eu.shop.test",), "narrowed, not widened"

    def test_a_recipe_never_widens_the_crawl(self, tmp_path: Path) -> None:
        """The failure that matters: running a recipe must not let the crawl
        reach a host the spec excluded."""
        save_recipe_file(recipe(allowed_domains=("shop.test", "other.test")), tmp_path)
        plan = resolve_plan(
            spec(recipe="laptops", allowed_domains=("shop.test",)), recipes_dir=tmp_path
        )
        assert "other.test" not in plan.spec.allowed_domains

    def test_a_recipe_with_no_domains_constrains_nothing(self, tmp_path: Path) -> None:
        save_recipe_file(recipe(allowed_domains=()), tmp_path)
        plan = resolve_plan(
            spec(recipe="laptops", allowed_domains=("anywhere.test",)), recipes_dir=tmp_path
        )
        assert plan.spec.allowed_domains == ("anywhere.test",)

    def test_no_overlap_is_refused_rather_than_run_empty(self, tmp_path: Path) -> None:
        """An empty scope would fail spec validation somewhere further in, or
        silently crawl nothing. Saying so here names the actual problem."""
        save_recipe_file(recipe(allowed_domains=("shop.test",)), tmp_path)
        with pytest.raises(RecipeNotApplicableError, match="does not overlap"):
            resolve_plan(
                spec(recipe="laptops", allowed_domains=("unrelated.test",)), recipes_dir=tmp_path
            )


class TestParity:
    def test_an_active_recipe_resolves_the_same_way(self, tmp_path: Path) -> None:
        """Status is a claim about quality, not a gate on running - the CLI
        warns and runs candidates on purpose."""
        save_recipe_file(
            recipe(
                status=RecipeStatus.ACTIVE,
                quality=RecipeQuality(
                    record_count=4,
                    container_matched=True,
                    consistency=1.0,
                    fill_rates={"title": 1.0},
                    # Without this the whole quality block is dropped on save,
                    # and an active recipe cannot be read back.
                    measured_at=datetime.now(UTC),
                ),
            ),
            tmp_path,
        )
        plan = resolve_plan(spec(recipe="laptops"), recipes_dir=tmp_path)
        assert plan.extraction.container == "li.item"
