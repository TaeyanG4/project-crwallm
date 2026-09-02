"""Filling a frontier from sitemaps before crawling starts.

Link discovery works, but it pays a fetch and a parse for every page it has to
walk through to find the next one. A sitemap index hands over the same
addresses for the cost of a handful of requests
(docs/05_SPIDER_ARCHITECTURE.md).

Best-effort by construction. Every step - robots.txt, the conventional paths,
each nested index - can fail, and each failure just means the crawl falls back
to following links. A site without a sitemap is normal, not an error.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from crwallm.crawler.contracts import Fetcher, FetchFailure, FetchRequest
from crwallm.crawler.discovery.sitemap import (
    MAX_INDEX_DEPTH,
    SitemapEntry,
    candidate_sitemap_urls,
    parse_robots_txt,
    parse_sitemap,
)
from crwallm.policy.url import UrlNormalizationError, normalize
from crwallm.schemas.types import FetchMode

__all__ = ["SeedingReport", "discover_sitemaps", "seed_from_sitemaps"]

SITEMAP_BYTE_LIMIT = 50 * 1024 * 1024
"""Sitemaps are large by design - fifty thousand URLs of text - so the normal
page limit would reject legitimate ones."""

SITEMAP_TIMEOUT_S = 30.0


@dataclass(slots=True)
class SeedingReport:
    """What seeding found, and what it cost.

    Reported rather than logged: "the sitemap gave us 8,000 URLs in 6 fetches"
    versus "we found nothing, falling back to links" changes what the crawl is
    about to do, and the operator should be able to see which happened.
    """

    entries: list[SitemapEntry] = field(default_factory=list)
    documents_fetched: int = 0
    sources: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    elapsed_ms: int = 0

    @property
    def found_any(self) -> bool:
        return bool(self.entries)

    def summary(self) -> str:
        if not self.entries:
            return (
                f"no sitemap found in {self.documents_fetched} request(s) "
                "- falling back to link discovery"
            )
        return (
            f"{len(self.entries)} URLs from {self.documents_fetched} sitemap "
            f"document(s) in {self.elapsed_ms / 1000:.1f}s"
        )


async def _fetch_text(fetcher: Fetcher, url: str) -> bytes | None:
    try:
        normalized = normalize(url)
    except UrlNormalizationError:
        return None

    outcome = await fetcher.fetch(
        FetchRequest(
            url=normalized,
            depth=0,
            mode=FetchMode.HTTP,
            timeout_s=SITEMAP_TIMEOUT_S,
            byte_limit=SITEMAP_BYTE_LIMIT,
        )
    )
    if isinstance(outcome, FetchFailure):
        return None
    return outcome.body


async def discover_sitemaps(fetcher: Fetcher, base_url: str) -> tuple[list[str], list[str]]:
    """Where this site's sitemaps are. Returns ``(urls, notes)``.

    robots.txt first because it is authoritative - a site that puts its
    sitemap somewhere unconventional says so there, and guessing paths would
    never find it. The conventional paths are the fallback, tried only when
    robots.txt named nothing.
    """
    notes: list[str] = []

    try:
        root = normalize(base_url)
    except UrlNormalizationError:
        return [], ["seed URL could not be normalised"]
    origin = f"{root.scheme}://{root.host_port}"

    robots = await _fetch_text(fetcher, f"{origin}/robots.txt")
    if robots is not None:
        declared = parse_robots_txt(robots.decode("utf-8", "replace"), base_url=origin)
        if declared:
            notes.append(f"robots.txt named {len(declared)} sitemap(s)")
            return list(declared), notes
        notes.append("robots.txt has no Sitemap directive")
    else:
        notes.append("no robots.txt")

    return list(candidate_sitemap_urls(base_url)), notes


async def seed_from_sitemaps(
    fetcher: Fetcher,
    base_url: str,
    *,
    max_entries: int = 100_000,
    max_documents: int = 50,
    dedupe_whitelist: frozenset[str] | None = None,
) -> SeedingReport:
    """Walk this site's sitemaps and collect every URL they list.

    Indexes are followed breadth-first to a bounded depth, and both the
    document count and the entry count are capped - a sitemap index can point
    at a thousand shards, and reading all of them before the first fetch would
    trade one problem for another.
    """
    started = time.perf_counter()
    report = SeedingReport()

    discovered, notes = await discover_sitemaps(fetcher, base_url)
    report.errors.extend(notes)
    report.documents_fetched += 1  # the robots.txt attempt

    queue: list[tuple[str, int]] = [(url, 0) for url in discovered]
    seen: set[str] = set(discovered)

    while queue and report.documents_fetched < max_documents:
        url, depth = queue.pop(0)

        body = await _fetch_text(fetcher, url)
        report.documents_fetched += 1
        if body is None:
            continue

        parsed = parse_sitemap(body, base_url=url, dedupe_whitelist=dedupe_whitelist)
        if parsed.error:
            report.errors.append(f"{url}: {parsed.error}")
            continue

        report.sources.append(url)

        if parsed.entries:
            remaining = max_entries - len(report.entries)
            report.entries.extend(parsed.entries[:remaining])
            if len(report.entries) >= max_entries:
                report.errors.append(f"stopped at {max_entries} URLs")
                break

        if depth < MAX_INDEX_DEPTH:
            for nested in parsed.nested:
                if nested not in seen:
                    seen.add(nested)
                    queue.append((nested, depth + 1))

    report.elapsed_ms = int((time.perf_counter() - started) * 1000)
    return report
