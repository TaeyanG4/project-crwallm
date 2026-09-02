"""Recipes and record filters.

docs/07_RECIPE_ARCHITECTURE.md, docs/06_EXTRACTION_ARCHITECTURE.md
"""

from __future__ import annotations

from pathlib import Path

import pytest

from crwallm.crawler.contracts import FetchResponse
from crwallm.policy.url import normalize
from crwallm.schemas.filters import RecordFilter, apply_filters
from crwallm.schemas.recipe import FieldRule, Recipe, RecipeQuality, RecipeStatus
from crwallm.schemas.types import FetchMode
from crwallm.services.recipe import (
    RecipeFileError,
    RecipeStore,
    activate,
    load_recipe_file,
    measure,
    to_css_spec,
)

LISTING = """<html><body><ul>
 <li class="item"><h3><a href="/p/1">Gaming laptop</a></h3><span class="price">1,290,000</span></li>
 <li class="item"><h3><a href="/p/2">Office laptop</a></h3><span class="price">890,000</span></li>
 <li class="item"><h3><a href="/p/3">Light laptop</a></h3><span class="price">2,490,000</span></li>
 <li class="item"><h3><a href="/p/4">Sold laptop</a></h3><span class="price">Sold out</span></li>
</ul></body></html>"""


def response(html: str = LISTING) -> FetchResponse:
    return FetchResponse(
        url=normalize("https://shop.test/list"),
        status=200,
        headers={"content-type": "text/html"},
        body=html.encode(),
        elapsed_ms=1,
        fetch_mode=FetchMode.HTTP,
    )


