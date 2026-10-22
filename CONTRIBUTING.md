# Contributing

This guide covers setting up, testing, changing and evaluating CI Repair. The design
itself is in [docs/lifecycle.md](docs/lifecycle.md).

## Setup

Requires Python 3.12+, [uv](https://docs.astral.sh/uv/) and Git. Docker is needed
only for the full gate; on macOS use Colima.

```sh
uv sync --locked
bash scripts/check.sh --unit
```

No model key is needed for development. Never commit `.env` (it is ignored); keep
the operator policy outside the repository.

## The gate

`scripts/check.sh` is the one acceptance gate, locally and in GitHub Actions:

```sh
bash scripts/check.sh --unit    # lock sync, ruff lint and format, tests without Docker
bash scripts/check.sh --colima  # macOS: the full gate; starts Colima and stops it on exit
bash scripts/check.sh           # Linux or CI: the full gate on a running Docker engine
```

The full gate adds the Docker tests (`tests/test_docker.py`, `tests/test_orchestrate.py`,
real images and the real mini loop with a scripted model) and `git diff --check`.

On macOS, Colima holds its CPUs and memory while it runs. Prefer `--colima`; if you
start Colima yourself, run `colima stop` afterwards and confirm with `colima status`.

## Code map

| Path | What lives there |
|---|---|
| `src/ci_repair/webhook.py` | Signed intake, queue, worker (`ci-repair-webhook`) |
| `src/ci_repair/github.py` | Collection of a run's failed jobs (`ci-repair-github`) |
| `src/ci_repair/reconstruct.py` | Workflow → replay spec → image |
| `src/ci_repair/orchestrate.py` | Multi-job repair and final verification (`ci-repair-run`) |
| `src/ci_repair/pipeline.py` | One job: baseline, session, gate, fresh verification |
| `src/ci_repair/agent.py` | `RepairAgent`, mini's loop with a gated submission |
| `src/ci_repair/policy.py` | Operator policy: budgets, patch and publication verdicts |
| `src/ci_repair/publish.py` | Draft PR publication (`ci-repair-pr`) |
| `src/ci_repair/workspace.py`, `context.py`, `model.py`, `diagnostics.py` | Containers, log evidence, model adapter, private tracebacks |
| `eval/` | Evaluation harness and archived evidence; not part of the package |
| `examples/buggy` | The deliberately broken fixture used by the Docker tests |

## Tests

- Tests never call a paid model. The Docker tests drive the real mini loop with a
  scripted model; the rest replace containers with fakes.
- `tests/test_orchestrate_flow.py` shows the main fake: each check is a predicate on
  the source tree and each "agent" edits the tree, while Git state, ordering,
  budgets, policy and reports stay real. Use it for orchestration changes.
- GitHub is faked at the `gh` command boundary (`tests/test_github.py`,
  `tests/test_webhook.py`, `tests/test_end_to_end.py`).
- A bug fix starts with a test that fails before the fix.
- Mark tests that need Docker with `pytest.mark.docker` and the
  `CI_REPAIR_DOCKER_TESTS` skip, as `tests/test_docker.py` does.

## Style

- `ruff` with a line length of 100; the gate checks lint and format.
- Small, explicit functions; add an abstraction only for a concrete need.
- Comments say why, not what. Public entry points have a docstring.
- Reports are shared (draft PR bodies, evaluation artifacts): never put a traceback,
  log excerpt or environment value in one. Use `diagnostics.record_error`.

## Commits and the changelog

- One concern per commit, with a Conventional Commits prefix: `feat`, `fix`,
  `refactor`, `perf`, `test`, `docs`, `eval`, `chore`, and a scope when useful
  (`fix(policy): ...`).
- Each feature iteration adds a stage to [CHANGELOG.md](CHANGELOG.md) under
  **Unreleased**, in the same commit or the next: Motivation, Added/Changed/Fixed/
  Removed, Verification and Known limits, with commit hashes, and an updated
  timeline row.

## Evaluation

Paid or long experiments run on hosted runners, never locally:

1. Run `.github/workflows/compare.yml` (paired CI Repair / Pi trials) or
   `lca-replay.yml` (environment replay) from the Actions tab.
2. Download the run: `gh run download RUN_ID --dir runs/compare-gh-RUN_ID`.
3. Archive its evidence: `uv run python eval/archive.py runs/compare-gh-RUN_ID
   eval/results/compare-RUN_ID`. Add the run to `eval/results/README.md`.
4. Add the run's tables to `eval/README.md` and a section to `eval/reproduce.py` that
   recomputes them; then `uv run python eval/reproduce.py > eval/results/reproduced.md`.
5. `uv run python eval/reproduce.py --check` must pass, and the unit gate runs it.

Every reported number names its run, commit, model, budget and task set. Numbers
derived from earlier data are labelled estimates. A toy run shows that a path works;
it is never reported as a success rate.
