# 12. Tech Stack

## Backend

### Python 3.12

이유:

- scraping/data ecosystem
- asyncio
- LLM ecosystem
- browser automation ecosystem
- FastAPI/Pydantic

### FastAPI

REST + SSE API.

### Pydantic v2

CrawlSpec/Recipe/LLM structured output validation.

### SQLAlchemy 2 async

PostgreSQL persistence.

### PostgreSQL 16

초기 단일 source of truth.

용도:

- specs
- recipes
- jobs
- events
- results
- records
- worker queue state

### Alembic

schema migrations.

## Crawling

### httpx

기본 HTTP dataplane.

### BeautifulSoup / lxml

DOM parsing.

### Browser

후기 단계:

- Crawl4AI adapter 또는
- Playwright direct adapter

중요한 것은 core contract와 격리하는 것.

## LLM

초기:

OpenAI-compatible gateway 하나.

이후 필요 시:

- Anthropic
- Gemini
- Ollama
- vLLM
- LiteLLM

## Document parsing

Docling은 PDF/office/document 단계에서 추가.

## Frontend

후기:

- Next.js
- TypeScript
- Vercel AI SDK
- assistant-ui
- shadcn/ui
- TanStack Table

백엔드 contract가 안정되기 전에 frontend를 먼저 크게 만들지 않는다.
