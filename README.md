# CI Repair

A small local CI repair pipeline around **mini-SWE-agent**, with an independent
Docker verifier. Alpha v0.3: local repair, GitHub Actions failure import, explicit PR provenance,
and verified-patch draft PR publication. No automatic triggers or merging.

See [design](docs/design.md) for acceptance criteria and boundaries.

## Import GitHub Actions failures

Use `ci-repair-github OWNER/REPO RUN_ID --output runs/import-01` to collect a
failed job and its exact source commit. See [GitHub Actions import](docs/github-actions.md)
for supported events, job selection and connection to the repair pipeline.

## Prepare and publish repair PRs

Use `ci-repair-pr prepare` to produce a local commit and reviewable PR body, then
`ci-repair-pr publish` to explicitly create a draft PR. PR inputs require an
explicit checkout SHA; moved branches and changed patches are rejected.
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
baseline commit, exact local image ID, test outputs, changed files, duration,
model calls and estimated cost. `patch.diff`, `trajectory.json`, `context.json`,
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
