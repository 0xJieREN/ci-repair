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

# v0.2 validation — 2026-09-18

- Final GitHub CI: **33 tests passed**, including the three real Docker integration
  cases. Locally, 30 unit tests passed; Docker regressions and the imported-input
  smoke test also passed.
- Ruff lint, formatting and Git diff whitespace checks passed.
- Real GitHub Actions collection tested against the deliberately failing manual
  [fixture run](https://github.com/0xJieREN/ci-repair/actions/runs/35307081679),
  attempt 1, job `105481378122`, source SHA
  `931e711c8bb7462b89791e0b77c8db996f1e2ee1`.
- Verified the imported checkout is clean and detached at that SHA; the raw log
  matches the manifest hash. Live testing found and fixed gh 2.101.0's refusal
  to capture logs containing terminal escape sequences.
- Complete imported-input smoke test passed: original failure reproduced,
  scripted model changed only `examples/buggy/src/ranges.py`, and a fresh Docker
  verifier passed the original test and all four regression tests. Report and
  model context retained the same run, attempt, job and commit provenance.
- Private evidence: `runs/github-import-01/` and `runs/github-repair-smoke-01/`.
  No paid model API calls were made for this iteration; the scripted smoke test
  demonstrates integration, not a new measurement of model repair ability.
- Colima was stopped after each local Docker test session.

# v0.3 validation — 2026-09-18

- Local full suite: **53 passed**, including four Docker integration cases.
  Ruff lint, formatting and diff whitespace checks passed.
- Real local Git repositories/bare remotes exercise commit preparation,
  creation-only push, repeated publish, existing branch conflicts, closed PRs,
  branch movement, modified evidence and dirty prepared checkouts.
- A real Docker verifier's PASS report and patch digest fed the preparation and
  publication path successfully. GitHub PR creation/list responses were simulated;
  the source repository remained unchanged. No paid model requests were made.
- PR source tests cover explicit head checkout, historical merge parents,
  different run/checkout SHAs, fork rejection and rejection of pull_request_target.
  The expected run/PR repository-ID and SHA field shapes were also checked against
  public mini-SWE-agent Actions run `34902917572` using read-only GitHub API access.
- `gh pr list` field selection was verified against this repository. No actual
  GitHub repair PR was created: live PR publication remains an operator-run
  acceptance step using `ci-repair-pr publish` and fresh verified evidence.
- Colima was stopped and its `Stopped` status confirmed after the local test run.

# Repository audit and shared verification gate — 2026-09-18

- Ran `bash scripts/check.sh --colima` locally before pushing: locked dependency
  sync, Ruff lint/format checks, cached Docker image build, **69 tests passed**
  in 11.27 seconds of pytest execution, and diff whitespace checks passed.
- The test suite now uses mini-SWE-agent's native deterministic model and covers
  native factory configuration, finite budgets, timeout process termination,
  false-PASS prevention, archive transformation detection and conditional fetch.
- The shared script automatically stopped Colima at the end of the full gate.
  No paid model API calls or real PR creation were involved.
- See [review findings](review-2026-09-18.md) and
  [local/server commands](verification.md) for resolved issues and remaining limits.

# Verification isolation — 2026-09-18

- Local full gate: **71 passed**, including two new Docker state-contamination
  cases. A marker created by the failing-command verifier cannot help or break
  the regression verifier. Both start from the same input archive and patch.
- Colima was stopped after the run. No paid model calls were made.

# Synthetic evaluation controls — 2026-09-18

- Full local gate: **81 passed**. All six correct references passed both public
  verification and the hidden oracle. The overfit control passed public checks
  but failed the hidden oracle; the no-op control produced NO_PATCH.
- These are evaluator controls, not measured model repair results. No paid model
  calls were made. Colima was stopped after the full run.

# Structured context and replay plans — 2026-09-18

- Final local full gate: **94 passed**, including a reviewed-plan-to-Docker repair
  using a nested working directory and Bash fail-fast semantics. Unit cases
  cover stale inputs, unsupported workflows, ambiguous YAML and review policy.
- Generated `runs/replay-plan-01.json` from the historical real collection at
  `runs/github-import-01/`; it remains an unreviewed, unexecuted local draft.
- Ran the evaluation CLI on all six deliberate overfit controls. All six passed
  public verification and failed the separate hidden oracle, with zero evaluator
  errors and zero paid model calls. Evidence: `runs/eval-overfit-01/summary.json`.
  This demonstrates detection of known false PASS, not a measured model failure rate.
- Colima was stopped and Stopped status confirmed after each Docker session.
- Real model comparison against an upstream mini-SWE baseline and expansion to
  20–50 representative failures remain future experiments, not completed evidence.

# Paired upstream evaluation — 2026-09-19

- Added an upstream DefaultAgent/stock-prompt runner and a common external grader;
  pinned fixture commits, captured real failure logs, paired scheduling and total
  call preflight checks. Evaluation errors now have an explicit valid denominator.
- Corrected upstream host-platform leakage into Docker prompt variables. Final
  local full gate and GitHub Linux CI passed **108 tests**.
- Completed 36 valid DeepSeek trials: both runners **18/18 success**, zero false
  PASS and zero infrastructure errors. Effective identical limit: 8 calls/trial.
- Used 180 calls in the valid batch; including the invalidated platform pilot,
  total attempted calls were 251, within the authorized 360. Colima is stopped.
- See [experiment report](experiments/2026-09-19-upstream-baseline.md) and its
  sanitized per-trial CSV. This is a small synthetic result, not proof of a
  success-rate advantage or production readiness.

## Historical corpus admission (2026-09-19)

- Added two full-repository more-itertools historical cases, with immutable
  source/fix commits and upstream-diff verification before Docker execution.
- Original public and hidden checks fail; all four reference controls (two cases
  times two runners) pass. Both runners reject all four no-op controls.
- Reference fixes pass the original 907-test and 914-test upstream suites.
- Local `scripts/check.sh --colima` passed **119 tests**, Ruff and diff checks.
  Colima was independently verified `Stopped` after each Docker batch.
- No external model calls were made. See [control evidence and limitations](
  experiments/2026-09-19-historical-controls.md). Real-model evaluation of this
  separate corpus requires a separately authorized experiment budget.

## First historical model experiment (2026-09-19)

- Separately authorized four DeepSeek trials, at most 10 calls each / 40 total.
  Completed with **37 calls**, estimated USD **0.024594576**.
- Both runners produced **2/2 verified patches**, with no observed false PASS or
  evaluator error. Three runs reached `LimitsExceeded`; only CI Repair's zip
  trial explicitly submitted (7 calls). Verification success and autonomous
  submission are reported separately, rather than hiding this distinction.
- Source archives, model configurations and patch hashes were checked; no
  configured credential values were found in artifacts. Colima is `Stopped`.
- [Full results and caveats](experiments/2026-09-19-historical-deepseek.md).
