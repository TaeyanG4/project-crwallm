"""Finding repeated structure without a model.

The highest-leverage part of the project, and the least obvious. A listing
page is a set of siblings that share a shape - and that is a property you can
compute, not one you need to ask a language model about.

**What this changes.** Asking a model to "find the selectors" is the hard
version of the task: it needs long context, precise structural reasoning, and
an exact string, where one wrong character yields zero records. Asking it
"which of these four columns is the price" is the easy version - a 4B model
does it. Detecting the containers here is what turns the first question into
the second (docs/08_LLM_ARCHITECTURE.md).

It also means the tool works with no model at all. Levels 0 and 1 of
docs/02_PRODUCT_MODEL.md - hand-written selectors, or picking from what was
found - are the whole product until Phase 4, and stay the fallback after it.

**How.** Group siblings by a structural signature, keep the groups that repeat
and carry text, then expand each group into columns by walking the paths that
appear in most members. Scoring is deliberately boring: repetition count, how
consistently each column is filled, and text density. A navigation menu
repeats too, and the thing that separates it from a product grid is that its
items carry two words each.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from selectolax.lexbor import LexborHTMLParser, LexborNode

__all__ = [
    "Candidate",
    "Column",
    "detect_containers",
    "node_signature",
    "selector_for",
]

# Classes that are generated per-element rather than describing a kind of
# element. Including them in a signature makes every sibling unique, which is
# exactly backwards.
_NOISE_CLASS = re.compile(
    r"""^(?:
        [a-z]+(?:[-_][a-z]+)*[-_]\d+       # col-md-3, item-2, span_4
        | \d+
        | [0-9a-f]{6,}                      # hashed build classes
        | (?:is|has|js)[-_].*               # state flags
        | (?:active|selected|current|open|first|last|odd|even|hover)
        | css-[0-9a-z]+                     # emotion / styled-components
        | sc-[0-9a-zA-Z]+
    )$""",
    re.VERBOSE | re.IGNORECASE,
)

_LAYOUT_CLASS = re.compile(
    # Short prefixes must carry a value: "p-2" is padding, a bare "p" is
    # probably somebody's own class name and dropping it costs specificity.
    r"^(?:d|p|m|w|h|gap|mt|mb|ml|mr|pt|pb|pl|pr|px|py|mx|my)[-_].+$"
    r"|^(?:col|grid|flex|clearfix|rounded|shadow)(?:[-_].*)?$"
    # These read as layout only when parameterised.
    r"|^(?:row|container|wrapper|text|bg|border|align|justify)[-_].+$",
    re.IGNORECASE,
)
"""Utility classes from Bootstrap, Tailwind and their imitators.

