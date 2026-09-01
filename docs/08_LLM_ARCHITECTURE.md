# 8. LLM Architecture

## 기본 원칙

LLM을 crawling dataplane으로 사용하지 않는다.

```text
LLM    = 계획 / 컴파일 / 적응 / 수리
크롤러 = 결정론적 실행
```

**LLM은 절대 최종 authority가 아니다.** 출력은 손으로 쓴 입력과 동일한 게이트를 통과한다.

```text
자연어 → LLM ─┐
              ├→ CrawlSpec / Recipe → Pydantic → Policy → 크롤러
직접 작성 ────┘
```

## 브레인 — 두 가지

```text
① 로컬 LLM (Ollama)      로컬, 무료, GPU 필요, 데이터 유출 0   ← 기본
② 클라우드 API           원격, 종량과금, GPU 불필요            ← 폴백/저사양
```

둘의 코드는 거의 같다. Ollama가 OpenAI 호환 엔드포인트(`/v1`)를 제공하므로 `base_url`만 다르다.
vLLM, LM Studio, Groq, Together도 같은 클라이언트로 커버된다.
별도 구현이 필요한 건 Anthropic(tool-use 기반 structured output) 정도.

## ModelGateway

```python
class TaskKind(StrEnum):
    COMPILE_SPEC    = "compile_spec"      # 쉬움 — 4B도 충분
    ADAPT_SELECTORS = "adapt_selectors"   # 어려움 — 14B 이상 권장
    REPAIR_RECIPE   = "repair_recipe"     # 어려움
    CLASSIFY        = "classify"          # 쉬움 — semantic 필터
    EMBED           = "embed"             # 임베딩

class ModelGateway(Protocol):
    async def generate_structured(
        self, *, task: TaskKind, prompt: str, schema: type[BaseModel]
    ) -> BaseModel: ...
    async def embed(self, texts: list[str]) -> list[list[float]]: ...
    async def health(self) -> GatewayHealth: ...
```

## 작업별 라우팅

**저사양 GPU 대응의 핵심.** 쉬운 작업은 로컬, 어려운 작업만 API로 보낼 수 있다.

```toml
[llm.backends.local]
kind      = "openai_compat"
base_url  = "http://127.0.0.1:11434/v1"
model     = "qwen3:14b"
num_ctx   = 16384                     # 필수. 아래 "함정" 참조

[llm.backends.api]
kind        = "openai_compat"
base_url    = "https://api.openai.com/v1"
api_key_ref = "env:OPENAI_API_KEY"    # 값이 아니라 참조
model       = "gpt-4.1-mini"

[llm.backends.embed]
kind     = "openai_compat"
base_url = "http://127.0.0.1:11434/v1"
model    = "bge-m3"                   # 한국어 포함 다국어에 강함

[llm.routing]
compile_spec    = "local"
adapt_selectors = "local"             # 저사양이면 "api"
repair_recipe   = "local"
classify        = "local"
embed           = "embed"
fallback        = "api"               # 로컬 실패/타임아웃 시 전환
```

---

## 품질 기법 — 소형 모델 실용화

작업 난이도가 극단적으로 다르다.

| 작업 | 난이도 | 소형 모델 |
|---|---|---|
| 자연어 → CrawlSpec | 낮음 (작고 제약된 JSON) | **4B도 충분** |
| DOM → selector | **높음** (긴 컨텍스트 + 구조 추론 + 정확한 문자열) | 14B 이상 권장 |

selector는 한 글자만 틀려도 레코드 0건이다. 아래 5가지로 소형 모델을 실용화한다.

### ① 결정론적 구조 탐지로 LLM 작업을 줄인다 — 최대 레버

LLM에게 "selector를 찾아라"고 하지 않는다. **반복 구조는 코드로 찾을 수 있다.**

```text
1. 모든 형제 노드 그룹 스캔
2. 동일 (tag + class 시그니처) 노드가 N개 이상인 그룹 수집
3. 텍스트 밀도 / 자식 다양성이 높은 것 = 목록 컨테이너 후보
4. 각 후보의 하위 구조를 컬럼으로 전개
```

