# 11. Folder Structure

권장 구조:

```text
backend/
├── src/
│   └── crwallm/
│       ├── api/
│       │   ├── app.py
│       │   ├── dependencies.py
│       │   └── routers/
│       │       ├── health.py
│       │       ├── specs.py
│       │       ├── recipes.py
│       │       └── jobs.py
│       │
│       ├── services/
│       │   ├── spec.py
│       │   ├── adaptation.py
│       │   ├── recipe.py
│       │   ├── job.py
│       │   └── export.py
│       │
│       ├── schemas/
│       │   ├── spec.py
│       │   ├── recipe.py
│       │   └── job.py
│       │
│       ├── policy/
│       │   └── validator.py
│       │
│       ├── crawler/
│       │   ├── engine.py
│       │   ├── types.py
│       │   ├── fetching/
│       │   │   ├── contracts.py
│       │   │   ├── http.py
│       │   │   ├── browser.py
│       │   │   └── coordinator.py
│       │   └── extraction/
│       │       ├── css.py
│       │       ├── jsonld.py
│       │       ├── metadata.py
│       │       └── xml.py
│       │
│       ├── llm/
│       │   ├── gateway.py
│       │   └── compiler.py
│       │
│       ├── recipes/
│       │   └── validation.py
│       │
│       ├── jobs/
│       │   ├── worker.py
│       │   ├── events.py
│       │   └── cancellation.py
│       │
│       ├── storage/
│       │   └── export.py
│       │
│       ├── db/
│       │   ├── models.py
│       │   └── session.py
│       │
│       ├── config.py
│       └── main.py
│
├── migrations/
├── tests/
│   ├── unit/
│   ├── integration/
│   └── e2e/
├── docs/
├── pyproject.toml
└── docker-compose.yml
```

## 철학

필요한 시점에만 디렉터리를 만든다.

피해야 할 것:

```text
domain/
ports/
adapters/
repositories/
usecases/
managers/
factories/
```

를 패턴 이름만을 위해 무조건 생성하는 것.

필요한 실제 책임이 생겼을 때 도입한다.

## 핵심 원칙

- routers thin
- services orchestrate
- crawler pure from API/ORM
- infra behind small contracts
- composition > inheritance
- no duplicate runner/engine
