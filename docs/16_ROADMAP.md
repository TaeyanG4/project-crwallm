# 16. Roadmap

## Phase 0 — Foundation  ✅

- Python 3.12 + uv, pyproject
- FastAPI 골격, `/health`
- PostgreSQL 16 + SQLAlchemy 2 async + Alembic
- **Linux 컨테이너 실행 환경 (docker-compose) + uvloop**
- **로컬 API 보호** — 127.0.0.1 바인딩, Host 화이트리스트, CORS 고정, 토큰 헤더
- ruff + mypy strict, pytest, CI
- 테스트 픽스처 뼈대 (`tests/fixtures/html`, `malicious_server`)

목표: 깨끗한 설치/테스트 기반.

---

## Phase 1 — Contracts & Policy  ✅

- `CrawlSpec` / `Recipe` / `CrawlEvent` Pydantic 계약 확정
- **엔진 인터페이스 확정** — `async def crawl(spec) -> AsyncIterator[CrawlEvent]`
- **URL 정규화 완전판** — 추적 파라미터 제거, 파라미터 정렬/화이트리스트, fragment/포트/슬래시, canonical
- **SSRF + DNS 피닝** — 해석한 IP 검증 후 그 IP로 연결 (TOCTOU 해결 + 성능)
- Public Suffix List 기반 도메인 스코프
- `HostPolicy` — 적응형 동시성(AIMD), 백오프, `Retry-After`, UA. `respect_robots` 필드는 자리만
- 악성 로컬 서버 테스트 (127.0.0.1, 169.254.169.254, DNS rebinding, 무한 리다이렉트, 거대 응답)

### Phase 1에서 확정된 것

| 결정 | 근거 |
|---|---|
| URL을 **두 값**으로 (`url` / `dedupe_key`) | 하나로 합치면 공격적 dedupe가 fetch를 깨거나, 보수적 정규화가 프론티어를 폭발시킨다 |
| DNS 응답 중 **하나라도** 내부면 호스트 전체 거부 | 브라우저 fetcher는 레코드를 고를 수 없어 피닝이 불가능하다 |
| `Resolver`를 주입 | DNS rebinding은 실제 리졸버로 테스트할 수 없다. 테스트 가능성이 설계를 결정했다 |
| 8자 이상 식별자 세그먼트는 **하나의** placeholder | 숫자 세션 ID와 hex 세션 ID가 다른 예산을 받으면 트랩이 살아남는다 |
| 어댑터가 `aclosing`을 강제 | `break`는 async generator를 닫지 않는다. 크롤이 소비자보다 오래 산다 |
| 이벤트에 `job_id` 없음 | job은 서비스 계층 개념. 엔진에 넣으면 core가 영속화를 알게 된다 |
| tldextract `include_psl_private_domains=True` | `github.io`를 scope로 지정해 전체를 크롤하는 것을 막는다 |

---

## Phase 2 — Deterministic Crawl MVP  ✅

- Safe HTTP fetcher — 스트리밍, byte limit, 수동 리다이렉트 재검증, 조건부 요청
- BFS traversal, dedupe
- **selectolax CSS 추출**
- **트랩 가드** — URL 길이, 경로 깊이, 반복 세그먼트, 쿼리 화이트리스트, **패턴별 예산**
- **원본 아카이빙** — 콘텐츠 주소 + zstd
- **에러 택소노미** — enum + 집계
- **얇은 job 껍데기 + 별도 워커 프로세스** (queued/running/completed, 폴링)
- **배치 DB 쓰기** (COPY)
- **CLI** — inspect / crawl / job / results
- API E2E

**이 시점에서 실제 usable crawler가 처음 완성된다.**

### Phase 2에서 확정된 것

| 결정 | 근거 |
|---|---|
| 피닝을 **네트워크 백엔드**에서 (URL 재작성 아님) | httpcore 커넥션 풀은 `(scheme, host, port)`로만 매칭하고 SNI는 키가 아니다. URL을 IP로 바꾸면 같은 IP의 두 호스트가 커넥션을 공유해 두 번째가 첫 번째로 검증된 TLS를 탄다 |
| 핀 없는 호스트는 **연결 거부** | 게이트 우회가 "깜빡함"이 아니라 "코드 삭제"를 요구하게 된다 |
| `Accept-Encoding`을 **디코딩 가능한 것에서 유도** | 하드코딩하면 의존성 하나가 빠졌을 때 압축 바이트를 HTML로 저장하고 조용히 통과한다 (실제로 brotli에서 발생) |
| 게이트 2단계 — `check_enqueue` / `admit_fetch` | 각 검사가 URL당 정확히 한 번. 예산을 두 번 차감하면 모든 예산이 절반이 된다 |
| SSRF를 게이트 **마지막**에 | 목록에서 유일하게 DNS를 타는 검사. 스코프 밖 링크를 위해 해석하면 스파이더가 DNS에 지배된다 |
| 종료 조건 = 큐 비었고 **진행 중 0** | 페이지를 들고 있는 워커가 링크 100개를 막 발견하려던 참일 수 있다 |
| 아카이브를 **추출 전에** 무조건 | 추출기는 Phase마다 바뀌고 바이트는 안 바뀐다 |
| IP 리터럴을 스코프로 허용 | PSL 규칙은 "TLD 전체로 범위가 열리는 것"을 막는 것. IP는 그 반대로 최대한 구체적이다 |

