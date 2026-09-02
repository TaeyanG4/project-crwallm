"""Recipe - "how to extract", as declarative data.

Pairs with ``CrawlSpec``, which owns "what to crawl and how far". The split is
load-bearing: when a spec references a recipe, the recipe is system-of-record
for its own fields and the spec cannot widen them
(docs/07_RECIPE_ARCHITECTURE.md). Domain scope is intersected, never unioned,
so reuse can narrow reach but never grant it.

**Declarative, not executable.** Every field here is data a validator can
check. Transforms come from a fixed whitelist, filters from a fixed set of
operators. A recipe arrives from a YAML file on disk or - from Phase 4 - from
a language model, and neither is a source you want executing code
(docs/17_NON_GOALS.md).

**Files are the original, the database is a copy.** A recipe that lives only
in a table cannot be edited in a text editor, reviewed in a diff, or kept in
version control - and those are how selectors actually get fixed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from crwallm.schemas.filters import RecordFilter
from crwallm.schemas.types import FetchMode

__all__ = [
    "FieldRule",
    "PaginationRule",
    "Recipe",
    "RecipeQuality",
    "RecipeStatus",
]


_SCHEMA_KNOWN_SOURCES = frozenset({"feed", "table", "article"})
"""Sources that supply their own field names, so a recipe need not."""


class RecipeStatus(StrEnum):
    CANDIDATE = "candidate"
    """Proposed but not yet proven against a real page."""

    ACTIVE = "active"
    """Passed the quality gate. Safe to reuse."""

    DEPRECATED = "deprecated"
    """Superseded, or drifted past repair."""


class FieldRule(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1, max_length=64)
    selector: str = ""
    """Relative to the container. Empty means the container itself.

    A CSS selector for a ``css`` recipe, a dotted JSON path for the others
    (``offers.price``, ``author.name``)."""

    type: str = "text"
    attr: str | None = None
    transform: tuple[str, ...] = ()
    required: bool = False
    """Drops the record when this field is empty.

    Off by default. A sold-out product with no price is still a product, and
    treating a missing optional field as a broken record is how a working
    recipe starts returning nothing.
    """

    @field_validator("transform")
    @classmethod
    def _known_transforms(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        from crwallm.crawler.extraction.transforms import validate_chain

        validate_chain(v)
        return v

    @field_validator("type")
    @classmethod
    def _known_type(cls, v: str) -> str:
        allowed = {"text", "html", "href", "src", "attr"}
        if v not in allowed:
            raise ValueError(f"unknown field type {v!r}; expected one of {sorted(allowed)}")
        return v

    @model_validator(mode="after")
    def _attr_needs_a_name(self) -> FieldRule:
        if self.type == "attr" and not self.attr:
            raise ValueError(f"field {self.name!r} has type 'attr' but no attr name")
        return self


class PaginationRule(BaseModel):
    """How to reach the next page of a listing.

    Without it, following pagination means letting the spider discover it
    among every other link, which works but spends the budget on category
    pages and footers on the way. Naming it turns "crawl this site" into
    "walk this listing".
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    next_selector: str | None = None
    """A link to the next page - the most common shape and the most robust,
    since the site computes the URL."""

    max_pages: Annotated[int, Field(ge=1, le=10_000)] = 50
    detail_selector: str | None = None
    """Link from a listing row to its detail page, when the fields worth
    having are not on the listing."""


class RecipeQuality(BaseModel):
    """What a deterministic run against a sample produced.

    The same numbers serve twice. Here they gate activation: a recipe that
    extracts nothing must not become ``active``. In Phase 11 they are the
    drift signal - the same measurement, taken later, against production
    pages. Building one metric for two uses is why activation is scored at
    all rather than just eyeballed (docs/07_RECIPE_ARCHITECTURE.md).
    """

    model_config = ConfigDict(frozen=True)

    record_count: int = 0
    container_matched: bool = False
    fill_rates: dict[str, float] = Field(default_factory=dict)
    consistency: float = 0.0
    """How uniformly each field's values are shaped. A "price" column that is
    a number in nine rows and a sentence in the tenth is suspicious even when
    every row is filled."""

    measured_at: datetime | None = None
    sample_url: str | None = None

    @property
    def mean_fill(self) -> float:
        return (
            round(sum(self.fill_rates.values()) / len(self.fill_rates), 3)
            if self.fill_rates
            else 0.0
        )

    @property
    def score(self) -> float:
        """One number for ranking candidates.

        Used in Phase 4 to pick between several proposals: let the model be
        imprecise and let the scorer choose (docs/08_LLM_ARCHITECTURE.md).
        """
        if not self.container_matched or self.record_count == 0:
            return 0.0
        return round(self.record_count * self.mean_fill * (0.5 + self.consistency / 2), 3)


MIN_RECORDS_FOR_ACTIVATION = 1
MIN_MEAN_FILL_FOR_ACTIVATION = 0.5


