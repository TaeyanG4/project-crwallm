# 7. Job Architecture

브라우저/대량 crawl이 들어오면 요청 하나가 수초~수분 이상 걸릴 수 있다.

따라서 API request lifecycle과 crawl execution을 분리한다.

## 흐름

```text
POST /jobs
 ↓
queued job persisted
 ↓
immediate response

Worker
 ↓
claim job
 ↓
running
 ↓
CrawlEngine
 ↓
results/events
 ↓
completed / failed
```

## 상태

초기:

```text
queued
running
completed
failed
cancelled
```

후기 확장:

- retry_wait
- waiting_for_user

필요할 때만 추가.

## Queue

초기 추천:

PostgreSQL 기반 queue.

가능한 방식:

```sql
SELECT ...
FOR UPDATE SKIP LOCKED
```

장점:

- Redis 불필요
- 현재 PostgreSQL과 일관성
- transactional claim 가능

규모가 커진 이후 별도 queue를 검토.

## Worker

저장 정보:

- worker_id
- started_at
- heartbeat_at
- cancel_requested_at
- completed_at

## CrawlEvents

예:

```text
job.queued
job.started
page.started
page.completed
page.records_extracted
job.completed
job.failed
job.cancel.requested
job.cancelled
```

PostgreSQL에 저장하여:

- SSE
- replay
- audit
- reconnect

에 활용.

## SSE

```text
GET /api/jobs/{job_id}/events
```

지원:

- event id
- event type
- JSON data
- Last-Event-ID
- heartbeat
- terminal 종료

WebSocket은 일반 progress에는 필요 없다.

## Retry / Resume

별도 단계에서 추가.

Retry와 Resume은 다르다.

### Retry

job을 처음부터 다시 시도.

### Resume

persisted frontier/checkpoint부터 이어서 실행.

Resume을 구현하면 결과 중복 방지와 idempotency가 필요하다.
