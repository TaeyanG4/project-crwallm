# 9. Job Architecture

브라우저/대량 crawl은 요청 하나가 수 분 이상 걸린다.
따라서 API request lifecycle과 crawl execution을 분리한다.

**이 분리는 Phase 2에 들어간다.** 원본 설계는 Phase 6이었으나, 그때 도입하면
API 라우터·서비스 계층·엔진 호출 규약·결과 저장 방식을 동시에 뜯어야 한다.

## 단계적 도입

### Phase 2 — 얇은 껍데기
```text
POST /api/jobs → DB에 queued 저장 → 즉시 job_id 반환
Worker(별도 프로세스) → claim → running → CrawlEngine → completed/failed
GET /api/jobs/{id} → 상태 폴링
```

### Phase 8 — 완전판
SSE, cancel, heartbeat, retry, stale recovery, resume, checkpoint, idempotency

## 흐름

```text
POST /jobs
 ↓
queued job persisted
 ↓
즉시 응답 (job_id)

Worker
 ↓ claim (FOR UPDATE SKIP LOCKED)
running
 ↓
CrawlEngine.crawl(spec)  →  AsyncIterator[CrawlEvent]
 ↓
EventSink: DB 영속화 + 진행률 갱신 + SSE 발행
 ↓
completed / failed / cancelled
```

## 상태

```text
초기:  queued → running → completed | failed | cancelled
후기:  + retry_wait, waiting_for_user
```

필요할 때만 추가한다.

## Queue

PostgreSQL 기반. Redis 불필요.

```sql
SELECT ... FROM crawl_jobs
WHERE status = 'queued'
ORDER BY priority DESC, created_at
FOR UPDATE SKIP LOCKED
LIMIT 1;
```

장점: transactional claim, 기존 PG와 일관성, 인프라 추가 없음.
규모가 커진 이후에 별도 queue를 검토한다.

**프론티어도 같은 곳에 둔다** (`05_SPIDER_ARCHITECTURE.md` §7).

## Worker

저장 정보: `worker_id`, `started_at`, `heartbeat_at`, `cancel_requested_at`, `completed_at`

**Linux 컨테이너에서 실행한다** — uvloop을 쓰기 위해. → `12_PERFORMANCE.md`

## CrawlEvents

`04_CRAWLING_ARCHITECTURE.md`의 이벤트 계약 참조.

PostgreSQL에 저장하여 SSE / replay / audit / reconnect에 활용한다.

로컬 단일 사용자이므로 `Last-Event-ID` 재연결은 우선순위가 낮다.

## SSE

```text
GET /api/jobs/{job_id}/events
```

event id, event type, JSON data, heartbeat, terminal 종료.
WebSocket은 일반 progress에 필요 없다.

## 에러 택소노미

**Phase 2에 넣는다.** enum + 카운터라 비용이 거의 0이고, 없으면 튜닝이 불가능하다.

```text
dns_fail | conn_timeout | read_timeout | tls_error
http_4xx | http_5xx | blocked_403 | blocked_429 | captcha_detected
parse_fail | policy_reject | trap_reject | size_exceeded | duplicate
extract_empty | filter_dropped
```

"1000페이지 중 400 실패"만 보면 아무것도 못 한다.
"400 중 380이 `blocked_429`"를 보면 동시성을 낮추면 된다는 걸 안다.

## Retry vs Resume

**다르다.**

- **Retry** — job을 처음부터 다시 시도
- **Resume** — 영속화된 frontier/checkpoint부터 이어서 실행

Resume을 구현하면 결과 중복 방지와 idempotency가 필요하다.
`ExtractedRecord`에 `(job_id, page_url, record_hash)` 유니크 제약을 두는 방식이 단순하다.

## 스케줄링 (후기)

반복 크롤 정의 + 증분:
```text
cron 표현식 → 주기 실행
sitemap <lastmod> / ETag / If-Modified-Since → 변경분만
콘텐츠 해시 비교 → 변경 감지 이벤트
```
