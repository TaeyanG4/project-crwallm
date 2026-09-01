# 10. API Plan

초기 권장 API.

## Health

```text
GET /health
```

## CrawlSpec

```text
POST /api/specs
GET  /api/specs/{id}

POST /api/specs/compile
POST /api/specs/adapt
POST /api/specs/from-recipe
```

## Recipe

```text
POST /api/recipes
GET  /api/recipes/{id}
POST /api/recipes/{id}/activate
```

추후:

```text
POST /api/recipes/{id}/deprecate
GET  /api/recipes/{id}/versions
```

## Jobs

```text
POST /api/jobs
GET  /api/jobs/{id}
GET  /api/jobs/{id}/results
POST /api/jobs/{id}/cancel
GET  /api/jobs/{id}/events
```

추후:

```text
POST /api/jobs/{id}/retry
POST /api/jobs/{id}/resume
```

## Export

추후:

```text
GET /api/jobs/{id}/export?format=jsonl
GET /api/jobs/{id}/export?format=csv
```

## Research / Explore

기본 Job API와 별도 product mode를 붙이되 실행 runtime을 중복 구축하지 않는다.