---

## Phase 3 — Structure & Recipe (LLM 없음)  ✅

- **결정론적 반복 구조 탐지** — 형제 그룹 시그니처, 텍스트 밀도, 컬럼 전개
- **DOM 축약기** — 2~4k 토큰 목표
- **구조 지문(fingerprint)**
- Recipe 스키마 + `recipes/*.yaml` ↔ DB 동기화
- **transform 화이트리스트**
- **레코드 필터** (결정론적 연산자)
- **품질 지표** — record_count, fill_rate, match_rate, consistency → activation 게이트
- 응답 캐시(아카이브 재사용)로 개발 루프 가속

**수준 0~1이 여기서 완성된다. LLM 없이 쓸 수 있는 도구가 된다.**

```bash
crwallm inspect <url>                                  # 무엇이 반복되는가
crwallm recipe init laptops --url <url> --pick title=0,price=2
crwallm recipe test laptops                            # 레코드 수 + 점수
crwallm recipe activate laptops
crwallm crawl <url> --recipe laptops --follow
```

### Phase 3에서 확정된 것

| 결정 | 근거 |
|---|---|
| 컬럼 값을 **selector로 되읽기** | 클래스가 필터링되면 `span.price`가 `span`이 되고, 그건 컨테이너의 *첫* span을 고른다. 샘플과 실제 추출값이 갈리면 그 샘플은 거짓말이다 |
| 레이아웃 클래스 제거, 단 **파라미터가 붙은 것만** | `p-2`는 패딩이지만 맨 `p`는 남의 클래스명일 수 있다. 과도 필터링은 selector 특정성을 잃는다 |
| 래퍼 컬럼 제거 | `div.card-body` 텍스트는 카드 전체다. 구조상 컬럼이고 의미상 아무것도 아니며, 언뜻 그럴듯해 보여서 더 나쁘다 |
| 값이 같은 컬럼 병합, **얕은 쪽 채택** | `h3`와 `h3 > a`는 같은 텍스트. 얕은 쪽이 내부 마크업 변경에 강하다 |
| `quality`를 YAML에 유지 | active 상태의 근거이고, 빼면 활성화한 recipe를 다시 읽을 수 없다 (실제로 발생) |
| `--allow-local`은 CLI 전용 | 사용자가 터미널에 친 플래그와 웹페이지가 보낸 요청은 다르다. loopback만 열고 사설 대역·메타데이터는 그대로 막는다 |
| PyYAML `safe_load` | recipe는 데이터이고, Phase 4부터는 모델이 쓴다. full loader는 임의 객체를 만든다 |

---

## Phase 4 — LLM Runtime  ✅

- `ModelGateway` Protocol + OpenAI 호환 구현 (Ollama/API 공용)
- **작업별 라우팅** + fallback
- **모델 관리** — list/catalog/pull/delete/route
- **하드웨어 감지** (VRAM) + 티어 추천 + 온보딩
- **bench** — 고정 샘플 5종 실측
- Grammar-constrained structured output (`format` + JSON schema)
- **N-후보 생성 + 결정론적 채점**
- **피드백 재시도 루프**
- 자연어 → CrawlSpec 컴파일 (자동 실행 금지, 검토 가능한 후보 반환)
- 시간/토큰 예산

**마일스톤 1 완료. 수준 2~3 동작.**

```bash
crwallm setup                                    # 한 줄 설치
crwallm model catalog                            # 이 머신이 돌릴 수 있는 것
crwallm recipe adapt shop --url <url>            # 모델이 컬럼 이름 지정
```

### Phase 4에서 확정된 것

