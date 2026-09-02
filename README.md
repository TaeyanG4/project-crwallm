# CRWALLM

정적·동적 웹에서 정형·반정형·비정형 데이터를 모으는 **로컬 AI 크롤러**.
대화로 시키거나, 직접 조작하거나, 둘을 섞어 쓸 수 있습니다.

```text
대화       ──┐
             ├→ CrawlSpec / Recipe → Policy → 크롤러 → 추출 → 필터 → 저장
직접 작성  ──┘
```

두 입력이 **같은 산출물**을 만들고 **같은 게이트**를 통과합니다.
LLM에게 특권 경로는 없습니다 — 편의 레이어이지 필수 의존이 아닙니다.

## 왜 이렇게 만들었나

**LLM은 컨트롤 플레인, 크롤러는 결정론적 실행.**
사이트를 한 번 이해해서 레시피를 만들고, 그 뒤 10만 페이지는 모델 호출
0회로 돌립니다. 대량 수집에 LLM을 태우면 느리고 비싸고 재현되지 않습니다.

**결정론 우선.** 반복 구조는 코드가 먼저 찾습니다. 모델은 찾아낸 컬럼에
*이름을 붙일 뿐*이고 셀렉터는 쓰지 않습니다 — 셀렉터는 한 글자만 틀려도
0건이 나오는 일이라 모델에게 맡길 종류가 아닙니다.

**로컬 완결.** Ollama 로컬 모델. 데이터가 기기 밖으로 나가지 않습니다.
GPU가 부족하면 클라우드 API로 바꾸거나, 모델 없이 수동으로도 씁니다.

**브라우저는 최후 수단.** HTTP보다 20~50배 비쌉니다. `auto` 모드는 HTTP로
먼저 시도하고 **레코드가 0건일 때만** 렌더합니다.

## 설치

```bash
git clone https://github.com/TaeyanG4/project-crwallm.git
cd project-crwallm
uv sync
```

이게 전부입니다. `uv sync`가 가상환경까지 만듭니다 — `uv venv`를 따로 부르지
마세요. 이미 `.venv`가 있으면 그것을 맞춰 갱신합니다.

