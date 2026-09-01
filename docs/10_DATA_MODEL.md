# 10. Data Model

로컬 단일 사용자이므로 **workspace / tenant / user 개념이 없다.**

## 엔티티

### CrawlSpec — "무엇을 / 어디까지"
```text
id
name?
seed_urls[]
allowed_domains[]
max_pages, max_depth
url_filters {include[], exclude[]}
limits {concurrency, timeouts, byte_limit, ...}
spider_config?          # 트랩 방어, 프론티어 전략
recipe_id?, recipe_version?
mode                    # collect | spider | research
created_at
```

### Recipe — "어떻게"
```text
id
name (unique)
source_url
allowed_domains[]
fetch_mode
status                  # candidate | active | deprecated
version
container
fields[]                # {name, selector, type, transform[]}
pagination?
api?                    # 내부 API 전략
filters[]
fingerprint             # 구조 지문
quality {record_count, fill_rates, consistency}
file_path?              # recipes/*.yaml 동기화용
created_at, updated_at
```

### CrawlJob
```text
id
spec_id
status
priority
worker_id?, heartbeat_at?, cancel_requested_at?
pages_crawled, records_extracted, records_filtered
error_kind?, error_message?
started_at, completed_at, created_at
```

### Frontier — Spider용, 영속화
```text
id
job_id
url, url_normalized, url_pattern
depth, priority
state                   # queued | claimed | done | rejected
reject_reason?
host                    # 호스트별 큐잉/라운드로빈 인덱스
discovered_from?
created_at, claimed_at?
UNIQUE (job_id, url_normalized)
```

### CrawlResult — 페이지 단위 fetch 결과
```text
id
job_id
url, canonical_url?
status_code, content_type, content_length
depth
fetch_mode              # http | browser
elapsed_ms
body_ref                # 원본 아카이브 콘텐츠 주소 (sha256)
content_hash            # 근사 중복 판정용 simhash
error_kind?
created_at
```

### ExtractedRecord
```text
id
job_id, result_id
page_url
data JSONB
extractor               # 어떤 추출기가 성공했는지
recipe_id?, recipe_version?
record_hash             # 멱등성/중복 제거
created_at
UNIQUE (job_id, page_url, record_hash)
```

### Document — 비정형
```text
id
job_id, result_id
url
content_type            # html | pdf | docx | ...
title?
text                    # 본문 추출 결과
raw_ref                 # 원본 블롭
meta JSONB
created_at
```

### CrawlEvent
```text
id BIGSERIAL
job_id
event_type
payload JSONB
created_at
```

### Evidence — field-level provenance (후기)
```text
record_id, field_name
source_url
extractor_type, selector_or_path
recipe_id, recipe_version
body_ref                # 그때 원본
confidence?
```

### ModelConfig — 모델 라우팅/카탈로그 상태
```text
id
backend_name            # local | api | embed
kind, base_url, model
api_key_ref?            # 값이 아니라 참조
options JSONB           # num_ctx 등
task_routing JSONB
bench_result JSONB?
updated_at
```

### Credential — 인증 (후기)
```text
id
name
site_pattern
kind                    # form | cookie | header | oauth
secret_ref              # Vault 참조. 값 저장 금지
session_state JSONB?    # 쿠키 등, 암호화
expires_at?
```

### Embedding — RAG (후기)
```text
id
document_id, chunk_index
text
vector VECTOR(1024)     # pgvector
meta JSONB
```

## 관계

```text
CrawlSpec 1─N CrawlJob
                ├── N Frontier
                ├── N CrawlResult ──┬── N ExtractedRecord ── N Evidence
                │                   └── N Document ── N Embedding
                └── N CrawlEvent

Recipe ──┬── CrawlSpec (참조)
         └── provenance (Record/Evidence)
```

## 인덱스 주의

대량 삽입 경로(`ExtractedRecord`, `CrawlResult`, `Frontier`)는 인덱스를 최소화한다.
`Frontier`의 `UNIQUE (job_id, url_normalized)`는 필수지만, 나머지 조회용 인덱스는
실제 쿼리가 생긴 뒤에 추가한다.
