# 6. Extraction Architecture

## 자동 폴백 체인

단일 `ExtractorPipeline`이 순서대로 시도하고 성공 지점에서 멈춘다.
어떤 추출기가 성공했는지 provenance에 기록한다.

```text
정형     ① XHR / 내부 JSON API 발견 → 직접 호출   ← 최고. HTML 파싱 자체가 불필요
         ② __NEXT_DATA__ / __NUXT__ / __INITIAL_STATE__ 등 임베디드 JSON
         ③ JSON-LD (schema.org: Product, Article, JobPosting, VideoObject)
         ④ RSS / Atom / sitemap.xml
         ⑤ <table> → rows
           ↓ 실패
반정형   ⑥ Recipe의 CSS selector
         ⑦ microdata / RDFa / OpenGraph / meta
           ↓ 실패
비정형   ⑧ 본문 텍스트 추출 (trafilatura)
         ⑨ 문서 파싱 (PDF/docx → Docling)
         ⑩ 원본 아티팩트 보존
```

### ①이 압도적으로 중요하다

현대 사이트 대부분은 내부 JSON API로 목록을 로드한다.
그걸 찾으면 selector도, 브라우저도, 페이지네이션 추측도 전부 불필요해진다.

발견 방법:
- 브라우저 모드에서 네트워크 요청 관찰
- `<script>` 내 JSON 블록 탐지
- 알려진 경로 패턴 프로빙 (`/api/`, `/graphql`, `_next/data`)

**투자 대비 효과가 가장 큰 항목.**

---

## API 페이지네이션

내부 API를 찾아도 페이지를 못 넘기면 첫 20건에서 끝난다. 선언형 전략:

```yaml
api:
  endpoint: /api/v1/products
  method: GET
  pagination:
    kind: offset          # offset | page | cursor | link_header | graphql
    param: offset
    size_param: limit
    size: 100
    stop_when: items_lt_size
  items_path: "$.data.items"     # JSONPath
```

5개 전략(offset / page / cursor / `Link: rel=next` 헤더 / GraphQL `after`)이면 현실의 대부분을 덮는다.

---

## 미디어 메타데이터

영상/이미지 수집용 전용 추출기.

| 소스 | 얻는 것 |
|---|---|
| JSON-LD `VideoObject` | name, description, duration, uploadDate, thumbnailUrl, contentUrl, embedUrl, 조회수 |
| `og:video`, `og:video:duration`, `og:image` | 제목/썸네일/길이 |
| oEmbed 엔드포인트 | 플랫폼 표준 메타데이터 |
| `<video src>`, `<source>`, `<iframe src>` | 재생 URL, 임베드 ID |
| `data-video-id`, `data-src` | 플랫폼별 ID |
| 본문 텍스트 URL 정규식 | 흩어진 링크 |

---

## Transform — 화이트리스트만

임의 Python/JS 실행은 금지(`17_NON_GOALS.md`). 선언형 화이트리스트만 허용한다.

```text
trim | normalize_ws | lower | upper
to_number | to_int | to_float          "₩1,234,000" → 1234000
to_absolute_url                         상대경로 → 절대 URL
parse_date | duration_to_seconds        "1:23:45" → 5025
regex_extract(pattern, group)
split(sep, index)
strip_html
default(value)
```

체인 가능:
```yaml
- {name: price, selector: "span.price", type: text, transform: [strip_html, to_number]}
```

---

## 레코드 필터

**URL 필터(fetch 전)와 별개로, 추출 후 레코드 단위 필터가 필요하다.**

```yaml
filters:
  # 결정론적 — 비용 0
  - {field: duration_s,  op: between, value: [60, 1800]}
  - {field: upload_date, op: gte,     value: "2025-01-01"}
  - {field: view_count,  op: gte,     value: 10000}
  - {field: title,       op: matches, value: "(?i)강의|튜토리얼"}
  - {field: channel,     op: not_in,  value: ["광고채널A"]}
  - {field: price,       op: lte,     value: 2000000}

  # 의미 기반 — 임베딩 또는 LLM
  - field: title
    op: semantic
    value: "백엔드 개발 실무 강의"
    threshold: 0.72
```

연산자: `eq ne gt gte lt lte between in not_in matches not_matches exists semantic`

### 적용 순서 — 비용 순으로

```text
url_filters       (fetch 전)      → 가장 싸다. 네트워크를 아낌
   ↓
결정론적 filters  (추출 직후)      → 대부분 여기서 걸러진다
   ↓
semantic filters  (살아남은 것만)  → 비싼 것의 입력을 최소화
   ↓
저장
```

### semantic 필터

정규식으로는 "요리 영상만"을 못 거른다. 제목+설명은 50토큰 남짓이므로:

- **임베딩 방식(권장)**: `bge-m3`로 코사인 유사도. 초당 수백~수천 건. 비용 사실상 0
- **LLM 방식**: 판정이 복잡할 때만. 4B 모델로도 충분

DOM adaptation과 달리 소형 모델이 아주 잘하는 작업이다. 저사양 GPU에서도 문제없다.

---

## 원본 아카이빙

**Phase 2 필수.** 투자 대비 효과 1위 항목.

```text
CrawlResult.body_ref → sha256 콘텐츠 주소 저장 (zstd 압축, HTML은 ~10:1)
```

효과:
- **재크롤 없이 재추출** — Recipe 개발 중 같은 페이지를 수백 번 다시 파싱한다.
  매번 네트워크를 타면 개발 속도가 10배 느려진다
- 드리프트 진단 (그때 HTML이 어땠는지)
- Evidence/provenance의 실체
- 새 추출기를 과거 수집분에 소급 적용

---

## Evidence / Provenance

```text
record_id, field_name, source_url
extractor_type, selector/path
recipe_id, recipe_version
body_ref            ← 원본 아카이브 포인터
confidence?
```
