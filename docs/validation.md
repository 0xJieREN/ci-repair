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

## Real model acceptance run

The local v0.1 acceptance experiment passed using `deepseek/deepseek-flash`.
The model received the failure context and explored the repository itself;
it was not given the source location or desired fix.

- Input commit: `ff4796a`; clean synthetic fixture, unchanged after the run.
- Baseline: original failing test exited 1 (`AssertionError: 9 != 14`).
- Patch: only `src/ranges.py`, changing `range(start, end)` to
  `range(start, end + 1)` to include the upper endpoint.
- Independent fresh container: original test passed; all 4 regression tests passed.
- Model calls: 3. Duration: 4.84 seconds.
- Estimated cost: $0.000759684 using the checked-in conservative price registry;
  not a provider billing receipt.
- Local evidence: `runs/deepseek-live-01/` contains the patch, trajectory,
  baseline, verification outputs and report. The credential was not found in
  the run artifacts or console log; `.env` is ignored by Git and mode 600.

This demonstrates one successful toy repair, not a measured success rate on
real-world CI failures. Production deployment, broader evaluation and automatic
repair PR integration remain outside v0.1. Run artifacts and secrets stay local.
