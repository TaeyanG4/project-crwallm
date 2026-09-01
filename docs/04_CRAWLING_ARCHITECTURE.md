# 4. Crawling Architecture

## 목표

단일 CrawlEngine이 모든 fetch 전략을 사용한다. 브라우저 전용 별도 크롤러를 만들지 않는다.

```text
CrawlEngine
   ↓
Frontier  ←──────────────┐
   ↓                     │
Policy Gate              │
   ↓                     │
FetchCoordinator         │
   ├── HTTP Fetcher      │
   └── Browser Fetcher   │
   ↓                     │
Archive (원본 저장)      │
   ↓                     │
Extractor Pipeline       │
   ↓                     │
Record Filters           │
   ↓                     │
Links ───────────────────┘
   ↓
Records
```

## 엔진 인터페이스 계약 — 확정

**Primitive는 async generator 하나.** 나머지는 전부 그 위의 얇은 어댑터.

```python
# crawler/engine.py — 엔진이 구현하는 유일한 것
async def crawl(spec: CrawlSpec) -> AsyncIterator[CrawlEvent]: ...
```

```python
# jobs/adapters.py — 엔진 밖
async def run_collect(spec) -> CrawlOutcome            # 전부 모아 반환 (소규모/CLI)
async def run_with_sink(spec, sink: EventSink)         # DB 영속화 (워커)
async def run_to_sse(spec) -> AsyncIterator[str]       # SSE 스트림
async def run_with_callbacks(spec, on_page, on_record) # 콜백 스타일
```

### 왜 generator인가

네 가지 중 유일하게 나머지 셋으로 **무손실 변환**된다.
generator → 콜백은 3줄. 콜백 → generator는 큐와 태스크가 필요하다.
백프레셔와 취소도 공짜다(소비가 멈추면 생산이 멈춤).

### 내부 구조

동시성을 쓰면서 순차 이벤트를 내보내야 하므로:

```text
fetch worker pool (N개 동시)
   → asyncio.Queue
   → generator가 drain
   → yield
```

취소: 소비자가 iteration을 멈추거나 `agen.aclose()` 호출 → 워커 풀 정리.

## 이벤트 계약

Phase 1에 확정. 이후 **추가만** 하고 변경하지 않는다.

```text
job.started        {spec_id, seeds}
job.completed      {pages, records, elapsed_s}
job.failed         {error_kind, message}

page.fetched       {url, status, bytes, elapsed_ms, from_cache, fetch_mode}
page.failed        {url, error_kind, retryable}

records.extracted  {url, count, extractor, records[]}
records.filtered   {url, kept, dropped, reason_counts}

links.discovered   {url, found, enqueued}
url.rejected       {url, reason}            # policy / trap / duplicate / scope
pattern.budget_exhausted {pattern, limit}
duplicate.detected {url, canonical_of}

progress           {pages_done, pages_queued, records_total, hosts_active}
```

## Fetch Mode

```text
http     httpx만 사용
browser  JS 렌더링이 반드시 필요한 사이트
auto     HTTP 우선 → 결과 기반 폴백
```

### auto의 판정 — 휴리스틱이 아니라 결과 기반

```text
HTTP fetch → 추출 실행
  ├─ 레코드 ≥ 1건       → HTTP로 확정
  └─ 레코드 0건          → Browser 재시도
```

DOM 휴리스틱("JS shell인가?")보다 견고하다. 브라우저는 20~50배 비싸므로 항상 HTTP 우선.

## HTTP Fetcher 요구사항

- async httpx, HTTP/2, persistent connection pool
- **DNS 캐시 + IP 피닝** — 해석한 IP로 직접 연결(Host/SNI는 원 도메인 유지).
  성능 + SSRF TOCTOU 해결을 동시에 달성. → `11_SECURITY_MODEL.md`
- manual redirects + 매 홉 재검증
- streaming response + hard byte limit
- 조건부 요청 (`ETag` / `If-Modified-Since`)
- `Accept-Encoding: gzip, br`
- 명시적 client ownership
- **바이너리 다운로드 채널 분리** — 미디어/문서는 별도 byte limit, 재개 가능(Range)

## Browser Fetcher 요구사항

Playwright direct. Crawl4AI는 route interception 제어권 때문에 채택하지 않는다.

- Chromium headless, sandbox ON
- **브라우저 인스턴스 1개 유지 + context 재사용** (페이지마다 브라우저 생성 금지)
- 페이지 풀 재사용
- route interception으로 이미지/미디어/폰트/CSS 차단
- `wait_until="domcontentloaded"` (`networkidle` 금지)
- main-frame allowed-domain 강제, private network subresource 차단
- `file://` 차단, 다운로드 기본 차단
- 무한 스크롤 지원 (`max_rounds`, `stop_when_no_growth`)
- deterministic cleanup

**브라우저는 최후 수단이다.** 무한 스크롤도 결국 XHR을 호출하므로, 그 XHR을 찾아 직접 호출하는 쪽이 20배 빠르다.

## 리소스 상한

```text
max_pages / max_depth / allowed_domains
response_byte_limit  (문서·미디어는 별도 채널)
request_timeout / job_timeout
redirect_max
global_concurrency / per_host_concurrency
min_interval_ms (per host)
```
