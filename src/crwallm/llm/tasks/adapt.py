"""Turning a page into a recipe, with a model that is allowed to be wrong.

This is the task the whole architecture was arranged around, and the
arrangement is what makes a 9B model sufficient for it.

**The model does not find selectors.** Phase 3's detector already found the
repeated containers and their columns, deterministically. What is left is
naming them - "column 2 holds 190,000 and 890,000, what is that?" - which is a
question a small model answers well and a question with a *checkable* answer.
Asking a model to produce selectors from raw HTML is the hard version, needs
long context and exact strings, and one wrong character yields zero records
(docs/08_LLM_ARCHITECTURE.md).

**Several proposals, one deterministic judge.** The model produces N namings;
each becomes a real recipe, each is run against the real page, and the one
that extracts the most complete and most consistent records wins. The model is
allowed to be imprecise because the scorer is not
(docs/07_RECIPE_ARCHITECTURE.md).

**Failure is fed back, not swallowed.** A proposal that extracts nothing is
returned to the model with what actually happened - which columns exist, what
it picked, how many records came out. Three rounds of that turns a mediocre
first answer into a working recipe far more often than one careful attempt.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Annotated

from pydantic import BaseModel, Field, StringConstraints

from crwallm.crawler.contracts import FetchResponse
from crwallm.crawler.extraction.css import parse
from crwallm.llm.gateway import GatewayError, GenerationOptions, ModelGateway, TaskKind, Usage
from crwallm.schemas.recipe import FieldRule, Recipe
from crwallm.services.recipe import RecipeTestResult, measure
from crwallm.structure.detector import Candidate, Column, detect_containers
from crwallm.structure.fingerprint import fingerprint_of

__all__ = ["AdaptationOutcome", "adapt_page", "build_prompt"]

MAX_ROUNDS = 3
CANDIDATES_PER_ROUND = 3

_SYSTEM = """You name the columns of a web listing.

The columns have already been found. Your only job is to say what each one
holds, using short snake_case English names.

Prefer these names when they fit: title, url, price, image, date, author,
description, category, rating, duration, views, location, id.

Skip a column if it is decoration, a repeated button label, or has no clear
meaning. Naming fewer columns well is better than naming all of them badly.