| 결정 | 근거 (실측) |
|---|---|
| **`think: false` 기본값** | qwen3:14b 같은 질문에 **69초 vs 2.0초**. 추론 모드는 토큰 예산을 다 쓰고 JSON을 못 내놓는 경우가 많다 |
| **qwen3.5:9b 기본 모델** | 14b와 정확도 동일(1.0), 속도 2.5s vs 2.64s, 크기 2GB 작음. 3.6/3.8은 최소 27b(17.7GB)라 16GB에 안 들어감 |
| **후보 1개씩 생성, 통과 즉시 중단** | 3개 일괄 생성은 쉬운 페이지에서 같은 답을 3번 산다. **36.3초 → 2.9초** |
| **모델은 selector를 쓰지 않는다** | 탐지기가 찾은 컬럼에 *이름만* 붙인다. 환각 selector가 실패 모드에서 사라진다 |
| fallback은 **가용성**에만 | 답이 나빠서 유료 API로 넘어가면 사용자가 하지 않은 판단에 돈을 쓴다. 나쁜 답은 채점기 몫 |
| Ollama를 **compose로** | 네이티브 설치는 불필요. Docker GPU 패스스루 실측 확인 |
| 모델은 **프로젝트 폴더**에 | `data/ollama` (gitignore). 체크아웃 하나가 백업·이동·삭제 단위 |

---

## Phase 5 — Spider Mode  ✅

- **Sitemap 시딩** (robots.txt의 `Sitemap:` 포함, sitemap index, `<lastmod>`)
- **호스트별 프론티어 + 라운드로빈 스케줄러**
- **우선순위 프론티어**
- **simhash 근사 중복 제거**
- **Postgres 프론티어 영속화**
- **soft 404 탐지**
- rbloom 대규모 중복 필터

**마일스톤 1 = Phase 0~5 완료.** 만능 크롤러의 실질적 하한선.

```bash
crwallm spider https://example.com/ --max-pages 500
```

### Phase 5에서 확정된 것

| 결정 | 근거 |
|---|---|
| robots.txt는 **읽되 따르지 않는다** | 규칙은 무시하지만 sitemap 위치를 아는 유일한 표준 경로다. 지시를 따르지 않고 목차만 쓰는 것 |
| soft 404는 **패턴 예산**을 태운다 | `/ghost/{n}`이 200을 반환하면 나머지 전부도 그렇다. 한 페이지만 버리면 나머지 400개가 큐에 남는다 |
| soft 404 shape 신호에 **최소 6토큰** | 실행 중 발견: 내용이 "b"뿐인 정상 페이지가 다른 한 단어 페이지와 닮았다는 이유로 잡혔고, **URL 패턴 전체**를 날렸다 |
| 짧은 페이지는 중복 비교 제외 (30토큰) | 상품 상세는 템플릿을 공유하고 몇 단어만 다르다. 비교하면 카탈로그가 한 줄로 무너진다 |
| canonical과 content 중복을 **구분** | 하나는 사이트가 알려준 것, 하나는 우리가 알아챈 것. 합치면 어느 쪽이 동작했는지 안 보인다 |
| 호스트별 큐 + 라운드로빈 | 한 호스트를 100배 빠르게 때리는 것보다 100개 호스트를 병렬로 도는 게 빠르고 안 막힌다 |
| CJK 토크나이저 | 공백 분리로는 한국어 페이지 전체가 토큰 1개가 되어 전부 고유해진다 |

---

## Phase 6 — Extraction Expansion  ✅

- ✅ **내부 JSON API 발견** — Next.js `buildId` → 데이터 라우트, `<link rel=alternate>`
- ✅ **API 페이지네이션** — link_header / next_url / cursor / page / offset
- ✅ 임베디드 JSON (`__NEXT_DATA__` 등)
- ✅ JSON-LD (`@graph` 언랩, 중첩 엔티티, CDATA)
- ✅ **microdata** — JSON-LD가 빠뜨린 duration·channel을 채운다
- ✅ RSS/Atom, `<table>`
- ✅ OpenGraph/Twitter — 영상 페이지 판별
- ✅ 본문 추출 (자체 구현, trafilatura 미도입)
- ✅ 임베딩 + **semantic 필터** (bge-m3)
- ✅ provenance — 레코드마다 page_url + extractor

### Phase 6에서 확정된 것