def recipe(**overrides: object) -> Recipe:
    base: dict[str, object] = {
        "name": "laptops",
        "source_url": "https://shop.test/list",
        "allowed_domains": ("shop.test",),
        "container": "li.item",
        "fields": (
            FieldRule(name="title", selector="h3", type="text"),
            FieldRule(name="url", selector="h3 > a", type="href"),
            FieldRule(name="price", selector="span.price", transform=("to_number",)),
        ),
    }
    base.update(overrides)
    return Recipe(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------- schema


class TestRecipeSchema:
    def test_duplicate_field_names_are_rejected(self) -> None:
        with pytest.raises(ValueError, match="duplicate field names"):
            recipe(
                fields=(
                    FieldRule(name="title", selector="h3"),
                    FieldRule(name="title", selector="h4"),
                )
            )

    def test_unknown_transforms_are_rejected_at_load(self) -> None:
        """A typo should fail when the recipe is read, not three hundred pages
        into a crawl."""
        with pytest.raises(ValueError, match="unknown transform"):
            FieldRule(name="price", selector="span", transform=("to_numbr",))

    def test_a_filter_on_an_unextracted_field_is_rejected(self) -> None:
        """It would drop every record, silently, looking exactly like a site
        that returned nothing."""
        with pytest.raises(ValueError, match="unknown field"):
            recipe(filters=(RecordFilter(field="rating", op="gte", value=4),))

    def test_attr_type_needs_an_attribute_name(self) -> None:
        with pytest.raises(ValueError, match="no attr name"):
            FieldRule(name="x", selector="div", type="attr")

    def test_a_name_that_is_not_filename_safe_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            recipe(name="../etc/passwd")


class TestActivationGate:
    def test_an_untested_recipe_cannot_be_active(self) -> None:
        """Activation is a claim that this works, and a claim needs evidence."""
        with pytest.raises(ValueError, match="extracted no records"):
            recipe(status=RecipeStatus.ACTIVE)

    def test_a_recipe_that_extracts_nothing_cannot_be_activated(self) -> None:
        result = measure(recipe(container="li.nonexistent"), response())
        with pytest.raises(ValueError, match="matched nothing"):
            activate(result)

    def test_a_measured_recipe_activates(self) -> None:
        promoted = activate(measure(recipe(), response()))
        assert promoted.status is RecipeStatus.ACTIVE
        assert promoted.quality.record_count == 4

    def test_a_low_fill_recipe_is_refused_with_the_offending_fields(self) -> None:
        broken = recipe(
            fields=(
                FieldRule(name="title", selector="h3"),
                FieldRule(name="rating", selector="span.rating"),
                FieldRule(name="stock", selector="span.stock"),
            )
        )
        with pytest.raises(ValueError, match="rating"):
            activate(measure(broken, response()))


class TestQuality:
    def test_fill_rates_are_per_field(self) -> None:
        result = measure(recipe(), response())
        assert result.quality.fill_rates["title"] == 1.0
        assert result.quality.fill_rates["price"] == 0.75  # "Sold out" is not a number

    def test_consistency_notices_a_field_of_mixed_shape(self) -> None:
        """Fill rate alone calls this perfect: every row has a price. Only the
        shape check sees that one of them is prose."""
        mixed = recipe(
            fields=(
                FieldRule(name="title", selector="h3"),
                FieldRule(name="price", selector="span.price"),
            )
        )
        result = measure(mixed, response())
        assert result.quality.fill_rates["price"] == 1.0
        assert result.quality.consistency < 1.0

    def test_a_clean_page_scores_full_consistency(self) -> None:
        clean = LISTING.replace("Sold out", "990,000")
        result = measure(recipe(), response(clean))
        assert result.quality.consistency == 1.0

    def test_score_is_zero_when_nothing_matched(self) -> None:
        assert measure(recipe(container="li.nope"), response()).quality.score == 0.0

    def test_required_fields_drop_the_record(self) -> None:
        """A row missing its title is not a partial record, it is noise."""
        strict = recipe(
            fields=(
                FieldRule(name="title", selector="h3"),
                FieldRule(
                    name="price", selector="span.price", transform=("to_number",), required=True
                ),
            )
        )
        assert measure(strict, response()).quality.record_count == 3


# ---------------------------------------------------------------- filters


ROWS = [
    {"title": "Python tutorial", "duration_s": 900, "views": 50_000, "date": "2025-06-01"},
    {"title": "Cooking show", "duration_s": 2400, "views": 900, "date": "2024-01-15"},
    {"title": "Rust tutorial", "duration_s": 45, "views": 120_000, "date": "2025-11-20"},
    {"title": "Go lecture", "duration_s": 1200, "views": 8_000},
]


class TestFilters:
    def test_between_narrows_a_numeric_range(self) -> None:
        kept = apply_filters(
            ROWS, [RecordFilter(field="duration_s", op="between", value=[60, 1800])]
        )
        assert [r["title"] for r in kept.kept] == ["Python tutorial", "Go lecture"]

    def test_regex_on_a_title(self) -> None:
        kept = apply_filters(ROWS, [RecordFilter(field="title", op="matches", value="tutorial")])
        assert len(kept.kept) == 2

    def test_dates_compare_chronologically_not_lexically(self) -> None:
        kept = apply_filters(ROWS, [RecordFilter(field="date", op="gte", value="2025-01-01")])
        assert {r["title"] for r in kept.kept} == {"Python tutorial", "Rust tutorial"}

    def test_a_missing_field_drops_the_record(self) -> None:
        """A filter exists to narrow, so an unanswerable rule fails closed."""
        kept = apply_filters(ROWS, [RecordFilter(field="date", op="exists")])
        assert len(kept.kept) == 3

    def test_prices_written_as_text_still_compare(self) -> None:
        rows = [{"price": "1,290,000 won"}, {"price": "890,000 won"}]
        kept = apply_filters(rows, [RecordFilter(field="price", op="lte", value=1_000_000)])
        assert len(kept.kept) == 1

    def test_rules_combine_as_and(self) -> None:
        kept = apply_filters(
            ROWS,
            [
                RecordFilter(field="title", op="contains", value="tutorial"),
                RecordFilter(field="views", op="gte", value=100_000),
            ],
        )
        assert [r["title"] for r in kept.kept] == ["Rust tutorial"]

    def test_reasons_say_which_rule_dropped_what(self) -> None:
        """A filter that removes everything is a common mistake, and the count
        is what says which one did it."""
        result = apply_filters(ROWS, [RecordFilter(field="views", op="gte", value=1_000_000)])
        assert result.kept == []
        assert result.reasons == {"views gte": 4}

    def test_semantic_rules_are_skipped_until_phase_4(self) -> None:
        """A recipe written against a future capability should still collect
        today, not silently return nothing."""
        result = apply_filters(
            ROWS, [RecordFilter(field="title", op="semantic", value="programming lesson")]
        )
        assert len(result.kept) == len(ROWS)

    def test_an_invalid_regex_is_rejected_when_the_rule_is_built(self) -> None:
        with pytest.raises(ValueError, match="invalid regex"):
            RecordFilter(field="title", op="matches", value="[unclosed")

    def test_between_needs_two_values(self) -> None:
        with pytest.raises(ValueError, match="two values"):
            RecordFilter(field="x", op="between", value=[1])

    def test_filters_apply_during_measurement(self) -> None:
        filtered = recipe(filters=(RecordFilter(field="price", op="gte", value=1_000_000),))
        result = measure(filtered, response())
        assert result.quality.record_count == 2
        assert result.filtered_out == 2


# ------------------------------------------------------------------ files


class TestRecipeFiles:
    def test_round_trip_through_yaml(self, tmp_path: Path) -> None:
        store = RecipeStore(tmp_path)
        path = store.save(recipe())
        reloaded = store.load("laptops")
        assert reloaded.container == "li.item"
        assert [f.name for f in reloaded.fields] == ["title", "url", "price"]
        assert path.exists()

    def test_an_activated_recipe_can_be_read_back(self, tmp_path: Path) -> None:
        """The activation gate demands evidence, so the file has to carry it.

        Dropping ``quality`` as "machine-owned" produced a recipe that could be
        written and never loaded again - found by running the workflow end to
        end, not by a unit test.
        """
        store = RecipeStore(tmp_path)
        store.save(activate(measure(recipe(), response())))
        reloaded = store.load("laptops")
        assert reloaded.status is RecipeStatus.ACTIVE
        assert reloaded.quality.record_count == 4

    def test_an_untested_recipe_omits_the_empty_quality_block(self, tmp_path: Path) -> None:
        RecipeStore(tmp_path).save(recipe())
        assert "quality" not in (tmp_path / "laptops.yaml").read_text(encoding="utf-8")

    def test_korean_survives_the_round_trip(self, tmp_path: Path) -> None:
        store = RecipeStore(tmp_path)
        store.save(recipe(notes="노트북 목록 페이지"))
        assert store.load("laptops").notes == "노트북 목록 페이지"

    def test_a_broken_file_does_not_hide_the_working_ones(self, tmp_path: Path) -> None:
        store = RecipeStore(tmp_path)
        store.save(recipe())
        (tmp_path / "broken.yaml").write_text("this: [is: not, valid", encoding="utf-8")

        loaded, errors = store.load_all()
        assert [r.name for r in loaded] == ["laptops"]
        assert len(errors) == 1

    def test_yaml_cannot_construct_python_objects(self, tmp_path: Path) -> None:
        """A recipe is data, possibly written by a model. PyYAML's full loader
        would make "declarative" untrue in the one way that matters."""
        evil = tmp_path / "evil.yaml"
        evil.write_text(
            "name: evil\nsource_url: https://a.test/\n"
            "container: !!python/object/apply:os.system ['echo pwned']\n",
            encoding="utf-8",
        )
        with pytest.raises(RecipeFileError):
            load_recipe_file(evil)

    def test_a_missing_recipe_says_so(self, tmp_path: Path) -> None:
        with pytest.raises(RecipeFileError, match="no recipe named"):
            RecipeStore(tmp_path).load("nope")


class TestToCssSpec:
    def test_transforms_survive_the_conversion(self) -> None:
        spec = to_css_spec(recipe())
        price = next(f for f in spec.fields if f.name == "price")
        assert price.transform == ("to_number",)

    def test_link_following_is_the_specs_decision_not_the_recipes(self) -> None:
        """The recipe says how to extract; whether to follow links is "what to
        crawl", which belongs to the spec (docs/07_RECIPE_ARCHITECTURE.md)."""
        assert to_css_spec(recipe(), follow_links=True).follow_links is True
        assert to_css_spec(recipe(), follow_links=False).follow_links is False


class TestQualityModel:
    def test_mean_fill_of_an_unmeasured_recipe_is_zero(self) -> None:
        assert RecipeQuality().mean_fill == 0.0

    def test_score_needs_a_matched_container(self) -> None:
        quality = RecipeQuality(record_count=10, container_matched=False, fill_rates={"a": 1.0})
        assert quality.score == 0.0