결과:
```text
후보 A: li.product-item  × 24
  [1] h3 > a        "게이밍 노트북 15인치"   24/24
  [2] span.price    "1,290,000원"           24/24
  [3] a@href        "/product/8821"          24/24
  [4] img@src       "/img/p8821.jpg"         23/24

후보 B: div.banner  × 3   (텍스트 밀도 낮음)
```

**LLM의 작업이 "찾기"에서 "이름 붙이기"로 바뀐다.** `[2]가 price다`는 4B 모델도 한다.
목록형 페이지 상당수를 LLM 없이 처리하고, 나머지도 아주 쉬운 작업이 된다.

### ② N-후보 생성 + 결정론적 채점

단발 생성보다 강하다.

```text
LLM에게 후보 3~5개 요구 (temperature 상향 또는 n>1)
   → 전부 recipe_test로 실행
   → score = record_count × field_fill_rate × value_consistency
   → 최고점 채택
```

**모델이 부정확해도 채점기가 고른다.** 약한 모델일수록 이득이 크다.

### ③ 피드백 재시도 루프

```text
0건 → "컨테이너 .product-card 매칭 0개. 실제 반복 요소는
       li.product-item(24), div.card(12). 다시 제안하라."
```

3~5회. ①②③을 합치면 8B로도 실용 수준이 된다.

### ④ Grammar-constrained decoding — 필수

"JSON으로 출력해줘"라고 프롬프트하지 않는다.
Ollama의 `format` 파라미터에 **JSON 스키마를 직접 전달**하면 제약 디코딩이 걸려
유효하지 않은 JSON이 물리적으로 나올 수 없다. 파싱 실패라는 실패 유형이 사라진다.

### ⑤ 계층적 폴백

```text
결정론적 구조 탐지 → 성공 시 LLM 0회
     ↓ 실패
로컬 LLM 3회 시도  → 성공 시 종료
     ↓ 실패
클라우드 API 1회   → 어려운 사이트만 과금
```

---

## 모델 관리

Ollama를 백엔드로 고른 결정적 이유. 모델 pull/delete/list가 일급 API다.

| 엔드포인트 | 기능 |
|---|---|
| `GET /api/models` | 설치 모델 + 디스크 사용량 + task 할당 현황 |
| `GET /api/models/catalog` | **감지된 하드웨어에 맞는 추천 목록** |
| `POST /api/models/pull` | 다운로드 (SSE 진행률) |
| `DELETE /api/models/{name}` | 삭제 |
| `POST /api/models/route` | task별 백엔드/모델 지정 |
| `POST /api/models/{name}/bench` | **실측 벤치마크** |

### bench

고정 샘플 페이지 5종(정적 목록형, JS 렌더링형, 테이블형, 기사형, JSON-LD형)으로 adaptation을 실제 실행:

```text
qwen3:14b   성공 5/5  평균 1.8회 시도  평균 12초  VRAM 9.1GB
qwen3:8b    성공 4/5  평균 3.2회 시도  평균  6초  VRAM 5.4GB
qwen3:4b    성공 2/5  평균 4.8회 시도  평균  3초  VRAM 3.1GB
```

"내 GPU에 뭐가 맞나"를 추측이 아니라 측정으로 답한다.
**Phase 3의 Recipe 품질 지표를 그대로 재사용한다.**

### 하드웨어 감지

`pynvml`로 VRAM 조회 → 실패 시 `nvidia-smi` 파싱 → 실패 시 시스템 RAM 기준 CPU 모드.
감지 결과로 catalog를 필터링한다.

### 권장 모델

**카탈로그는 코드가 아니라 `models.toml`로 외부화한다.** 모델 판도가 빠르게 바뀐다.

