# CRWALLM Project Blueprint

이 문서는 CRWALLM 프로젝트를 처음부터 재구축하기 위한 **제품 정의, 목적, 아키텍처, 데이터 모델, 보안 원칙, 단계별 개발 계획**을 정리한 설계 팩입니다.

특정 AI 도구의 사용법이나 작업 절차는 포함하지 않습니다.

## 문서 구성

- `01_PRODUCT_OVERVIEW.md` — 프로젝트의 목적과 핵심 가치
- `02_PRODUCT_MODEL.md` — 사용자 경험과 주요 실행 모드
- `03_SYSTEM_ARCHITECTURE.md` — 전체 시스템 구조와 책임 분리
- `04_CRAWLING_ARCHITECTURE.md` — 크롤링 엔진, Fetch, Extraction 구조
- `05_LLM_ARCHITECTURE.md` — LLM의 역할과 사용 범위
- `06_RECIPE_ARCHITECTURE.md` — Site Adaptation과 Recipe 시스템
- `07_JOB_ARCHITECTURE.md` — 비동기 작업, 이벤트, 재시도/복구 구조
- `08_DATA_MODEL.md` — 주요 엔티티와 관계
- `09_SECURITY_MODEL.md` — SSRF, 브라우저, 범위 제한, 비밀정보 정책
- `10_API_PLAN.md` — 권장 API 구조
- `11_FOLDER_STRUCTURE.md` — 권장 모듈/폴더 구조
- `12_TECH_STACK.md` — 기술 스택과 선택 이유
- `13_ROADMAP.md` — 단계별 구축 순서
- `14_NON_GOALS.md` — 초기에 하지 않을 것
