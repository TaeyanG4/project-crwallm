# 4. Crawling Architecture

## 목표

단일 CrawlEngine이 모든 fetch strategy를 사용한다.

브라우저 전용 별도 crawler를 만들지 않는다.

```text
CrawlEngine
   ↓
FetchCoordinator
   ├── HTTP Fetcher
   └── Browser Fetcher
   ↓
Extractor Pipeline
   ↓
Links / Records
```

## Fetch Mode

```text
http
auto
browser
```

### http

httpx만 사용.

### browser

JS rendering이 반드시 필요한 사이트.

### auto

```text
HTTP fetch
  ↓
Static extraction / JS-shell detection
  ├─ sufficient → use HTTP
  └─ dynamic required → Browser fallback
```

브라우저는 비싸기 때문에 기본값은 HTTP 우선.

## Crawl traversal

초기 Collect 모드는 BFS 기반.

```text
frontier
→ policy
→ fetch
→ extract
→ discover links
→ normalize
→ dedupe
→ enqueue
```

제약:

- max_pages
- max_depth
- allowed_domains
- response byte limit
- crawl timeout
- redirect limit

## URL safety

모든 실제 fetch 직전에는 network policy를 확인한다.

discovered link 단계에서는 비용을 줄이기 위해:

- syntax
- scheme
- domain scope

정도만 확인할 수 있다.

DNS/private network 검증은 fetch 경계에서 수행.

## HTTP Fetcher

요구사항:

- async httpx
- persistent connection pool
- manual redirects
- redirect target validation
- streaming response
- hard response byte limit
- timeout
- explicit client ownership

## Browser Fetcher

요구사항:

- Chromium headless
- process/session reuse
- main-frame domain scope enforcement
- private/internal subresource blocking
- sandbox enabled
- images/media/fonts 기본 차단 가능
- timeout
- deterministic cleanup

Crawl4AI 또는 direct Playwright는 adapter 내부 선택 문제이며 core contract에 영향 없어야 한다.
