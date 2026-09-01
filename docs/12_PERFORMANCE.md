# 12. Performance

세 가지 축으로 나눈다: **크롤 처리량 / LLM 품질 / LLM 추론 속도**.

---

## A. 크롤 처리량

영향도 순.

| # | 최적화 | 기대 효과 | Phase |
|---|---|---|---|
| 1 | **selectolax로 파서 교체** (BeautifulSoup 대신) | 파싱 5~15배 | 2 |
| 2 | **DB 배치 쓰기** (건별 INSERT 금지) | 쓰기 10~50배 | 2 |
| 3 | **JSON API 발견 → HTML 파싱 우회** | 해당 사이트 10~30배 | 6 |
| 4 | **브라우저 회피** (HTTP 50~200ms vs 브라우저 2~5s) | 20~50배 | 7 |
| 5 | **Linux 컨테이너 + uvloop** | 이벤트 루프 2~4배 | 0 |
| 6 | DNS 캐시 + IP 피닝 | 연결당 20~100ms | 1 |
| 7 | HTTP/2 + 커넥션 풀 튜닝 | 동일 호스트 반복 시 | 2 |
| 8 | 응답 캐시 (개발 루프) | Recipe 개발 체감 큼 | 3 |
| 9 | 호스트별 라운드로빈 | 차단 회피 + 병렬 극대화 | 5 |

### ① selectolax

bs4는 래퍼 오버헤드가 커서 수백만 페이지 파싱에 부적합하다.
selectolax(lexbor 엔진)가 CSS 선택에서 압도적으로 빠르다.
**XPath가 필요할 때만 lxml로 폴백.**

### ② 파싱을 이벤트 루프 밖으로

동시 fetch가 수백 개면 HTML 파싱(CPU 바운드)이 이벤트 루프를 막아 fetch 동시성이 무의미해진다.

`run_in_executor`로 오프로드하되, **스레드로 충분한지 먼저 측정한다** —
C 확장이 GIL을 놓아주면 스레드로 끝나고, 아니면 프로세스 풀이 필요하다.

### ③ DB 배치 쓰기

페이지마다 INSERT를 날리면 여기가 병목이 된다.

- `ExtractedRecord`는 500~1000건씩 모아 asyncpg `COPY` (시간 또는 건수 기준 flush)
- 개인용 크롤러라면 `synchronous_commit = off`가 정당한 트레이드오프
  (크래시 시 마지막 몇 초 손실 허용)
- 대량 삽입 테이블의 인덱스 최소화

### ④ Linux 컨테이너 + uvloop

**uvloop은 Windows를 지원하지 않는다.**
어차피 Postgres 때문에 docker-compose를 쓰므로 **워커도 Linux 컨테이너에서 돌리고 uvloop을 켠다.**
고동시성 소켓 작업에서 2~4배 차이가 난다.

개발은 Windows에서, 실행은 컨테이너에서.

### ⑤ DNS 캐시 + IP 피닝

성능과 보안이 같은 해법이다. → `11_SECURITY_MODEL.md` §2

### ⑥ 적응형 동시성 (AIMD)

**rate limit은 예절이 아니라 처리량 문제다.** 차단당하면 수집이 0이 된다.
목표는 "느리게"가 아니라 **"차단되지 않는 최대 속도"**.

```text
높은 동시성으로 시작
  → 429 / 403 / 타임아웃 감지 → 곱셈적 감소
  → 회복 → 가산적 증가
```

- 글로벌 세마포어와 per-host 세마포어를 **분리**
- `Retry-After` 존중, 지수 백오프
- 식별 가능한 User-Agent (브라우저 위장 UA를 기본값으로 쓰지 않는다 → `17_NON_GOALS.md`)

### ⑦ 기타

- 조건부 요청 (`ETag` / `If-Modified-Since`)으로 재크롤 비용 절감
- `Accept-Encoding: gzip, br`
- 스트리밍 + 조기 중단 (원하지 않는 content-type이면 즉시 abort)
- URL 정규화는 발견된 모든 링크마다 호출되므로 저렴해야 한다
- 수백만 URL 넘어가면 `rbloom`으로 중복 필터 전환
- 응답 캐시: 콘텐츠 주소 아카이브를 개발 중 재사용 (`06` 원본 아카이빙과 동일 저장소)

### ⑧ 브라우저를 쓸 수밖에 없을 때

- 브라우저 인스턴스 1개 유지 + context 재사용 (페이지마다 브라우저 생성 금지)
- route interception으로 이미지/미디어/폰트/CSS 차단
- `wait_until="domcontentloaded"` (`networkidle` 금지)
- 페이지 풀 재사용

순진한 Playwright 사용 대비 3~5배.

---

## B. LLM 품질

→ `08_LLM_ARCHITECTURE.md`의 "품질 기법" 참조.

요약: 결정론적 구조 탐지(최대 레버) → N-후보 + 채점 → 피드백 재시도 →
grammar-constrained decoding → 계층적 폴백.

**DOM 축약 품질이 모델 크기보다 중요하다.**

---

## C. LLM 추론 속도 / VRAM

→ `08_LLM_ARCHITECTURE.md`의 "Ollama 튜닝" 참조.

요약: Flash Attention + KV 캐시 양자화 + 모델 상주(`KEEP_ALIVE=-1`) + `num_ctx` 명시.

**가장 큰 승리는 LLM을 부르지 않는 것이다.**
Recipe 재사용, 구조 지문 매칭, 결정론적 구조 탐지가 호출 자체를 없앤다.

---

## 측정 없이 추가하지 않는다

Redis / Kafka / 분산 크롤링 / 마이크로서비스는 측정으로 필요성이 확인될 때만 도입한다.