| VRAM | 권장 | 용도 |
|---|---|---|
| 8GB 미만 / CPU | Qwen3 4B (Q4_K_M, ~2.5GB) | compile_spec, classify만. adaptation은 API |
| 8~10GB | Qwen3 8B (Q4_K_M, ~5GB) | 간단한 사이트 adaptation |
| **12~16GB** | **Qwen3 14B (Q4_K_M, ~9GB)** ← **기본값** | 대부분의 adaptation |
| 24GB+ | Qwen3 32B / Qwen2.5-Coder 32B (~20GB) | 복잡한 DOM도 1~2회에 |
| 임베딩 | `bge-m3` (~1.2GB) | semantic 필터, RAG |

기본값을 14B로 잡은 이유: 12GB VRAM(RTX 3060 12G / 4070 등 가장 흔한 구성)에 16k 컨텍스트까지 여유,
Qwen 계열이 structured output에 안정적, selector는 코드에 가까워 코드 학습 강한 모델이 유리,
한국어 처리가 준수.

### 온보딩

첫 실행: 하드웨어 감지 → 티어 제안 → 사용자 확인 → pull → bench 자동 실행.

### 디스크

모델은 개당 3~20GB. Windows 기본 경로가 `C:\Users\...\.ollama\models`라 C 드라이브가 금방 찬다.
`OLLAMA_MODELS` 환경변수로 데이터 드라이브 이전을 설정 UI에 노출한다.

---

## 실전 함정

### ① `num_ctx` — 1번 실패 원인

Ollama는 모델별 Modelfile에 명시가 없으면 컨텍스트를 2048~4096으로 잡는다.
축약한 DOM은 3~8k 토큰이다. **에러 없이 조용히 잘려나가고**, 모델은 잘린 DOM을 보고 엉뚱한 selector를 뱉는다.

대응:
- 요청마다 `options.num_ctx` 명시 (16384 권장)
- **DOM 축약 후 토큰 수를 세서 컨텍스트 초과 시 거부하거나 더 축약**
- 조용히 실패하게 두지 않는다

### ② 컨텍스트가 VRAM을 먹는다

14B Q4가 9GB인데 16k 컨텍스트 KV 캐시가 1~2GB 더 붙는다. bench에서 실제 VRAM을 측정해 보고한다.

### ③ DOM 축약 품질 > 모델 크기

8k 토큰짜리 지저분한 HTML을 14B에 넣는 것보다,
잘 축약한 2k 토큰 골격을 8B에 넣는 게 낫다. **축약기에 시간을 쓴다.**

---

## Ollama 튜닝 (코드 변경 없음)

| 설정 | 효과 |
|---|---|
| `OLLAMA_FLASH_ATTENTION=1` | 속도 향상 + VRAM 절감 |
| `OLLAMA_KV_CACHE_TYPE=q8_0` | **KV 캐시 VRAM 대폭 절감** — 같은 GPU로 더 긴 컨텍스트. Flash Attention 필요 |
| `OLLAMA_KEEP_ALIVE=-1` | 모델 상주. **재시도 루프에 필수** — 미설정 시 호출마다 10~30초 로딩 |
| `options.num_predict` 상한 | selector JSON은 짧다. 폭주 방지 |
| 양자화 `Q4_K_M` | 품질/크기 스위트스팟. 여유 시 `Q5_K_M` |

---

## DOM 축약기

`inspect_structure`의 핵심. 목표는 **어떤 페이지든 2~4k 토큰**.

- `script` / `style` / `svg` / `noscript` / `comment` / `iframe` 제거
- 속성 화이트리스트: `class`, `id`, `href`, `src`, `data-testid`, `itemprop`, `role`
- 동일 구조 형제 노드를 `.product-card × 24` 형태로 압축 + 샘플 2개
- 텍스트 노드 ~80자로 절단
- 목록 탐색 시 `nav` / `footer` / `header` 휴리스틱 제거

---

## 비용/예산

ModelGateway 레벨에 per-job 토큰·비용 상한과 회계를 둔다.
로컬은 비용 0이지만 **시간 예산**은 필요하다(재시도 루프가 무한히 돌지 않도록).

```text
adaptation:  LLM 필요
bulk crawl:  LLM 이상적으로 0회
```
