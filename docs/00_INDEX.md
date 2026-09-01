# CRWALLM Blueprint (Revised)

원본 설계 팩(`CRWALLM_Project_Blueprint`)을 실사용 조건에 맞춰 재검토한 개정판입니다.

## 확정된 전제

| 항목 | 결정 |
|---|---|
| 사용 형태 | **로컬 단일 사용자 도구.** UI만 웹 형식 차용 |
| 인증/멀티테넌트 | **없음.** workspace 개념 제거 |
| robots.txt | **미준수.** `Sitemap:` 지시어 파싱 용도로만 읽음 |
| Rate limit | **유지.** 예절이 아니라 차단 회피 = 처리량 최적화 수단 |
| 브레인(LLM) | **로컬 LLM(Ollama) + 클라우드 API** 두 가지 |
| MCP | 브레인에서 제외. 후기 phase에 선택적 export로만 |
| 목표 | 다양한 정형/반정형/비정형 데이터를 빠르고 많이 |
| 용도 | 개인/비상업 |

## 원본 대비 주요 변경

1. **브레인 플러거블화** — LLM은 특권 경로가 없다. 손으로 쓴 입력과 동일한 게이트를 통과한다
2. **결정론적 구조 분석기 신설** — LLM 이전에 코드로 반복 구조를 찾는다. LLM의 일을 "찾기"에서 "이름 붙이기"로 축소
3. **Spider 모드 신설** — 원본에는 트랩 방어/sitemap/프론티어 전략이 없었다
4. **레코드 레벨 필터 신설** — URL 필터만 있고 추출 후 필터가 없었다
5. **원본 아카이빙을 Phase 2로 승격** — 재크롤 없는 재추출이 개발 속도를 좌우한다
6. **비동기 job 경계를 Phase 2로 승격** — 원본은 Phase 6. 그때 가면 API/서비스/엔진을 동시에 뜯어야 한다
7. **성능 장(章) 신설** — selectolax, 배치 쓰기, DNS 피닝, uvloop 등
8. **CLI를 Phase 2로 편입** — 웹 UI 이전에도 수동 조작이 가능해야 한다

## 문서 구성

| 파일 | 내용 |
|---|---|
| `01_PRODUCT_OVERVIEW.md` | 목적, 핵심 흐름, 차별점 |
| `02_PRODUCT_MODEL.md` | 실행 모드, 4단계 조작 수준, 워크플로우 |
| `03_SYSTEM_ARCHITECTURE.md` | 계층, 의존성 방향, 모듈 경계 |
| `04_CRAWLING_ARCHITECTURE.md` | 엔진, Fetch, 엔진 인터페이스 계약 |
| `05_SPIDER_ARCHITECTURE.md` | 프론티어, 트랩 방어, sitemap, 스케줄링 |
| `06_EXTRACTION_ARCHITECTURE.md` | 정형/반정형/비정형 파이프라인, transform, 필터 |
| `07_RECIPE_ARCHITECTURE.md` | Recipe, 파일 동기화, 구조 지문, 드리프트 |
| `08_LLM_ARCHITECTURE.md` | ModelGateway, 라우팅, 모델 관리, 품질 기법 |
| `09_JOB_ARCHITECTURE.md` | Job, 이벤트, 워커, SSE, retry/resume |
| `10_DATA_MODEL.md` | 엔티티와 관계 |
| `11_SECURITY_MODEL.md` | SSRF, 로컬 API 보호, 시크릿, 인젝션 |
| `12_PERFORMANCE.md` | 처리량/품질/추론속도 최적화 |
| `13_API_PLAN.md` | REST + CLI |
| `14_FOLDER_STRUCTURE.md` | 모듈 구조 |
| `15_TECH_STACK.md` | 스택과 선택 이유 |
| `16_ROADMAP.md` | Phase별 구축 순서 |
| `17_NON_GOALS.md` | 하지 않을 것 |
