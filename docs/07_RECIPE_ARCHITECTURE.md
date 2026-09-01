# 7. Recipe Architecture

## 정의

사이트 구조를 재사용하기 위한 **선언형** extraction plan. 실행 가능한 코드가 아니다.

## 파일이 원본, DB는 사본

DB에만 있으면 손으로 못 고치고 git으로 버전관리도 안 된다.

```text
recipes/*.yaml   ← 원본 (편집 가능, git 추적)
      ↕ sync
   PostgreSQL    ← 실행 시 조회, 통계/버전 이력
```

```bash
crwallm recipe pull <name>    # DB → 파일
crwallm recipe push <file>    # 파일 → DB (검증 후)
crwallm recipe test <file>    # 결정론적 검증
```

## 예시

```yaml
name: example-laptops
source_url: https://example.com/products
allowed_domains: [example.com]
fetch_mode: http
status: active
version: 3

container: "li.product-item"
fields:
  - {name: title, selector: "h3 > a",     type: text}
  - {name: price, selector: "span.price", type: text, transform: [to_number]}
  - {name: url,   selector: "a.detail",   type: href, transform: [to_absolute_url]}
  - {name: image, selector: "img",        type: src}

pagination:
  next_selector: "a.pagination__next"
  max_pages: 50

filters:
  - {field: price, op: lte, value: 2000000}

fingerprint: "sha256:9f2a..."     # 구조 지문
```

### 필드 타입
```text
text | html | href | src | attr(name) | json(path)
```

## 책임 분리 — CrawlSpec vs Recipe

원본 설계에서 두 엔티티가 `allowed_domains` / `fetch_mode` / `extraction_fields`를 중복 소유했다. 해소:

| | 담당 |
|---|---|
| **CrawlSpec** | **무엇을 / 어디까지** — seed_urls, max_pages, max_depth, 예산, url_filters |
| **Recipe** | **어떻게** — container, fields, transform, pagination, 호환 도메인, 선호 fetch_mode |

`recipe_id`가 지정되면 Recipe 소유 필드는 **Recipe가 system-of-record**이며 CrawlSpec이 덮어쓸 수 없다.
`allowed_domains`는 교집합을 취한다(범위 확대 금지).

## 생성

```text
샘플 페이지
  → 원본 아카이브 저장
  → 결정론적 구조 탐지        ← 여기서 대부분 해결
  → (필요 시) LLM 후보 N개
  → 전부 결정론적 실행 + 채점
  → 최고점 선택 → preview
  → candidate
```

## Activation — LLM 불사용

```text
candidate
  → 샘플 fetch (또는 아카이브 재사용)
  → 결정론적 추출 실행
  → 품질 지표 게이트 통과
  → active
```

### 품질 지표

**Phase 11의 드리프트 지표와 동일한 것을 activation 게이트로 먼저 쓴다. 한 번 만들어 두 번 쓴다.**

```text
record_count          ≥ 임계
field_fill_rate       필드별 채움률
selector_match_rate   selector가 매칭된 비율
value_consistency     같은 필드 값들의 형식 일관성
parse_error_rate      ≤ 임계
```

점수: `score = record_count × mean(field_fill_rate) × value_consistency`

## 상태

```text
candidate → active → deprecated
```

## 구조 지문 (Fingerprint)

DOM 골격의 시그니처를 해시해 Recipe에 저장한다.

새 URL의 지문이 기존 Recipe와 일치하면 **LLM 호출 없이 그 Recipe를 먼저 시도**한다.
도메인이 달라도 같은 쇼핑몰 솔루션(카페24, 메이크샵 등)을 쓰면 지문이 일치한다.
한국 사이트 크롤링에서 특히 효과가 크다.

지문 구성: 컨테이너 태그+클래스 시그니처, 반복 횟수 구간, 자식 구조 깊이, 주요 속성 집합.

## 버전 관리

CrawlSpec / Job / Record는 어느 Recipe version을 썼는지 provenance를 남긴다.

## 드리프트 / 자가치유 (후기)

감지: selector match rate 급락, record count 급락, required field missing 증가, parse error 증가

**production Recipe를 즉시 덮어쓰지 않는다.**

```text
drift → 수리 후보 (N개) → 결정론적 회귀 검증 → 카나리 → 승격
```

회귀 검증은 **아카이브된 과거 원본**에 대해 수행한다(새 버전이 과거 데이터도 잘 뽑는지).
