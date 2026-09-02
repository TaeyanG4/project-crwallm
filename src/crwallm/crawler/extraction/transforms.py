"""Declarative field transforms.

``extract_type: text`` alone does not get you data. It gets you
``"₩1,290,000원"`` where a number belongs, ``"/product/8821"`` where a
URL belongs, and ``"1:23:45"`` where a duration belongs.

Arbitrary Python or JavaScript in a recipe is ruled out (docs/17_NON_GOALS.md):
a recipe is data, and data that executes is no longer data - it is a payload
that an LLM or a compromised recipe file can write. A fixed whitelist covers
the real cases without that.

Every transform is total: given input it does not understand, it returns
``None`` rather than raising. One bad row should cost one field, not the crawl.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import urljoin

__all__ = ["TransformError", "apply_chain", "available", "register"]


class TransformError(ValueError):
    """A recipe named a transform that does not exist."""


type TransformFn = Callable[[Any, "TransformContext"], Any]


class TransformContext:
    """What a transform needs beyond its input.

    ``base_url`` is the only member today; it exists because relative links are
    the single most common thing a field needs resolving against, and threading
    it through explicitly beats a global.
    """

    __slots__ = ("base_url",)

    def __init__(self, base_url: str | None = None) -> None:
        self.base_url = base_url


_REGISTRY: dict[str, TransformFn] = {}


def register(name: str) -> Callable[[TransformFn], TransformFn]:
    def decorator(fn: TransformFn) -> TransformFn:
        _REGISTRY[name] = fn
        return fn

    return decorator


def available() -> tuple[str, ...]:
    return tuple(sorted(_REGISTRY))


# ------------------------------------------------------------------ strings


@register("trim")
def _trim(value: Any, ctx: TransformContext) -> Any:
    return value.strip() if isinstance(value, str) else value


@register("normalize_ws")
def _normalize_ws(value: Any, ctx: TransformContext) -> Any:
    """Collapse runs of whitespace. HTML indentation is not data."""
    if not isinstance(value, str):
        return value
    return re.sub(r"\s+", " ", value).strip()


@register("lower")
def _lower(value: Any, ctx: TransformContext) -> Any:
    return value.lower() if isinstance(value, str) else value


@register("upper")
def _upper(value: Any, ctx: TransformContext) -> Any:
    return value.upper() if isinstance(value, str) else value


@register("strip_html")
def _strip_html(value: Any, ctx: TransformContext) -> Any:
    if not isinstance(value, str):
        return value
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", value)).strip()


# ------------------------------------------------------------------ numbers

_NUMERIC = re.compile(r"-?\d+(?:[.,]\d+)*")


@register("to_number")
def _to_number(value: Any, ctx: TransformContext) -> Any:
    """First number in the string.

    Handles the shapes prices actually arrive in: ``"1,290,000원"``,
    ``"₩1,290,000"``, ``"$1,299.00"``, ``"월 12,900원~"``. Thousands
    separators are dropped; a single trailing group of one to two digits after
    the last separator is treated as a decimal fraction.
    """
    if isinstance(value, int | float):
        return value
    if not isinstance(value, str):
        return None
    match = _NUMERIC.search(value)
    if not match:
        return None
    raw = match.group(0)

    # "1.234.567" and "1,234,567" are both thousands-grouped; "1,99" and
    # "1.99" are both fractional. The last separator decides.
    tail = re.search(r"[.,](\d+)$", raw)
    if tail and len(tail.group(1)) <= 2 and raw.count(tail.group(0)[0]) == 1:
        integer = re.sub(r"[.,]", "", raw[: tail.start()])
        try:
            return float(f"{integer}.{tail.group(1)}")
        except ValueError:  # pragma: no cover - regex guarantees digits
            return None

    digits = re.sub(r"[.,]", "", raw)
    try:
        return int(digits)
    except ValueError:  # pragma: no cover
        return None


@register("to_int")
def _to_int(value: Any, ctx: TransformContext) -> Any:
    number = _to_number(value, ctx)
    return int(number) if isinstance(number, int | float) else None


@register("to_float")
def _to_float(value: Any, ctx: TransformContext) -> Any:
    number = _to_number(value, ctx)
    return float(number) if isinstance(number, int | float) else None


# --------------------------------------------------------------------- urls


@register("to_absolute_url")
def _to_absolute_url(value: Any, ctx: TransformContext) -> Any:
    """Resolve against the page URL.

    Without this every ``href`` in a record is a fragment that means nothing
    once the data leaves the crawl.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    if ctx.base_url is None:
        return value
    return urljoin(ctx.base_url, value.strip())


# -------------------------------------------------------------------- times

_DURATION = re.compile(r"^(?:(\d+):)?(\d{1,2}):(\d{2})$")
"""Clock notation, which is what a rendered video listing shows."""

