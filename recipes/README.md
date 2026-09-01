# Recipes

Recipe의 **원본은 이 디렉터리의 YAML 파일**이고 DB는 사본이다
(docs/07_RECIPE_ARCHITECTURE.md).

```bash
crwallm recipe test <file.yaml>    # 결정론적 검증
crwallm recipe push <file.yaml>    # 파일 -> DB
crwallm recipe pull <name>         # DB -> 파일
```

Phase 3부터 채워진다. `_scratch/`는 git에서 제외된다.
