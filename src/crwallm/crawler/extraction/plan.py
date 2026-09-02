"""Everything a recipe says about extraction, in one shape.

This exists because there were four conversions from ``Recipe`` - one per
extraction source - and adding a field to a recipe meant remembering all four.
Three times that was forgotten, and each time the result was the same kind of
failure: a crawl that ran perfectly and produced nothing, with a recipe that
looked correct.

    79eebe7  the worker never loaded a recipe at all
    9ba5630  a recipe's filters were ignored during a crawl
    11ee3cd  transforms never reached the declared-data sources

The fix is not fewer shapes - each extractor wants its own - but one place
that knows how a recipe becomes them. ``Recipe -> Extraction`` is that place,
and ``build`` below is the only code that constructs an extractor. Adding a
field to ``Recipe`` now means editing one function, and ``test_extraction_plan``
fails if it is not.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from crwallm.schemas.filters import RecordFilter

if TYPE_CHECKING:  # pragma: no cover - import cost only
    from crwallm.crawler.contracts import Extractor

__all__ = ["DECLARED_SOURCES", "DOCUMENT_SOURCES", "Extraction", "Field", "build"]

DECLARED_SOURCES = frozenset({"jsonld", "microdata", "embedded"})
"""Sources that read what the page states about itself."""

DOCUMENT_SOURCES = frozenset({"feed", "table", "article"})
"""Shapes that carry their own schema, so a recipe need name no fields."""


@dataclass(frozen=True, slots=True)
class Field:
    """One value to pull out, whatever the source.

    ``path`` is a CSS selector, a dotted JSON path, or a key of a known-schema
    record - the language changes with the source, the role does not. Keeping
    them one type is what stopped ``transform`` from being dropped on three of
    the four paths.
    """

    name: str
    path: str = ""
    kind: str = "text"
    """``text``, ``html``, ``href``, ``src``, ``attr``. CSS only; the other
    sources read whatever the data holds."""

    attr: str | None = None
    transform: tuple[str, ...] = ()
    required: bool = False
    """Drops the record when empty, rather than lowering a fill rate."""


@dataclass(frozen=True, slots=True)
class Extraction:
    """What to pull off a page, and what to keep."""

    source: str = "css"
    container: str | None = None
    fields: tuple[Field, ...] = ()
    filters: tuple[RecordFilter, ...] = field(default_factory=tuple)
    link_selector: str = "a[href]"
    follow_links: bool = True

    @property
    def required_names(self) -> tuple[str, ...]:
        return tuple(f.name for f in self.fields if f.required)

    @property
    def extracts_records(self) -> bool:
        """Whether anything was asked for.

        A crawl with no extraction produces zero records on every page by
        design, so ``auto`` must not read that as "this page needs
        rendering" - it would render every page of a spider run. The
        known-schema sources count without fields because they supply their
        own (docs/04_CRAWLING_ARCHITECTURE.md).
        """
        return bool(self.fields) or self.source in DOCUMENT_SOURCES


def build(extraction: Extraction) -> Extractor:
    """The one place an extractor is constructed.

    Each source keeps the shape its extractor wants; this is where an
    ``Extraction`` becomes one. Every field of ``Extraction`` has to be used
    here or it silently does nothing, which is exactly the failure this module
    exists to prevent.
    """
    from crwallm.crawler.extraction.css import CssExtractor, CssSpec, FieldSpec
    from crwallm.crawler.extraction.documents import DocumentExtractor
    from crwallm.crawler.extraction.structured import (
        FieldPath,
        StructuredExtractor,
        StructuredSpec,
    )

    css = CssSpec(
        container=extraction.container if extraction.source == "css" else None,
        fields=tuple(
            FieldSpec(
                name=f.name,
                selector=f.path,
                type=f.kind,  # type: ignore[arg-type]
                attr=f.attr,
                transform=f.transform,
            )
            for f in extraction.fields
        )
        if extraction.source == "css"
        else (),
        link_selector=extraction.link_selector,
        follow_links=extraction.follow_links,
    )

    if extraction.source in DOCUMENT_SOURCES:
        return DocumentExtractor(
            kind=extraction.source,
            container=extraction.container,
            # A known-schema source names keys, not paths, and a field with no
            # path means "keep this one as it is".
            fields=tuple(
                FieldPath(name=f.name, path=f.path or f.name, transform=f.transform)
                for f in extraction.fields
            ),
            css=css,
        )

    if extraction.source in DECLARED_SOURCES:
        return StructuredExtractor(
            spec=StructuredSpec(
                kind=extraction.source,
                container=extraction.container,
                fields=tuple(
                    FieldPath(name=f.name, path=f.path, transform=f.transform)
                    for f in extraction.fields
                ),
            ),
            css=css,
        )

    return CssExtractor(css)
