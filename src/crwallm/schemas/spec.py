"""CrawlSpec - "what to crawl and how far".

Pairs with Recipe, which owns "how to extract" (Phase 3). The split matters:
when a spec references a recipe, the recipe is system-of-record for its own
fields and the spec cannot widen them. docs/07_RECIPE_ARCHITECTURE.md

Validation happens in two gates (docs/08_LLM_ARCHITECTURE.md)::

    LLM or human -> Pydantic (structure, bounds) -> Policy (SSRF, PSL) -> engine

This module is the first gate only. It deliberately does no DNS, no network
and no public-suffix lookup - those live in ``crwallm.policy``.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from crwallm.schemas.types import CrawlMode, FetchMode

# Ceilings no spec may exceed, whoever wrote it. A model that emits
# max_pages=10_000_000 is rejected here rather than at 3am.
HARD_MAX_PAGES = 1_000_000
HARD_MAX_DEPTH = 32
HARD_BYTE_LIMIT = 100 * 1024 * 1024


class CrawlLimits(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    max_pages: Annotated[int, Field(ge=1, le=HARD_MAX_PAGES)] = 100
    max_depth: Annotated[int, Field(ge=0, le=HARD_MAX_DEPTH)] = 3

    response_byte_limit: Annotated[int, Field(ge=1024, le=HARD_BYTE_LIMIT)] = 5_000_000
    """Applies to the normal fetch path. Documents and media get a separate
    binary channel with its own ceiling (Phase 10)."""

    request_timeout_s: Annotated[float, Field(gt=0, le=300)] = 15.0
    job_timeout_s: Annotated[float, Field(gt=0, le=86_400)] = 3600.0
    redirect_max: Annotated[int, Field(ge=0, le=20)] = 5

    global_concurrency: Annotated[int, Field(ge=1, le=1024)] = 32
    per_host_concurrency: Annotated[int, Field(ge=1, le=64)] = 8

    min_interval_ms: Annotated[int, Field(ge=0, le=60_000)] = 0
    """Floor on the gap between requests to one host. Zero by default - this is
    a personal tool tuned for throughput. The adaptive controller raises it on
    its own once a host starts pushing back. docs/12_PERFORMANCE.md"""


class SpiderConfig(BaseModel):
    """Crawler-trap defences.

    ``max_pages`` alone does not protect a spider: an infinite calendar will
    happily consume the entire budget. docs/05_SPIDER_ARCHITECTURE.md
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_url_length: Annotated[int, Field(ge=64, le=8192)] = 2048
    max_path_depth: Annotated[int, Field(ge=1, le=64)] = 12

    max_repeated_segment: Annotated[int, Field(ge=1, le=16)] = 2
    """``/a/b/a/b/a`` - how many times one segment may repeat in a path."""

    max_query_params: Annotated[int, Field(ge=0, le=64)] = 8

    query_whitelist: frozenset[str] | None = None
    """When set, only these query parameters survive dedupe normalisation. The
    single most effective defence against faceted-navigation explosion."""

    per_pattern_budget: Annotated[int, Field(ge=1, le=1_000_000)] = 500
    """Cap per URL *pattern* (``/calendar/{n}/{n}``), not per URL. One knob
    that kills calendars, endless pagination and facet combinatorics at once."""


class UrlFilters(BaseModel):
    """Pre-fetch URL gating - the cheapest filter available, it saves the request.

    Record-level filtering happens after extraction (Phase 3).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()

    @field_validator("include", "exclude")
    @classmethod
    def _compilable(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        for pattern in v:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"invalid regex {pattern!r}: {exc}") from exc
        return v


class BrowserConfig(BaseModel):
    """What the browser is allowed to do, when it is used at all.

    Separate from ``CrawlLimits`` because these are meaningless in the HTTP
    path, and a spec that mixed them would invite setting a scroll depth on a
    crawl that will never open a browser.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    scroll_rounds: Annotated[int, Field(ge=0, le=200)] = 0
    """Zero means do not scroll, which is right for most pages.

    An infinite feed loads by calling an XHR, and calling that XHR directly is
    roughly twenty times faster - which is what the browser's recorded
    requests are for. Scrolling is the fallback for when that call cannot be
    reconstructed (docs/04_CRAWLING_ARCHITECTURE.md)."""

    scroll_pause_ms: Annotated[int, Field(ge=50, le=10_000)] = 700
    scroll_selector: str | None = None
    """What to count between scroll rounds. Page height stands in when absent,
    and it also grows when a footer lazy-loads."""


class CrawlSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    name: str | None = None
    mode: CrawlMode = CrawlMode.COLLECT

    seed_urls: tuple[str, ...]

    allowed_domains: tuple[str, ...]
    """Registrable domains. Bare public suffixes ("com", "co.uk") are rejected
    by the policy gate, not here - that needs the PSL."""

    fetch_mode: FetchMode = FetchMode.HTTP

    follow_links: bool = False
    """Collect mode extracts the seed pages only unless this is set."""

    limits: CrawlLimits = CrawlLimits()
    spider: SpiderConfig = SpiderConfig()
    browser: BrowserConfig = BrowserConfig()
    url_filters: UrlFilters = UrlFilters()

    recipe: str | None = None
    """Name of the recipe to extract with - the ``recipes/*.yaml`` stem.

    The name, not ``Recipe.id``: the id is generated on each load and never
    written to the file, so it identifies nothing across a queue boundary.
    The filename is the identity the store, the CLI and the user all use
    (docs/07_RECIPE_ARCHITECTURE.md).
    """

    recipe_version: int | None = None
    """Refuse to run if the recipe on disk is not this version.

    A job can sit in the queue while the recipe is edited underneath it.
    Pinning turns "quietly extracted with different rules" into an error.
    """

    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("seed_urls")
    @classmethod
    def _seeds_present(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        if not v:
            raise ValueError("at least one seed URL is required")
        return v

    @field_validator("allowed_domains")
    @classmethod
    def _normalise_domains(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        if not v:
            raise ValueError("allowed_domains must not be empty - that is an unbounded crawl")
        cleaned = []
        for raw in v:
            d = raw.strip().lower().removeprefix("*.").rstrip(".")
            if not d:
                raise ValueError(f"empty domain in allowed_domains: {raw!r}")
            cleaned.append(d)
        return tuple(cleaned)

    @model_validator(mode="after")
    def _spider_implies_following(self) -> CrawlSpec:
        if self.mode is CrawlMode.SPIDER and not self.follow_links:
            raise ValueError("spider mode requires follow_links=True")
        return self

    @model_validator(mode="after")
    def _recipe_version_pairs_with_a_recipe(self) -> CrawlSpec:
        if self.recipe_version is not None and self.recipe is None:
            raise ValueError("recipe_version given without recipe")
        return self
