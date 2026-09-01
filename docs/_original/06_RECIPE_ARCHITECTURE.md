# 6. Recipe Architecture

## Recipe 정의

사이트 구조를 재사용하기 위한 선언형 extraction plan.

예:

```json
{
  "source_url": "https://example.com/products",
  "allowed_domains": ["example.com"],
  "fetch_mode": "http",
  "container_selector": ".product-card",
  "fields": [
    {
      "name": "title",
      "selector": "h2",
      "extract_type": "text"
    },
    {
      "name": "url",
      "selector": "a.detail",
      "extract_type": "href"
    }
  ]
}
```

## Recipe 상태

```text
candidate
active
deprecated
```

## 생성

```text
Sample page
→ DOM reduction
→ LLM candidate
→ syntax validation
→ execute against sample
→ preview
→ candidate
```

## Activation

LLM을 사용하지 않는다.

```text
Recipe candidate
→ Fetch source/sample
→ Run deterministic extractor
→ Validate output
→ active
```

## Reuse

```text
active Recipe
→ CrawlSpec
→ CrawlJob
```

Recipe가 system-of-record로 결정해야 하는 값:

- extraction rules
- compatible domain scope
- preferred fetch mode

사용자가 재사용 과정에서 이를 임의 override하지 못하게 한다.

## Versioning

향후:

```text
Recipe v1
Recipe v2
Recipe v3
```

CrawlSpec/Job/Record는 어느 Recipe version을 사용했는지 provenance를 남기는 것이 좋다.

## Drift Detection

후기 단계.

예:

- selector match rate 급락
- record count 급락
- required field missing rate 증가
- parse error 증가

Repair는 즉시 production Recipe를 덮어쓰지 않는다.

```text
Drift
→ candidate repair
→ regression
→ canary
→ promote
```
