# 사용법

이 문서는 **무엇을 어떻게 하는지**를 다룹니다.
설계 근거는 [docs/](docs/00_INDEX.md), 개발 환경은 [DEVELOPMENT.md](DEVELOPMENT.md).

> `crwallm` 명령은 가상환경 안에 있습니다. 새 터미널에서 그냥 치면
> `command not found`가 납니다. 저장소 안에서는 `./crwallm inspect ...`,
> 어디서나 쓰려면 `uv run crwallm inspect ...`.
> 한 번 활성화해두면 이름만으로 됩니다 — `source .venv/Scripts/activate`
> (Windows) 또는 `source .venv/bin/activate`.

- [0. 창](#0-창)
- [조작 수준 네 가지](#조작-수준-네-가지)
- [1. 페이지 살펴보기](#1-페이지-살펴보기)
- [2. 레시피](#2-레시피)
- [3. 크롤 실행](#3-크롤-실행)
- [4. Spider](#4-spider)
- [5. 필터](#5-필터)
- [6. 브라우저가 필요할 때](#6-브라우저가-필요할-때)
- [7. 잡과 워커](#7-잡과-워커)
- [8. 데이터 꺼내기](#8-데이터-꺼내기)
- [9. 브라우저 화면](#9-브라우저-화면)
- [10. 모델 관리](#10-모델-관리)
- [자주 겪는 것](#자주-겪는-것)

---

## 0. 창

터미널을 쓰지 않는다면 여기서 끝납니다.

**`crwallm.bat` 더블클릭**, 또는 `crwallm desktop`.
브라우저가 편하면 `crwallm up` 뒤 `localhost:8000` — **같은 화면입니다**
(거기에는 [작업·레시피·대화 탭](#9-브라우저-화면)이 더 붙습니다).

```text
①  주소를 붙여넣고  [ 살펴보기 ]
②  이름이 이미 채워진 표가 나옵니다  →  그대로  [ 모으기 ]
③  [ 엑셀로 저장 ]
```

**타이핑은 주소 한 번뿐입니다.** 컬럼 이름은 페이지가 스스로 밝힌 것에서
가져옵니다 — `small.author` → 작성자, `p.price_color` → 가격,
`h3` → 제목. 모델을 부르지 않으니 Ollama가 없는 빌드에서도 똑같이 됩니다.

- **필요한 것이 없습니다.** Docker도, 데이터베이스도, 서버도, 모델도.
  창 자체가 프로그램입니다.
- **셀렉터를 보지 않습니다.** 화면에 나오는 건 그 페이지에서 실제로 뽑은
  예시 두 줄입니다.
- 이름이 마음에 안 들면 고치세요. **필요 없는 칸은 이름을 지우면**
  모으지 않습니다.
- **"다음 페이지들도 따라가기"** 를 켜면 링크를 따라갑니다. 느려집니다.
- **엑셀로 저장**은 화면의 500건이 아니라 모은 것 전부를 씁니다.
  Excel이 한글을 깨뜨리지 않도록 BOM을 붙인 UTF-8 CSV입니다.
- 같은 이름을 두 번 쓰면 막습니다 — 레코드는 딕셔너리라, 둘째가 첫째를
  덮어써서 절반이 조용히 사라집니다.

이력을 남기지 않습니다. 창을 닫으면 결과는 사라지고, 저장한 파일만
남습니다. 이력이 필요하면 [7. 잡과 워커](#7-잡과-워커)로 — 거기서 처음으로
Docker가 필요해집니다.

---

## 조작 수준 네 가지

같은 일을 다섯 가지 방법으로 할 수 있고, 아래로 갈수록 손이 덜 갑니다.
**어느 것도 다른 것보다 특권을 갖지 않습니다** — 전부 같은 `CrawlSpec`과
같은 정책 게이트를 통과합니다.

| 수준 | 방법 | 모델 |
|---|---|---|
| — | 창에 주소만 붙여넣음. 이름은 마크업에서 나옴 | 불필요 |
| 0 | `--field title=h3` 처럼 셀렉터를 직접 씀 | 불필요 |
| 1 | `inspect`가 찾은 컬럼에 `--pick`으로 이름만 붙임 | 불필요 |
| 2 | `recipe adapt`가 모델에게 이름을 짓게 함 | 1회 |
| 3 | 대화 탭에 문장으로 요청 | 턴당 몇 회 |

수준 2에서 레시피가 만들어지면, **이후 크롤은 모델을 부르지 않습니다.**
사이트 하나를 한 번 이해해서 10만 페이지를 돌리는 것이 이 도구의 요점입니다.

---

## 1. 페이지 살펴보기

```bash
crwallm inspect https://quotes.toscrape.com/
```

```text
status       200
title        Quotes to Scrape
fingerprint  fp1:860596de201a28cfd110cfa3532f27b8

repeated structure:
 * div.quote  x10  (score 27.2, 25.2 words each)
     [0] span.text                text  100%  "The world as we have created..."
     [1] span > small.author      text  100%  Albert Einstein
     [3] span > a                 href  100%  /author/Albert-Einstein
     [4] div.tags > a.tag         text  100%  change
```

**왼쪽의 `[숫자]`가 다음 단계의 입력입니다.** 셀렉터를 쓰는 것은 어렵고,
컬럼에 이름을 붙이는 것은 누구나 할 수 있습니다 — 그 경계가 이 출력의 이유입니다.

페이지가 스스로 데이터를 선언하고 있으면 그것도 함께 나옵니다.

```text
declared (JSON-LD):
 * VideoObject  x1
     name                         Rick Astley - Never Gonna Give You Up
     uploadDate                   2009-10-24T23:57:33-07:00

declared (microdata):
 * VideoObject
     duration                     PT3M34S
     author.name                  Rick Astley
```

이 경로들은 레시피의 `selector`에 그대로 씁니다.
**선언된 데이터는 셀렉터보다 낫습니다** — 사이트를 리스타일해도 안 깨집니다.

| 플래그 | |
|---|---|
| `--no-links` | 발견된 링크 목록을 생략 |
| `--render` | 브라우저로 열고, **페이지가 부르는 API를 보고** |
| `--pick` | 인덱스로 컬럼을 골라 바로 레시피 초안 생성 |
| `--allow-local` | 로컬 개발 서버(loopback) 허용 |

---

## 2. 레시피

레시피는 **무엇을 어떻게 뽑을지**에 대한 선언이고 `recipes/*.yaml`에 삽니다.

### 만드는 세 가지 방법

```bash
# 수준 1 — inspect의 인덱스로 이름만 붙임. 모델 불필요.
crwallm recipe init quotes --url https://quotes.toscrape.com/ \
  --pick "quote=0,author=1,author_url=3"

# 수준 2 — 모델이 이름을 짓고, 점수를 매기고, 제일 나은 것을 남김
crwallm recipe adapt quotes --url https://quotes.toscrape.com/

# 손으로 — recipes/quotes.yaml 을 직접 편집
```

`adapt`는 후보를 하나씩 만들어 실제 페이지에 돌려보고 채점합니다.
통과하는 것이 나오면 멈춥니다 — 세 개를 만들어 고르는 것보다 빠릅니다.

### 확인하고 활성화

```bash
crwallm recipe test quotes        # 샘플 페이지에 돌려 점수를 봄
crwallm recipe activate quotes    # 다시 채점한 뒤 active 로 승격
crwallm recipe list               # 무엇이 있는지
crwallm recipe show quotes        # 저장된 그대로
```

`activate`는 **재측정을 거칩니다.** `active`는 "이게 동작한다"는 주장이고,
주장에는 근거가 필요합니다. 근거는 YAML의 `quality` 블록에 남습니다.

### 레시피 소스 일곱 가지

`source:` 필드가 레코드를 어디서 읽을지 정합니다.
나머지 필드의 의미는 그대로입니다 — `container`가 반복 단위를 찾고
`fields`가 그 안의 값을 찾습니다. **언어만 바뀝니다.**

| source | container | selector | 쓰는 곳 |
|---|---|---|---|
| `css` (기본) | CSS 셀렉터 | CSS 셀렉터 | 목록 페이지 |
| `jsonld` | schema.org `@type` | 점 표기 경로 | 상세 페이지 |
| `microdata` | schema.org `@type` | 점 표기 경로 | JSON-LD가 빠뜨린 것 |
| `embedded` | `__NEXT_DATA__.props...` | 점 표기 경로 | Next.js 등 |
| `feed` | — | (이름 변경만) | RSS / Atom |
| `table` | 표를 고르는 CSS 셀렉터 | (이름 변경만) | `<table>` |
| `article` | — | (이름 변경만) | 본문 |

`feed`·`table`·`article`은 **필드 목록이 필요 없습니다.** 피드 항목에 제목과
링크가 있는 건 그게 피드 항목이기 때문이고, 표의 필드 이름은 헤더 행입니다.

```yaml
# recipes/yt-video.yaml — CSS 셀렉터 0개
name: yt-video
source: microdata
source_url: https://www.youtube.com/watch?v=dQw4w9WgXcQ
allowed_domains: [youtube.com]
container: VideoObject
fields:
  - {name: title,    selector: name}
  - {name: channel,  selector: author.name}
  - {name: duration, selector: duration}
```

```yaml
# recipes/hn-feed.yaml — 필드 정의 없음
name: hn-feed
source: feed
source_url: https://news.ycombinator.com/rss
allowed_domains: [ycombinator.com]
```

### 값 다듬기 (transform)

```yaml
fields:
  - name: price
    selector: span.price
    transform: [to_number]          # "1,290,000원" → 1290000
  - name: url
    selector: a
    type: href
    transform: [to_absolute_url]
```

쓸 수 있는 것: `trim`, `normalize_ws`, `lower`, `upper`, `strip_html`,
`to_number`, `to_int`, `to_float`, `to_absolute_url`, `duration_to_seconds`,
`parse_date`

인자를 받는 것: `regex_extract(\d+, 0)`, `split(-, 1)`, `default(없음)`

`type:`은 `text`(기본) / `html` / `href` / `src` / `attr`.

---

## 3. 크롤 실행

```bash
# 레시피로
crwallm crawl https://quotes.toscrape.com/ --recipe quotes --follow --max-pages 50

# 셀렉터를 직접 (수준 0)
crwallm crawl https://shop.test/list \
  --container "li.product" \
  --field "title=h3" \
  --field "price=span.price::to_number" \
  --field "url=h3 > a::href|to_absolute_url"
```

`--field`는 `이름=셀렉터[::타입|transform|transform]` 형식이고 반복 가능합니다.

| 플래그 | 기본 | |
|---|---|---|
| `--recipe` | — | 저장된 레시피 사용 |
| `--container` / `--field` | — | 레시피 대신 직접 지정 |
| `--domain` | 시드에서 추론 | 허용 도메인 |
| `--max-pages` | 20 | |
| `--max-depth` | 2 | |
| `--follow` | 끔 | 링크를 따라감 |
| `--mode` | `http` | `http` / `browser` / `auto` |
| `--include` / `--exclude` | — | URL 정규식 |
| `--output` / `-o` | stdout | JSONL 파일로 |
| `--archive` | — | 원본 바이트 보관 디렉터리 |
| `--concurrency` | 8 | |

**`--recipe`와 `--domain`을 같이 쓰면 범위가 좁아집니다.** 레시피가 크롤 범위를
넓히는 방향은 허용하지 않습니다 — 교집합만 남습니다.

---

## 4. Spider

`crawl`은 아는 페이지에서 뽑고, `spider`는 **모르는 사이트를 훑습니다.**

```bash
crwallm spider https://example.com/ --recipe products --max-pages 500 --per-host 4
```

다른 점:

- **sitemap을 먼저 읽습니다.** `robots.txt`의 `Sitemap:` 지시어를 따라가
  URL을 미리 채웁니다. 요청 4번으로 10,000 URL을 얻는 쪽이 10,000번 fetch해서
  발견하는 것보다 낫습니다. (규칙은 따르지 않고 지시어만 읽습니다.)
- **호스트별 라운드로빈** — 한 사이트에 몰리지 않습니다.
- **중복 제거** — simhash로 "광고만 다른 같은 글"을 잡습니다.
- **soft 404 탐지** — 200을 주면서 "없습니다"라고 하는 페이지.
  하나 걸리면 그 URL 패턴 전체의 예산을 소진시킵니다.
- **트랩 방어** — 무한 달력, 무한 페이지네이션.

| 플래그 | 기본 | |
|---|---|---|
| `--per-host` | 4 | 호스트당 동시 요청 |
| `--interval-ms` | — | 호스트당 최소 간격 |
| `--no-sitemaps` | — | sitemap 시딩 끄기 |
| `--no-dedupe` | — | 콘텐츠 중복 제거 끄기 |

---

## 5. 필터

레코드를 뽑은 *뒤에* 무엇을 남길지 정합니다. 레시피의 `filters:`에 씁니다.

```yaml
filters:
  - {field: price,    op: lte,      value: 2000000}
  - {field: title,    op: contains, value: 노트북}
  - {field: duration, op: between,  value: [60, 1800]}
```

연산자: `eq` `ne` `gt` `gte` `lt` `lte` `between` `in` `not_in`
`matches` `not_matches` `contains` `not_contains` `exists` `missing` `semantic`

### semantic — 뜻으로 거르기

`contains`로는 "튜토리얼"이 "초보자를 위한 안내"에 매칭되지 않습니다.

```yaml
filters:
  - field: title
    op: semantic
    value: 프로그래밍 튜토리얼 영상
    threshold: 0.45
```

임베딩(bge-m3)으로 판정하므로 **재실행해도 같은 답이 나옵니다.**
`field: "*"`로 쓰면 레코드 전체를 봅니다 — 제목에 있을 수도, 요약에 있을 수도
있을 때 씁니다.

싼 필터가 먼저 돌아서, 비싼 판정은 살아남은 것만 봅니다.
**모델이 없으면 semantic 규칙은 건너뜁니다** — 모델이 없다고 전부 버리면
밖에서는 잘 도는 필터처럼 보이기 때문입니다.

---

## 6. 브라우저가 필요할 때

브라우저는 HTTP보다 **20~50배 비쌉니다.** 세 모드가 있습니다.

```bash
crwallm crawl <url> --mode http      # 기본. 렌더하지 않음
crwallm crawl <url> --mode auto      # HTTP 먼저, 0건일 때만 렌더
crwallm crawl <url> --mode browser   # 항상 렌더
```

`auto`의 판정은 **결과 기반**입니다 — "JS shell처럼 생겼나?"가 아니라
"추출이 레코드를 만들었나?"를 봅니다. 앞의 질문은 양방향으로 틀립니다.

실측 (스크립트로만 렌더되는 페이지):

```text
http     0 records  1.01s
auto    10 records  2.90s   ← 승격
browser 10 records  3.00s
auto(서버 렌더 페이지)  0.79s  ← 브라우저를 열지 않음
```

### 브라우저 대신 API를 찾기

무한 스크롤도 결국 XHR을 부릅니다. **그 주소를 찾으면 브라우저가 다시
필요 없습니다.**

```bash
crwallm inspect https://example.com/ --render
```

```text
api calls made while rendering:
  https://example.com/api/items?page=1
  ^ crawl one of these directly and the browser is not needed again
```

정말 스크롤이 필요하면:

```bash
crwallm crawl <url> --mode browser --scroll 5
```

---

## 7. 잡과 워커

앞의 명령들은 전경에서 돕니다. 오래 걸리는 것은 큐에 넣습니다.

```bash
crwallm worker                      # 워커 (별도 터미널)

crwallm jobs submit https://example.com/ --follow --max-pages 500
crwallm jobs list
crwallm jobs show <id>
crwallm jobs cancel <id>
crwallm jobs retry <id>
```

**취소는 요청이지 강제 종료가 아닙니다.** 워커가 페이지 사이에서 읽고 멈춥니다
— 이미 뽑은 레코드와 이미 저장한 원본은 그대로 남습니다.

**재시도는 처음부터 다시 돌립니다.** 카운터는 초기화되고 레코드는 남습니다
(같은 페이지를 다시 수집하면 같은 행이라 무시됩니다). 이어서 하는 것이
아니라 처음부터인 이유는, 실패한 *원인*을 고쳤을 때 결과를 믿으려면
처음부터가 맞기 때문입니다.

워커가 죽으면(크래시, 노트북 덮개) 그 잡은 `running`에 영원히 남습니다.
유휴 워커가 heartbeat이 끊긴 잡을 찾아 **다시 큐에 넣습니다.** 3회까지.

`jobs show`는 실패를 분류해서 보여줍니다.

```text
error_counts   {"blocked_429": 380, "conn_timeout": 20}
reject_counts  {"scope": 1204, "pattern_budget": 88}
```

"400개 실패"는 아무것도 알려주지 않지만 "380개가 429"는 동시성을 낮추라는
뜻입니다.

---

## 8. 데이터 꺼내기

```bash
crwallm jobs results <id>                            # JSONL, stdout
crwallm jobs export <id> -f csv -o out.csv           # CSV 파일로
crwallm jobs export <id> -f jsonl --source -o out.jsonl
```

`--source`는 각 행이 **어느 페이지에서 어느 추출기로** 나왔는지 붙입니다
(`_page_url`, `_extractor`). 추출 소스가 일곱 개이므로, "이 행의 값이
이상하다"는 그것을 셀렉터에서 읽었는지 JSON-LD에서 읽었는지 알아야
답할 수 있습니다.

전 구간 스트리밍입니다. 50만 행짜리도 메모리에 올리지 않습니다.

CSV 컬럼은 **전체 행**에서 도출합니다 — 마지막 페이지에만 나오는 필드가
빠진 파일은 느린 헤더보다 나쁩니다. 완전해 보이니까요.

Parquet은 없습니다. pyarrow 40MB를 로컬에서 아무도 열지 않는 포맷을 위해
치를 값이 아닙니다. 필요하면 JSONL에서 변환하세요.

---

## 9. 브라우저 화면

### 화면 네 개

```bash
crwallm up           # API + 워커 + 화면, 그리고 localhost:8000
```

| 탭 | 하는 일 | 필요한 것 |
|---|---|---|
| **모으기** | [0. 창](#0-창)과 같은 흐름 | 없음 |
| **작업** | 큐에 넣고, 진행 상황을 라이브로 보고, 중지·재실행·내보내기 | Docker |
| **레시피** | 무엇이 있고 얼마나 잘 도는지 | 없음 |
| **대화** | 문장으로 시키면 살펴보고·레시피 만들고·크롤을 걸어둠 | Ollama |

**창에서는 모으기 하나만 나옵니다.** 창 뒤에는 서버가 없어서 잡 큐도 모델도
없습니다. 되지 않는 탭을 보여주고 눌렀을 때 실패하는 것보다, 아예 내놓지
않는 쪽이 정직합니다.

같은 HTML 한 벌을 창은 파일에서 읽고 브라우저는 API에서 받습니다. 창에서는
`window.pywebview.api`, 브라우저에서는 같은 출처의 `/api/ui/*`를 부르고, 둘 다
`services/quick.py` 하나를 지나갑니다. **포트는 하나입니다** — 화면을 API가
직접 내보내니 앞단에 프록시도 Node도 없습니다.

모으기 탭에서 브라우저만 다른 점이 둘 있습니다. **저장**은 네이티브 대화상자
대신 브라우저 다운로드이고, **진행 상황**은 실시간으로 흐르지 않습니다(창은
흐릅니다). 탭마다 별개의 작업이라 두 탭이 서로를 덮어쓰지 않습니다.

| `up` 플래그 | |
|---|---|
| `--web` | 예전 Next.js 앱도 함께 띄움 (기본은 끔, Node 필요) |
| `--no-worker` | 잡을 큐에만 넣고 실행하지 않음 |
| `--no-open` | 브라우저를 열지 않음 |
| `--port` / `--ui-port` | 포트 변경 |

**워커가 없으면 작업 탭이 잡을 큐에 넣고 아무 일도 일어나지 않습니다** —
화면에는 `대기` 상태로만 보이고 이유가 나오지 않습니다. `up`이 함께 띄우는
이유가 그것입니다.

따로 띄우려면:

```bash
crwallm serve                      # API + 화면   127.0.0.1:8000
crwallm worker                     # 워커
npm run dev --prefix web           # 예전 웹 UI   localhost:3000
```

### 대화

```text
https://quotes.toscrape.com/ 에서 명언이랑 작가 이름 좀 모아줘
```

모델이 한 번에 한 단계씩 결정하고, 각 단계가 카드로 보입니다.

```text
페이지 살펴보기  https://quotes.toscrape.com/
  10 repeating items, container div.quote

레시피 만들기    https://quotes.toscrape.com/
  recipe 'quotes' scored 10.0 (10 records, 100% fill)
  [quote] [author]

크롤 실행        https://quotes.toscrape.com/
  job 1fbc3f7e queued with recipe 'quotes'
  실행 화면 열기 →
```

미리 순서를 짜지 않고 방금 일어난 일을 보고 다음을 고릅니다 — 비어 있던
페이지나 점수가 나쁜 레시피에 반응할 수 있는 유일한 방법입니다.

### 크롤 화면

이벤트가 라이브로 흐릅니다. 카운터는 크롤이 움직인다는 것만 말하고
무엇을 하는지는 말하지 않습니다.

```text
200 /shop/page/2      224ms · d1
+12 /shop/page/2      레코드
거부 /login           scope
링크 /shop/page/3     19 발견 → 12 큐
```

아무것도 못 건졌을 때 필요한 건 정확히 이 구분입니다 — 전부 범위 밖으로
거부됐는지, 전부 404였는지, 페이지는 멀쩡한데 셀렉터가 안 맞았는지.

**레코드 0건**은 경고색으로 칠하고 이유 후보를 띄웁니다.
**페이지** 탭이 그 진단의 근거입니다.

### REST API

`crwallm serve` 후 `http://127.0.0.1:8000/docs`.

| | |
|---|---|
| `POST /api/jobs` | 크롤 제출 (토큰 필요) |
| `GET /api/jobs` | 목록 |
| `GET /api/jobs/{id}` | 상세 + 실패 분류 |
| `GET /api/jobs/{id}/results` | 추출된 레코드 |
| `GET /api/jobs/{id}/pages` | 가져온 페이지 |
| `GET /api/jobs/{id}/stream` | 라이브 SSE |
| `GET /api/jobs/{id}/export` | `?format=jsonl\|csv` |
| `POST /api/jobs/{id}/cancel` | 중지 요청 |
| `POST /api/jobs/{id}/retry` | 재실행 |
| `GET /api/recipes` | 레시피 목록 |

쓰기 엔드포인트는 `X-CRWALLM-Token` 헤더가 필요합니다. `.env`에 있습니다.
읽기는 열려 있습니다 — 로컬 사용자가 이미 볼 수 있는 것뿐입니다.

---

## 10. 모델 관리

```bash
crwallm model status      # 서버가 살아 있는지, 무엇이 있는지
crwallm model catalog     # 이 기기가 돌릴 수 있는 것 (측정 기반)
crwallm model pull qwen3.5:9b
crwallm model use qwen3.5:9b
crwallm model rm <name>
```

기본값은 `qwen3.5:9b`입니다. 14b와 비교해 **정확도는 같고 더 빠르고 작았습니다.**

모델은 저장소에 없습니다. `data/ollama/`에 받아지고 `.gitignore`에 있습니다.

GPU가 없거나 모델을 쓰고 싶지 않으면 수준 0~1은 그대로 동작합니다.
클라우드 API로 바꾸려면 `.env`의 `CRWALLM_OLLAMA_BASE_URL`을
OpenAI 호환 엔드포인트로 돌리면 됩니다.

---

## 자주 겪는 것

### 레코드가 0건인데 페이지는 가져와졌다

셋 중 하나입니다. **페이지 탭**을 보면 구분됩니다.

| 페이지 탭 | 원인 |
|---|---|
| 전부 200 | 레시피 셀렉터가 이 페이지들과 안 맞음 |
| 4xx가 많음 | 시드나 범위가 틀림 |
| 페이지 자체가 적음 | `--follow`를 안 켰거나 `max_depth`가 0 |

레시피 없이 돌리면 **의도적으로** 0건입니다 — 페이지는 가져오되 추출하지
않습니다.

JS로 렌더되는 페이지라면 `--mode auto`를 쓰세요.

### 크롤이 시드 한 장에서 멈춘다

`--follow`가 꺼져 있습니다. `crawl`의 기본값은 "준 페이지만"입니다.

### `429`가 잔뜩 나온다

`jobs show`의 `error_counts`에 `blocked_429`가 많으면 동시성을 낮추세요.

```bash
crwallm spider <url> --per-host 1 --interval-ms 1000
```

### 셀렉터가 이상하게 길다

Tailwind 같은 유틸리티 클래스는 걸러내지만, 걸러낸 뒤에도 클래스가 많으면
셀렉터는 최대 3개까지만 씁니다. 8개짜리 셀렉터는 유틸리티가 아니어도
취약합니다 — 주장이 많을수록 페이지가 매칭에서 벗어날 경로가 많아집니다.

### 한글이 콘솔에서 깨진다

출력 자체는 UTF-8입니다. Windows 콘솔이 cp949라 못 그리는 것이고,
`--output` 파일에는 원문이 그대로 들어갑니다.

### 사이트가 렌더는 되는데 데이터가 안 나온다

`crwallm inspect <url> --render`로 **페이지가 부르는 API**를 찾으세요.
그 주소를 직접 크롤하는 쪽이 20배 빠르고, 값도 이미 숫자입니다.

### 워커가 잡을 안 집는다

잡이 `대기`에서 움직이지 않으면 워커가 없는 것입니다. `crwallm up`은 셋을
함께 띄우지만, `serve`만 돌렸다면 워커는 없습니다.

`crwallm worker`가 돌고 있는지, DB가 살아 있는지(`docker compose ps`),
`crwallm config`의 `database_url`이 맞는지 확인하세요.

### `crwallm: command not found`

가상환경이 활성화되지 않았습니다. `uv run crwallm ...`을 쓰거나
`source .venv/Scripts/activate`(Windows) / `source .venv/bin/activate`.

### `uv venv`가 실패한다

`uv sync`를 쓰세요. 가상환경을 만들고 의존성까지 맞춥니다. 이미 `.venv`가
있으면 갱신하고, `uv venv`처럼 지웠다 다시 만들지 않습니다 — Windows에서
그 삭제가 실패하는 경우가 있습니다.

---

## 안전장치

기억해 둘 것 몇 가지입니다. 전체는 [docs/11_SECURITY_MODEL.md](docs/11_SECURITY_MODEL.md).

- **SSRF 차단** — 사설·loopback·메타데이터 주소로는 가지 않습니다.
  DNS를 한 번 해석하고 확인한 그 IP로 연결합니다(리바인딩 방어).
  브라우저 모드에서는 페이지가 만드는 **모든 요청**을 검사합니다.
- **API 토큰** — 사용자가 방문한 아무 페이지나 `127.0.0.1`로 POST할 수
  있습니다. 쓰기 엔드포인트의 커스텀 헤더가 그것을 막습니다.
- **바이트 상한** — 압축 해제 후 기준입니다(gzip 폭탄).
- **로컬 바인딩** — API는 `127.0.0.1`에만 붙습니다.