Answer only with JSON matching the schema."""


FieldName = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,31}$", strip_whitespace=True),
]


class NamedColumn(BaseModel):
    """One column, named.

    ``index`` refers to the detector's numbering rather than a selector: the
    model never writes a selector, so it cannot write a broken one.
    """

    index: int = Field(ge=0)
    name: FieldName


class ColumnNaming(BaseModel):
    fields: list[NamedColumn] = Field(min_length=1, max_length=20)


@dataclass(slots=True)
class AdaptationOutcome:
    recipe: Recipe | None
    result: RecipeTestResult | None
    rounds: int
    usage: list[Usage] = field(default_factory=list)
    attempts: list[str] = field(default_factory=list)
    """One line per proposal: what it was named and what it scored. This is the
    record of how the answer was reached, and the input to the next round."""

    @property
    def succeeded(self) -> bool:
        return self.recipe is not None and self.result is not None and self.result.passes

    @property
    def total_elapsed_ms(self) -> int:
        return sum(u.elapsed_ms for u in self.usage)

    @property
    def total_tokens(self) -> int:
        return sum(u.total_tokens for u in self.usage)


def build_prompt(candidate: Candidate, *, feedback: str = "") -> str:
    """Describe the detected columns.

    Samples, fill rates and selectors - everything a person would need to name
    them, and nothing about the surrounding page. A reduced DOM would be more
    context for a worse question.
    """
    lines = [
        f"A listing page repeats {candidate.count} items matching "
        f"`{candidate.selector}`. These columns were detected inside each item:",
        "",
    ]
    for column in candidate.usable_columns:
        samples = " | ".join(s[:60] for s in column.samples[:2])
        lines.append(
            f"[{column.index}] {column.selector}  ({column.kind}, "
            f"filled in {column.fill_rate:.0%})  ->  {samples}"
        )
    lines.append("")
    lines.append("Name each column you can identify.")

    if feedback:
        lines += ["", "Your previous attempt did not work:", feedback]
    return "\n".join(lines)


def _to_recipe(
    naming: ColumnNaming,
    candidate: Candidate,
    *,
    name: str,
    source_url: str,
    allowed_domains: tuple[str, ...],
    fingerprint: str | None,
) -> Recipe | None:
    """Assemble a real recipe from names plus detected selectors.

    The selectors come from the detector and the names from the model, so a
    hallucinated selector is not a possible failure. Names that collide or
    point at nothing are dropped here rather than reaching validation.
    """
    by_index: dict[int, Column] = {c.index: c for c in candidate.usable_columns}

    rules: list[FieldRule] = []
    seen: set[str] = set()
    for named in naming.fields:
        column = by_index.get(named.index)
        if column is None or named.name in seen:
            continue
        seen.add(named.name)
        rules.append(
            FieldRule(
                name=named.name,
                selector=column.selector,
                type=column.kind,
                transform=("to_absolute_url",) if column.kind in ("href", "src") else (),
            )
        )

    if not rules:
        return None

    try:
        return Recipe(
            name=name,
            source_url=source_url,
            allowed_domains=allowed_domains,
            container=candidate.selector,
            fields=tuple(rules),
            fingerprint=fingerprint,
            notes=f"named by model from {len(candidate.usable_columns)} detected columns",
        )
    except ValueError:
        # The schema is the authority, not the model. A proposal it rejects is
        # simply not a candidate.
        return None


def _feedback_for(result: RecipeTestResult | None, recipe: Recipe | None) -> str:
    if recipe is None:
        return "None of the indices you gave matched a detected column. Use the [n] numbers above."
    if result is None:
        return "The recipe could not be built. Use only the indices listed."

    named = ", ".join(f"[{i}] -> {f.name}" for i, f in enumerate(recipe.fields))
    if result.quality.record_count == 0:
        return (
            f"You named: {named}. That produced 0 records. "
            "Check you used the indices from the list, not invented ones."
        )
    empty = sorted(n for n, rate in result.quality.fill_rates.items() if rate < 0.5)
    return (
        f"You named: {named}. That produced {result.quality.record_count} records "
        f"at {result.quality.mean_fill:.0%} average fill. "
        + (f"These were mostly empty: {empty}. Try different columns." if empty else "")
    )


async def adapt_page(
    gateway: ModelGateway,
    response: FetchResponse,
    *,
    name: str,
    allowed_domains: tuple[str, ...],
    rounds: int = MAX_ROUNDS,
    candidates: int = CANDIDATES_PER_ROUND,
    options: GenerationOptions | None = None,
) -> AdaptationOutcome:
    """Produce a working recipe for ``response``, or explain why not.

    The loop is: propose several namings, build each into a recipe, run each
    against the real page, keep the best. If nothing passes, tell the model
    what happened and go again.
    """
    tree, _ = parse(response)
    detected = detect_containers(tree)
    outcome = AdaptationOutcome(recipe=None, result=None, rounds=0)

    if not detected:
        outcome.attempts.append(
            "no repeated structure on this page - it looks like a detail page, "
            "so there are no columns to name"
        )
        return outcome

    candidate = detected[0]
    fingerprint = str(fingerprint_of(tree))
    best_recipe: Recipe | None = None
    best_result: RecipeTestResult | None = None
    feedback = ""

    for round_index in range(rounds):
        outcome.rounds = round_index + 1
        prompt = build_prompt(candidate, feedback=feedback)

        round_best: RecipeTestResult | None = None
        round_recipe: Recipe | None = None
        failed = False

        # One proposal at a time, stopping as soon as one passes. Asking for
        # all N up front costs N generations even when the first is correct,
        # and on an easy page the first usually is - measured at 36s for three
        # identical answers where one would have taken twelve. The scorer still
        # chooses; it just does not pay for choices it does not need.
        for attempt in range(candidates):
            try:
                proposals = await gateway.generate_structured(
                    task=TaskKind.ADAPT_SELECTORS,
                    prompt=prompt,
                    schema=ColumnNaming,
                    system=_SYSTEM,
                    options=options,
                    n=1,
                )
            except GatewayError as exc:
                outcome.attempts.append(f"round {round_index + 1}: {exc}")
                failed = True
                break

            proposal = proposals[0]
            outcome.usage.append(proposal.usage)
            recipe = _to_recipe(
                proposal.value,
                candidate,
                name=name,
                source_url=response.url.url,
                allowed_domains=allowed_domains,
                fingerprint=fingerprint,
            )
            if recipe is None:
                outcome.attempts.append(f"round {round_index + 1}.{attempt + 1}: unusable proposal")
                continue

            result = measure(recipe, response)
            outcome.attempts.append(
                f"round {round_index + 1}.{attempt + 1}: "
                f"{', '.join(f.name for f in recipe.fields)} -> {result.summary}"
            )
            if round_best is None or result.quality.score > round_best.quality.score:
                round_best, round_recipe = result, recipe
            if result.passes:
                break

        if failed:
            break

        if round_best is not None and (
            best_result is None or round_best.quality.score > best_result.quality.score
        ):
            best_result, best_recipe = round_best, round_recipe

        if best_result is not None and best_result.passes:
            break

        feedback = _feedback_for(round_best, round_recipe)

    outcome.recipe = best_recipe if best_result is None else best_result.recipe
    outcome.result = best_result
    return outcome
