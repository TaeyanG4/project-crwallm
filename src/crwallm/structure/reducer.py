"""Shrinking a page to something a model can read.

A real listing page is 100-500KB of HTML. A local model's usable context is a
few thousand tokens, and - this is the part that bites - exceeding it does not
raise. Ollama silently truncates, the model reasons about half a document, and
the selectors it returns are confidently wrong (docs/08_LLM_ARCHITECTURE.md).

So reduction is not a nicety, it is the difference between the model seeing
the page and seeing a fragment of it. Target: 2-4k tokens for any input.

**Reduction beats model size.** A well-reduced 2k skeleton handed to an 8B
model beats 8k of raw markup handed to a 14B one. Every token spent on
``<div class="col-md-6 px-3">`` is a token not spent on the structure that
matters.

The output is not HTML. It is a skeleton meant to be read once and thrown
away, so it optimises for information per token rather than for being
re-parseable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from selectolax.lexbor import LexborHTMLParser, LexborNode

from crwallm.structure.detector import (
    MIN_GROUP_SIZE,
    _element_children,
    node_signature,
    selector_for,
)

__all__ = ["ReducedDom", "estimate_tokens", "reduce_dom"]

_DROP_TAGS = frozenset(
    {
        "script",
        "style",
        "noscript",
        "template",
        "svg",
        "path",
        "canvas",
        "iframe",
        "object",
        "embed",
        "br",
        "hr",
        "link",
        "meta",
        "source",
        "track",
        "param",
        "picture",
    }
)

_KEEP_ATTRS = ("class", "id", "href", "src", "data-src", "itemprop", "role", "type")
"""Attributes that identify or address something.

Inline styles, event handlers, ARIA labels and framework bookkeeping are all
noise for the question being asked, and together they are most of the bytes.
"""

_TEXT_CLIP = 80
_MAX_SAMPLES = 2
_MAX_ATTR_LEN = 60

_WS = re.compile(r"\s+")


def estimate_tokens(text: str) -> int:
    """Rough token count.

    Deliberately approximate and deliberately cheap: this runs on every
    reduction to decide whether to shrink further, and loading a tokeniser to
    answer "is this too big" would cost more than the answer is worth. Roughly
    four characters per token for markup-like text, and CJK closer to one and
    a half - a Korean page would otherwise be badly underestimated.
    """
    if not text:
        return 0
    cjk = sum(1 for ch in text if "　" <= ch <= "鿿" or "가" <= ch <= "힣")
    ascii_like = len(text) - cjk
    return int(ascii_like / 4 + cjk / 1.5) + 1


@dataclass(frozen=True, slots=True)
class ReducedDom:
    skeleton: str
    tokens: int
    original_tokens: int
    truncated: bool
    """True when the budget forced structure to be dropped, not just
    compressed. The caller should know it is reasoning about a partial page."""

    @property
    def ratio(self) -> float:
        return round(self.tokens / self.original_tokens, 4) if self.original_tokens else 0.0


def _clean_text(node: LexborNode) -> str:
    own = "".join(
        child.text_content or "" for child in node.iter(include_text=True) if child.tag == "-text"
    )
    return _WS.sub(" ", own).strip()


def _attrs_of(node: LexborNode) -> str:
    parts: list[str] = []
    attrs = node.attributes
    for name in _KEEP_ATTRS:
        value = attrs.get(name)
        if not value:
            continue
        value = _WS.sub(" ", value).strip()
        if len(value) > _MAX_ATTR_LEN:
            value = value[:_MAX_ATTR_LEN] + "..."
        parts.append(f'{name}="{value}"')
    return " " + " ".join(parts) if parts else ""


def _render(node: LexborNode, depth: int, out: list[str], budget: _Budget) -> None:
    if budget.spent:
        return
    if node.tag in _DROP_TAGS:
        return

    indent = "  " * depth
    text = _clean_text(node)
    text_part = f"  {text[:_TEXT_CLIP]}" if text else ""
    children = _element_children(node)

    if not children:
        budget.emit(out, f"{indent}<{node.tag}{_attrs_of(node)}>{text_part}")
        return

    # Collapse repeated siblings. This is the single biggest saving on a
    # listing page: twenty identical product cards become one card and a
    # count, which is all the structure question needs.
    groups: dict[str, list[LexborNode]] = {}
    for child in children:
        groups.setdefault(node_signature(child), []).append(child)

    budget.emit(out, f"{indent}<{node.tag}{_attrs_of(node)}>{text_part}")

    for signature, members in groups.items():
        if len(members) >= MIN_GROUP_SIZE:
            budget.emit(
                out,
                f"{indent}  [x{len(members)}] {selector_for(members[0])}",
            )
            for member in members[:_MAX_SAMPLES]:
                _render(member, depth + 2, out, budget)
            del signature
            continue
        for member in members:
            _render(member, depth + 1, out, budget)


class _Budget:
    """Stops rendering once the skeleton is big enough.

    A hard stop rather than a post-hoc trim: truncating the output text would
    cut mid-structure and leave the model with a malformed tail, which reads
    as broken markup rather than as "there was more".
    """

    def __init__(self, max_tokens: int) -> None:
        self._max = max_tokens
        self._used = 0
        self.spent = False

    def emit(self, out: list[str], line: str) -> None:
        if self.spent:
            return
        cost = estimate_tokens(line)
        if self._used + cost > self._max:
            self.spent = True
            out.append("... (truncated: page exceeded the reduction budget)")
            return
        self._used += cost
        out.append(line)


DEFAULT_MAX_TOKENS = 3000


def reduce_dom(tree: LexborHTMLParser, *, max_tokens: int = DEFAULT_MAX_TOKENS) -> ReducedDom:
    """Reduce ``tree`` to a skeleton within ``max_tokens``."""
    body = tree.body
    original = estimate_tokens(tree.html or "")

    if body is None:
        return ReducedDom("", 0, original, truncated=False)

    out: list[str] = []
    budget = _Budget(max_tokens)

    title = tree.css_first("title", default=None, strict=False)
    if title is not None:
        budget.emit(out, f"<title>  {title.text(strip=True)[:_TEXT_CLIP]}")

    _render(body, 0, out, budget)

    skeleton = "\n".join(out)
    return ReducedDom(
        skeleton=skeleton,
        tokens=estimate_tokens(skeleton),
        original_tokens=original,
        truncated=budget.spent,
    )
