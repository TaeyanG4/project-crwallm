# 16. Roadmap

## Phase 0 — Foundation  ✅

- Python 3.12 + uv, pyproject
- FastAPI 골격, `/health`
- PostgreSQL 16 + SQLAlchemy 2 async + Alembic
- **Linux 컨테이너 실행 환경 (docker-compose) + uvloop**
- **로컬 API 보호** — 127.0.0.1 바인딩, Host 화이트리스트, CORS 고정, 토큰 헤더
- ruff + mypy strict, pytest, CI
- 테스트 픽스처 뼈대 (`tests/fixtures/html`, `malicious_server`)

목표: 깨끗한 설치/테스트 기반.

---

## Phase 1 — Contracts & Policy  ✅

- `CrawlSpec` / `Recipe` / `CrawlEvent` Pydantic 계약 확정
- **엔진 인터페이스 확정** — `async def crawl(spec) -> AsyncIterator[CrawlEvent]`
- **URL 정규화 완전판** — 추적 파라미터 제거, 파라미터 정렬/화이트리스트, fragment/포트/슬래시, canonical
- **SSRF + DNS 피닝** — 해석한 IP 검증 후 그 IP로 연결 (TOCTOU 해결 + 성능)
- Public Suffix List 기반 도메인 스코프
- `HostPolicy` — 적응형 동시성(AIMD), 백오프, `Retry-After`, UA. `respect_robots` 필드는 자리만
- 악성 로컬 서버 테스트 (127.0.0.1, 169.254.169.254, DNS rebinding, 무한 리다이렉트, 거대 응답)

### Phase 1에서 확정된 것

| 결정 | 근거 |
|---|---|
| URL을 **두 값**으로 (`url` / `dedupe_key`) | 하나로 합치면 공격적 dedupe가 fetch를 깨거나, 보수적 정규화가 프론티어를 폭발시킨다 |
| DNS 응답 중 **하나라도** 내부면 호스트 전체 거부 | 브라우저 fetcher는 레코드를 고를 수 없어 피닝이 불가능하다 |
| `Resolver`를 주입 | DNS rebinding은 실제 리졸버로 테스트할 수 없다. 테스트 가능성이 설계를 결정했다 |
| 8자 이상 식별자 세그먼트는 **하나의** placeholder | 숫자 세션 ID와 hex 세션 ID가 다른 예산을 받으면 트랩이 살아남는다 |
| 어댑터가 `aclosing`을 강제 | `break`는 async generator를 닫지 않는다. 크롤이 소비자보다 오래 산다 |
| 이벤트에 `job_id` 없음 | job은 서비스 계층 개념. 엔진에 넣으면 core가 영속화를 알게 된다 |
| tldextract `include_psl_private_domains=True` | `github.io`를 scope로 지정해 전체를 크롤하는 것을 막는다 |

---

## Phase 2 — Deterministic Crawl MVP

- Safe HTTP fetcher — 스트리밍, byte limit, 수동 리다이렉트 재검증, 조건부 요청
- BFS traversal, dedupe
- **selectolax CSS 추출**
- **트랩 가드** — URL 길이, 경로 깊이, 반복 세그먼트, 쿼리 화이트리스트, **패턴별 예산**
- **원본 아카이빙** — 콘텐츠 주소 + zstd
- **에러 택소노미** — enum + 집계
- **얇은 job 껍데기 + 별도 워커 프로세스** (queued/running/completed, 폴링)
- **배치 DB 쓰기** (COPY)
- **CLI** — inspect / crawl / job / results
- API E2E

**이 시점에서 실제 usable crawler가 처음 완성된다.**

---

## Phase 3 — Structure & Recipe (LLM 없음)

- **결정론적 반복 구조 탐지** — 형제 그룹 시그니처, 텍스트 밀도, 컬럼 전개
- **DOM 축약기** — 2~4k 토큰 목표
- **구조 지문(fingerprint)**
- Recipe 스키마 + `recipes/*.yaml` ↔ DB 동기화
- **transform 화이트리스트**
- **레코드 필터** (결정론적 연산자)
- **품질 지표** — record_count, fill_rate, match_rate, consistency → activation 게이트
- 응답 캐시(아카이브 재사용)로 개발 루프 가속

**수준 0~1이 여기서 완성된다. LLM 없이 쓸 수 있는 도구가 된다.**

---

## Phase 4 — LLM Runtime

