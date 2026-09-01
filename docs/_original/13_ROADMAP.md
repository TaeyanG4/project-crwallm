# 13. Build Roadmap

## Phase 0 — Foundation

- Python project
- FastAPI
- PostgreSQL
- SQLAlchemy
- Alembic
- CI
- health
- strict typing/lint

목표:
깨끗한 설치/테스트 기반.

---

## Phase 1 — CrawlSpec + Policy

- minimal CrawlSpec
- allowed domains
- max pages/depth
- fetch mode
- extraction fields
- URL normalization
- SSRF
- async DNS validation
- bounded inputs

---

## Phase 2 — Static Collect MVP

- Safe HTTP fetcher
- redirect validation
- response size limit
- BFS
- dedupe
- CSS extraction
- DB result persistence
- API E2E

이 시점에서 실제 usable crawler가 처음 완성.

---

## Phase 3 — Natural Language Compiler

```text
Natural language
→ ModelGateway
→ CrawlSpec candidate
→ Pydantic
→ Policy
```

LLM 결과 자동 실행 금지.
검토 가능한 candidate 반환.

---

## Phase 4 — Site Adaptation + Recipe

```text
Unknown site
→ sample HTML
→ LLM selector candidate
→ deterministic validation
→ preview
→ Recipe
```

Recipe explicit reuse 지원.

---

## Phase 5 — Browser Fallback

```text
HTTP
→ enough? yes → done
→ no + JS shell → Browser
```

정적 crawling이 기본.

---

## Phase 6 — Durable Jobs

### 6A

- queued jobs
- separate worker
- atomic claim
- events
- SSE
- cancellation
- heartbeat

### 6B

- retry
- stale recovery
- resume
- persisted frontier/checkpoint
- idempotency

---

## Phase 7 — Deterministic Extraction Expansion

우선순위:

1. JSON/API
2. JSON-LD
3. RSS/XML
4. OpenGraph/meta
5. CSS
6. XPath if needed
7. semantic LLM fallback

Evidence/provenance 추가.

---

## Phase 8 — Storage / Documents

- JSONL
- CSV
- PostgreSQL upsert
- raw artifact
- Document
- Docling

---

## Phase 9 — Research / Explore

- multi-source discovery
- source ranking
- sufficiency/saturation
- bounded semantic crawling

---

## Phase 10 — Authenticated Interact

- session/auth
- browser interaction
- waiting for user
- challenge handoff

CAPTCHA 자동 우회 금지.

---

## Phase 11 — Drift / Self-Healing

- drift metrics
- repair candidates
- regression
- canary
- recipe version promotion

---

## Phase 12 — Frontend

- natural language entry
- adaptation preview
- job progress
- SSE
- results/evidence
- recipes
- approvals

---

## Phase 13 — Production

- observability
- rate/concurrency limits
- load testing
- security review
- deployment
- backup/restore
- release process

## Scaling rule

Redis/Kafka/microservices/etc.는 측정으로 필요성이 확인될 때만 추가한다.
