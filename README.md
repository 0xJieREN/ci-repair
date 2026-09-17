# CI Repair

A small local CI repair pipeline around **mini-SWE-agent**, with an independent
Docker verifier. Alpha: local v0.1, no automated repair PRs or webhook service.

See [design](docs/design.md) for acceptance criteria and boundaries.

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

# Collect the real failure in the same image used by the pipeline.
docker run --rm --network=none \
  -v /tmp/ci-repair-example:/workspace:ro ci-repair-demo:local \
  python -m unittest discover -s tests -k test_positive_interval \
  > /tmp/ci-repair-failure.log 2>&1
# Exit 1 above is expected.
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

## Verify the implementation

```sh
uv run ruff check .
uv run ruff format --check .
uv run pytest -q                         # unit tests; Docker cases skipped
CI_REPAIR_DOCKER_TESTS=1 uv run pytest -q  # also exercises real containers
```

Docker integration tests use a scripted model and the real mini-SWE-agent loop.
They verify orchestration and rejection of test modifications, not model repair
ability. GitHub Actions runs these tests without API credentials. This is CI for
this project, not automated repair integration for other repositories.

For restricted local environments, set `UV_CACHE_DIR` and
`MSWEA_GLOBAL_CONFIG_DIR` to writable directories.
