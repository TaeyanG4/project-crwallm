# Test fixtures

## `html/`

고정 HTML 샘플. 크롤러/추출기 테스트는 네트워크를 타지 않는다.

Phase 3의 구조 분석기와 Phase 4의 모델 bench가 같은 샘플 세트를 쓴다
(정적 목록형 / JS 렌더링형 / 테이블형 / 기사형 / JSON-LD형).

## `malicious_server/` — Phase 1

SSRF와 트랩 방어를 실제로 검증하는 로컬 서버. 다음 시나리오를 제공한다.

- `127.0.0.1` / `169.254.169.254` / private range 로의 리다이렉트
- DNS rebinding (TTL 0으로 두 번째 조회에서 사설 IP 반환)
- 무한 리다이렉트 체인
- 응답 크기 상한 초과 (무한 스트림)
- 무한 캘린더 `/calendar/{y}/{m}`
- 패싯 조합 폭발 `?a=&b=&c=`
- 세션 ID가 매 요청 바뀌는 URL
- soft 404 (200 + "not found" 본문)

**Phase 1의 SSRF 코드와 동시에 작성한다.** 나중에 만들면 검증되지 않은 채로 굳는다.
