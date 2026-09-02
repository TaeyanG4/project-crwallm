"""One conversion from a recipe, and a guard that it stays complete.

This project's most repeated bug has one shape: a field added to ``Recipe``
that never reaches the crawl. It happened four times.

    79eebe7  the worker never loaded a recipe at all
    9ba5630  a recipe's filters were ignored during a crawl
    11ee3cd  transforms never reached the declared-data sources
    (this)   `recipe test` scored every recipe with CSS, so a microdata one
             came back 0.0 and could never be activated

Each was found by running the thing, never by reading it, and each looked
identical from outside: a crawl that ran perfectly and produced nothing.

The cause was four converters. There is now one, and the first test below
fails if a new field on ``Recipe`` does not appear in it - which is the point
of this file.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from crwallm.crawler.extraction.css import CssExtractor
from crwallm.crawler.extraction.documents import DocumentExtractor
from crwallm.crawler.extraction.plan import Extraction, Field, build
from crwallm.crawler.extraction.structured import StructuredExtractor
from crwallm.schemas.filters import RecordFilter
from crwallm.schemas.recipe import FieldRule, Recipe
from crwallm.services.recipe import to_extraction

EXTRACTION_FIELDS = {
    "source",
    "container",
    "fields",
    "filters",
}
"""Fields of ``Recipe`` that describe extraction and must survive conversion.

