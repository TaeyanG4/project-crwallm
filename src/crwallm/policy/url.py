"""URL normalisation.

Bad normalisation is the classic way a spider ends up crawling forever, so
this module produces **two** values for every URL and never conflates them:

``url``
    The thing we actually fetch. Normalised only in ways that cannot change
    what the server returns: case of scheme and host, default ports, dot
    segments, the fragment, percent-encoding.

``dedupe_key``
    The identity used for "have we seen this?". Normalised aggressively -
    tracking parameters dropped, remaining parameters filtered and sorted.
    Never fetched.

Collapsing the two breaks one way or the other. Dedupe aggressively with a
single value and you fetch ``/list?sort=price`` as ``/list`` and get the wrong
page; keep it conservative and ``?utm_source=`` variants multiply until the
budget is gone.

``url_pattern`` is a third, coarser projection used only for trap budgets.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, quote, unquote, urlsplit, urlunsplit

__all__ = [
    "TRACKING_PARAMS",
    "NormalizedUrl",
    "join_url",
    "normalize",
    "url_pattern",
]

DEFAULT_PORTS = {"http": "80", "https": "443"}

TRACKING_PARAMS = frozenset(
    {
        # analytics
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "utm_id",
        "utm_name",
        "utm_source_platform",
        "_ga",
        "_gl",
        "_gid",
        "ga_source",
        "ga_medium",
        # ad networks / referrers
        "fbclid",
        "gclid",
        "gclsrc",
        "dclid",
        "msclkid",
        "twclid",
        "igshid",
        "mc_cid",
        "mc_eid",
        "yclid",
        "wbraid",
        "gbraid",
        # generic referral noise
        "ref",
        "referer",
        "referrer",
        "source",
        "src",
        "spm",
        "scm",
        "from",
        "trk",
        "trk_params",
        # korean portals / commerce
        "n_media",
        "n_query",
        "n_rank",
        "n_ad_group",
        "n_ad",
        "n_keyword",
        "NaPm",
        "nl_query",
        "cm_id",
        "acode",
    }
)
"""Dropped from the dedupe key. Also stripped from the fetch URL: these are
inert for content and keeping them multiplies the frontier."""

_SESSIONISH = re.compile(
    r"^(?:jsessionid|phpsessid|aspsessionid[a-z]*|sid|session_?id|zenid|osCsid)$",
    re.IGNORECASE,
)

# Unreserved per RFC 3986 - safe to leave decoded, and decoding them makes two
# spellings of the same URL compare equal.
_UNRESERVED_SAFE = "-._~"
_PATH_SAFE = "/-._~!$&'()*+,;=:@"
_QUERY_SAFE = "-._~!$'()*+,;=:@/?"

_STRIP_WHITESPACE_CONTROLS = str.maketrans("", "", "\t\r\n")

_HEX_ESCAPE = re.compile(r"%([0-9a-fA-F]{2})")
_NUMERIC_SEGMENT = re.compile(r"^\d+$")
_UUID_SEGMENT = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)
_DATE_SEGMENT = re.compile(r"^\d{4}-\d{2}(?:-\d{2})?$")
_OPAQUE_SEGMENT = re.compile(r"^(?=.*\d)[A-Za-z0-9_-]+$")
_OPAQUE_MIN_LEN = 8
"""Above this length, a segment containing digits is an identifier rather than
a meaningful path component. Collapsing them all to one placeholder matters:
a site that mints ``/s/<session>/page`` per request would otherwise create a
fresh pattern - and a fresh budget - for every single request."""


@dataclass(frozen=True, slots=True)
class NormalizedUrl:
    """The two forms plus the pieces callers keep asking for."""

    url: str
    """Fetch this."""

    dedupe_key: str
    """Compare this. Never fetch it."""

    scheme: str
    host: str
    port: int | None
    path: str

    @property
    def host_port(self) -> str:
        return f"{self.host}:{self.port}" if self.port else self.host


class UrlNormalizationError(ValueError):
    """The input is not a URL we are willing to touch."""


def _normalize_percent_encoding(value: str, safe: str) -> str:
    """Decode unreserved escapes, re-encode everything else consistently.

    ``%7Eb`` and ``~b`` are the same resource; ``%7e`` and ``%7E`` are the same
    escape. Without this, all three enter the frontier separately.
    """

    def _decode_unreserved(m: re.Match[str]) -> str:
        char = chr(int(m.group(1), 16))
        if (char.isalnum() and char.isascii()) or char in _UNRESERVED_SAFE:
            return char
        return "%" + m.group(1).upper()

    decoded = _HEX_ESCAPE.sub(_decode_unreserved, value)
    # Re-encode anything that must not travel raw, leaving existing escapes
    # alone (quote does not touch '%' when it is in ``safe``).
    return quote(decoded, safe=safe + "%")


def _resolve_dot_segments(path: str) -> str:
    """RFC 3986 section 5.2.4, plus collapsing of empty segments.

    ``/a/./b/../c`` and ``/a//c`` both denote ``/a/c`` on essentially every
    server, and treating them as distinct is how ``/a/b/a/b/...`` traps grow.
    """
    if not path:
        return "/"
    trailing_slash = path.endswith("/")
    out: list[str] = []
    for segment in path.split("/"):
        if segment in ("", "."):
            continue
        if segment == "..":
            if out:
                out.pop()
            continue
        out.append(segment)
    resolved = "/" + "/".join(out)
    if trailing_slash and len(resolved) > 1:
        resolved += "/"
    return resolved


def _clean_query(
    query: str,
    *,
    whitelist: frozenset[str] | None,
    drop_tracking: bool,
    sort: bool,
) -> str:
    if not query:
        return ""
    pairs = parse_qsl(query, keep_blank_values=True)
    kept: list[tuple[str, str]] = []
    for key, value in pairs:
        if drop_tracking and (key in TRACKING_PARAMS or _SESSIONISH.match(key)):
            continue
        if whitelist is not None and key not in whitelist:
            continue
        kept.append((key, value))
    if sort:
        kept.sort()

    def _render(key: str, value: str) -> str:
        encoded_key = quote(key, safe=_QUERY_SAFE)
        if not value:
            return encoded_key
        return f"{encoded_key}={quote(value, safe=_QUERY_SAFE)}"

    return "&".join(_render(k, v) for k, v in kept)


def _idna(host: str) -> str:
    """Punycode non-ASCII hosts so one site is not two frontier entries."""
    if host.isascii():
        return host
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError:
        # Leave it; the SSRF gate will reject it if it is unresolvable.
        return host


def normalize(
    raw: str,
    *,
    base: str | None = None,
    dedupe_whitelist: frozenset[str] | None = None,
) -> NormalizedUrl:
    """Normalise ``raw``, resolving against ``base`` when it is relative.

    Raises ``UrlNormalizationError`` for anything that is not http(s) or is
    too malformed to reason about. The scheme check here is a convenience,
    not the security boundary - that is ``crwallm.policy.ssrf``.
    """
    candidate = raw.strip()
    if not candidate:
        raise UrlNormalizationError("empty URL")

    # Tab, CR and LF are removed rather than rejected, matching what browsers
    # do, because HTML wraps long hrefs across lines and those links are real:
    #
    #     <a href="/some/very/long/
    #              path">
    #
    # A crafted href such as "/ok\r\nX-Injected: 1" therefore collapses into a
    # single nonsense path rather than a smuggled header. That is the property
    # that matters - no CR or LF survives into the request - and it costs one
    # 404 in the rare case somebody bothers to try it.
    candidate = candidate.translate(_STRIP_WHITESPACE_CONTROLS).strip()

    # Everything else in C0/C1 is refused outright. No markup produces those
    # by accident, so their presence means the URL was constructed to be
    # misparsed by something downstream.
    if any(ch.isascii() and not ch.isprintable() and ch != " " for ch in candidate):
        raise UrlNormalizationError("URL contains control characters")

    if not candidate:
        raise UrlNormalizationError("empty URL after stripping whitespace")

    if base is not None:
        candidate = join_url(base, candidate)

    parts = urlsplit(candidate)

    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise UrlNormalizationError(f"unsupported scheme {parts.scheme!r}")

    host = _idna((parts.hostname or "").lower())
    if not host:
        raise UrlNormalizationError("missing host")

    try:
        port = parts.port
    except ValueError as exc:
        raise UrlNormalizationError(f"invalid port: {exc}") from exc
    if port is not None and str(port) == DEFAULT_PORTS.get(scheme):
        port = None

    netloc = f"[{host}]" if ":" in host else host
    if port is not None:
        netloc = f"{netloc}:{port}"

    path = _resolve_dot_segments(_normalize_percent_encoding(parts.path, _PATH_SAFE))

    # Fetch form: tracking params are dropped here too. They never change the
    # response, and leaving them in multiplies frontier entries for one page.
    fetch_query = _clean_query(parts.query, whitelist=None, drop_tracking=True, sort=False)
    # Dedupe form: additionally filtered to the whitelist and sorted, so that
    # ?b=2&a=1 and ?a=1&b=2 are one entry.
    dedupe_query = _clean_query(
        parts.query, whitelist=dedupe_whitelist, drop_tracking=True, sort=True
    )

    url = urlunsplit((scheme, netloc, path, fetch_query, ""))
    dedupe_path = path.rstrip("/") or "/"
    dedupe_key = urlunsplit((scheme, netloc, dedupe_path, dedupe_query, ""))

    return NormalizedUrl(
        url=url,
        dedupe_key=dedupe_key,
        scheme=scheme,
        host=host,
        port=port,
        path=path,
    )


def join_url(base: str, href: str) -> str:
    """Resolve ``href`` against ``base``.

    ``urljoin`` with one guard: a scheme-relative ``//host/path`` inherits the
    base scheme, which ``urljoin`` already does correctly, but an empty href
    must not silently become the base.
    """
    from urllib.parse import urljoin

    if not href.strip():
        raise UrlNormalizationError("empty href")
    return urljoin(base, href.strip())


def url_pattern(normalized: NormalizedUrl) -> str:
    """Coarse shape of a URL, for per-pattern budgets.

    ``/product/8821`` and ``/product/9134`` share a pattern and therefore a
    budget; ``/calendar/2031/07`` collapses to ``/calendar/{n}/{n}`` so an
    infinite calendar exhausts twenty slots instead of the whole crawl.
    docs/05_SPIDER_ARCHITECTURE.md
    """
    segments = []
    for segment in normalized.path.split("/"):
        if not segment:
            continue
        segments.append(_segment_placeholder(segment))
    path = "/" + "/".join(segments)

    query_keys = sorted(
        k for k, _ in parse_qsl(urlsplit(normalized.dedupe_key).query, keep_blank_values=True)
    )
    suffix = "?" + "&".join(f"{k}={{v}}" for k in query_keys) if query_keys else ""
    return f"{normalized.host}{path}{suffix}"


def _segment_placeholder(segment: str) -> str:
    """Order is load-bearing.

    Long identifiers must collapse to a *single* placeholder whatever they are
    spelled with, or a session id that happens to be all digits gets a
    different budget from one containing a letter - and the trap survives.
    """
    if _UUID_SEGMENT.match(segment):
        return "{uuid}"
    if _DATE_SEGMENT.match(segment):
        return "{date}"
    if len(segment) >= _OPAQUE_MIN_LEN and _OPAQUE_SEGMENT.match(segment):
        return "{id}"
    if _NUMERIC_SEGMENT.match(segment):
        return "{n}"
    return unquote(segment)
