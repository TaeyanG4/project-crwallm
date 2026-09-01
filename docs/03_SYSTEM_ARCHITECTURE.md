# 3. System Architecture

## 기본 방향

**Modular Monolith.** 마이크로서비스는 도입하지 않는다.

```text
┌──────────────────────────────────────────────────┐
│  Entry Points                                    │
│  CLI (typer)  /  REST + SSE (FastAPI)            │
└────────────────────────┬─────────────────────────┘
                         ↓
┌──────────────────────────────────────────────────┐
│  Application Services                            │
│  Spec / Structure / Recipe / Job / Model / Export│
└────────────────────────┬─────────────────────────┘
                         ↓
┌──────────────────────────────────────────────────┐
│  Core                                            │
│  Policy · CrawlEngine · Frontier · Extraction    │
│  Filters · Job state machine · Event contracts   │
└────────────────────────┬─────────────────────────┘
                         ↓
┌──────────────────────────────────────────────────┐
│  Infrastructure Adapters                         │
│  HTTP · Browser · ModelGateway · PostgreSQL      │
│  Blob storage · Vault                            │
└──────────────────────────────────────────────────┘
```

## 의존성 방향

```text
CLI / API  →  Services  →  Core  →  Infrastructure
```

역방향 의존 금지. 구체적으로:

- FastAPI가 crawler core 안으로 들어가지 않는다
- SQLAlchemy ORM 객체가 CrawlEngine의 입출력 타입이 되지 않는다
- Playwright 타입이 core에 노출되지 않는다
- LLM vendor SDK가 application service에 직접 노출되지 않는다
- **CLI와 REST는 같은 service 함수를 호출한다.** 로직 중복 금지

## 모듈 경계

### Entry Points
HTTP/CLI 입출력, 인자 파싱, 응답 포맷, SSE 연결.
crawling loop / DOM parsing / 복잡한 DB orchestration 금지.

### Services
Use case orchestration.

- `SpecService` — CrawlSpec 생성/검증
- `StructureService` — 구조 분석, DOM 축약
- `RecipeService` — Recipe CRUD, 파일 동기화, 검증, 채점
- `JobService` — job 생성/조회/취소
- `ModelService` — 모델 목록/다운로드/삭제/라우팅/벤치
- `ExportService` — 결과 내보내기

### Core
- `CrawlEngine` — `AsyncIterator[CrawlEvent]` 하나만 노출
- `Policy` — URL 게이트, 트랩 방어, 리소스 상한
- `Frontier` — 프론티어 전략(BFS / 우선순위 / 호스트별)
- `Extraction` — 추출기 파이프라인
- `Filters` — 레코드 필터
- `CrawlSpec` / `Recipe` / `CrawlEvent` — 계약

### Infrastructure
- `SafeHttpFetcher` — DNS 피닝, 리다이렉트 검증, 스트리밍 제한
- `BrowserFetcher` — Playwright direct
- `ModelGateway` — OpenAI 호환 / Anthropic
- `Repository` — PostgreSQL
- `BlobStore` — 원본 아카이브 (콘텐츠 주소, zstd)
- `Vault` — 크리덴셜 (OS 키링 또는 암호화 파일)

## 핵심 원칙

- entry points thin, services orchestrate
- crawler는 API/ORM으로부터 순수
- infra는 작은 계약 뒤에
- composition > inheritance
- 중복 runner/engine 금지
- 패턴 이름만을 위한 디렉터리 생성 금지 — 실제 책임이 생겼을 때 도입
