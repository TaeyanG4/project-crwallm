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

from crwallm.crawler.extraction.structured import FieldPath
from crwallm.schemas.filters import RecordFilter
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


class TestDeclaredDataRecipes:
    """A recipe that reads JSON-LD instead of the DOM, end to end.

    The wiring is where this breaks: the spec loads, the extractor is built,
    and if `structured` is dropped anywhere between the two the crawl runs
    perfectly and extracts nothing - which is exactly the failure that made
    the worker useless before.
    """

    def jsonld_recipe(self, tmp_path: Path, **overrides: object) -> None:
        base: dict[str, object] = {
            "name": "products",
            "source": "jsonld",
            "source_url": "https://shop.test/p/1",
            "allowed_domains": ("shop.test",),
            "container": "Product",
            "fields": (
                FieldRule(name="title", selector="name"),
                FieldRule(name="price", selector="offers.price"),
            ),
        }
        base.update(overrides)
        save_recipe_file(Recipe(**base), tmp_path)  # type: ignore[arg-type]

    def test_the_source_survives_a_save_and_load(self, tmp_path: Path) -> None:
        self.jsonld_recipe(tmp_path)
        plan = resolve_plan(spec(recipe="products"), recipes_dir=tmp_path)
        assert plan.structured is not None
        assert plan.structured.kind == "jsonld"
        assert plan.structured.container == "Product"

    def test_field_paths_come_through(self, tmp_path: Path) -> None:
        self.jsonld_recipe(tmp_path)
        plan = resolve_plan(spec(recipe="products"), recipes_dir=tmp_path)
        assert plan.structured is not None
        assert plan.structured.fields == (
            FieldPath("title", "name"),
            FieldPath("price", "offers.price"),
        )

    def test_the_built_extractor_reads_declared_data(self, tmp_path: Path) -> None:
        from crwallm.crawler.extraction.structured import StructuredExtractor
        from crwallm.services.crawl import build_extractor

        self.jsonld_recipe(tmp_path)
        plan = resolve_plan(spec(recipe="products"), recipes_dir=tmp_path)
        assert isinstance(build_extractor(plan), StructuredExtractor)

    def test_a_css_recipe_still_builds_a_css_extractor(self, store: Path) -> None:
        from crwallm.crawler.extraction.css import CssExtractor
        from crwallm.services.crawl import build_extractor

        plan = resolve_plan(spec(recipe="laptops"), recipes_dir=store)
        assert plan.structured is None
        assert isinstance(build_extractor(plan), CssExtractor)

    def test_links_are_still_followed_from_the_dom(self, tmp_path: Path) -> None:
        """A JSON-LD block does not list the site's navigation. A recipe that
        changed source must not quietly become a one-page crawl."""
        from crwallm.services.crawl import build_extractor

        self.jsonld_recipe(tmp_path)
        plan = resolve_plan(spec(recipe="products", follow_links=True), recipes_dir=tmp_path)
        extractor = build_extractor(plan)
        assert extractor.css.follow_links is True  # type: ignore[union-attr]


