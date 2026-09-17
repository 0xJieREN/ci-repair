# Validation record — 2026-09-17

- macOS/Apple Silicon, Colima + Docker, Python 3.12, uv locked dependencies.
- `CI_REPAIR_DOCKER_TESTS=1 uv run pytest -q`: **19 passed**.
- Ruff lint and formatting checks passed.
- GitHub Actions on Linux passed, including Docker integration tests.
- Demo baseline reproduced in the prepared container:
  `test_positive_interval`: `AssertionError: 9 != 14`.
- Original fixture remains broken and unchanged by repair attempts.

The three Docker integration cases run the real mini-SWE-agent loop with a
scripted model: valid source patch passes fresh verification; test modification
is rejected; no edit yields NO_PATCH. They are not evidence of LLM repair ability.

A real model repair has **not yet been run**. The operator switched from MiniMax
to DeepSeek during setup; the live experiment awaits local DeepSeek credentials.
v0.1's model-based acceptance criterion remains open until that run succeeds and
its patch, trajectory and independent test records are inspected.

No model API requests were made during these checks. Run artifacts and secrets
are excluded from Git. There is no automatic repair PR integration in this version.
