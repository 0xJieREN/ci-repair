# Small evaluation baseline

This first corpus contains six **synthetic** Python failures: three unit-logic
cases, a boolean configuration parser, an incorrect JSON import/API use, and a
syntax failure. It does not yet represent lint, static typing, package resolution,
external services, or a measured real-world CI failure distribution.

Each JSON case defines broken source, a public check, hidden oracle assertions,
a correct reference, and a deliberately overfitted reference. Only broken source
and public checks enter the repair repository. The oracle is copied into a new
container **after** the repair gate completes. References are used only when an
explicit control mode is selected; model mode does not send them to the agent.
Fixtures are public for reproducibility, so this is held-out runtime evidence,
not a claim of contamination-free training data.

## Running controls

Build `ci-repair-demo:local` with the existing full acceptance script first. With
Docker running, from the repository root:

```sh
uv run python -m ci_repair.evaluate benchmarks/cases \
  --control correct --output runs/eval-correct-01
uv run python -m ci_repair.evaluate benchmarks/cases \
  --control overfit --output runs/eval-overfit-01
uv run python -m ci_repair.evaluate benchmarks/cases \
  --control noop --output runs/eval-noop-01
```

On macOS start Colima before an experiment and always stop it afterwards, even
on failure. For example, run an experiment in a subshell:

```sh
(
  trap 'colima stop' EXIT
  colima start
  uv run python -m ci_repair.evaluate benchmarks/cases \
    --control correct --output runs/eval-correct-02
)
colima list  # confirm Stopped
```

The normal acceptance gate runs all six correct controls, plus one overfit and
one no-op control. It does not make paid model requests.

## Model runs and interpretation

An explicit `--model PROVIDER/MODEL` replaces `--control`. `--env-file .env`,
`--steps N`, and `--cost USD` configure the existing native model factory and
**per-case** budgets. Six cases at 30 calls each allow up to 180 calls; do not
confuse that with a 30-call total budget. Upstream cost limits can overshoot by
one response. This iteration has not run a paid corpus evaluation.

`score.json` records case content hash, source commit, pinned image ID, gate and
oracle outcomes, model calls, estimated cost, repair duration and patch size.
`summary.json` is updated after each completed case and distinguishes partial
runs from a completed corpus. Wall time refers to the repair pipeline, excluding
oracle and fixture preparation time. Deterministic controls have zero model cost.

- Repair success: the public gate accepts and the separate oracle passes.
- False PASS: the public gate accepts but the oracle fails.
- Oracle infrastructure errors remain unknown, not successful or safe passes.
- False-PASS rate uses accepted candidates with completed oracle outcomes as its
  denominator. Report error counts alongside rates.

Correct references should pass; overfits should expose false PASS; no-ops should
produce NO_PATCH. These controls validate measurement, not model capability.

Before claiming benefits over upstream mini-SWE-agent, add a paired upstream
runner using identical model/version, source, image, budgets and independent
oracle; record prompt differences, repetitions and the full denominator. The
current control runner is **not** that upstream comparison. Expand to 20–50
real failures only after this harness and the comparison contract are stable.
