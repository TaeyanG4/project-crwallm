# 17. Non-Goals

초기 구현에서 의도적으로 하지 않을 것.

## Infra

- Kubernetes / Kafka / RabbitMQ / Redis queue / Temporal / Celery
- 분산 크롤링, 다중 노드, multi-region
- 마이크로서비스

## Product

- **멀티테넌트 / 인증 / 과금** — 로컬 단일 사용자 도구다
- frontend-first 개발
- Recipe 마켓플레이스
- 플러그인 생태계

## AI

- 페이지마다 LLM extraction
- 복잡한 ModelRouter
- 자율 브라우저 에이전트
- 멀티 에이전트 오케스트레이션
- LangChain류 프레임워크 도입 (structured output 호출 몇 개뿐)

## Browser

- **CAPTCHA 자동 해결**
- 안티봇 우회를 **핵심 기능으로** 삼기 (프록시는 인프라이며 별개 항목)
- 브라우저 위장 UA를 기본값으로 사용
- 임의 사용자 생성 JS 실행

## Extraction

- 모든 extractor를 처음부터 구현
- 임의 Python / JavaScript transform (화이트리스트만)

## Media

- HLS/DASH 세그먼트 조립 직접 구현 — 외부 도구(yt-dlp/ffmpeg)에 위임
- DRM 보호 콘텐츠

## 준수하지 않기로 한 것

- `robots.txt` 규칙 (단, `Sitemap:` 지시어는 활용)
- crawl-delay 지시어

개인·비상업 용도 전제. `respect_robots` 필드는 자리만 남겨둔다.

---

## 핵심 순서

```text
결정론적 크롤러가 먼저 동작
  → 구조 분석 + 재사용 가능한 Recipe
  → LLM 컴파일/적응
  → 스파이더
  → 넓은 커버리지
  → 내구성
  → 확장 능력
```
