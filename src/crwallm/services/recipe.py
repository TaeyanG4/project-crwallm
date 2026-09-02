"""Recipe lifecycle: files, testing, scoring, activation.

**Files are the original.** A recipe that lives only in a table cannot be
opened in an editor, reviewed in a diff, or kept in version control - and
those are how selectors actually get fixed
(docs/07_RECIPE_ARCHITECTURE.md). ``recipes/*.yaml`` is the source; the
database holds a copy for the crawler to read and for statistics to accumulate
against.

**Testing is deterministic and needs no network.** A tested recipe runs
against an archived body, so iterating on selectors is a local read rather
than a request. That is what the Phase 2 archive was for, and it is the
difference between a fast edit loop and a slow one that also annoys the site
being developed against (docs/12_PERFORMANCE.md).

**Activation is earned.** ``candidate`` becomes ``active`` only after a run
produced records at an acceptable fill rate. The same measurement becomes the
drift signal in Phase 11 and the scorer that picks between model proposals in
Phase 4 - one metric, three uses.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from crwallm.crawler.contracts import FetchResponse
from crwallm.crawler.extraction.css import CssExtractor, CssSpec, FieldSpec
from crwallm.crawler.extraction.documents import DocumentExtractor
from crwallm.crawler.extraction.structured import StructuredSpec
from crwallm.schemas.filters import apply_filters
from crwallm.schemas.recipe import Recipe, RecipeQuality, RecipeStatus

__all__ = [
    "RecipeStore",
    "RecipeTestResult",
    "load_recipe_file",
    "measure",
    "save_recipe_file",
    "to_css_spec",
]

RECIPE_SUFFIX = ".yaml"


# ------------------------------------------------------------------- files


class RecipeFileError(ValueError):
    """A file on disk is not a usable recipe."""


def load_recipe_file(path: Path) -> Recipe:
    """Read one ``recipes/*.yaml``.

    ``safe_load``, not ``load``. A recipe file is data - possibly written by a
    model from Phase 4 - and PyYAML's full loader constructs arbitrary Python
    objects, which would make "declarative recipe" untrue in the one way that
    matters (docs/17_NON_GOALS.md).
    """
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise RecipeFileError(f"{path.name}: {exc}") from exc

    if not isinstance(raw, dict):
        raise RecipeFileError(f"{path.name}: expected a mapping at the top level")

    raw.setdefault("name", path.stem)
    try:
        return Recipe.model_validate(raw)
    except Exception as exc:
        raise RecipeFileError(f"{path.name}: {exc}") from exc


def save_recipe_file(recipe: Recipe, directory: Path) -> Path:
    path = directory / f"{recipe.name}{RECIPE_SUFFIX}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(
            recipe.to_yaml_dict(),
            sort_keys=False,
            allow_unicode=True,  # Korean selectors and notes stay readable
            default_flow_style=False,
        ),
        encoding="utf-8",
    )
    return path


class RecipeStore:
    """The ``recipes/`` directory."""

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)

    def list_files(self) -> list[Path]:
        if not self.directory.exists():
            return []
        return sorted(
            p for p in self.directory.glob(f"*{RECIPE_SUFFIX}") if not p.name.startswith("_")
        )

    def load_all(self) -> tuple[list[Recipe], list[tuple[Path, str]]]:
        """Every recipe, plus the ones that failed to load.

        Failures are returned rather than raised: one broken file should not
        hide the twenty that work.
        """
        loaded: list[Recipe] = []
        errors: list[tuple[Path, str]] = []
        for path in self.list_files():
            try:
                loaded.append(load_recipe_file(path))
            except RecipeFileError as exc:
                errors.append((path, str(exc)))
        return loaded, errors

    def load(self, name: str) -> Recipe:
        path = self.directory / f"{name}{RECIPE_SUFFIX}"
        if not path.exists():
            raise RecipeFileError(f"no recipe named {name!r} in {self.directory}")
        return load_recipe_file(path)

    def save(self, recipe: Recipe) -> Path:
        return save_recipe_file(recipe, self.directory)


# --------------------------------------------------------------- execution


def to_css_spec(recipe: Recipe, *, follow_links: bool = False) -> CssSpec:
    """Recipe to the extractor's shape."""
    return CssSpec(
        container=recipe.container,
        fields=tuple(
            FieldSpec(
                name=f.name,
                selector=f.selector,
                type=f.type,  # type: ignore[arg-type]
                attr=f.attr,
                transform=f.transform,
            )
            for f in recipe.fields
        ),
        follow_links=follow_links,
    )


DOCUMENT_SOURCES = frozenset({"feed", "table", "article"})


def to_structured_spec(recipe: Recipe) -> StructuredSpec | None:
    """The declared-data half of a recipe, or None for any other source."""
    if recipe.source not in {"jsonld", "embedded"}:
        return None
    return StructuredSpec(
        kind=recipe.source,
        container=recipe.container,
        fields=tuple((f.name, f.selector) for f in recipe.fields),
    )


def to_document_spec(recipe: Recipe) -> DocumentExtractor | None:
    """The known-schema half, or None.

    Returns the extractor rather than a spec because there is no spec to
    speak of: the shape carries its own schema, and what the recipe adds is
    at most a rename.
    """
    if recipe.source not in DOCUMENT_SOURCES:
        return None
    return DocumentExtractor(
        kind=recipe.source,
        container=recipe.container,
        fields=tuple((f.name, f.selector or f.name) for f in recipe.fields),
    )


@dataclass(frozen=True, slots=True)
class RecipeTestResult:
    """What a deterministic run produced, and whether it is good enough."""

    recipe: Recipe
    quality: RecipeQuality
    records: tuple[dict[str, Any], ...]
    filtered_out: int
    filter_reasons: dict[str, int]

    @property
    def passes(self) -> bool:
        from crwallm.schemas.recipe import (
            MIN_MEAN_FILL_FOR_ACTIVATION,
            MIN_RECORDS_FOR_ACTIVATION,
        )

        return (
            self.quality.container_matched
            and self.quality.record_count >= MIN_RECORDS_FOR_ACTIVATION
            and self.quality.mean_fill >= MIN_MEAN_FILL_FOR_ACTIVATION
        )

    @property
    def summary(self) -> str:
        return (
            f"{self.quality.record_count} records, "
            f"fill {self.quality.mean_fill:.0%}, "
            f"consistency {self.quality.consistency:.0%}, "
            f"score {self.quality.score}"
        )


def _shape_of(value: Any) -> str:
    """Coarse type of a value, for the consistency metric.

    Coarse on purpose. "Every price is a number except one, which is a
    sentence" is worth knowing; "these two numbers differ" is not.
    """
    if value is None or value == "":
        return "empty"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int | float):
        return "number"
    text = str(value)
    if text.startswith(("http://", "https://", "/")):
        return "url"
    if text.replace(",", "").replace(".", "").replace("-", "").isdigit():
        return "numeric-text"
    if len(text) > 200:
        return "long-text"
    return "text"


def _consistency(records: list[dict[str, Any]], field_names: list[str]) -> float:
    """How uniformly each field is shaped across records.

    A field that is a number in nine rows and prose in the tenth usually means
    the selector is picking up a neighbouring element on some cards. Fill rate
    alone would call that perfect.
    """
    if not records or not field_names:
        return 0.0

    scores: list[float] = []
    for name in field_names:
        shapes = [_shape_of(r.get(name)) for r in records]
        non_empty = [s for s in shapes if s != "empty"]
        if not non_empty:
            scores.append(0.0)
            continue
        dominant = max(set(non_empty), key=non_empty.count)
        scores.append(non_empty.count(dominant) / len(non_empty))
    return round(sum(scores) / len(scores), 3)


def measure(recipe: Recipe, response: FetchResponse) -> RecipeTestResult:
    """Run ``recipe`` against one page and score the result.

    No network: the response can come from the archive, which is what makes
    the edit loop fast.
    """
    extractor = CssExtractor(to_css_spec(recipe))
    result = extractor.extract(response)
    records = list(result.records)

    container_matched = bool(records) or recipe.container is None
    field_names = [f.name for f in recipe.fields]

    fill_rates = (
        {
            name: round(sum(1 for r in records if r.get(name) not in (None, "")) / len(records), 3)
            for name in field_names
        }
        if records
        else dict.fromkeys(field_names, 0.0)
    )

    # Required fields drop the record rather than lowering a rate: an entry
    # missing its title is not a partial record, it is noise.
    required = [f.name for f in recipe.fields if f.required]
    if required:
        records = [r for r in records if all(r.get(n) not in (None, "") for n in required)]

    filtered = apply_filters(records, recipe.filters)

    quality = RecipeQuality(
        record_count=len(filtered.kept),
        container_matched=container_matched,
        fill_rates=fill_rates,
        consistency=_consistency(filtered.kept, field_names),
        measured_at=datetime.now(UTC),
        sample_url=response.url.url,
    )

    return RecipeTestResult(
        recipe=recipe.with_quality(quality),
        quality=quality,
        records=tuple(filtered.kept),
        filtered_out=filtered.dropped,
        filter_reasons=filtered.reasons,
    )


def activate(result: RecipeTestResult) -> Recipe:
    """Promote a tested recipe, or explain why it cannot be.

    The check lives in ``Recipe``'s validators too, so this cannot be
    sidestepped by constructing the object directly - this is the version that
    produces a message worth reading.
    """
    if not result.quality.container_matched:
        raise ValueError(
            f"container {result.recipe.container!r} matched nothing on {result.quality.sample_url}"
        )
    if result.quality.record_count == 0:
        raise ValueError(
            "the recipe matched a container but extracted no records - "
            "check the field selectors, or a filter that drops everything"
        )
    if not result.passes:
        empty = sorted(n for n, rate in result.quality.fill_rates.items() if rate < 0.5)
        raise ValueError(
            f"mean fill rate {result.quality.mean_fill:.0%} is too low; rarely filled: {empty}"
        )
    return result.recipe.activated()


def status_line(recipe: Recipe) -> str:
    marker = {
        RecipeStatus.ACTIVE: "active",
        RecipeStatus.CANDIDATE: "candidate",
        RecipeStatus.DEPRECATED: "deprecated",
    }[recipe.status]
    return (
        f"{recipe.name:<28} v{recipe.version:<3} {marker:<10} "
        f"{len(recipe.fields)} fields  {recipe.source_url}"
    )