_ISO_DURATION = re.compile(
    r"^P(?:(?P<days>\d+)D)?"
    r"(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+(?:\.\d+)?)S)?)?$",
    re.IGNORECASE,
)
"""ISO 8601, which is what schema.org uses - ``PT3M34S``.

Added when the declared-data extractors arrived. Every ``VideoObject`` states
its length this way and nothing could turn it into a number, so "only videos
between one and thirty minutes" had no field to compare against - the exact
question the media work was for.

Years and months are not handled on purpose: they have no fixed length in
seconds, and a media duration never uses them."""
_DATE_FORMATS = (
    "%Y-%m-%d",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y/%m/%d",
    "%Y.%m.%d",
    "%d/%m/%Y",
    "%d.%m.%Y",
    "%b %d, %Y",
    "%d %b %Y",
    "%Y년 %m월 %d일",
)


@register("duration_to_seconds")
def _duration_to_seconds(value: Any, ctx: TransformContext) -> Any:
    """Seconds, from either notation a video length arrives in.

    ``"1:23:45"`` and ``"12:34"`` come off a rendered page; ``"PT3M34S"``
    comes from JSON-LD and microdata. A recipe should not have to know which
    of the two a given site chose, and on a page carrying both they mean the
    same thing.
    """
    if isinstance(value, int | float):
        return int(value)
    if not isinstance(value, str):
        return None

    text = value.strip()
    match = _DURATION.match(text)
    if match:
        hours, minutes, seconds = match.groups()
        return int(hours or 0) * 3600 + int(minutes) * 60 + int(seconds)

    iso = _ISO_DURATION.match(text)
    if iso is None or not any(iso.groupdict().values()):
        # A bare "P" or "PT" matches the pattern and means nothing.
        return None
    parts = iso.groupdict()
    return int(
        int(parts["days"] or 0) * 86400
        + int(parts["hours"] or 0) * 3600
        + int(parts["minutes"] or 0) * 60
        + float(parts["seconds"] or 0)
    )


@register("parse_date")
def _parse_date(value: Any, ctx: TransformContext) -> Any:
    """ISO 8601 string, or ``None``.

    Returned as text rather than a ``datetime`` because records are JSONB and
    a round-trip through the database must not change the value.
    """
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).isoformat()
    except ValueError:
        pass
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=UTC).isoformat()
        except ValueError:
            continue
    return None


# ------------------------------------------------------------- parametrised


def _parse_args(name: str) -> tuple[str, tuple[str, ...]]:
    """``regex_extract(\\d+, 0)`` -> ``("regex_extract", ("\\d+", "0"))``."""
    if "(" not in name or not name.rstrip().endswith(")"):
        return name.strip(), ()
    head, _, tail = name.partition("(")
    body = tail.rstrip()[:-1]
    args = tuple(a.strip().strip("\"'") for a in body.split(",")) if body else ()
    return head.strip(), args


def _regex_extract(value: Any, args: tuple[str, ...]) -> Any:
    if not isinstance(value, str) or not args:
        return None
    try:
        match = re.search(args[0], value)
    except re.error:
        return None
    if not match:
        return None
    group = int(args[1]) if len(args) > 1 and args[1].isdigit() else 0
    try:
        return match.group(group)
    except (IndexError, re.error):
        return None


def _split(value: Any, args: tuple[str, ...]) -> Any:
    if not isinstance(value, str) or not args:
        return None
    parts = value.split(args[0])
    index = int(args[1]) if len(args) > 1 and args[1].lstrip("-").isdigit() else 0
    try:
        return parts[index]
    except IndexError:
        return None


def _default(value: Any, args: tuple[str, ...]) -> Any:
    if value in (None, "", [], {}):
        return args[0] if args else None
    return value


_PARAMETRISED: dict[str, Callable[[Any, tuple[str, ...]], Any]] = {
    "regex_extract": _regex_extract,
    "split": _split,
    "default": _default,
}


# ---------------------------------------------------------------- the chain


def apply_chain(
    value: Any, chain: tuple[str, ...] | list[str], *, base_url: str | None = None
) -> Any:
    """Run ``chain`` left to right.

    Once a transform yields ``None`` the rest are skipped: the value is already
    unusable and running ``to_absolute_url`` on nothing only obscures which
    step actually failed. ``default(...)`` is the exception - it exists to
    recover from exactly that.
    """
    ctx = TransformContext(base_url=base_url)
    result = value
    for step in chain:
        name, args = _parse_args(step)

        if name in _PARAMETRISED:
            result = _PARAMETRISED[name](result, args)
            continue

        if result is None:
            continue

        fn = _REGISTRY.get(name)
        if fn is None:
            raise TransformError(
                f"unknown transform {name!r} - available: {', '.join(available())}, "
                f"{', '.join(sorted(_PARAMETRISED))}"
            )
        result = fn(result, ctx)
    return result


def validate_chain(chain: tuple[str, ...] | list[str]) -> None:
    """Reject unknown transforms up front.

    Called when a recipe is loaded so a typo fails at ``recipe test`` rather
    than three hundred pages into a crawl.
    """
    for step in chain:
        name, _ = _parse_args(step)
        if name not in _REGISTRY and name not in _PARAMETRISED:
            raise TransformError(
                f"unknown transform {name!r} - available: {', '.join(available())}, "
                f"{', '.join(sorted(_PARAMETRISED))}"
            )