| 결정 | 근거 |
|---|---|
| 인라인 JS에서 API URL 긁기 **안 함** | 실제 4개 사이트에서 0건. 요즘 번들에 리터럴 엔드포인트가 없다 |
| 관례 경로(`/wp-json`, `/products.json`) **투기적 요청 안 함** | 만나는 호스트마다 404 여덟 번은 무례하고 느리다. 목록으로 제공만 |
| JSON-LD는 **상세 페이지** 포맷 | 목록 6곳 조사에서 1곳, 그것도 `WebPage`뿐. "목록은 레시피로 → 상세에서 JSON-LD" |
| microdata를 JSON-LD와 **합치지 않음** | 한 페이지가 둘 다 갖고 다를 수 있다. 합치면 무엇이 바뀌었는지 안 보인다 |
| 피드는 **XML로 파싱** | HTML 파서는 `<link>`를 void로 처리해 모든 항목의 URL을 잃는다 |
| XML은 **선언 거부 + stdlib** | 외부 엔티티는 stdlib가 이미 안 푼다. 남은 엔티티 폭발은 선언을 막으면 끝 |
| semantic 필터는 **임베딩**, 챗 모델 아님 | 재실행 안정성. 챗 모델은 행마다 생성 + 매번 다른 답 |
| 모델 없으면 semantic **건너뜀** | 모델 없다고 전부 버리면 밖에서는 잘 도는 필터로 보인다 |
| trafilatura **미도입** | 자체 밀도 채점으로 위키백과 7,558단어 추출 확인. 의존성 하나를 아낀다 |
| oEmbed **미구현** | 5곳 중 1곳(YouTube)뿐이고 거기서는 microdata가 더 많이 준다 |

**Phase 6에서 실행이 잡은 것** — 전부 코드를 읽어서가 아니라 돌려서 나왔다:
워커가 레시피를 로드하지 않음 · 레시피 필터가 크롤에서 무시됨 ·
selectolax `.css()`가 자기 자신을 포함 · HTML 파서의 `<link>` void 처리 ·
`tbody tr, tr`이 행을 두 번 셈 · Tailwind 유틸리티가 셀렉터에 섞임 ·
선언 없는 의존성 4개 · cp949 콘솔에서 CLI가 죽음

---

## Phase 7 — Browser  ✅

- ✅ Playwright direct, 브라우저 1개 + context 재사용 + 페이지 풀
- ✅ route interception 리소스 차단 (image/media/font/stylesheet)
- ✅ **결과 기반 auto 폴백** (레코드 0건 → 브라우저)
- ✅ **무한 스크롤** (`max_rounds`, `stop_when_no_growth`)
- ✅ 브라우저 네트워크 관찰 → `inspect --render`가 API를 보고

### Phase 7에서 확정된 것

| 결정 | 근거 |
|---|---|
| `settle_ms` 기본값 **0이 아님** | 실측: 0에서 XHR 콘텐츠가 5번 중 **2번**만 렌더. 500ms에서 5/5. 절반만 되는 브라우저는 없는 것보다 나쁘다 — auto가 빈 렌더를 "정말 비었다"로 읽는다 |
| route 가드가 **렌더 전체**를 감쌈 | goto 직후 unroute하면 로드 이후 요청이 SSRF 검사를 안 받는다. 스크롤 구간 전체가 무방비였다 |
| `domcontentloaded`, `networkidle` 금지 | 폴링 위젯·웹소켓이 있는 페이지는 영원히 idle이 아니다 |
| `--no-sandbox` **안 씀** | 모든 포럼의 첫 제안이고, 적대적 페이지의 렌더러 버그를 이 사용자 권한 코드 실행으로 바꾼다 |
| auto 판정은 **결과 기반** | "JS shell인가?"는 양방향으로 틀린다. "추출이 레코드를 만들었나?"가 실제 질문 |
| 승격 판단은 **아카이브 이전** | 이후면 페이지를 두 번 세고 두 번 저장한다 |
| 스크롤은 **최후 수단** | 무한 피드도 결국 XHR을 부른다. `inspect --render`로 그 주소를 찾으면 브라우저가 다시 필요 없다 |

**실측** (`quotes.toscrape.com/js/`, 스크립트로만 렌더):

```text
http     0 records  1.01s
auto    10 records  2.90s   <- 승격
browser 10 records  3.00s
auto(서버 렌더 페이지)  0.79s   <- 브라우저를 열지 않음
```

---

## Phase 8 — Durability & Observability  ✅

- ✅ **cancel** — 요청이지 kill이 아니다. 워커가 페이지 사이에서 읽는다
- ✅ **stale recovery** — heartbeat이 끊긴 job을 재큐잉, 3회 후 실패 처리
- ✅ **retry** — 처음부터 다시. 카운터는 초기화, 레코드는 유지
- ✅ **heartbeat** — 이미 쓰이고 있었고, 이제 읽는 쪽이 생겼다
- ✅ **SSE** — Phase 6에서 완료
- ✅ **export** — JSONL / CSV, 스트리밍
- ✅ **idempotency** — `(job_id, page_url, record_hash)` 유니크 제약
- ⬜ **resume** — 아래 참조
- ⬜ Parquet — 아래 참조

