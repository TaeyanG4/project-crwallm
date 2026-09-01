# 5. Spider Architecture

Collect(타겟형)와 Spider(탐색형)는 요구사항이 다르다.

| | Collect | Spider |
|---|---|---|
| 입력 | 목록 페이지 + Recipe | 시드 URL |
| 범위 | 좁고 명확 | 사이트 전체 / 다수 도메인 |
| 성공 기준 | 필드 정확도 | 예산 내 커버리지 |
| 핵심 난제 | selector | **프론티어 관리, 트랩 회피** |

BFS 골격만으로는 스파이더가 되지 않는다. 아래가 없으면 첫 대규모 크롤에서 예산 전부를 쓰레기로 채운다.

---

## 1. 크롤러 트랩 방어 — 최우선

`max_pages`만으로는 못 막는다.

| 트랩 | 증상 |
|---|---|
| 무한 캘린더 | `/calendar/2031/07`, `/2031/08`, ... 끝없이 생성 |
| 패싯 폭발 | `?color=red&size=M&sort=price&page=3` → 조합 수백만 |
| 세션 ID | 요청마다 새 URL → dedupe 무력화 |
| 무한 페이지네이션 | `?page=99999`도 200 반환 |
| 재귀 경로 | `/a/b/a/b/a/b/...` |
| Soft 404 | 200 + "찾을 수 없음" 본문 |

### 단일 최고 방어책: URL 패턴별 예산

URL을 패턴으로 정규화하고 패턴마다 상한을 건다.

```text
/product/8821      → /product/{n}       예산 5000
/calendar/2031/07  → /calendar/{n}/{n}  예산 20     ← 캘린더 즉사
?page=3            → ?page={n}          예산 200    ← 무한 페이지네이션 즉사
```

하나로 캘린더·무한 페이지네이션·패싯 폭발이 동시에 잡힌다.

### 설정

```yaml
spider:
  max_url_length: 2048
  max_path_depth: 12
  max_repeated_segment: 2          # /a/b/a/b/a → 차단
  max_query_params: 8
  query_whitelist: [page, p, offset, id, category]   # 나머지 제거
  per_pattern_budget: 500
  near_duplicate_threshold: 0.92
  soft_404_detect: true
```

Phase 2에 최소치(URL 길이, 경로 깊이, 반복 세그먼트, 쿼리 화이트리스트, 패턴 예산)를 넣는다.

---

## 2. Sitemap 우선 시딩 — 최대 효율

```text
BFS:      10,000 URL 발견하려고 10,000 페이지를 파싱
Sitemap:  sitemap index 1 + sitemap 5 파싱 → 10,000 URL 즉시 확보
```

수십 배 차이. `<lastmod>`로 증분 크롤까지 가능해진다.

**robots.txt는 규칙을 준수하지 않지만 `Sitemap:` 지시어를 읽기 위해 파싱한다.**

발견 순서: `robots.txt`의 Sitemap → `/sitemap.xml` → `/sitemap_index.xml` → HTML `<link rel="sitemap">`

---

## 3. URL 정규화

스파이더가 무한 루프에 빠지는 고전적 원인. "normalize" 한 단어로 넘기면 안 된다.

- 추적 파라미터 제거 (`utm_*`, `fbclid`, `gclid`, `ref`, `_ga`)
- 쿼리 파라미터 정렬 + 화이트리스트
- fragment 제거
- 기본 포트 제거 (`:80`, `:443`)
- 호스트 소문자화 (경로는 대소문자 유지)
- trailing slash 통일
- percent-encoding 정규화
- **`<link rel="canonical">` 존중** — 가장 저렴하고 효과 큰 중복 제거

---

## 4. 호스트별 프론티어 + 라운드로빈

단일 BFS 큐는 한 호스트를 연속으로 두들기고 다른 호스트를 놀린다.

```text
단일 큐:      host A ×1000 연속       → 차단당함, 처리량 낮음
호스트별 큐:  A,B,C,D,... 라운드로빈  → 차단 안 되고 처리량 최대
```

**"규제 완화 + 최대 처리량"이 목표라면 rate limit보다 이게 중요하다.**
호스트 100개를 병렬로 도는 게 호스트 1개를 100배 빠르게 때리는 것보다 빠르고 안전하다.

---

## 5. 우선순위 프론티어

순수 BFS는 예산을 약관·태그·페이지네이션 꼬리에 태운다.

```text
score = 경로패턴 가중치 × (1 / 깊이) × 신규성 × 상위페이지 관련도
```

예: `/product/` `/article/` 가산, `/tag/` `/print/` `/login` `/cart` 감산.

---

## 6. 콘텐츠 중복 제거

URL이 달라도 내용이 같은 경우가 흔하다(프린트 버전, 미러 경로, 정규화를 빠져나간 변형).

추출 텍스트의 **simhash**로 근사 중복을 걸러낸다. 예산이 크게 절약된다.

---

## 7. 프론티어 영속화

10만 URL 크롤 중 크래시하면 전부 날아간다. 메모리도 문제(URL 100만 개 = 수백 MB).

**Postgres 기반 job 큐가 이미 있으므로 프론티어도 같은 곳에 둔다.**
대규모 스파이더에서는 이걸 Phase 8까지 미룰 수 없다.

대안: 메모리 프론티어 + `rbloom`(Rust Bloom filter) 중복 필터를 소규모 크롤에 사용, 임계 초과 시 Postgres로 승격.

---

## 8. Soft 404 탐지

200을 반환하지만 실제로는 없는 페이지.

판정 힌트: 알려진 404 본문과의 유사도, 콘텐츠 길이 이상치, 추출 레코드 0건 + 특정 문구 매칭.
탐지되면 해당 패턴의 예산을 즉시 소진 처리한다.
