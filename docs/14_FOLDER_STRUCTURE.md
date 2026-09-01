# 14. Folder Structure

```text
backend/
├── src/crwallm/
│   ├── api/
│   │   ├── app.py
│   │   ├── deps.py
│   │   ├── security.py           # Host 검증, 토큰, CORS
│   │   └── routers/
│   │       ├── health.py
│   │       ├── structure.py
│   │       ├── specs.py
│   │       ├── recipes.py
│   │       ├── jobs.py
│   │       └── models.py
│   │
│   ├── cli/
│   │   ├── main.py               # typer entry
│   │   └── commands/
│   │
│   ├── services/                 # CLI와 API가 공유
│   │   ├── spec.py
│   │   ├── structure.py
│   │   ├── recipe.py
│   │   ├── job.py
│   │   ├── model.py
│   │   └── export.py
│   │
│   ├── schemas/                  # Pydantic 계약
│   │   ├── spec.py
│   │   ├── recipe.py
│   │   ├── job.py
│   │   ├── events.py
│   │   └── filters.py
│   │
│   ├── policy/
│   │   ├── url.py                # 정규화, 스코프
│   │   ├── ssrf.py               # IP 검증, DNS 피닝
│   │   ├── traps.py              # 패턴 예산, 깊이, 반복 세그먼트
│   │   └── host.py               # HostPolicy, 적응형 동시성
│   │
│   ├── crawler/
│   │   ├── engine.py             # AsyncIterator[CrawlEvent]
│   │   ├── types.py
│   │   ├── frontier/
│   │   │   ├── contracts.py
│   │   │   ├── memory.py         # BFS + bloom
│   │   │   ├── postgres.py       # 영속 프론티어
│   │   │   └── scheduler.py      # 호스트별 라운드로빈, 우선순위
│   │   ├── fetching/
│   │   │   ├── contracts.py
│   │   │   ├── http.py
│   │   │   ├── browser.py
│   │   │   ├── binary.py         # 대용량 다운로드 채널
│   │   │   └── coordinator.py
│   │   ├── discovery/
│   │   │   ├── sitemap.py
│   │   │   ├── links.py
│   │   │   └── api_probe.py      # 내부 JSON API 발견
│   │   └── extraction/
│   │       ├── pipeline.py
│   │       ├── json_api.py
│   │       ├── embedded_json.py
│   │       ├── jsonld.py
│   │       ├── xml_feed.py
│   │       ├── table.py
│   │       ├── css.py
│   │       ├── metadata.py       # OG / microdata / oEmbed / VideoObject
│   │       ├── text.py           # trafilatura
│   │       ├── transforms.py     # 화이트리스트
│   │       └── filters.py        # 레코드 필터
│   │
│   ├── structure/
│   │   ├── detector.py           # 결정론적 반복 구조 탐지
│   │   ├── reducer.py            # DOM 축약
│   │   └── fingerprint.py
│   │
│   ├── llm/
│   │   ├── gateway.py            # Protocol
│   │   ├── openai_compat.py      # Ollama / OpenAI / vLLM / ...
│   │   ├── anthropic.py
│   │   ├── routing.py
│   │   ├── manager.py            # pull / delete / catalog / bench
│   │   ├── hardware.py           # VRAM 감지
│   │   ├── prompts/
│   │   └── tasks/
│   │       ├── compile_spec.py
│   │       ├── adapt.py          # N-후보 + 채점 + 재시도
│   │       └── classify.py
│   │
│   ├── jobs/
│   │   ├── worker.py
│   │   ├── adapters.py           # run_collect / run_with_sink / run_to_sse
│   │   ├── sink.py               # 배치 DB 쓰기
│   │   ├── events.py
│   │   └── cancellation.py
│   │
│   ├── storage/
│   │   ├── blob.py               # 콘텐츠 주소 아카이브 (zstd)
│   │   ├── export.py
│   │   └── vault.py              # 크리덴셜 참조
│   │
│   ├── db/
│   │   ├── models.py
│   │   ├── session.py
│   │   └── bulk.py               # COPY 기반 배치 삽입
│   │
│   ├── config.py
│   └── main.py
│
├── recipes/                      # *.yaml — 원본, git 추적
├── migrations/
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── e2e/
│   └── fixtures/
│       ├── html/                 # 고정 HTML 샘플
│       └── malicious_server/     # SSRF/트랩 테스트용 로컬 서버
├── docker-compose.yml
├── Dockerfile
├── models.toml                   # 모델 카탈로그 (코드 밖)
└── pyproject.toml
```

## 철학

**필요한 시점에만 디렉터리를 만든다.**

패턴 이름만을 위해 `domain/ ports/ adapters/ repositories/ usecases/ managers/ factories/`를
무조건 생성하지 않는다. 실제 책임이 생겼을 때 도입한다.

위 구조도 Phase가 진행되며 채워지는 것이지, Phase 0에 전부 만들지 않는다.

## 핵심 원칙

- routers/cli thin, services orchestrate
- crawler는 API/ORM으로부터 순수
- infra는 작은 계약 뒤에
- composition > inheritance
- 중복 runner/engine 금지
