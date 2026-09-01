# 5. LLM Architecture

## 기본 원칙

LLM을 crawling dataplane으로 사용하지 않는다.

```text
LLM = planning / compilation / adaptation / repair
Crawler = deterministic execution
```

## ModelGateway

Application에서는 다음 정도의 contract만 본다.

```text
ModelGateway
  generate_structured(...)
```

구현은 나중에 교체 가능:

- OpenAI API
- Anthropic API
- Gemini
- Ollama
- vLLM
- OpenAI-compatible local endpoint
- LiteLLM gateway

초기에는 한 가지 implementation이면 충분하다.

## Natural Language Compiler

```text
Prompt
 ↓
ModelGateway
 ↓
Structured candidate
 ↓
Pydantic validation
 ↓
Policy validation
 ↓
CrawlSpec
```

LLM은 절대로 최종 authority가 아니다.

예:

LLM이 다음을 출력해도:

```json
{
  "max_pages": 10000000,
  "seed_urls": ["file:///etc/passwd"]
}
```

Pydantic + Policy에서 차단.

## Site Adaptation

LLM에게 전달할 데이터:

- user intent
- bounded cleaned DOM
- field descriptions

LLM 출력:

- extraction field candidate
- container selector
- optional pagination/discovery hints later

LLM이 생성하면 안 되는 것:

- arbitrary code
- shell commands
- server credentials
- expanded network scope

## 비용 전략

한 사이트에 대한 adaptation 비용과 bulk crawl 비용을 분리한다.

```text
Adaptation:
LLM required

Bulk crawl:
LLM ideally 0
```
