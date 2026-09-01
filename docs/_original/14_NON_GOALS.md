# 14. Early Non-Goals

초기 구현에서 의도적으로 하지 않을 것.

## Infra

- Kubernetes
- Kafka
- RabbitMQ
- Redis queue
- Temporal
- Celery
- multi-region
- microservices

## AI

- 페이지마다 LLM extraction
- 복잡한 ModelRouter
- autonomous browser agent
- multi-agent orchestration platform

## Browser

- CAPTCHA 자동 해결
- anti-bot bypass 전문 기능
- arbitrary user-generated JS 실행
- stealth/anti-detection을 핵심 기능으로 삼기

## Extraction

- 모든 extractor 종류를 처음부터 구현
- arbitrary Python transform
- arbitrary JavaScript transform

## Product

- frontend-first 개발
- 너무 이른 billing/multi-tenant
- Recipe semantic marketplace
- plugin ecosystem

핵심 순서는 항상:

```text
working deterministic crawler
→ AI compiler/adaptation
→ reusable recipes
→ durable runtime
→ broader capabilities
```
