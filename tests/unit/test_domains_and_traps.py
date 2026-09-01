"""Domain scoping and crawler-trap guards.

docs/11_SECURITY_MODEL.md section 3, docs/05_SPIDER_ARCHITECTURE.md section 1
"""

from __future__ import annotations

import pytest

from crwallm.policy.domains import (
    DomainScope,
    InvalidDomainError,
    registrable_domain,
    validate_allowed_domains,
)
from crwallm.policy.traps import PatternBudget, TrapGuard
from crwallm.policy.url import normalize
from crwallm.schemas.spec import SpiderConfig
from crwallm.schemas.types import RejectReason


class TestRegistrableDomain:
    @pytest.mark.parametrize(
        ("host", "expected"),
        [
            ("example.com", "example.com"),
            ("www.example.com", "example.com"),
            ("a.b.c.example.com", "example.com"),
            ("shop.example.co.uk", "example.co.uk"),
            ("blog.example.co.kr", "example.co.kr"),
            ("x.github.io", "x.github.io"),  # private suffix
        ],
    )
    def test_extracts_etld_plus_one(self, host: str, expected: str) -> None:
        assert registrable_domain(host) == expected

    @pytest.mark.parametrize("host", ["com", "co.uk", "co.kr", "kr", "github.io", ""])
    def test_bare_public_suffix_has_none(self, host: str) -> None:
        assert registrable_domain(host) is None

    @pytest.mark.parametrize("host", ["1.2.3.4", "127.0.0.1", "[::1]"])
    def test_ip_literals_have_none(self, host: str) -> None:
        assert registrable_domain(host) is None


class TestValidateAllowedDomains:
    def test_real_domains_pass(self) -> None:
        assert validate_allowed_domains(("example.com", "shop.example.co.kr"))

    @pytest.mark.parametrize("bad", ["com", "co.uk", "kr", "co.kr"])
    def test_bare_suffix_is_refused(self, bad: str) -> None:
        """Otherwise a typo scopes the crawl to an entire TLD."""
        with pytest.raises(InvalidDomainError, match="registrable"):
            validate_allowed_domains((bad,))

    def test_empty_scope_is_refused(self) -> None:
        with pytest.raises(InvalidDomainError):
            validate_allowed_domains(())

    @pytest.mark.parametrize("bad", ["https://example.com", "example.com/path"])
    def test_urls_are_refused(self, bad: str) -> None:
        with pytest.raises(InvalidDomainError, match="URL"):
            validate_allowed_domains((bad,))


class TestDomainScope:
    @pytest.fixture
    def scope(self) -> DomainScope:
        return DomainScope.from_spec(("example.com",))

    @pytest.mark.parametrize(
        "host", ["example.com", "www.example.com", "a.b.example.com", "EXAMPLE.com", "example.com."]
    )
    def test_domain_and_subdomains_are_inside(self, scope: DomainScope, host: str) -> None:
        assert scope.contains(host)

    @pytest.mark.parametrize(
        "host",
        [
            "evil-example.com",  # suffix match would wrongly allow this
            "notexample.com",
            "example.com.evil.test",  # and this
            "example.org",
            "",
        ],
    )
    def test_lookalikes_are_outside(self, scope: DomainScope, host: str) -> None:
        assert not scope.contains(host)

    def test_intersection_narrows(self) -> None:
        spec = DomainScope.from_spec(("example.com", "other.com"))
        narrowed = spec.intersect(("example.com",))
        assert narrowed.contains("example.com")
        assert not narrowed.contains("other.com")

    def test_intersection_cannot_widen(self) -> None:
        """Reusing a recipe must not grant reach it was never validated for."""
        spec = DomainScope.from_spec(("example.com",))
        narrowed = spec.intersect(("example.com", "newsite.com"))
        assert not narrowed.contains("newsite.com")

    def test_disjoint_scopes_raise(self) -> None:
        with pytest.raises(InvalidDomainError, match="do not overlap"):
            DomainScope.from_spec(("example.com",)).intersect(("other.com",))


class TestPatternBudget:
    def test_slots_run_out(self) -> None:
        b = PatternBudget(limit=3)
        assert [b.take("p") for _ in range(5)] == [True, True, True, False, False]

    def test_patterns_are_independent(self) -> None:
        b = PatternBudget(limit=1)
        assert b.take("a") and b.take("b")
        assert not b.take("a")

    def test_exhaustion_fires_once(self) -> None:
        b = PatternBudget(limit=2)
        fired = []
        for _ in range(5):
            b.take("p")
            fired.append(b.just_exhausted("p"))
        assert fired.count(True) == 1


class TestTrapGuard:
    @staticmethod
    def guard(**kw: object) -> TrapGuard:
        return TrapGuard(SpiderConfig(**kw))  # type: ignore[arg-type]

    def test_ordinary_url_passes(self) -> None:
        assert self.guard().check(normalize("https://a.com/product/1")).ok

    def test_overlong_url_is_denied(self) -> None:
        v = self.guard(max_url_length=64).check(normalize("https://a.com/" + "x" * 200))
        assert v.reason is RejectReason.URL_LENGTH

    def test_deep_path_is_denied(self) -> None:
        deep = "https://a.com/" + "/".join(f"s{i}" for i in range(20))
        assert self.guard(max_path_depth=5).check(normalize(deep)).reason is RejectReason.PATH_DEPTH

    def test_repeated_segments_are_denied(self) -> None:
        """A symlink loop or a router that accepts its own prefix."""
        v = self.guard().check(normalize("https://a.com/a/b/a/b/a/b"))
        assert v.reason is RejectReason.REPEATED_SEGMENT

    def test_non_adjacent_repeats_still_count(self) -> None:
        v = self.guard(max_repeated_segment=2).check(normalize("https://a.com/x/a/y/a/z/a"))
        assert v.reason is RejectReason.REPEATED_SEGMENT

    def test_too_many_query_params_is_denied(self) -> None:
        url = "https://a.com/l?" + "&".join(f"p{i}={i}" for i in range(12))
        v = self.guard(max_query_params=4).check(normalize(url))
        assert v.reason is RejectReason.QUERY_PARAMS

    def test_infinite_calendar_is_capped(self) -> None:
        """The scenario the budget exists for."""
        g = self.guard(per_pattern_budget=20)
        allowed = sum(
            g.check(normalize(f"https://a.com/calendar/{y}/{m:02d}")).ok
            for y in range(2030, 2060)
            for m in range(1, 13)
        )
        assert allowed == 20, "360 calendar URLs must not cost 360 fetches"

    def test_endless_pagination_is_capped(self) -> None:
        g = self.guard(per_pattern_budget=10)
        allowed = sum(g.check(normalize(f"https://a.com/list?page={i}")).ok for i in range(500))
        assert allowed == 10

    def test_real_content_is_not_starved_by_a_trap(self) -> None:
        """Budgets are per pattern, so a calendar cannot exhaust the products."""
        g = self.guard(per_pattern_budget=5)
        for m in range(1, 50):
            g.check(normalize(f"https://a.com/calendar/2031/{m}"))
        products = sum(g.check(normalize(f"https://a.com/product/{i}")).ok for i in range(5))
        assert products == 5

    def test_session_ids_do_not_multiply_patterns(self) -> None:
        """Session-ish segments collapse into one placeholder, so a site that
        mints a new URL per request burns one budget rather than infinite ones."""
        g = self.guard(per_pattern_budget=10)
        allowed = sum(g.check(normalize(f"https://a.com/s/{i:016x}/page")).ok for i in range(100))
        assert allowed == 10
