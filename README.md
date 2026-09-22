# CI Repair

A small CI repair system around **mini-SWE-agent**, with an independent Docker
verifier. Alpha: GitHub webhook intake, run-level collection of every failed job,
automatic CI environment reconstruction, a deterministic operator policy, bounded
repair with system-decided stopping, fresh-environment verification of one
cumulative patch, and gated draft PR publication. Never merges.

Start with the [repair lifecycle](docs/lifecycle.md). See also [policy](docs/policy.md),
[environment reconstruction](docs/environment.md) and the historical
[v0.1 design](docs/design.md).

```text
CI failure → webhook → canonical admission → collect all failed jobs
→ reconstruct + build environment → policy → repair (ordered, cumulative)
→ verify → publication gate → Draft PR (ALLOW) or REVIEW_REQUIRED
```

## Automatic flow

```sh
cp config/policy.example.yaml ~/ci-repair-policy.yaml   # keep it outside repositories
export CI_REPAIR_WEBHOOK_SECRET=...                     # the GitHub webhook secret
uv run ci-repair-webhook serve --state-dir ~/ci-repair-state \
  --policy ~/ci-repair-policy.yaml --env-file .env
```

The same steps can be run by hand for one run:

```sh
uv run ci-repair-github OWNER/REPO RUN_ID --all-jobs --output runs/run-import
uv run ci-repair-run runs/run-import --policy ~/ci-repair-policy.yaml --env-file .env \
  --output runs/run-repair
uv run ci-repair-pr auto runs/run-repair --policy ~/ci-repair-policy.yaml \
  --output runs/run-publication   # draft PR only if the gate says ALLOW
```

Automatic draft publication is off unless the policy sets `publication.draft_pr:
ALLOW`; read the branch-workflow secrets caveat in the [lifecycle](docs/lifecycle.md)
first.

## Import GitHub Actions failures

Use `ci-repair-github OWNER/REPO RUN_ID --output runs/import-01` to collect a
failed job and its exact source commit. See [GitHub Actions import](docs/github-actions.md)
for supported events, job selection and connection to the repair pipeline.

## Prepare and publish repair PRs

Use `ci-repair-pr prepare` to produce a local commit and reviewable PR body, then
`ci-repair-pr publish` to explicitly create a draft PR, or `ci-repair-pr auto` to
do both only when the deterministic publication gate says ALLOW. Moved branches
and changed patches are rejected; DENY blocks every path.
See [PR provenance and publication](docs/pull-requests.md) for the complete flow
and current limits, including head-only publication and no fork support.

## Run locally

Requires macOS/Linux, Python 3.12+, uv, and a running Docker engine. On macOS,
`brew install colima docker` then `colima start --cpu 2 --memory 4 --disk 20`.

```sh
uv sync --locked
docker build -t ci-repair-demo:local -f examples/Dockerfile examples

# Create a separate committed fixture, leaving this repository unchanged.
cp -R examples/buggy /tmp/ci-repair-example
git -C /tmp/ci-repair-example init
git -C /tmp/ci-repair-example add .
git -C /tmp/ci-repair-example -c user.name=demo -c user.email=demo@localhost commit -m fixture

# Collect the real failure without relying on host bind mounts.
docker create --name ci-repair-collect --network=none ci-repair-demo:local \
  python -m unittest discover -s tests -k test_positive_interval
docker cp /tmp/ci-repair-example/. ci-repair-collect:/workspace/
docker start -a ci-repair-collect > /tmp/ci-repair-failure.log 2>&1
# The captured test failure is expected.
docker rm ci-repair-collect
```

Configure DeepSeek credentials in an ignored `.env` file (copy `.env.example`
and fill in the key locally). Never commit or paste your key into a run command.

```sh
uv run ci-repair /tmp/ci-repair-example \
  --env-file .env \
  --failure-log /tmp/ci-repair-failure.log \
  --image ci-repair-demo:local \
  --model deepseek/deepseek-flash \
  --test 'python -m unittest discover -s tests -k test_positive_interval' \
  --regression 'python -m unittest discover -s tests' \
  --allow src/ --steps 30 --cost 1 --wall-seconds 600
```

