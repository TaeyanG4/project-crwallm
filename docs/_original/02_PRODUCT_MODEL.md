# 2. Product Model

## 주요 실행 모드

### Collect

명확한 사이트/범위에서 데이터를 수집한다.

예:

- 쇼핑몰 상품 수집
- 뉴스 기사 목록 수집
- 채용 공고 수집
- 특정 사이트의 문서 메타데이터 수집

초기 MVP의 핵심 모드.

### Research

여러 페이지/사이트를 읽으며 질문에 필요한 정보를 충분히 모은다.

종료 기준:

- relevance
- sufficiency
- saturation
- page/time/token/cost budget

### Explore

특정 주제와 관련된 새로운 사이트/소스를 탐색한다.

예:

> "한국에서 AI 반도체 스타트업 목록을 찾아."

Collect와 달리 초기 domain set이 완전히 알려져 있지 않을 수 있다.

### Interact

로그인, 버튼 클릭, 폼 등 명시적인 browser interaction이 필요한 작업.

초기 버전에서는 후순위.

---

## 핵심 사용자 워크플로우

### A. 이미 구조를 아는 사용자

```text
Natural language
→ CrawlSpec compile
→ Review
→ Run
```

### B. selector를 모르는 사용자

```text
Natural language
→ Sample page fetch
→ Site Adaptation
→ Preview
→ Recipe
→ Run
```

### C. 기존 Recipe 재사용

```text
Recipe
→ New CrawlSpec
→ Run
```

LLM 불필요.

### D. 사이트 구조 변경

```text
Recipe fails
→ Drift Detection
→ Representative failed samples
→ LLM proposes new Recipe
→ Regression validation
→ Promote new version
```
