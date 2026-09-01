# CRWALLM

자연어 또는 직접 조작으로 크롤링 계획을 만들고, 정적/동적 웹에서
정형·반정형·비정형 데이터를 대량 수집하는 **로컬 AI 크롤러**.

```text
자연어 요청  ──┐
               ├→ CrawlSpec / Recipe → Pydantic → Policy → 크롤러 → 추출 → 필터 → 저장
직접 작성    ──┘
```

두 입력 경로가 동일한 산출물을 만들고 동일한 게이트를 통과합니다.
**LLM에게 특권 경로는 없습니다** — 편의 레이어이지 필수 의존이 아닙니다.

## 특징

- **LLM = 컨트롤 플레인, 크롤러 = 결정론적 실행** — 대량 수집에 LLM을 태우지 않음
- **결정론 우선** — 코드로 반복 구조를 먼저 찾고, 안 되면 LLM
- **Recipe 재사용** — 사이트 1회 이해 → 10만 페이지를 LLM 0회로
- **로컬 완결** — Ollama 로컬 LLM. 데이터가 기기 밖으로 나가지 않음. GPU가 부족하면 클라우드 API 또는 수동 조작
- **Collect + Spider** — 정밀 타겟 수집과 광역 탐색 모두 지원

## 문서

설계 전체는 [docs/00_INDEX.md](docs/00_INDEX.md)에 있습니다.

| | |
|---|---|
| [01 Product Overview](docs/01_PRODUCT_OVERVIEW.md) | 목적, 핵심 흐름, 차별점 |
| [02 Product Model](docs/02_PRODUCT_MODEL.md) | 실행 모드, 4단계 조작 수준 |
| [03 System Architecture](docs/03_SYSTEM_ARCHITECTURE.md) | 계층, 의존성 방향 |
| [04 Crawling](docs/04_CRAWLING_ARCHITECTURE.md) | 엔진 인터페이스, Fetch |
| [05 Spider](docs/05_SPIDER_ARCHITECTURE.md) | 프론티어, 트랩 방어, sitemap |
| [06 Extraction](docs/06_EXTRACTION_ARCHITECTURE.md) | 정형/반정형/비정형, transform, 필터 |
| [07 Recipe](docs/07_RECIPE_ARCHITECTURE.md) | Recipe, 구조 지문, 드리프트 |
| [08 LLM](docs/08_LLM_ARCHITECTURE.md) | ModelGateway, 모델 관리, 품질 기법 |
| [09 Job](docs/09_JOB_ARCHITECTURE.md) | Job, 이벤트, 워커 |
| [10 Data Model](docs/10_DATA_MODEL.md) | 엔티티 |
| [11 Security](docs/11_SECURITY_MODEL.md) | SSRF, 로컬 API 보호, 시크릿 |
| [12 Performance](docs/12_PERFORMANCE.md) | 처리량/품질/추론속도 |
| [13 API & CLI](docs/13_API_PLAN.md) | REST + CLI |
| [14 Folder Structure](docs/14_FOLDER_STRUCTURE.md) | 모듈 구조 |
| [15 Tech Stack](docs/15_TECH_STACK.md) | 스택 |
| [16 Roadmap](docs/16_ROADMAP.md) | Phase별 순서 |
| [17 Non-Goals](docs/17_NON_GOALS.md) | 하지 않을 것 |

## 시작하기

[DEVELOPMENT.md](DEVELOPMENT.md) 참조.

```bash
uv venv --python 3.12 && uv pip install -e ".[dev]" && cp .env.example .env
```

```bash
docker compose up -d db && alembic upgrade head && crwallm serve
```

## 상태

**Phase 0 — Foundation 완료.**
**Phase 1 — Contracts & Policy 완료.** 다음은 Phase 2 (Deterministic Crawl MVP).
→ [로드맵](docs/16_ROADMAP.md)

## 전제

로컬 단일 사용자 도구. 개인·비상업 용도.
인증/멀티테넌트 없음. `robots.txt` 규칙 미준수(`Sitemap:` 지시어만 활용).
