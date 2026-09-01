# 15. Tech Stack

## Runtime

**Python 3.12** — scraping/data 생태계, asyncio, LLM 생태계, 브라우저 자동화, FastAPI/Pydantic

**uv** — 의존성/가상환경 관리

**실행 환경: Linux 컨테이너** — uvloop이 Windows를 지원하지 않는다.
개발은 Windows, 실행은 docker-compose. → `12_PERFORMANCE.md`

## Backend

| 패키지 | 용도 |
|---|---|
| FastAPI | REST + SSE |
| typer | CLI |
| Pydantic v2 | CrawlSpec / Recipe / LLM structured output 검증 |
| SQLAlchemy 2 (async) | ORM |
| asyncpg | 드라이버 + `COPY` 배치 삽입 |
| PostgreSQL 16 | 단일 source of truth. specs/recipes/jobs/events/results/records/frontier/queue |
| Alembic | 마이그레이션 |
| uvloop | 이벤트 루프 (Linux) |

## Crawling

| 패키지 | 용도 |
|---|---|
| httpx | HTTP dataplane. HTTP/2, 커스텀 transport(IP 피닝) |
| **selectolax** | **주력 파서.** lexbor 엔진. bs4 대비 5~15배 |
| lxml | XPath가 필요할 때만 |
| Playwright | 브라우저 (direct). Crawl4AI는 route interception 제어권 때문에 미채택 |
| tenacity | 재시도/백오프 |
| brotli, h2 | 압축, HTTP/2 |
| rbloom | 대규모 URL 중복 필터 |
| zstandard | 원본 아카이브 압축 |
| publicsuffix2 | registrable domain 판정 |

## Extraction

| 패키지 | 용도 |
|---|---|
| trafilatura | 비정형 본문 텍스트 추출 |
| Docling | PDF/Office (후기) |

## LLM

| 항목 | 선택 |
|---|---|
| 로컬 런타임 | **Ollama** — 모델 pull/delete/list가 일급 API인 유일한 선택지 |
| 클라이언트 | OpenAI 호환 (httpx). Ollama/vLLM/LM Studio/OpenAI/Groq를 동일 코드로 커버 |
| 별도 구현 | Anthropic (tool-use 기반 structured output) |
| 기본 모델 | **Qwen3 14B Q4_K_M** (~9GB) |
| 임베딩 | **bge-m3** (~1.2GB) — 한국어 포함 다국어 |
| 하드웨어 감지 | nvidia-ml-py (pynvml) |
| 카탈로그 | `models.toml` — 코드 밖 |

## RAG (후기)

pgvector — 기존 PostgreSQL에 확장만 활성화. 새 인프라 없음.

## Frontend (후기)

Next.js / TypeScript / shadcn-ui / TanStack Table

**백엔드 contract가 안정되기 전에 frontend를 먼저 크게 만들지 않는다.**
CLI가 그때까지 UI 역할을 한다.

## 채택하지 않은 것

| | 이유 |
|---|---|
| BeautifulSoup (주력) | 래퍼 오버헤드. selectolax로 대체 |
| Crawl4AI | 보안 요구(subresource 차단, private network 차단)에 필요한 route interception 제어권이 불명확 |
| Redis / Celery / Kafka | PostgreSQL 큐로 충분. 측정 후 재검토 |
| MCP / FastMCP | 브레인에서 제외. 후기 phase에 선택적 export로만 |
| LangChain 계열 | 이 프로젝트의 LLM 사용은 structured output 호출 몇 개뿐. 프레임워크 불필요 |