- `ModelGateway` Protocol + OpenAI 호환 구현 (Ollama/API 공용)
- **작업별 라우팅** + fallback
- **모델 관리** — list/catalog/pull/delete/route
- **하드웨어 감지** (VRAM) + 티어 추천 + 온보딩
- **bench** — 고정 샘플 5종 실측
- Grammar-constrained structured output (`format` + JSON schema)
- **N-후보 생성 + 결정론적 채점**
- **피드백 재시도 루프**
- 자연어 → CrawlSpec 컴파일 (자동 실행 금지, 검토 가능한 후보 반환)
- 시간/토큰 예산

**마일스톤 1 완료 지점 후보. 수준 2~3 동작.**

---

## Phase 5 — Spider Mode

- **Sitemap 시딩** (robots.txt의 `Sitemap:` 포함, sitemap index, `<lastmod>`)
- **호스트별 프론티어 + 라운드로빈 스케줄러**
- **우선순위 프론티어**
- **simhash 근사 중복 제거**
- **Postgres 프론티어 영속화**
- **soft 404 탐지**
- rbloom 대규모 중복 필터

**마일스톤 1 = Phase 0~5.** 만능 크롤러의 실질적 하한선.

---

## Phase 6 — Extraction Expansion

- **내부 JSON API 발견** + **API 페이지네이션** (offset/page/cursor/link_header/graphql)
- 임베디드 JSON (`__NEXT_DATA__` 등)
- JSON-LD (Product/Article/JobPosting/**VideoObject**)
- RSS/Atom, `<table>`
- OpenGraph/microdata/oEmbed — **미디어 메타데이터 추출기**
- trafilatura 본문 추출 → `Document`
- 임베딩 최소 인프라 + **semantic 필터**
- Evidence/provenance

---

## Phase 7 — Browser

- Playwright direct, 인스턴스/context 재사용, 페이지 풀
- route interception 리소스 차단
- **결과 기반 auto 폴백** (레코드 0건 → 브라우저)
- **무한 스크롤**
- 브라우저 네트워크 관찰로 API 발견 보강

---

## Phase 8 — Durability & Observability

- retry, stale recovery, resume, 영속 checkpoint, idempotency
- SSE, cancel, heartbeat
- export — JSONL / CSV / Parquet
- 관측 — 에러 분류 대시보드, 처리량/차단률 지표

---

## Phase 9 — Auth & Interact

- 선언형 로그인 레시피, 세션 재사용/TTL
- `Vault` 크리덴셜 참조 저장 (OS 키링/암호화 파일)
- 폼 기반 검색 (POST 크롤)
- waiting_for_user 상태

**CAPTCHA 자동 우회는 하지 않는다.**

---

## Phase 10 — Documents & Media

- PDF/Office → Docling
- 바이너리 다운로드 채널 (별도 byte limit, Range 재개)
- 이미지/파일 저장 정책
- (선택) yt-dlp 어댑터 — 인자 화이트리스트 필수. HLS/DASH는 외부 도구에 위임

---

## Phase 11 — Scheduling & Incremental

- cron 반복 크롤
- 증분 — `<lastmod>` / ETag / If-Modified-Since / 콘텐츠 해시
- 변경 감지(diff) 이벤트

---

## Phase 12 — RAG Loop

- 청킹 → 임베딩(bge-m3) → pgvector
- 수집 데이터에 대한 로컬 LLM 질의

새 인프라 없음(Ollama + Postgres 재사용). "LLM ↔ 크롤러" 루프가 양방향으로 닫힌다.

---

## Phase 13+ — Later

- 프록시 풀 (실제 IP 차단을 겪은 뒤에)
- 드리프트 감지 / 자가치유 (수리 후보 → 회귀 → 카나리 → 승격)
- 웹 UI (Next.js)
- (선택) MCP export
- 프로덕션화 — 부하 테스트, 백업/복구, 릴리스 프로세스

---

## 마일스톤

| | 범위 | 완료 시 |
|---|---|---|
| **1** | Phase 0~5 | 로컬 LLM 자연어 크롤링 + 스파이더 + CLI 수동 조작 |
| **2** | Phase 6~8 | 커버리지와 안정성 |
| **3** | Phase 9~12 | 확장 능력 |

## 규칙

**Phase 5까지 끝내고 실제로 한 달 써본 뒤 6번 이후를 재평가한다.**
어떤 사이트에서 막히는지가 다음 우선순위를 정한다.

Redis / Kafka / 분산 / 마이크로서비스는 **측정으로 필요성이 확인될 때만** 추가한다.