class TestKnownSchemaRecipes:
    """Feed, table and article recipes, wired end to end.

    The wiring is the whole risk. Each of these extractors was written and
    tested in isolation and then reached from nowhere - which is exactly how
    the worker ran crawls without a recipe for a whole phase.
    """

    def save(self, tmp_path: Path, source: str, **overrides: object) -> None:
        base: dict[str, object] = {
            "name": "doc",
            "source": source,
            "source_url": "https://shop.test/feed",
            "allowed_domains": ("shop.test",),
        }
        base.update(overrides)
        save_recipe_file(Recipe(**base), tmp_path)  # type: ignore[arg-type]

    def test_a_feed_recipe_needs_no_fields(self, tmp_path: Path) -> None:
        from crwallm.crawler.extraction.documents import DocumentExtractor
        from crwallm.services.crawl import build_extractor

        self.save(tmp_path, "feed")
        plan = resolve_plan(spec(recipe="doc"), recipes_dir=tmp_path)
        extractor = build_extractor(plan)
        assert isinstance(extractor, DocumentExtractor)
        assert extractor.kind == "feed"

    def test_an_active_feed_recipe_is_allowed_without_fields(self, tmp_path: Path) -> None:
        """Activation normally demands at least one field. A shape that
        supplies its own schema has nothing to demand."""
        self.save(
            tmp_path,
            "feed",
            status=RecipeStatus.ACTIVE,
            quality=RecipeQuality(
                record_count=30,
                container_matched=True,
                consistency=1.0,
                fill_rates={"title": 1.0},
                measured_at=datetime.now(UTC),
            ),
        )
        plan = resolve_plan(spec(recipe="doc"), recipes_dir=tmp_path)
        assert plan.document is not None

    def test_a_table_recipe_carries_its_container(self, tmp_path: Path) -> None:
        from crwallm.services.crawl import build_extractor

        self.save(tmp_path, "table", container="#results")
        plan = resolve_plan(spec(recipe="doc"), recipes_dir=tmp_path)
        extractor = build_extractor(plan)
        assert extractor.container == "#results"  # type: ignore[union-attr]

    def test_an_article_recipe_maps_renames(self, tmp_path: Path) -> None:
        from crwallm.services.crawl import build_extractor

        self.save(
            tmp_path,
            "article",
            fields=(FieldRule(name="heading", selector="title"),),
        )
        plan = resolve_plan(spec(recipe="doc"), recipes_dir=tmp_path)
        assert build_extractor(plan).fields == (FieldPath("heading", "title"),)  # type: ignore[union-attr]

    def test_a_field_without_a_selector_renames_to_itself(self, tmp_path: Path) -> None:
        """``- name: title`` on a known-schema source means "keep title"."""
        from crwallm.services.crawl import build_extractor

        self.save(tmp_path, "feed", fields=(FieldRule(name="title"),))
        assert build_extractor(resolve_plan(spec(recipe="doc"), recipes_dir=tmp_path)).fields == (
            FieldPath("title", "title"),
        )  # type: ignore[union-attr]

    def test_following_is_still_configured_from_the_spec(self, tmp_path: Path) -> None:
        from crwallm.services.crawl import build_extractor

        self.save(tmp_path, "feed")
        plan = resolve_plan(spec(recipe="doc", follow_links=True), recipes_dir=tmp_path)
        assert build_extractor(plan).css.follow_links is True  # type: ignore[union-attr]


