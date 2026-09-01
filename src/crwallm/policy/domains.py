"""Domain scoping against the Public Suffix List.

``allowed_domains`` bounds the crawl, so the values in it have to be real
registrable domains. ``"com"`` or ``"co.kr"`` would scope a crawl to the entire
TLD - a spider pointed at the whole internet by a typo, or by a model that
guessed. docs/11_SECURITY_MODEL.md section 3

``tldextract`` is configured to use its bundled snapshot and never fetch: a
crawler that phones home for a suffix list during startup is both a surprise
and a failure mode.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from functools import lru_cache

import tldextract

__all__ = [
    "DomainScope",
    "InvalidDomainError",
    "registrable_domain",
    "validate_allowed_domains",
]


class InvalidDomainError(ValueError):
    """A domain is not usable as a crawl scope."""


@lru_cache(maxsize=1)
def _extractor() -> tldextract.TLDExtract:
    return tldextract.TLDExtract(
        # Pin to the snapshot shipped with the package. A crawler that phones
        # home for a suffix list at startup is a surprise and a failure mode.
        suffix_list_urls=(),
        fallback_to_snapshot=True,
        # Private suffixes on: github.io, blogspot.com, s3.amazonaws.com and
        # friends become suffixes, so ``user.github.io`` is the registrable
        # unit. That is what a crawl scope means here - one site - and it stops
        # ``allowed_domains=["github.io"]`` from scoping a crawl to every page
        # anyone ever published there.
        include_psl_private_domains=True,
    )


def registrable_domain(host: str) -> str | None:
    """eTLD+1 for ``host``, or ``None`` when there isn't one.

    ``None`` means the host is a bare public suffix ("com", "co.uk"), an IP
    literal, or otherwise not a registrable name.
    """
    cleaned = host.strip().lower().rstrip(".")
    if not cleaned:
        return None
    try:
        ipaddress.ip_address(cleaned.strip("[]"))
    except ValueError:
        pass
    else:
        return None  # IP literals have no registrable domain

    result = _extractor()(cleaned)
    if not result.domain or not result.suffix:
        return None
    return f"{result.domain}.{result.suffix}"


def validate_allowed_domains(domains: tuple[str, ...]) -> tuple[str, ...]:
    """Second gate for ``CrawlSpec.allowed_domains``.

    Pydantic normalised the strings; this rejects the ones that would blow the
    scope open. Separate from the model on purpose - the schema layer stays
    free of the PSL and of anything that needs data files.
    """
    if not domains:
        raise InvalidDomainError("allowed_domains is empty - that is an unbounded crawl")

    out: list[str] = []
    for domain in domains:
        if not domain:
            raise InvalidDomainError("empty domain")
        if "/" in domain or ":" in domain.rstrip("]").lstrip("["):
            raise InvalidDomainError(f"{domain!r} looks like a URL, not a domain")

        registrable = registrable_domain(domain)
        if registrable is None:
            raise InvalidDomainError(
                f"{domain!r} is not a registrable domain - a bare public suffix "
                "would scope the crawl to an entire TLD"
            )
        out.append(domain)
    return tuple(out)


@dataclass(frozen=True, slots=True)
class DomainScope:
    """Decides whether a host is inside the crawl.

    A host matches when it equals an allowed domain or is a subdomain of one.
    Suffix comparison alone is wrong: ``evil-example.com`` ends with
    ``example.com``.
    """

    domains: frozenset[str]

    @classmethod
    def from_spec(cls, allowed_domains: tuple[str, ...]) -> DomainScope:
        return cls(frozenset(validate_allowed_domains(allowed_domains)))

    def contains(self, host: str) -> bool:
        candidate = host.strip().lower().rstrip(".")
        if not candidate:
            return False
        for domain in self.domains:
            if candidate == domain or candidate.endswith("." + domain):
                return True
        return False

    def intersect(self, other: tuple[str, ...]) -> DomainScope:
        """Narrow this scope with another set.

        Used when a spec references a recipe: the result is the intersection,
        never the union, so reuse cannot widen what the recipe was validated
        against. docs/07_RECIPE_ARCHITECTURE.md
        """
        other_scope = DomainScope.from_spec(other)
        kept = {
            d
            for d in self.domains | other_scope.domains
            if self.contains(d) and other_scope.contains(d)
        }
        if not kept:
            raise InvalidDomainError(
                f"domain scopes do not overlap: {sorted(self.domains)} "
                f"vs {sorted(other_scope.domains)}"
            )
        return DomainScope(frozenset(kept))