The model receives the command and log, not the bug location or desired edit.
The [official DeepSeek quick start](https://api-docs.deepseek.com/) currently
names `deepseek-flash`. The included registry uses conservative peak rates from
[DeepSeek pricing](https://api-docs.deepseek.com/quick_start/pricing), checked
2026-09-17. Estimates are not billing receipts; off-peak charges may be lower.

Other LiteLLM providers work with their standard environment variables. For
MiniMax's Anthropic interface, set `ANTHROPIC_API_KEY` (or `ANTHROPIC_AUTH_TOKEN`),
`ANTHROPIC_BASE_URL=https://api.minimax.io/anthropic`, and
`LITELLM_MODEL_REGISTRY_PATH=config/minimax-pricing.json`; use
`--model anthropic/MiniMax-M2.7`. Its registry follows the
[official pricing table](https://platform.minimax.io/docs/guides/pricing-paygo).

`runs/<id>/report.json` is authoritative: only `PASS` exits zero. It records
baseline commit, exact local image ID, test outputs, changed files, `stop_reason`
and `agent_exit`, requested/policy/effective budgets and actual usage (models,
calls, steps, cost, command and wall time). Add `--policy FILE` to apply an
operator policy; budget flags are requests clamped to its maxima. `patch.diff`, `trajectory.json`, `context.json`,
`failure.log`, `source.tar` and individual test records remain local and ignored
by Git. The input repository is never edited. Apply a successful patch manually
after reviewing it. Unexpected provider errors record their type; details may
be available in the private trajectory.

The fixture deliberately contains failing tests. Our test suite lives in
`tests/`; do not treat the fixture's intentional failure as a project regression.

## Verify locally or on a server

```sh
bash scripts/check.sh --colima  # macOS: full gate, automatically stops Colima
bash scripts/check.sh           # Linux/server with Docker already running
bash scripts/check.sh --unit    # fast checks; Docker acceptance cases skipped
```

GitHub Actions calls the same script. You do not need to push code before
checking it. The full gate includes Docker repair/verification and local Git
publication tests; no model API key is required. See [verification boundaries](docs/verification.md)
for platform differences and what still requires a real GitHub service.

The model CLI uses mini-SWE-agent's native `get_model()` factory. Use
`--model-class litellm` (default) or `--model-class openrouter`; provider adapters,
cost accounting and the agent loop remain upstream. For OpenRouter, set
`OPENROUTER_API_KEY` and use its model name. See the [repository review](docs/review-2026-09-18.md)
for the fixes, efficiency changes and remaining limits.

For restricted local environments, set `UV_CACHE_DIR` and
`MSWEA_GLOBAL_CONFIG_DIR` to writable directories.

## Evaluation baseline

Six fixed synthetic cases and hidden-oracle controls are available through
`uv run python -m ci_repair.evaluate`. See [evaluation protocol](docs/evaluation.md)
for control runs, per-trial budgets and the limits of these measurements.
Use `--runner both --repetitions 3` for a paired CI Repair/upstream comparison;
paid experiments also require an explicit `--max-total-calls` ceiling.

A separate [historical seed corpus](benchmarks/historical/README.md) replays two
real more-itertools bugs from complete pinned upstream repositories, with
reference-fix/no-op controls and the original upstream regression suites.

## Reviewed replay plans

`ci-repair-plan COLLECTION --output PLAN.json` creates a review-only draft from
the pinned workflow. Fill in the prepared image, regression command and allowed
paths, then use `ci-repair --plan PLAN.json --model ...`. See the
[plan contract and supported workflow subset](docs/replay-plan.md).

The first external CI dataset integration uses [LCA local check replay](benchmarks/lca/README.md),
with pinned archived logs and reviewed lint/type-check recipes. See the
[benchmark comparison](docs/benchmark-selection-2026-09-22.md) for selection and scoring limits.
