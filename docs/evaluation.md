# Paired repair evaluation

The six fixed synthetic Python cases cover unit logic, boolean configuration,
JSON import/API use and syntax. Each includes an explicit behavior specification,
broken source, public checks, hidden assertions and correct/overfit controls.
Specifications describe required behavior; hidden test inputs and reference fixes
never enter model tasks or repair containers. This is not a representative corpus
of real-world CI failures, nor a claim of training-data isolation.

## Runners and the comparison contract

- `--runner ci-repair`: the production pipeline, including baseline reproduction,
  structured failure context and its own independent public verification.
- `--runner upstream`: upstream `DefaultAgent` with the **unmodified agent
  templates from mini-swe-agent 2.4.6's `mini.yaml`**, raw failure output and the
  same task specification, commands and path constraints. It produces a candidate;
  it does not invoke CI Repair's context builder or internal verifier.
- `--runner both`: paired trials. Runner order alternates by case and repetition
  to reduce systematic warm-cache/order effects. No model seed is implied.

Both use the same hardened Docker environment and native LiteLLM adapter
configuration (including observation formatting, max output tokens and disabled
retries). The upstream comparison is therefore **an upstream agent/prompt baseline
inside a common harness**, not an untouched interactive `mini` CLI installation.
Prompt file hash, package versions and these adaptations are recorded. Container
platform information is queried inside Docker rather than copied from the host;
otherwise a macOS host would incorrectly activate upstream BSD sed guidance.

A case is prepared once. Both runners receive the exact same clean Git commit,
real captured failing output, image ID, task, allowed paths and step/cost/wall
budgets. Git fixture metadata is deterministic. Each trial uses a fresh model
instance and fresh containers. References are used only by explicit control mode.

After either runner returns, the same external grader reapplies its patch to the
original source and checks source boundaries, original failure, regression and a
separate hidden oracle. Public checks and the oracle each get a fresh container.
CI Repair's internal verification is retained as part of its runner overhead;
external grading has the same cost and rules for both. Upstream submission is
never labeled as verified success.

## Commands

The full gate `bash scripts/check.sh --colima` exercises correct controls for all
six cases with both runners, plus overfit and no-op controls. No paid calls.
With a prepared `ci-repair-demo:local` image and Docker running:

```sh
uv run python -m ci_repair.evaluate benchmarks/cases \
  --runner both --repetitions 3 --control overfit \
  --output runs/paired-overfit-01

# Explicit paid experiment: 6 cases x 2 runners x 3 repetitions x at most 10 calls.
uv run python -m ci_repair.evaluate benchmarks/cases \
  --runner both --repetitions 3 --model deepseek/deepseek-flash \
  --env-file .env --steps 10 --max-total-calls 360 \
  --cost 0.1 --wall-seconds 180 --command-seconds 30 \
  --output runs/paired-model-01
```

`--max-total-calls` is required for model experiments; insufficient planned
capacity is rejected **before any model or Docker work**. Each trial has its own
step limit, so unused calls are not transferred to another trial. Provider/model
errors stop a paid experiment rather than spending the remaining budget blindly.
Cost is an estimate checked between responses and can overshoot by one response;
the call ceiling is not an exact dollar billing cap. No automatic retries or
resumption of partial runs are provided.

On macOS, always stop and verify Colima after an experiment, even on failure:

```sh
(
  trap 'colima stop; colima list' EXIT
  colima start
  # Run one experiment command here.
)
```

## Outputs and metrics (schema version 2)

- `experiment.json`: declared budgets, full trial order, image, code/lock hashes,
  upstream prompt provenance, requested model and host/runtime information.
- `fixtures/`: one original source and actual failure per case.
- `trials/`: runner trajectory/report/patch, common grader evidence and score.
- `summary.json`: incrementally saved, with a completion flag, per-runner averages,
  paired outcomes and complete per-trial scores. `comparison.md` is its table.

**Repair success** means the common public gate and hidden oracle pass.
**False PASS** means the common public gate accepts but the hidden oracle fails.
Its rate is among accepted candidates with completed oracle outcomes, not among
all trials. `runner_verified` separately records CI Repair's own claim; upstream
has no such claim and records null.

Provider/infrastructure errors and unreproduced baselines are excluded from the
valid-trial success denominator and reported explicitly. The all-attempts rate
is also retained. Budget timeouts are unsuccessful trials. Invalid/incomplete
pairs do not count in paired outcomes. Unknown oracle results are not safe passes.

`repair_seconds` measures the entire runner invocation (including CI orchestration
where applicable); `grader_seconds` measures common external grading;
`trial_seconds` is their sum. Fixture preparation is excluded. Calls, estimated
cost, changed-line count and patch bytes are recorded per trial. Averages include
completed recorded attempts; inspect error counts before comparing them.

Requested aliases cannot guarantee an immutable backend model. Each score also
records the provider's returned model identifiers from raw responses. Check those
before interpreting results. Sequential order, provider caching and machine load
can affect time/cost; do not interpret a tiny timing delta as a stable advantage.

Six cases repeated three times remain **six distinct cases**, not 36 independent
problems. This experiment checks plumbing and provides descriptive results. It
cannot establish superiority to mini-SWE-agent or Claude Code. It changes prompts,
context and internal verification together; causal attribution needs ablations.
Expand to 20–50 representative failures (including lint/type/dependency failures)
and freeze an evaluation split before tuning prompts based on these results.

Upstream sources: [DefaultAgent](https://github.com/SWE-agent/mini-swe-agent/blob/v2.4.6/src/minisweagent/agents/default.py),
[mini.yaml](https://github.com/SWE-agent/mini-swe-agent/blob/v2.4.6/src/minisweagent/config/mini.yaml).
