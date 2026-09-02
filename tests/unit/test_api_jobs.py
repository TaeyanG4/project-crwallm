"""Job endpoints, without a database.

The routing, validation and authorisation layers are what these cover, and
none of them need Postgres - the session is overridden with a stub. The
database-backed behaviour has its own integration test.

The authorisation cases matter most. An unauthenticated localhost API is
reachable by any page the user happens to be visiting, so "can a request
without the token submit a crawl" is a security question, not a formality
(docs/11_SECURITY_MODEL.md section 1).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from crwallm.api.app import create_app
from crwallm.api.deps import session_dep
from crwallm.api.security import TOKEN_HEADER
from crwallm.config import Settings
from crwallm.db.models import CrawlJob, JobStatus
from tests.conftest import TEST_TOKEN


class FakeJobStore:
    """Enough of a session for the router to be exercised.

    A stub rather than a mock: the router should be judged on what it returns,
    not on which methods it happened to call.
    """

    def __init__(self) -> None:
        self.jobs: dict[uuid.UUID, CrawlJob] = {}
        self.records: list[dict[str, Any]] = []
        self.submitted: list[Any] = []

    def add_job(self, **overrides: Any) -> CrawlJob:
        job = CrawlJob(
            id=overrides.pop("id", uuid.uuid4()),
            spec_id=uuid.uuid4(),
            status=overrides.pop("status", JobStatus.COMPLETED),
            priority=0,
            pages_crawled=overrides.pop("pages_crawled", 3),
            pages_failed=overrides.pop("pages_failed", 1),
            records_extracted=overrides.pop("records_extracted", 7),
            # Explicit because SQLAlchemy applies column defaults at flush,
            # and nothing here is flushed - an in-memory row leaves them None.
            attempts=overrides.pop("attempts", 0),
            error_counts=overrides.pop("error_counts", {"blocked_429": 1}),
            reject_counts=overrides.pop("reject_counts", {"scope": 12}),
            created_at=datetime.now(UTC),
        )
        for key, value in overrides.items():
            setattr(job, key, value)
        self.jobs[job.id] = job
        return job


@pytest.fixture
def store() -> FakeJobStore:
    return FakeJobStore()


@pytest.fixture
def client(store: FakeJobStore, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    settings = Settings(api_token=TEST_TOKEN)
    monkeypatch.setattr("crwallm.api.deps.get_settings", lambda: settings)

    from crwallm.services import job as job_service

    async def fake_submit(self: Any, spec: Any, *, priority: int = 0) -> CrawlJob:
        store.submitted.append(spec)
        return store.add_job(id=spec.id, status=JobStatus.QUEUED, pages_crawled=0)

    async def fake_get(self: Any, job_id: uuid.UUID) -> CrawlJob | None:
        return store.jobs.get(job_id)

    async def fake_list(self: Any, *, limit: int = 20, status: str | None = None) -> list[CrawlJob]:
        jobs = list(store.jobs.values())
        if status:
            jobs = [j for j in jobs if j.status == status]
        return jobs[:limit]

    monkeypatch.setattr(job_service.JobService, "submit", fake_submit)
    monkeypatch.setattr(job_service.JobService, "get", fake_get)
    monkeypatch.setattr(job_service.JobService, "list_recent", fake_list)

    app = create_app(settings)

    class FakeSession:
        async def execute(self, *args: Any, **kwargs: Any) -> Any:
            class Result:
                @staticmethod
                def scalars() -> list[Any]:
                    return []

            return Result()

    async def fake_session() -> Any:
        yield FakeSession()

    app.dependency_overrides[session_dep] = fake_session
    with TestClient(app, headers={"Host": "127.0.0.1"}) as c:
        yield c


def spec_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "seed_urls": ["https://example.com/products"],
        "allowed_domains": ["example.com"],
    }
    payload.update(overrides)
    return {"spec": payload}


class TestSubmitAuthorisation:
    """A page the user is visiting must not be able to start a crawl."""

    def test_submission_without_a_token_is_refused(self, client: TestClient) -> None:
        r = client.post("/api/jobs", json=spec_payload())
        assert r.status_code == 401

    def test_submission_with_a_wrong_token_is_refused(self, client: TestClient) -> None:
        r = client.post("/api/jobs", json=spec_payload(), headers={TOKEN_HEADER: "nope"})
        assert r.status_code == 401

    def test_submission_with_the_token_is_accepted(self, client: TestClient) -> None:
        r = client.post("/api/jobs", json=spec_payload(), headers={TOKEN_HEADER: TEST_TOKEN})
        assert r.status_code == 202

    def test_reads_do_not_require_the_token(self, client: TestClient) -> None:
        """Reads expose nothing a local user cannot already see, and requiring
        the header would make the browsable API useless."""
        assert client.get("/api/jobs").status_code == 200


class TestSubmit:
    def test_accepted_not_created(self, client: TestClient) -> None:
        """202: the job is queued, not finished. A crawl runs for minutes and
        the response cannot wait for it."""
        r = client.post("/api/jobs", json=spec_payload(), headers={TOKEN_HEADER: TEST_TOKEN})
        assert r.status_code == 202
        body = r.json()
        assert body["status"] == JobStatus.QUEUED
        assert uuid.UUID(body["id"])

    def test_the_spec_reaches_the_service_intact(
        self, client: TestClient, store: FakeJobStore
    ) -> None:
        client.post(
            "/api/jobs",
            json=spec_payload(limits={"max_pages": 5}) | {"priority": 3},
            headers={TOKEN_HEADER: TEST_TOKEN},
        )
        assert store.submitted
        assert store.submitted[0].seed_urls == ("https://example.com/products",)
        assert store.submitted[0].limits.max_pages == 5

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("seed_urls", []),
            ("allowed_domains", []),
            ("limits", {"max_pages": 99_999_999}),
            ("limits", {"max_depth": -1}),
        ],
    )
    def test_invalid_specs_are_rejected_by_pydantic(
        self, client: TestClient, field: str, value: Any
    ) -> None:
        """The first of the two gates. A model that emits max_pages=10_000_000
        is refused here rather than at 3am (docs/08_LLM_ARCHITECTURE.md)."""
        r = client.post(
            "/api/jobs",
            json=spec_payload(**{field: value}),
            headers={TOKEN_HEADER: TEST_TOKEN},
        )
        assert r.status_code == 422

    def test_bare_public_suffix_is_rejected_at_submission(self, client: TestClient) -> None:
        """The second gate. A scope that can never run should fail where
        somebody can still fix it, not an hour later in a worker log."""
        r = client.post(
            "/api/jobs",
            json=spec_payload(allowed_domains=["com"]),
            headers={TOKEN_HEADER: TEST_TOKEN},
        )
        assert r.status_code == 422
        assert "registrable" in r.json()["detail"]

    def test_unknown_fields_are_rejected(self, client: TestClient) -> None:
        r = client.post(
            "/api/jobs",
            json={
                "spec": {"seed_urls": ["https://a.com"], "allowed_domains": ["a.com"]},
                "surprise": 1,
            },
            headers={TOKEN_HEADER: TEST_TOKEN},
        )
        assert r.status_code == 422


class TestRead:
    def test_missing_job_is_404(self, client: TestClient) -> None:
        assert client.get(f"/api/jobs/{uuid.uuid4()}").status_code == 404

    def test_detail_carries_the_tallies(self, client: TestClient, store: FakeJobStore) -> None:
        """The tallies are the reason the detail endpoint exists: "4 failed" is
        not actionable, "blocked_429: 1" is."""
        job = store.add_job()
        body = client.get(f"/api/jobs/{job.id}").json()
        assert body["error_counts"] == {"blocked_429": 1}
        assert body["reject_counts"] == {"scope": 12}

    def test_detail_does_not_leak_orm_internals(
        self, client: TestClient, store: FakeJobStore
    ) -> None:
        """The response model is not the row. Adding a column must not silently
        publish it - each one is a deliberate choice.

        ``cancel_requested_at`` used to be on this list and is now published on
        purpose: it is the whole difference between "running" and "stopping",
        and without it a caller cannot tell that a cancel was asked for and
        the worker has not reached its next page yet.
        """
        job = store.add_job()
        body = client.get(f"/api/jobs/{job.id}").json()
        assert "spec_id" not in body
        assert "cancel_requested_at" in body

    def test_listing_filters_by_status(self, client: TestClient, store: FakeJobStore) -> None:
        store.add_job(status=JobStatus.COMPLETED)
        store.add_job(status=JobStatus.QUEUED)
        assert len(client.get("/api/jobs?status=queued").json()) == 1

    def test_unknown_status_is_rejected(self, client: TestClient) -> None:
        r = client.get("/api/jobs?status=nonsense")
        assert r.status_code == 422
        assert "unknown status" in r.json()["detail"]

    def test_results_for_a_missing_job_are_404_not_empty(self, client: TestClient) -> None:
        """ "No records yet" and "wrong id" are different answers, and a caller
        polling for results has to tell them apart."""
        assert client.get(f"/api/jobs/{uuid.uuid4()}/results").status_code == 404

    def test_results_page_reports_its_window(self, client: TestClient, store: FakeJobStore) -> None:
        job = store.add_job()
        body = client.get(f"/api/jobs/{job.id}/results?limit=5&offset=10").json()
        assert body["limit"] == 5
        assert body["offset"] == 10
        assert body["records"] == []

    @pytest.mark.parametrize("query", ["limit=0", "limit=99999", "offset=-1"])
    def test_paging_bounds_are_enforced(
        self, client: TestClient, store: FakeJobStore, query: str
    ) -> None:
        job = store.add_job()
        assert client.get(f"/api/jobs/{job.id}/results?{query}").status_code == 422


class TestHostGuardStillApplies:
    def test_a_foreign_host_cannot_reach_the_job_api(self, client: TestClient) -> None:
        """DNS rebinding aims at exactly this endpoint."""
        r = client.post(
            "/api/jobs",
            json=spec_payload(),
            headers={TOKEN_HEADER: TEST_TOKEN, "Host": "evil.example.com"},
        )
        assert r.status_code == 421