Deliberately written out rather than derived. When someone adds a field to
``Recipe`` this test fails, and the person who added it decides which list it
belongs in - a field about extraction goes here and into ``to_extraction``, a
field about anything else goes into ``NOT_EXTRACTION``. A derived check would
quietly accept either."""

NOT_EXTRACTION = {
    "id",
    "name",
    "version",
    "status",
    "source_url",
    "allowed_domains",
    "fetch_mode",
    "pagination",
    "fingerprint",
    "quality",
    "notes",
    "created_at",
    "updated_at",
}
"""Recipe fields that are about identity, provenance or scoring."""


def test_every_recipe_field_is_accounted_for() -> None:
    """A new field on ``Recipe`` must be classified, not forgotten.

    The failure this prevents is silent: a field that describes extraction but
    is not converted does nothing at all, and the recipe still looks right.
    """
    declared = set(Recipe.model_fields)
    classified = EXTRACTION_FIELDS | NOT_EXTRACTION

    assert declared - classified == set(), (
        f"new Recipe field(s) {sorted(declared - classified)} - add each to "
        "EXTRACTION_FIELDS (and to to_extraction) or to NOT_EXTRACTION"
    )
    assert classified - declared == set(), (
        f"{sorted(classified - declared)} no longer exist on Recipe"
    )


def full_recipe(**overrides: object) -> Recipe:
    """A recipe with every extraction field set to something distinctive."""
    base: dict[str, object] = {
        "name": "everything",
        "source": "jsonld",
        "source_url": "https://shop.test/p/1",
        "allowed_domains": ("shop.test",),
        "container": "Product",
        "fields": (
            FieldRule(
                name="price",
                selector="offers.price",
                type="text",
                transform=("to_number",),
                required=True,
            ),
            FieldRule(name="title", selector="name"),
        ),
        "filters": (RecordFilter(field="price", op="lte", value=2_000_000),),
    }
    base.update(overrides)
    return Recipe(**base)  # type: ignore[arg-type]


class TestConversion:
    def test_the_source_survives(self) -> None:
        assert to_extraction(full_recipe()).source == "jsonld"

    def test_the_container_survives(self) -> None:
        assert to_extraction(full_recipe()).container == "Product"

    def test_field_names_and_paths_survive(self) -> None:
        got = to_extraction(full_recipe()).fields
        assert [(f.name, f.path) for f in got] == [("price", "offers.price"), ("title", "name")]

    def test_transforms_survive(self) -> None:
        """The 11ee3cd bug: dropped here, so a numeric filter matched nothing."""
        assert to_extraction(full_recipe()).fields[0].transform == ("to_number",)

    def test_required_survives(self) -> None:
        assert to_extraction(full_recipe()).required_names == ("price",)

    def test_filters_survive(self) -> None:
        """The 9ba5630 bug: honoured under test and ignored by the crawl."""
        got = to_extraction(full_recipe()).filters
        assert len(got) == 1
        assert got[0].value == 2_000_000

    def test_follow_links_comes_from_the_caller(self) -> None:
        """How far to crawl is the spec's business, not the recipe's."""
        assert to_extraction(full_recipe(), follow_links=True).follow_links is True
        assert to_extraction(full_recipe()).follow_links is False


class TestBuild:
    """One builder, so no source can be reached by a different path."""

    @pytest.mark.parametrize("source", ["jsonld", "microdata", "embedded"])
    def test_declared_sources_build_a_structured_extractor(self, source: str) -> None:
        assert isinstance(build(Extraction(source=source)), StructuredExtractor)

    @pytest.mark.parametrize("source", ["feed", "table", "article"])
    def test_known_schema_sources_build_a_document_extractor(self, source: str) -> None:
        assert isinstance(build(Extraction(source=source)), DocumentExtractor)

    def test_css_builds_a_css_extractor(self) -> None:
        assert isinstance(build(Extraction(source="css")), CssExtractor)

    def test_an_unknown_source_falls_back_to_css(self) -> None:
        """Recipes are YAML written by people and models. An unknown source
        should not take the crawl down."""
        assert isinstance(build(Extraction(source="nonsense")), CssExtractor)

    def test_link_discovery_is_configured_whatever_the_source(self) -> None:
        """A JSON-LD block does not list the site's navigation, so a recipe
        that changed source must not become a one-page crawl."""
        for source in ("css", "jsonld", "feed"):
            extractor = build(Extraction(source=source, follow_links=True))
            # The CSS extractor holds its spec directly; the others carry one
            # alongside, because links come off the DOM whatever the records
            # were read from.
            css = extractor.spec if source == "css" else extractor.css  # type: ignore[union-attr]
            assert css.follow_links is True, source

    def test_a_container_only_reaches_the_source_that_means_it(self) -> None:
        """ "Product" is a schema.org type, not a CSS selector. Passing it to
        the CSS side is how `recipe test` scored a working microdata recipe
        at 0.0 and refused to activate it."""
        built = build(Extraction(source="jsonld", container="Product"))
        assert built.css.container is None  # type: ignore[union-attr]
        assert built.spec.container == "Product"  # type: ignore[union-attr]

    def test_a_known_schema_field_without_a_path_keeps_its_name(self) -> None:
        """``- name: title`` on a feed means "keep title"."""
        built = build(Extraction(source="feed", fields=(Field(name="title"),)))
        assert built.fields[0].path == "title"  # type: ignore[union-attr]


class TestMeasuresTheSameWayItCrawls:
    """`recipe test` and a real crawl must not disagree.

    They did: one built a CssExtractor unconditionally and the other went
    through the source, so a microdata recipe scored 0.0 while crawling
    correctly - and `activate` refuses anything that scored zero, which made
    every non-CSS recipe unactivatable.
    """

    def test_a_declared_data_recipe_scores_on_its_own_terms(self) -> None:
        from crwallm.crawler.contracts import FetchResponse
        from crwallm.policy.url import normalize
        from crwallm.schemas.types import FetchMode
        from crwallm.services.recipe import measure

        html = (
            '<html><body><div itemscope itemtype="https://schema.org/Product">'
            '<meta itemprop="name" content="Keyboard">'
            '<meta itemprop="sku" content="K-1">'
            "</div></body></html>"
        )
        response = FetchResponse(
            url=normalize("https://shop.test/p/1"),
            status=200,
            headers={"content-type": "text/html"},
            body=html.encode(),
            elapsed_ms=1,
            fetch_mode=FetchMode.HTTP,
        )
        recipe = Recipe(
            name="p",
            source="microdata",
            source_url="https://shop.test/p/1",
            allowed_domains=("shop.test",),
            container="Product",
            fields=(FieldRule(name="title", selector="name"),),
        )

        result = measure(recipe, response)
        assert result.quality.record_count == 1
        assert result.records[0]["title"] == "Keyboard"

    def test_such_a_recipe_can_now_be_activated(self) -> None:
        """Activation demands evidence, and the evidence was unobtainable."""
        from crwallm.schemas.recipe import RecipeQuality

        recipe = Recipe(
            name="p",
            source="microdata",
            source_url="https://shop.test/p/1",
            allowed_domains=("shop.test",),
            container="Product",
            fields=(FieldRule(name="title", selector="name"),),
            quality=RecipeQuality(
                record_count=1,
                container_matched=True,
                consistency=1.0,
                fill_rates={"title": 1.0},
                measured_at=datetime.now(UTC),
            ),
        )
        assert recipe.activated().status.value == "active"
