# 8. Data Model

## 주요 엔티티

### CrawlSpec

```text
id
seed_urls
allowed_domains
fetch_mode
max_pages
max_depth
extraction_fields
recipe_id? 
recipe_version?
created_at
```

### Recipe

```text
id
name
source_url
allowed_domains
fetch_mode
status
version
extraction_fields
created_at
updated_at
```

### CrawlJob

```text
id
spec_id
status
worker_id?
heartbeat_at?
cancel_requested_at?
pages_crawled
records_extracted
error_message
started_at
completed_at
created_at
```

### CrawlResult

페이지 단위 fetch 결과.

```text
id
job_id
url
status_code
content_type
depth
error
created_at
```

### ExtractedRecord

```text
id
job_id
result_id
page_url
data JSONB
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

### Evidence

후기 단계에서 추가.

field-level provenance.

```text
record_id
field_name
source_url
extractor_type
selector/path
recipe_id
recipe_version
confidence?
```

## 관계

```text
CrawlSpec
  1
  │
  N
CrawlJob
  │
  ├── CrawlResult
  │      └── ExtractedRecord
  │
  └── CrawlEvent

Recipe
  └── CrawlSpec / provenance
```
