# Development

## 요구사항

- Python 3.12
- [uv](https://docs.astral.sh/uv/)
- Docker Desktop (PostgreSQL + 워커 실행)

확인된 환경: Windows 10, Python 3.12.6, uv 0.11.29, Docker 29.1.3

## 초기 설정

```bash
uv venv --python 3.12
uv pip install -e ".[dev]"
cp .env.example .env
```

`.env`의 `CRWALLM_API_TOKEN`을 생성한 값으로 바꿉니다.

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

## 실행

**Docker Desktop이 실행 중이어야 합니다.**

```bash
docker compose up -d db
```

```bash
alembic upgrade head
```

```bash
crwallm serve
```

전체 스택을 컨테이너로:

```bash
docker compose up -d
```

## 확인

```bash
curl http://127.0.0.1:8000/health
```

```bash
curl http://127.0.0.1:8000/ready
```

`/docs`에서 OpenAPI 문서를 볼 수 있습니다 (dev 환경만).

크롤을 큐에 넣습니다. 변경 엔드포인트는 토큰 헤더가 필요합니다.

```bash
curl -X POST http://127.0.0.1:8000/api/jobs -H "Content-Type: application/json" -H "X-CRWALLM-Token: $CRWALLM_API_TOKEN" -d '{"spec":{"seed_urls":["https://example.com/"],"allowed_domains":["example.com"]}}'
```

```bash
curl http://127.0.0.1:8000/api/jobs
```


## 검사

```bash
ruff check . && ruff format --check . && mypy src && pytest
```

개별 실행:

```bash
ruff check --fix .
```

```bash
ruff format .
```

```bash
mypy src
```

```bash
pytest -q
```

```bash
pytest -m "not integration and not e2e"
```

`tests/integration/test_job_pipeline.py`는 PostgreSQL이 응답하지 않으면 스스로
스킵합니다. Docker 없이도 나머지가 돌게 하기 위한 것이며, 그만큼 **CI에서만
검증되는 테스트**라는 뜻이기도 합니다. 로컬에서 돌리려면 DB를 띄우세요.

## 마이그레이션

모델을 바꾼 뒤:

```bash
alembic revision --autogenerate -m "설명"
```

```bash
alembic upgrade head
```

```bash
alembic downgrade -1
```

## CLI

```bash
crwallm config
```

```bash
crwallm inspect https://example.com/
```

```bash
crwallm crawl https://example.com/ --field "title=h1::text" --max-pages 5
```

```bash
crwallm jobs submit https://example.com/ --max-pages 100
```

```bash
crwallm worker
```

`recipe` 명령은 Phase 3에서 추가됩니다. → [docs/13_API_PLAN.md](docs/13_API_PLAN.md)

## 프로젝트 규약

- **Windows에서 개발, Linux 컨테이너에서 실행.** uvloop이 Windows를 지원하지 않습니다.
  워커는 반드시 컨테이너에서 돌립니다 → [docs/12_PERFORMANCE.md](docs/12_PERFORMANCE.md)
- **CLI와 REST는 같은 service 함수를 호출합니다.** 로직 중복 금지
- **mypy strict.** `Any`와 `type: ignore`는 이유를 주석으로 남깁니다
- **디렉터리는 필요할 때 만듭니다.** [docs/14_FOLDER_STRUCTURE.md](docs/14_FOLDER_STRUCTURE.md)의
  구조는 Phase가 진행되며 채워지는 목표이지 Phase 0의 산출물이 아닙니다

## 보안 회귀 테스트

`tests/unit/test_api_security.py`는 로컬 API 경계를 지킵니다.
**이게 깨지면 사용자가 방문한 아무 웹페이지나 크롤러를 조종할 수 있습니다.**
→ [docs/11_SECURITY_MODEL.md](docs/11_SECURITY_MODEL.md) §1

Phase 1에서 `tests/fixtures/malicious_server/`를 SSRF 코드와 **동시에** 작성합니다.
나중에 만들면 검증되지 않은 채로 굳습니다.
