"""Shared enums.

These are persisted (in ``crawl_events`` and ``crawl_results``) and returned
over the API, so treat them as append-only: adding a member is fine, renaming
or removing one is a migration.
"""

from __future__ import annotations

from enum import StrEnum


class FetchMode(StrEnum):
    HTTP = "http"
    BROWSER = "browser"

    AUTO = "auto"
    """Try HTTP, fall back to the browser when extraction yields nothing.

    Result-driven rather than a DOM heuristic - docs/04_CRAWLING_ARCHITECTURE.md.
    """


class CrawlMode(StrEnum):
    COLLECT = "collect"
    """Targeted extraction from known list pages."""

    SPIDER = "spider"
    """Broad traversal from seeds."""


class ErrorKind(StrEnum):
    """Failure taxonomy.

    Without this, "400 of 1000 pages failed" tells you nothing. With it,
    "380 of the 400 were blocked_429" tells you to lower concurrency.
    docs/09_JOB_ARCHITECTURE.md
    """

    # network
    DNS_FAIL = "dns_fail"
    CONN_REFUSED = "conn_refused"
    CONN_TIMEOUT = "conn_timeout"
    READ_TIMEOUT = "read_timeout"
    TLS_ERROR = "tls_error"

    # http
    HTTP_4XX = "http_4xx"
    HTTP_5XX = "http_5xx"
    BLOCKED_403 = "blocked_403"
    BLOCKED_429 = "blocked_429"
    CAPTCHA_DETECTED = "captcha_detected"

    # redirects
    REDIRECT_MAX = "redirect_max"
    REDIRECT_LOOP = "redirect_loop"

    # content
    SIZE_EXCEEDED = "size_exceeded"
    CONTENT_TYPE_REJECTED = "content_type_rejected"
    PARSE_FAIL = "parse_fail"
    EXTRACT_EMPTY = "extract_empty"

    # policy
    POLICY_REJECT = "policy_reject"
    SSRF_REJECT = "ssrf_reject"
    SCOPE_REJECT = "scope_reject"
    TRAP_REJECT = "trap_reject"

    # other
    DUPLICATE = "duplicate"
    CANCELLED = "cancelled"
    CONFIG = "config"
    """The job as submitted cannot run - a missing recipe, a version that no
    longer matches, a scope the recipe does not cover.

    Separate from ``INTERNAL`` because the operator caused it and can fix it,
    where an internal error is ours."""
    INTERNAL = "internal"


class RejectReason(StrEnum):
    """Why a discovered URL never became a fetch.

    Finer-grained than ``ErrorKind`` because tuning a spider means knowing
    which guard is doing the rejecting.
    """

    SCHEME = "scheme"
    SSRF = "ssrf"
    SCOPE = "scope"
    DUPLICATE = "duplicate"
    DEPTH = "depth"
    MAX_PAGES = "max_pages"
    URL_LENGTH = "url_length"
    PATH_DEPTH = "path_depth"
    REPEATED_SEGMENT = "repeated_segment"
    QUERY_PARAMS = "query_params"
    PATTERN_BUDGET = "pattern_budget"
    URL_FILTER = "url_filter"
    MALFORMED = "malformed"

    SOFT_404 = "soft_404"
    """A 200 response that means "not found".

    Added in Phase 5. The enum is append-only by design (Phase 1) because
    these values are persisted; a new member is safe, a renamed one is a
    migration.
    """

    CONTENT_DUPLICATE = "content_duplicate"
    """Same content, different URL - what URL dedupe cannot see."""