They describe where a thing sits, not what it is. Leaving them in a selector
makes the recipe break when a site changes its grid from three columns to
four - a restyle, not a restructure - and it makes two stores on the same
platform look like different templates to the fingerprint.
"""

# Structural wrappers that never identify a record on their own.
_SKIP_TAGS = frozenset(
    {"script", "style", "noscript", "template", "svg", "path", "br", "hr", "meta", "link"}
)

_CHROME_TAGS = frozenset({"nav", "header", "footer", "aside"})

MIN_GROUP_SIZE = 3
"""Below this, "repeated" is indistinguishable from coincidence. Two matching
siblings happen constantly; three is a pattern."""

MAX_COLUMN_DEPTH = 4
"""How far into a container to look for fields. Deeper than this and the paths
stop being stable across members."""

MIN_COLUMN_FILL = 0.5
"""A path present in fewer than half the members is not a column of this
listing - it is an optional badge on some of them."""


def _classes(node: LexborNode) -> tuple[str, ...]:
    """Classes that say what a node *is*.

    Both filters matter and for different reasons: noise classes make every
    sibling unique so nothing groups, and layout classes make every restyle
    look like a new template.
    """
    raw = node.attributes.get("class") or ""
    return tuple(
        sorted(
            c for c in raw.split() if c and not _NOISE_CLASS.match(c) and not _LAYOUT_CLASS.match(c)
        )
    )


def node_signature(node: LexborNode) -> str:
    """What makes two siblings "the same shape".

    Tag plus meaningful classes. Ids are excluded on purpose: they are unique
    by definition, so including them would make every sibling its own group.
    """
    classes = _classes(node)
    return f"{node.tag}.{'.'.join(classes)}" if classes else str(node.tag)


def selector_for(node: LexborNode) -> str:
    """A CSS selector matching this node's kind.

    Classes when there are any, tag otherwise. Not guaranteed unique - it is
    meant to match the whole group, which is the point.
    """
    classes = _classes(node)
    if classes:
        return f"{node.tag}.{'.'.join(classes)}"
    return str(node.tag)


def _text_of(node: LexborNode) -> str:
    return re.sub(r"\s+", " ", node.text(deep=True, separator=" ", strip=True)).strip()


@dataclass(frozen=True, slots=True)
class Column:
    """One field-shaped thing inside a repeated container."""

    index: int
    selector: str
    """Relative to the container. Empty when the container itself holds the
    value."""

    kind: str
    """``text``, ``href``, ``src`` or ``attr:<name>``."""

    fill_rate: float
    """Fraction of members where this produced a value. Low fill is not a bug -
    a sold-out product has no price - but it is what recipe activation is
    scored on (docs/07_RECIPE_ARCHITECTURE.md)."""

    samples: tuple[str, ...] = ()
    """Up to three values, for a human or a model deciding what to call it."""

    @property
    def looks_uniform(self) -> bool:
        """Every sample identical - a label, not data.

        "Add to cart" repeated twenty times is a button, and offering it as a
        field wastes the reviewer's attention.
        """
        return len(self.samples) > 1 and len(set(self.samples)) == 1


@dataclass(frozen=True, slots=True)
class Candidate:
    """A repeated container and the columns inside it."""

    selector: str
    count: int
    columns: tuple[Column, ...]
    score: float
    text_density: float
    depth: int
    parent_selector: str | None = None

    @property
    def usable_columns(self) -> tuple[Column, ...]:
        return tuple(c for c in self.columns if not c.looks_uniform)


@dataclass(slots=True)
class _Group:
    signature: str
    members: list[LexborNode] = field(default_factory=list)
    parent: LexborNode | None = None
    depth: int = 0


def _element_children(node: LexborNode) -> list[LexborNode]:
    return [c for c in node.iter(include_text=False) if c.tag not in _SKIP_TAGS]


def _sibling_groups(tree: LexborHTMLParser) -> list[_Group]:
    """Every set of same-shaped siblings, largest first.

    Grouping is done from each parent downward rather than by collecting nodes
    and bucketing them by their parent. selectolax hands back a *new* Python
    wrapper on every ``.parent`` access, so node identity is not usable as a
    dictionary key - doing it that way puts every sibling in its own bucket
    and finds nothing, silently.
    """
    body = tree.body
    if body is None:
        return []

    groups: list[_Group] = []

    def visit(parent: LexborNode, depth: int) -> None:
        children = _element_children(parent)
        buckets: dict[str, list[LexborNode]] = defaultdict(list)
        for child in children:
            buckets[node_signature(child)].append(child)

        for signature, members in buckets.items():
            if len(members) >= MIN_GROUP_SIZE:
                groups.append(
                    _Group(
                        signature=signature,
                        members=members,
                        parent=parent,
                        depth=depth + 1,
                    )
                )

        for child in children:
            visit(child, depth + 1)

    visit(body, 0)
    groups.sort(key=lambda g: len(g.members), reverse=True)
    return groups


def _relative_paths(member: LexborNode, limit: int = MAX_COLUMN_DEPTH) -> dict[str, LexborNode]:
    """Selector paths inside one container, keyed by path.

    Paths are built from signatures rather than positions, so ``h3 > a`` means
    the same thing in every member even when the members differ in how much
    optional markup they carry.

    The nodes are only used to enumerate paths. Values are read back through
    the selector (see ``_value_at``) rather than from the node found here,
    because the two can differ: when a class is filtered out, ``span.price``
    becomes ``span``, and ``span`` selects the *first* span in the container,
    which may be a different element entirely. Reporting a sample the recipe
    would not reproduce is worse than reporting nothing.
    """
    found: dict[str, LexborNode] = {}

    def descend(node: LexborNode, prefix: str, depth: int) -> None:
        if depth > limit:
            return
        for child in node.iter(include_text=False):
            if child.tag in _SKIP_TAGS:
                continue
            step = selector_for(child)
            path = f"{prefix} > {step}" if prefix else step
            found.setdefault(path, child)
            descend(child, path, depth + 1)

    descend(member, "", 1)
    return found


_ATTR_KINDS: tuple[tuple[str, str], ...] = (
    ("href", "href"),
    ("src", "src"),
)


def _value_at(container: LexborNode, path: str, kind: str) -> str | None:
    """What a recipe with this selector would actually get."""
    node = container.css_first(path, default=None, strict=False) if path else container
    return _column_values(node, kind) if node is not None else None


def _column_values(node: LexborNode, kind: str) -> str | None:
    if kind == "text":
        text = _text_of(node)
        return text or None
    if kind == "href":
        return node.attributes.get("href") or None
    if kind == "src":
        attrs = node.attributes
        return attrs.get("data-src") or attrs.get("src") or None
    return None


def _build_columns(members: list[LexborNode]) -> tuple[Column, ...]:
    """Turn a group of containers into the columns they share.

    A path counts as a column when it appears in at least ``MIN_COLUMN_FILL``
    of the members: anything rarer describes some of the records, not the
    listing.
    """
    total = len(members)
    per_member = [_relative_paths(m) for m in members]

    path_counts: Counter[str] = Counter()
    for paths in per_member:
        # keys(), not the mapping: Counter.update reads a mapping's *values*
        # as counts, and these values are nodes.
        path_counts.update(paths.keys())

    found: list[tuple[str, str, float, tuple[str | None, ...]]] = []

    for path, seen in path_counts.most_common():
        if seen / total < MIN_COLUMN_FILL:
            continue

        for kind in ("text", *[k for _, k in _ATTR_KINDS]):
            values = tuple(
                _column_values(paths[path], kind) if path in paths else None for paths in per_member
            )
            filled = [v for v in values if v]
            if not filled:
                continue
            fill_rate = len(filled) / total
            if fill_rate < MIN_COLUMN_FILL:
                continue
            found.append((path, kind, fill_rate, values))

    return tuple(
        Column(
            index=index,
            selector=path,
            kind=kind,
            fill_rate=round(fill_rate, 3),
            samples=tuple(v[:120] for v in values if v)[:3],
        )
        for index, (path, kind, fill_rate, values) in enumerate(
            _drop_aliases(_drop_wrappers(found))
        )
    )


def _drop_wrappers(
    found: list[tuple[str, str, float, tuple[str | None, ...]]],
) -> list[tuple[str, str, float, tuple[str | None, ...]]]:
    """Remove text columns that are just their descendants concatenated.

    ``<div class="card-body">`` reports the whole card as one value - title,
    price and button run together. It is structurally a column and
    semantically nothing, and offering it as a field is worse than useless
    because it looks plausible at a glance.

    A text column is a wrapper when a strictly deeper text column's value is a
    substring of it. Only text is checked: an ``href`` on an ancestor is a
    different fact from an ``href`` on a child, not a containing one.
    """
    texts = [(i, e) for i, e in enumerate(found) if e[1] == "text"]
    drop: set[int] = set()

    for i, (path_a, _, _, values_a) in texts:
        for j, (path_b, _, _, values_b) in texts:
            if i == j or _depth_of(path_b) <= _depth_of(path_a):
                continue
            pairs = [(a, b) for a, b in zip(values_a, values_b, strict=True) if a and b]
            if not pairs:
                continue
            if all(b in a and b != a for a, b in pairs):
                drop.add(i)
                break

    return [e for i, e in enumerate(found) if i not in drop]


def _drop_aliases(
    found: list[tuple[str, str, float, tuple[str | None, ...]]],
) -> list[tuple[str, str, float, tuple[str | None, ...]]]:
    """Collapse columns that carry the same values.

    ``<h3><a>Title</a></h3>`` yields identical text at ``h3`` and ``h3 > a``.
    Offering both doubles the work of whoever - or whatever - has to name the
    columns, for no extra information.

    The shallower path wins. It survives the inner element being restyled or
    replaced, which is the more common way a site breaks a recipe. The deeper
    path survives independently when it carries something else, such as the
    ``href`` on that same anchor.
    """
    kept: list[tuple[str, str, float, tuple[str | None, ...]]] = []
    seen: dict[tuple[str, tuple[str | None, ...]], int] = {}

    for entry in found:
        path, kind, _, values = entry
        key = (kind, values)
        previous = seen.get(key)
        if previous is None:
            seen[key] = len(kept)
            kept.append(entry)
            continue
        if _depth_of(path) < _depth_of(kept[previous][0]):
            kept[previous] = entry

    return kept


def _depth_of(path: str) -> int:
    return path.count(">")


def _text_density(members: list[LexborNode]) -> float:
    """Average words per member.

    The discriminator between a product grid and a navigation menu. Both
    repeat; only one of them carries content.
    """
    if not members:
        return 0.0
    words = sum(len(_text_of(m).split()) for m in members)
    return words / len(members)


def _inside_chrome(node: LexborNode) -> bool:
    parent = node.parent
    while parent is not None:
        if parent.tag in _CHROME_TAGS:
            return True
        parent = parent.parent
    return False


def _score(count: int, columns: tuple[Column, ...], density: float, chrome: bool) -> float:
    """Rank candidates.

    Repetition is necessary but not sufficient - the columns have to be filled
    and the members have to say something. Navigation lives in ``nav`` and
    ``footer`` and is penalised rather than excluded, because plenty of sites
    put their real listing inside a ``section`` that happens to be in an
    ``aside``.
    """
    if not columns:
        return 0.0
    mean_fill = sum(c.fill_rate for c in columns) / len(columns)
    variety = len({c.selector for c in columns})
    density_factor = min(density / 8.0, 1.5)
    score = count * mean_fill * (1 + variety * 0.15) * (0.2 + density_factor)
    if chrome:
        score *= 0.25
    return round(score, 2)


def detect_containers(
    tree: LexborHTMLParser, *, limit: int = 5, min_score: float = 1.0
) -> tuple[Candidate, ...]:
    """Repeated containers on this page, best first.

    Returns candidates rather than a single answer: choosing between "the
    product grid" and "the category sidebar" needs to know what the user
    wanted, and this function does not.
    """
    candidates: list[Candidate] = []

    for group in _sibling_groups(tree):
        members = group.members
        columns = _build_columns(members)
        if not columns:
            continue

        density = _text_density(members)
        chrome = _inside_chrome(members[0])
        score = _score(len(members), columns, density, chrome)
        if score < min_score:
            continue

        candidates.append(
            Candidate(
                selector=selector_for(members[0]),
                count=len(members),
                columns=columns,
                score=score,
                text_density=round(density, 1),
                depth=group.depth,
                parent_selector=(selector_for(group.parent) if group.parent is not None else None),
            )
        )

    candidates.sort(key=lambda c: c.score, reverse=True)
    return tuple(_dedupe(candidates)[:limit])


def _dedupe(candidates: list[Candidate]) -> list[Candidate]:
    """Drop candidates that describe the same rows at a different level.

    ``li.item`` and ``li.item > div.inner`` both repeat exactly N times and
    both look like the listing. Keeping both doubles the reviewer's work for
    no information, so the higher-scoring one wins.
    """
    kept: list[Candidate] = []
    seen_counts: dict[int, Candidate] = {}
    for candidate in candidates:
        previous = seen_counts.get(candidate.count)
        if previous is not None and _same_rows(previous, candidate):
            continue
        seen_counts[candidate.count] = candidate
        kept.append(candidate)
    return kept


def _same_rows(a: Candidate, b: Candidate) -> bool:
    if a.count != b.count:
        return False
    a_samples = {s for c in a.columns for s in c.samples}
    b_samples = {s for c in b.columns for s in c.samples}
    if not a_samples or not b_samples:
        return False
    overlap = len(a_samples & b_samples) / min(len(a_samples), len(b_samples))
    return overlap > 0.6
