# 13. API & CLI Plan

**CLI와 REST는 같은 service 함수를 호출한다.** 로직 중복 금지.

CLI는 Phase 2에 들어간다 — 웹 UI(후기 phase) 이전에도 수동 조작이 가능해야 한다.

---

## CLI

```bash
# 구조 분석 — LLM 불필요
crwallm inspect <url> [--mode http|browser|auto] [--json]

# Recipe
crwallm recipe list
crwallm recipe test <file.yaml> [--url <url>] [--use-archive]
crwallm recipe push <file.yaml>          # 파일 → DB (검증 후)
crwallm recipe pull <name>               # DB → 파일
crwallm recipe adapt <url> --fields title,price,url    # LLM 사용

# 크롤
crwallm crawl --recipe <name> [--seed <url>] [--max-pages N] [--follow]
crwallm crawl --spec <file.yaml>
crwallm spider --seed <url> --max-pages 10000 [--sitemap]

# Job
crwallm jobs [--status running]
crwallm job <id>                         # 상태 + 에러 분류 집계
crwallm job <id> --watch                 # 실시간 진행
crwallm cancel <id>

# 결과
crwallm results <job_id> [--format jsonl|csv|table] [--limit N]
crwallm export <job_id> --format parquet -o out.parquet

# 모델
crwallm model list
crwallm model catalog                    # 하드웨어 기반 추천
crwallm model pull qwen3:14b
crwallm model rm qwen3:8b
crwallm model route adapt_selectors=api
crwallm model bench qwen3:14b
```

---

## REST

로컬 바인딩 + Host 화이트리스트 + 토큰 헤더. → `11_SECURITY_MODEL.md` §1

### Health
```text
GET /health
```

### Structure
```text
POST /api/structure/inspect      # URL → 반복 구조 후보 + DOM 축약
```

### Spec
```text
POST /api/specs
GET  /api/specs/{id}
POST /api/specs/compile          # 자연어 → CrawlSpec 후보 (자동 실행 금지)
POST /api/specs/from-recipe
```

### Recipe
```text
POST   /api/recipes
GET    /api/recipes
GET    /api/recipes/{id}
POST   /api/recipes/{id}/test        # 결정론적 검증 + 품질 점수
POST   /api/recipes/{id}/activate
POST   /api/recipes/adapt            # LLM 후보 N개 + 채점
POST   /api/recipes/sync             # 파일 ↔ DB
후기:
POST   /api/recipes/{id}/deprecate
GET    /api/recipes/{id}/versions
```

### Jobs
```text
POST /api/jobs
GET  /api/jobs
GET  /api/jobs/{id}
GET  /api/jobs/{id}/results
GET  /api/jobs/{id}/errors           # 에러 분류 집계
POST /api/jobs/{id}/cancel
GET  /api/jobs/{id}/events           # SSE
후기:
POST /api/jobs/{id}/retry
POST /api/jobs/{id}/resume
```

### Models
```text
GET    /api/models
GET    /api/models/catalog
POST   /api/models/pull              # SSE 진행률
DELETE /api/models/{name}
POST   /api/models/route
POST   /api/models/{name}/bench
```

### Export
```text
GET /api/jobs/{id}/export?format=jsonl|csv|parquet
```

### 후기
```text
POST /api/credentials                # 참조만 저장
GET  /api/schedules                  # 반복 크롤
POST /api/search                     # RAG 질의
```

Research / Explore는 별도 product mode를 붙이되 **실행 runtime을 중복 구축하지 않는다.**