class Recipe(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    name: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    """Also the filename stem under ``recipes/``, which is why it is
    constrained to characters that are safe on every filesystem."""

    version: Annotated[int, Field(ge=1)] = 1
    status: RecipeStatus = RecipeStatus.CANDIDATE

    source_url: str
    allowed_domains: tuple[str, ...] = ()
    """Where this recipe is known to work. Intersected with the spec's scope
    at run time, never unioned."""

    fetch_mode: FetchMode = FetchMode.HTTP

    source: Literal["css", "jsonld", "embedded", "feed", "table", "article"] = "css"
    """Where the records come from.

    ``css`` reads the rendered DOM. ``jsonld`` and ``embedded`` read what the
    page declared about itself - and when a page declares it, that beats any
    selector, because it does not move when the site is restyled
    (docs/06_EXTRACTION_ARCHITECTURE.md).

    ``feed``, ``table`` and ``article`` are shapes whose schema is already
    known, and they need no field list at all: a feed entry has a title and a
    link because that is what a feed entry is, a table's field names are its
    header row, an article is one body of text. For these ``fields`` only
    renames or narrows what the shape already provides.

    For the rest the other fields keep their meaning: ``container`` finds the
    repeating unit and ``fields`` find values inside it. Only the language
    changes - a CSS selector, a schema.org ``@type``, or a dotted JSON path -
    which is what lets scoring and activation stay one code path.
    """

    container: str | None = None
    """One record per match. Absent means the page yields a single record -
    the detail-page shape.

    For ``jsonld`` this is the ``@type`` to collect ("Product",
    "VideoObject"); for ``embedded``, a dotted path whose first segment is the
    script id (``__NEXT_DATA__.props.pageProps.items``)."""

    fields: tuple[FieldRule, ...] = ()
    pagination: PaginationRule | None = None
    filters: tuple[RecordFilter, ...] = ()

    fingerprint: str | None = None
    """Structural fingerprint of the sample page. Lets this recipe be tried on
    a different domain running the same template."""

    quality: RecipeQuality = RecipeQuality()
    notes: str | None = None

    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("fields")
    @classmethod
    def _unique_field_names(cls, v: tuple[FieldRule, ...]) -> tuple[FieldRule, ...]:
        names = [f.name for f in v]
        duplicates = {n for n in names if names.count(n) > 1}
        if duplicates:
            raise ValueError(f"duplicate field names: {sorted(duplicates)}")
        return v

    @model_validator(mode="after")
    def _filters_reference_real_fields(self) -> Recipe:
        """A filter on a field the recipe never extracts drops everything.

        Silently, and looking exactly like a site that returned no results.
        Catching the typo here is the difference between a confusing empty
        crawl and an error message.
        """
        known = {f.name for f in self.fields}
        for rule in self.filters:
            if rule.field not in known:
                raise ValueError(
                    f"filter references unknown field {rule.field!r}; "
                    f"recipe extracts {sorted(known)}"
                )
        return self

    @model_validator(mode="after")
    def _active_recipes_must_have_been_measured(self) -> Recipe:
        """Activation is a claim that this works, and a claim needs evidence.

        docs/07_RECIPE_ARCHITECTURE.md
        """
        if self.status is not RecipeStatus.ACTIVE:
            return self
        if not self.fields and self.source not in _SCHEMA_KNOWN_SOURCES:
            raise ValueError("an active recipe must extract at least one field")
        if self.quality.record_count < MIN_RECORDS_FOR_ACTIVATION:
            raise ValueError(
                "cannot activate a recipe that extracted no records - "
                "run `crwallm recipe test` first"
            )
        if self.quality.mean_fill < MIN_MEAN_FILL_FOR_ACTIVATION:
            raise ValueError(
                f"mean fill rate {self.quality.mean_fill} is below "
                f"{MIN_MEAN_FILL_FOR_ACTIVATION}; the selectors match but produce little"
            )
        return self

    def with_quality(self, quality: RecipeQuality) -> Recipe:
        return self.model_copy(update={"quality": quality, "updated_at": datetime.now(UTC)})

    def activated(self) -> Recipe:
        """Promote to ``active``, re-running the validators.

        The quality gate is a model validator rather than a check here, so
        this cannot be bypassed by constructing the object directly.
        """
        return Recipe.model_validate(
            self.model_dump() | {"status": RecipeStatus.ACTIVE, "updated_at": datetime.now(UTC)}
        )

    def to_yaml_dict(self) -> dict[str, Any]:
        """Shape for a file on disk.

        Defaults and machine-managed fields are dropped: a recipe someone has
        to edit should show what was decided, not every field that exists.

        ``quality`` is the exception and is kept once it has been measured.
        It is the evidence for the ``active`` status, and the validators
        refuse to load an active recipe without it - dropping it would make a
        recipe that cannot be read back after being activated. It is also the
        thing a reviewer wants next to the claim: "active" means little on its
        own, "active, 8 records, 100% fill" means something.
        """
        data = self.model_dump(mode="json", exclude_none=True)
        for machine_owned in ("id", "created_at", "updated_at"):
            data.pop(machine_owned, None)
        if self.quality.measured_at is None:
            data.pop("quality", None)
        if not data.get("filters"):
            data.pop("filters", None)
        if data.get("fetch_mode") == FetchMode.HTTP.value:
            data.pop("fetch_mode", None)
        for field_data in data.get("fields", []):
            for key, default in (("transform", []), ("required", False), ("type", "text")):
                if field_data.get(key) == default:
                    field_data.pop(key, None)
            if field_data.get("attr") is None:
                field_data.pop("attr", None)
            if field_data.get("selector") == "":
                field_data.pop("selector", None)
        return data
