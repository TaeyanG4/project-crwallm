# 1. Product Overview

## 프로젝트명

CRWALLM

## 한 줄 정의

**자연어 요청을 안전하고 재사용 가능한 크롤링 계획으로 컴파일하고, 정적/동적 웹사이트에서 구조화 데이터를 수집하는 범용 AI 크롤러 플랫폼.**

## 핵심 흐름

```text
Natural Language
    ↓
Intent / CrawlSpec
    ↓
Policy Validation
    ↓
Site Discovery / Adaptation
    ↓
Reusable Recipe
    ↓
Deterministic Crawling
    ↓
Extraction
    ↓
Validation / Evidence
    ↓
Storage / Export
```

## 해결하려는 문제

기존 크롤러는 보통 사용자가 직접 다음을 알아야 한다.

- URL 구조
- CSS selector / XPath
- 페이지네이션
- 정적 페이지인지 JS 페이지인지
- 요청/리다이렉트 처리
- 데이터 저장 구조
- 사이트 변경 시 selector 수정

CRWALLM은 사용자가 다음처럼 자연어로 목적만 설명할 수 있게 한다.

> "이 쇼핑몰의 노트북 상품명, 가격, 링크를 최대 500개 수집해."

시스템은 이를 실제 실행 가능한 CrawlSpec과 Recipe로 변환하고, 이후 동일한 구조의 페이지는 LLM 없이 결정론적으로 반복 실행한다.

## 핵심 차별점

### 1. LLM은 crawler가 아니라 compiler/control plane

LLM이 페이지마다 데이터를 읽는 구조를 피한다.

LLM의 핵심 역할:

- 자연어 → CrawlSpec
- 처음 보는 사이트의 DOM 분석
- Extraction Recipe 후보 생성
- Recipe drift 발생 시 수정 후보 생성
- Research/Explore 모드의 계획 수립

실제 대량 페이지 수집은 가능한 한 deterministic하게 수행한다.

### 2. Recipe reuse

한 사이트를 한 번 이해하면:

```text
LLM Adaptation 1회
      ↓
Recipe 생성
      ↓
100 / 1,000 / 100,000 pages
      ↓
LLM 호출 없이 반복
```

### 3. 안전한 네트워크 정책

LLM 출력보다 시스템 Policy가 항상 우선한다.

```text
System Policy
  >
Workspace / Server Policy
  >
User Request
  >
LLM Suggestion
```

LLM은 crawling scope를 임의로 넓힐 수 없다.