### Phase 8에서 확정된 것

| 결정 | 근거 |
|---|---|
| cancel은 **요청**이지 kill이 아님 | 태스크를 죽이면 이미 추출한 레코드와 아카이브를 잃는다. 실측: 53페이지에서 요청 → 70페이지 206레코드로 정착 |
| stale은 **재큐잉**, 실패 아님 | 정전으로 죽은 job은 실패 행보다 재시도를 받을 자격이 있다. 유니크 제약이 중복 쓰기를 무해하게 만든다 |
| 3회 후에는 실패 | 워커 셋이 죽은 job은 운이 없는 게 아니다. 영원히 재큐잉하면 큐가 같이 죽는다 |
| reaper는 **유휴 워커**가 돌림 | 할 일 없는 워커가 버려진 일을 찾기에 가장 적합하다. 로컬 도구가 일관성을 위해 데몬을 하나 더 요구하면 더 나쁜 도구다 |
| export는 `seq` 기준 keyset | `(created_at, id)`는 순서를 못 준다 — sink는 배치로 쓰고 Postgres `now()`는 트랜잭션 시작 시각이라 배치 전체가 같은 타임스탬프를 갖는다 |
| CSV 컬럼은 **전체 행**에서 도출 | 마지막 페이지에만 나오는 필드가 빠진 파일은 느린 헤더보다 나쁘다 — 완전해 보인다 |
| 출처 컬럼은 **덧붙임** | 레시피에 `page_url` 필드가 있을 수 있고, 덮어쓰면 하류가 탐지할 수 없게 망가진다 |
| **Parquet 미구현** | pyarrow 40MB + 빌드 툴체인을, 로컬 워크플로에서 아무도 열지 않는 포맷을 위해. JSONL/CSV에서 필요한 쪽이 변환하면 된다 |
| **resume 미구현** | 영속 frontier가 필요한데, 실측된 필요가 없다. 실행은 초 단위로 끝나고 retry가 충분하다. 시간 단위 크롤을 만나면 그때 |

**실측** — UI에서 취소: 48페이지 169레코드가 그대로 남고 export 가능.
재시도: `attempts` 2로 증가, 카운터 0에서 재시작.

---

## Phase 9 — Auth & Interact

- 선언형 로그인 레시피, 세션 재사용/TTL
- `Vault` 크리덴셜 참조 저장 (OS 키링/암호화 파일)
- 폼 기반 검색 (POST 크롤)
- waiting_for_user 상태

**CAPTCHA 자동 우회는 하지 않는다.**

---

## Phase 10 — Documents & Media

- PDF/Office → Docling
- 바이너리 다운로드 채널 (별도 byte limit, Range 재개)
- 이미지/파일 저장 정책
- (선택) yt-dlp 어댑터 — 인자 화이트리스트 필수. HLS/DASH는 외부 도구에 위임

---

## Phase 11 — Scheduling & Incremental

- cron 반복 크롤
- 증분 — `<lastmod>` / ETag / If-Modified-Since / 콘텐츠 해시
- 변경 감지(diff) 이벤트

---

## Phase 12 — RAG Loop

- 청킹 → 임베딩(bge-m3) → pgvector
- 수집 데이터에 대한 로컬 LLM 질의

새 인프라 없음(Ollama + Postgres 재사용). "LLM ↔ 크롤러" 루프가 양방향으로 닫힌다.

---

## Phase 13+ — Later

- 프록시 풀 (실제 IP 차단을 겪은 뒤에)
- 드리프트 감지 / 자가치유 (수리 후보 → 회귀 → 카나리 → 승격)
- 웹 UI (Next.js)
- (선택) MCP export
- 프로덕션화 — 부하 테스트, 백업/복구, 릴리스 프로세스

---

## 마일스톤

| | 범위 | 완료 시 |
|---|---|---|
| **1** | Phase 0~5 | 로컬 LLM 자연어 크롤링 + 스파이더 + CLI 수동 조작 |
| **2** | Phase 6~8 | 커버리지와 안정성 |
| **3** | Phase 9~12 | 확장 능력 |

## 규칙

**Phase 5까지 끝내고 실제로 한 달 써본 뒤 6번 이후를 재평가한다.**
어떤 사이트에서 막히는지가 다음 우선순위를 정한다.

Redis / Kafka / 분산 / 마이크로서비스는 **측정으로 필요성이 확인될 때만** 추가한다.