필요한 것: Python 3.12와 [uv](https://docs.astral.sh/uv/). 끝입니다.
**Docker도 Node도 모델도 필요 없습니다.**

### 나중에, 필요해지면

| 하고 싶은 것 | 그때 필요한 것 | 준비 |
|---|---|---|
| 페이지 모으고 엑셀로 저장 | — | 없음 |
| 컬럼 이름 자동으로 붙이기 | Ollama | `crwallm setup --no-browser` |
| 스크립트로 그려지는 페이지 | Chromium | `crwallm setup --no-llm` |
| 잡 이력·재시도·웹 UI | Docker, Node 20+ | `crwallm setup` |

`setup`은 **부를 때만** 돕니다. 아무것도 부르지 않으면 아무것도 설치되지
않고, 창은 그대로 열립니다.

### `crwallm`을 어떻게 부르나

명령은 가상환경 안에 설치되므로 그냥 `crwallm`을 치면 **찾지 못합니다.**
둘 중 하나를 쓰세요.

```bash
uv run crwallm <명령>              # 활성화 없이. 어디서나 동작
```

```bash
source .venv/Scripts/activate      # Windows (Git Bash) — 한 번만
source .venv/bin/activate          # macOS / Linux
crwallm <명령>
```

아래 예제는 `crwallm`으로 적었습니다. 활성화하지 않았다면 앞에 `uv run`을
붙이세요.

## 바로 써보기

**`crwallm.bat`을 더블클릭하세요.** 창이 하나 뜹니다. 그게 전부입니다.
바탕화면에 두려면 우클릭 → 바로 가기 만들기.

첫 실행만 설치하느라 1분쯤 걸리고, 그 다음부터는 바로 열립니다.

창에서 하는 일은 세 단계입니다.

```text
①  주소를 붙여넣고  [ 살펴보기 ]
②  페이지에 있는 것들이 예시와 함께 나옵니다 → 모으고 싶은 것에 이름을 붙이고  [ 모으기 ]
③  표가 나오면  [ 엑셀로 저장 ]
```

셀렉터를 쓰지 않고, 모델을 부르지 않고, Docker를 켜지 않습니다.
터미널에서 같은 창을 열려면 `crwallm desktop`.

## 터미널에서 30초

```bash
crwallm inspect https://quotes.toscrape.com/
```

페이지에서 반복되는 구조와 컬럼을, 인덱스와 함께 보여줍니다.
그 인덱스가 다음 명령의 입력입니다.

```bash
crwallm recipe adapt quotes --url https://quotes.toscrape.com/
crwallm recipe activate quotes
crwallm crawl https://quotes.toscrape.com/ --recipe quotes --follow --max-pages 50
```

모델이 컬럼 이름을 붙이고(2초 남짓), 점수를 매기고, 레시피를 저장합니다.
그 뒤 크롤은 모델을 부르지 않습니다.

## 쓰는 방법

**[USAGE.md](USAGE.md)에 전체 사용법이 있습니다** — 4가지 조작 수준,
레시피 소스 7종, 필터, 브라우저 모드, 잡 운영, 내보내기.

짧게:

| 하고 싶은 것 | | Docker |
|---|---|---|
| 창 열기 | `crwallm desktop` (= `crwallm.bat`) | — |
| 페이지 구조 보기 | `crwallm inspect <url>` | — |
| 레시피 만들기 | `crwallm recipe adapt <name> --url <url>` | — |
| 한 번 돌려보기 | `crwallm crawl <url> --recipe <name>` | — |
| 사이트 전체 훑기 | `crwallm spider <url> --recipe <name>` | — |
| 백그라운드로 돌리기 | `crwallm jobs submit ...` + `crwallm worker` | 필요 |
| 데이터 꺼내기 | `crwallm jobs export <id> -f csv -o out.csv` | 필요 |
| 웹 UI | `crwallm up` | 필요 |

Docker가 필요한 줄은 셋뿐입니다. 이력을 남기는 일 — 잡 큐, 재시도,
나중에 다시 꺼내보기 — 만 데이터베이스를 씁니다. 나머지는 켜지 않습니다.

## 웹 UI

창으로 되는 일이면 창을 쓰세요. 웹 UI는 창에 없는 것 — 잡 이력, 재시도,
여러 크롤 동시 운영 — 이 필요할 때만입니다.

```bash
crwallm up
```

세 프로세스가 필요합니다 — API, 잡을 실제로 돌리는 워커, UI. `up`이 셋을
함께 띄우고, 여기서 처음으로 Docker를 켭니다. **워커 없이는 UI가 잡을 큐에
넣고 아무 일도 일어나지 않는데, 화면에는 그 이유가 나오지 않습니다.**

따로 띄우려면:

```bash
crwallm serve                      # API (127.0.0.1:8000)
crwallm worker                     # 워커
npm run dev --prefix web           # UI (localhost:3000)
```

세 화면입니다.

- **대화** — "이 URL에서 제목이랑 가격 뽑아줘". 모델이 페이지를 살펴보고,
  레시피를 만들고, 크롤을 큐에 넣습니다. 각 단계가 카드로 보입니다.
- **크롤** — 실행 목록과 상세. 이벤트가 라이브로 흐르고, 중지·재실행·
  CSV/JSONL 내보내기가 있습니다.
- **레시피** — 무엇이 있고 얼마나 잘 동작하는지. `active`는 주장이고
  옆의 측정치가 그 근거입니다.

## 상태

**마일스톤 1~2 완료 (Phase 0~8).**

| Phase | |
|---|---|
| 0~2 | 기반, 계약·정책, 결정론적 크롤 |
| 3~4 | 구조 탐지·레시피, 로컬 LLM |
| 5 | Spider (sitemap, 트랩 방어, 중복 제거) |
| 6 | 추출 확장 (JSON-LD·microdata·임베디드 JSON·피드·테이블·본문·API 발견·semantic 필터) |
| 7 | 브라우저 (Playwright, 결과 기반 auto 폴백, 무한 스크롤) |
| 8 | 내구성 (취소·stale 복구·재시도·내보내기) |

다음은 Phase 9(로그인/인터랙션) 이후 — [로드맵](docs/16_ROADMAP.md).
**측정으로 필요성이 확인될 때만** 추가한다는 것이 이 프로젝트의 규칙입니다.

## 전제

로컬 단일 사용자 도구. 개인·비상업 용도.
인증/멀티테넌트 없음. **`robots.txt` 규칙은 따르지 않습니다**
(`Sitemap:` 지시어만 읽습니다). rate limit은 지킵니다.

## 문서

설계 전체는 [docs/00_INDEX.md](docs/00_INDEX.md).

| | |
|---|---|
| [01 Product Overview](docs/01_PRODUCT_OVERVIEW.md) | 목적, 핵심 흐름, 차별점 |
| [02 Product Model](docs/02_PRODUCT_MODEL.md) | 실행 모드, 4단계 조작 수준 |
| [03 System Architecture](docs/03_SYSTEM_ARCHITECTURE.md) | 계층, 의존성 방향 |
| [04 Crawling](docs/04_CRAWLING_ARCHITECTURE.md) | 엔진 인터페이스, Fetch, 브라우저 |
| [05 Spider](docs/05_SPIDER_ARCHITECTURE.md) | 프론티어, 트랩 방어, sitemap |
| [06 Extraction](docs/06_EXTRACTION_ARCHITECTURE.md) | 정형/반정형/비정형, transform, 필터 |
| [07 Recipe](docs/07_RECIPE_ARCHITECTURE.md) | Recipe, 구조 지문, 드리프트 |
| [08 LLM](docs/08_LLM_ARCHITECTURE.md) | ModelGateway, 모델 관리, 품질 기법 |
| [09 Job](docs/09_JOB_ARCHITECTURE.md) | Job, 이벤트, 워커, 내구성 |
| [10 Data Model](docs/10_DATA_MODEL.md) | 엔티티 |
| [11 Security](docs/11_SECURITY_MODEL.md) | SSRF, 로컬 API 보호, 시크릿 |
| [12 Performance](docs/12_PERFORMANCE.md) | 처리량/품질/추론속도 |
| [13 API & CLI](docs/13_API_PLAN.md) | REST + CLI |
| [14 Folder Structure](docs/14_FOLDER_STRUCTURE.md) | 모듈 구조 |
| [15 Tech Stack](docs/15_TECH_STACK.md) | 스택 |
| [16 Roadmap](docs/16_ROADMAP.md) | Phase별 순서와 확정된 결정 |
| [17 Non-Goals](docs/17_NON_GOALS.md) | 하지 않을 것 |

개발은 [DEVELOPMENT.md](DEVELOPMENT.md).