class TestFiltersReachTheCrawl:
    """A recipe's filters must do the same thing under test and in a crawl.

    They did not. ``recipe test`` dropped seven of ten quotes and the crawl
    that followed collected all ten, with nothing saying so - the two things
    that decide whether a recipe works disagreed silently.
    """

    def save(self, tmp_path: Path, **overrides: object) -> None:
        base: dict[str, object] = {
            "name": "quotes",
            "source_url": "https://shop.test/list",
            "allowed_domains": ("shop.test",),
            "container": "div.quote",
            "fields": (
                FieldRule(name="quote", selector="span.text"),
                FieldRule(name="author", selector="small.author"),
            ),
        }
        base.update(overrides)
        save_recipe_file(Recipe(**base), tmp_path)  # type: ignore[arg-type]

    def test_a_recipes_filters_reach_the_plan(self, tmp_path: Path) -> None:
        self.save(tmp_path, filters=(RecordFilter(field="author", op="eq", value="Einstein"),))
        plan = resolve_plan(spec(recipe="quotes"), recipes_dir=tmp_path)
        assert plan.sieve is not None
        assert plan.sieve.active
        assert plan.sieve.filters[0].value == "Einstein"

    def test_required_fields_reach_the_plan(self, tmp_path: Path) -> None:
        """Same gap, same fix: `required` was also honoured only under test."""
        self.save(
            tmp_path,
            fields=(
                FieldRule(name="quote", selector="span.text", required=True),
                FieldRule(name="author", selector="small.author"),
            ),
        )
        plan = resolve_plan(spec(recipe="quotes"), recipes_dir=tmp_path)
        assert plan.sieve is not None
        assert plan.sieve.required == ("quote",)

    def test_a_recipe_with_neither_has_an_inactive_sieve(self, tmp_path: Path) -> None:
        """So the crawl can skip the whole step rather than paying for it."""
        self.save(tmp_path)
        plan = resolve_plan(spec(recipe="quotes"), recipes_dir=tmp_path)
        assert plan.sieve is not None
        assert not plan.sieve.active

    async def test_the_sieve_drops_and_explains(self, tmp_path: Path) -> None:
        self.save(tmp_path, filters=(RecordFilter(field="author", op="eq", value="Einstein"),))
        plan = resolve_plan(spec(recipe="quotes"), recipes_dir=tmp_path)
        assert plan.sieve is not None

        kept, dropped, reasons = await plan.sieve(
            ({"author": "Einstein"}, {"author": "Rowling"}, {"author": "Austen"})
        )
        assert kept == ({"author": "Einstein"},)
        assert dropped == 2
        assert reasons == {"author eq": 2}

    async def test_required_and_filters_are_counted_separately(self, tmp_path: Path) -> None:
        """A row missing its title and a row that failed a filter are two
        different problems, and merging them hides which recipe to fix."""
        self.save(
            tmp_path,
            fields=(
                FieldRule(name="quote", selector="span.text", required=True),
                FieldRule(name="author", selector="small.author"),
            ),
            filters=(RecordFilter(field="author", op="eq", value="Einstein"),),
        )
        plan = resolve_plan(spec(recipe="quotes"), recipes_dir=tmp_path)
        assert plan.sieve is not None

        _kept, dropped, reasons = await plan.sieve(
            (
                {"quote": "a", "author": "Einstein"},
                {"quote": "", "author": "Einstein"},
                {"quote": "c", "author": "Rowling"},
            )
        )
        assert dropped == 2
        assert reasons == {"required": 1, "author eq": 1}


class TestBrowserIsOnlyBuiltWhenItCanHelp:
    """`auto` reads "zero records" as "this page needs rendering".

    That reading is only true when extraction was configured. A crawl with no
    recipe produces zero records on every page by design, and escalating on
    that rendered every page of a spider run - measured at 5.8s for three
    pages that had asked for no data at all, against 0.5s once fixed.
    """

    def plan_for(self, **overrides: object):  # type: ignore[no-untyped-def]
        from crwallm.crawler.extraction.css import CssSpec, FieldSpec
        from crwallm.services.crawl import CrawlPlan

        base: dict[str, object] = {"spec": spec(), "extraction": CssSpec()}
        base.update(overrides)
        return CrawlPlan(**base), FieldSpec  # type: ignore[arg-type]

    def test_a_plan_with_no_extraction_asks_for_nothing(self) -> None:
        plan, _ = self.plan_for()
        assert not plan.extracts_records

    def test_css_fields_count(self) -> None:
        from crwallm.crawler.extraction.css import CssSpec, FieldSpec
        from crwallm.services.crawl import CrawlPlan

        plan = CrawlPlan(
            spec=spec(),
            extraction=CssSpec(fields=(FieldSpec(name="t", selector="h3"),)),
        )
        assert plan.extracts_records

    def test_a_declared_data_recipe_counts(self, tmp_path: Path) -> None:
        save_recipe_file(
            Recipe(
                name="p",
                source="jsonld",
                source_url="https://shop.test/x",
                allowed_domains=("shop.test",),
                container="Product",
                fields=(FieldRule(name="t", selector="name"),),
            ),
            tmp_path,
        )
        plan = resolve_plan(spec(recipe="p"), recipes_dir=tmp_path)
        assert plan.extracts_records

    def test_a_known_schema_recipe_counts_without_fields(self, tmp_path: Path) -> None:
        """A feed recipe names no fields and still extracts."""
        save_recipe_file(
            Recipe(
                name="f",
                source="feed",
                source_url="https://shop.test/rss",
                allowed_domains=("shop.test",),
            ),
            tmp_path,
        )
        plan = resolve_plan(spec(recipe="f"), recipes_dir=tmp_path)
        assert plan.extracts_records
