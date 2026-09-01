# 3. System Architecture

## 기본 방향

초기에는 **Modular Monolith**로 구축한다.

마이크로서비스는 처음부터 도입하지 않는다.

```text
┌────────────────────────────────────────────┐
│                 API Layer                  │
│ FastAPI / REST / SSE                       │
└─────────────────────┬──────────────────────┘
                      ↓
┌────────────────────────────────────────────┐
│             Application Services           │
│ Spec / Adaptation / Recipe / Job / Export  │
└─────────────────────┬──────────────────────┘
                      ↓
┌────────────────────────────────────────────┐
│                  Core                      │
│ Policy / Crawl Engine / Extraction /       │
│ Job Runtime / Event Contracts              │
└─────────────────────┬──────────────────────┘
                      ↓
┌────────────────────────────────────────────┐
│          Infrastructure Adapters           │
│ HTTP / Browser / LLM / PostgreSQL / Files  │
└────────────────────────────────────────────┘
```

## 의존성 방향

```text
API
 ↓
Services
 ↓
Core Contracts / Engine
 ↓
Infrastructure Adapters
```

중요:

- FastAPI가 crawler core 안으로 들어가지 않는다.
- SQLAlchemy ORM이 CrawlEngine 결과 타입이 되지 않는다.
- Crawl4AI/Playwright 타입이 core에 노출되지 않는다.
- OpenAI/Gemini/Ollama vendor SDK가 application service에 직접 노출되지 않는다.

## 모듈 경계

### API

책임:

- HTTP request/response
- authentication later
- response code
- service 호출
- SSE 연결

비책임:

- crawling loop
- DOM parsing
- DB orchestration의 복잡한 business rule

### Services

예:

- SpecService
- AdaptationService
- RecipeService
- JobService
- ExportService

Use case orchestration 담당.

### Core

- CrawlEngine
- Policy
- Extractor contracts
- CrawlSpec
- Job state machine
- Event contract

### Infrastructure

- SafeHttpFetcher
- BrowserFetcher
- ModelGateway implementation
- PostgreSQL
- object/file storage
