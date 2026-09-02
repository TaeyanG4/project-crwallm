"""Structural fingerprints.

A recipe is normally reused by domain: crawl this site again, use the recipe
that worked. A fingerprint lets it be reused by *shape* instead - when a new
page has the same skeleton as one already solved, try that recipe before
asking anyone anything.

**Why it pays off here specifically.** Most Korean commerce runs on a handful
of hosted platforms - Cafe24, MakeShop, Godo, NHN Commerce - and every store
on one of them ships near-identical markup under a different domain. Solve one
Cafe24 store and the fingerprint matches the next hundred. The same holds for
WordPress themes, Shopify, and any CMS with a default template.

**What it must and must not be sensitive to.** It has to survive different
*content* - a store selling shoes and one selling laptops share a template -
and it has to notice a different *structure*. So it hashes the shape: which
containers repeat, at what depth, with which columns. Text never enters it.

The fingerprint is a hint, never an authority. A match means "try this recipe
first"; the deterministic run then either produces records or does not, and
that answer is the one that counts (docs/07_RECIPE_ARCHITECTURE.md).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from selectolax.lexbor import LexborHTMLParser

from crwallm.structure.detector import _LAYOUT_CLASS, Candidate, detect_containers

__all__ = ["Fingerprint", "fingerprint_of", "similarity"]

_VERSION = "fp1"
"""Bumped when the hashing changes. Stored fingerprints from an older version
must not silently compare unequal-but-plausible against new ones; they compare
as a different scheme and simply do not match."""

# Shared with the detector: selectors it produces are already filtered, but a
# fingerprint may be computed from a stored selector that was not.


def _shape_of_selector(selector: str) -> str:
    """Reduce a selector to the part that describes a kind of thing.

    ``li.product-item.col-3`` and ``li.product-item.col-4`` are the same shape;
    keeping the layout class would make every store its own fingerprint.
    """
    tag, _, rest = selector.partition(".")
    if not rest:
        return tag
    classes = sorted(c for c in rest.split(".") if c and not _LAYOUT_CLASS.match(c))
    return f"{tag}.{'.'.join(classes)}" if classes else tag


def _bucket(count: int) -> str:
    """Coarse magnitude, not the number itself.

    A listing with 20 items and one with 24 are the same template; a listing
    with 20 and one with 2000 are not.
    """
    if count < 5:
        return "s"
    if count < 20:
        return "m"
    if count < 100:
        return "l"
    return "xl"


@dataclass(frozen=True, slots=True)
class Fingerprint:
    digest: str
    features: tuple[str, ...]
    """The strings that went into the hash. Kept so a near-match can be scored
    and, more usefully, so a mismatch can be explained."""

    def __str__(self) -> str:
        return self.digest

    @property
    def is_empty(self) -> bool:
        return not self.features


def _candidate_features(candidate: Candidate) -> list[str]:
    shape = _shape_of_selector(candidate.selector)
    parent = _shape_of_selector(candidate.parent_selector) if candidate.parent_selector else "-"
    features = [f"c:{parent}>{shape}@{candidate.depth}x{_bucket(candidate.count)}"]
    for column in candidate.usable_columns:
        features.append(f"f:{shape}|{_shape_of_selector(column.selector)}|{column.kind}")
    return features


def fingerprint_of(tree: LexborHTMLParser, *, top_n: int = 3) -> Fingerprint:
    """Fingerprint the page's repeated structure.

    Only the strongest candidates contribute. A page's incidental repetition -
    a footer link list, a set of social icons - varies between sites running
    the same template, and including it would defeat the purpose.
    """
    candidates = detect_containers(tree, limit=top_n)

    features: list[str] = []
    for candidate in candidates:
        features.extend(_candidate_features(candidate))

    # Sorted: the order candidates come back in depends on scores that shift
    # with content, and the fingerprint must not.
    features.sort()

    digest = hashlib.sha256("\n".join(features).encode()).hexdigest()[:32]
    return Fingerprint(digest=f"{_VERSION}:{digest}", features=tuple(features))


def similarity(a: Fingerprint, b: Fingerprint) -> float:
    """Jaccard overlap of two fingerprints' features.

    Exact digests match or they do not; this is for the case where they do
    not and the question is whether it is worth trying the recipe anyway.
    """
    if not a.features or not b.features:
        return 0.0
    left, right = set(a.features), set(b.features)
    return round(len(left & right) / len(left | right), 3)
