# Changelog

This file records how CI Repair evolved from a local prototype into an automatic,
evaluated repair service: what changed in each version, why, how it was verified,
and what remained open. Detailed designs live in [docs/](docs/lifecycle.md); detailed
measurements live in [eval/README.md](eval/README.md).

## Conventions

- **Versions** follow [Semantic Versioning](https://semver.org/). While the major
  version is 0, any minor version may change interfaces. A version begins at the
  commit that changed `version` in `pyproject.toml`. Each entry names its commit
  range, and the tag `vX.Y.Z` marks the last commit of that range (tags for 0.1.0
  to 0.4.0 were added retroactively on 2026-09-28). Work after the last release is
  listed under **Unreleased**.
- **Categories** follow [Keep a Changelog](https://keepachangelog.com/): Added,
  Changed, Fixed, Removed. Each version adds **Motivation** (the problem or evidence
  that led to it), **Verification** (what was actually run) and **Known limits**.
- **Evidence rule.** A claim about repair success or efficiency cites a recorded
  experiment: commit, model, budget, task set, repetitions and grader. Toy runs
  show that a path works; they are never reported as success rates. Estimates are
  labeled as estimates.
- Dates are commit dates (UTC+8). Commit hashes refer to this repository.

## Timeline

| Version | Dates | Commits | Theme | Key evidence |
|---|---|---|---|---|
| 0.1.0 | 09-17 | `ae8095d`…`6308035` | Local repair contract: one bounded agent attempt, independent Docker verifier | 1 live DeepSeek toy repair, 3 calls |
| 0.2.0 | 09-18 | `a1c0a34`…`df1ec51` | Import a real failed GitHub Actions job with provenance | Live fixture run imported and repaired (scripted model) |
| 0.3.0 | 09-18 | `57ebfcd`…`64860c1` | Pinned PR provenance, patch digests, resumable draft PR publication | 53 tests; publication against real bare remotes |
| 0.4.0 | 09-18…09-22 | `09afeb6`…`74c0d04` | Hardening, evaluation harnesses, then the automatic lifecycle: policy, stop gate, reconstruction, multi-job, webhook | Paired synthetic, historical and LCA pilots |
| 0.5.0 | 09-23…09-28 | `a0a3377`…`v0.5.0` | Scope focus, live acceptance, replay fidelity on a real dataset, paired comparison with Pi, orchestration and verification efficiency | Live webhook→draft PR; 39/68 LCA tasks usable; 234 + 21 paired trials; −53% wall time from lean verification (round 3, 117 trials) |
| 0.6.0 | 09-28 | `4504d62`…`v0.6.0` | Remove the manual single-job path; evaluation never scores provider failures; past results in one place | Full gate 240 passed; round 3 completed by a rerun with no provider errors |

## [Unreleased]

Nothing yet.

## [0.6.0] — 2026-09-28 — run-level only, sturdier evaluation

Commits `4504d62`…`v0.6.0`, newest stage first. Breaking: the manual single-job
commands are removed (R1).

### Stage R3: one record of past results (2026-09-28)

**Removed**
- `docs/research-results.md`. Its conclusions and caveats now live under 0.4.0 below,
  with links to revision `a0a3377`, so results are recorded in one place.

### Stage R2: evaluation harness robustness (2026-09-28)

**Motivation.** Round 3 (run `36399826597`) lost 41 of 117 trials to an exhausted
provider balance. The harness scored them as failed repairs, and a rerun would have
skipped them because they had a result.

**Fixed**
- An exception out of an agent loop (a CI Repair job report with status `ERROR` in
  phase `agent`, or a Pi error other than the turn-limit abort) is an infrastructure
  error: reported under Errors, never scored, and retried on the next invocation.

**Changed**
- Node and Pi are installed and required only when the `pi` arm runs;
  `experiment.jsonl` records the arms.

**Verification.** Lint and the unit gate; the harness itself has no automated tests.
Hosted run `36405797931` (commit `2492dd0`) reran every repetition of the 18 affected
tasks, CI Repair arm only: 54 trials, no provider errors, and the arms were recorded.

### Stage R1: remove the manual single-job path (2026-09-28)

**Motivation.** Since 0.4.0 every product path (webhook, `ci-repair-run`, evaluation)
goes through run-level collection, reconstruction and orchestration. The single-job
commands needed an operator to supply the image and commands, could only be
published after review, and no path or experiment used them any more, yet they kept
their own CLI, plan format, context loading and publication branch.

**Removed**
- `ci-repair`, the single-job CLI with an operator-chosen image and commands, and
  its `--plan` input.
- `ci-repair-plan` and the reviewed replay-plan format (`docs/replay-plan.md`);
  workflow reconstruction replaced it in 0.4.0.
- Single-job collection (`ci-repair-github --job-id`). `ci-repair-github` always
  collects every failed job of the attempt, so `--all-jobs` is gone.
- Single-job CI context in `pipeline.run` (`Config.ci_context`) and the publication
  branch for manual single-job reports; `ci-repair-pr` accepts run reports only.

**Changed**
- `make_model` moved to `ci_repair/model.py`, the strict YAML loader to `policy.py`,
  the workflow path helpers to `reconstruct.py` and `clean_head` to `workspace.py`.
- `ci-repair-pr prepare/publish` remain as the operator's path for `REVIEW` results.

**Verification.** Full gate `scripts/check.sh --colima`: 240 passed. The collection
tests that targeted the single-job collector now exercise `collect_run` (attempt
pinning, pagination, unsupported runs, empty logs, no overwrite, PR merge
provenance). The single-job Docker publication test was replaced by a check that a
single-job *run* report built from reused evidence passes `verified_run`.

**Known limits.** A workflow the reconstructor cannot replay can no longer be repaired
with a hand-chosen image; it stays `UNSUPPORTED_ENVIRONMENT`.

## [0.5.0] — 2026-09-28 — replay on real workflows, agent comparison, lean verification

Everything after 0.4.0, newest stage first. The version was not bumped while these
stages landed, so they ship together; the stages below keep their order and commits.
New report fields: `baseline_source`, `verification_source`, `same_as` (U4); per-job
`fixup` and `REGRESSED` handling (U3).

### Stage U4: verification without redundant reruns (2026-09-28, `7cee58c`)

**Motivation.** Analysis of round 1 of the Pi comparison (hosted run
`36149891145`, 117 CI Repair trials) showed that the agent loop was not the slow
part. CI Repair took 227 s per trial on average (median 60 s, p90 761 s), against
66 s for Pi. The share of that time by phase was: run-level baseline 16%, a second
identical baseline inside the pipeline 15%, probes 22%, final verification 23%, and
the agent loop about 18%. For 39 of 49 replayed jobs the regression command is the
failing command, yet every verification ran it twice in two fresh containers. A
single-job run also re-verified the patch its probe had just verified. On task 27,
one 88 s command ran six times for a 3-call repair.

**Changed**
- `verify_patch` runs an identical regression command once. The evidence still has
  one record per check; the regression record is marked `same_as: failing` and
  counts zero seconds.
- `pipeline.run` accepts the caller's `baseline`. The run orchestrator passes its
  baseline for a job repaired with no earlier repairs, because the candidate is
  then the original tree. Later jobs still replay their baseline on the candidate.
  The pipeline report records `baseline_source: provided`.
- Final verification reuses a job's evidence when that job's checks already passed
  with a patch that was applied to the original snapshot, in the job's image, and
  that yields exactly the final tree (compared by Git tree ID). This applies to
  single-job runs and to jobs that no later repair changed. The job records
  `verification_source`. Other jobs are re-verified as before. The publication gate's
  requirement of two passing records per job is unchanged.
- The fix-up task log no longer repeats an identical regression output.

**Verification.** Full gate `scripts/check.sh --colima`: 255 passed, including a new
Docker test for a single-job run that reuses its baseline and verification and
unit tests for identical checks and a provided baseline. Replaying the recorded
round-1 durations under these rules estimated 53% less CI Repair wall time (mean
108 s instead of 227 s). Hosted round 3 confirmed the estimate on all 39 tasks × 3
repetitions: 21 tasks from run `36399826597` (commit `7ab19ca`) and the 18 tasks hit by
an exhausted provider balance rerun in `36405797931` (commit `2492dd0`, same repair
path). Paired with round 1, wall time fell 53% (median 60 → 40 s, mean 227 → 107 s,
p90 761 → 274 s), model calls did not change materially (mean 9.2 → 8.6), and 111 of
117 passed against 107. Pi on the same trials: mean 66 s, 113 passed. See
[eval/README.md](eval/README.md#round-3-verification-without-redundant-reruns-2026-09-28).

**Known limits.** A job reused this way is no longer rerun a second time, so the final
verification is not a flakiness retry. Probes are still synchronous, and the agent is
still not told why a probe failed.

### Stage U3: paired comparison with a general coding agent (2026-09-25 … 09-27)

**Motivation.** Earlier pilots compared CI Repair only with upstream mini-SWE-agent on
small or selected cases. The open question was whether the CI-specific orchestration
beats a strong general coding agent given the same environment.

**Added**
- `eval/compare.py` (`95ea6b2`). It packages each usable LCA task once, then runs
  CI Repair (the production `repair_run` path) and one Pi session over all failed
  jobs, in alternating order. Both arms use the same images, model
  (`deepseek/deepseek-flash`, default thinking), per-job budget and network-less
  containers. One external grader decides for both.
- Hosted-runner workflow `compare.yml` (`8cd5bce`). Throttled burstable CPU
  (81% steal) made wall-clock budgets depend on the host, and that hurt the
  verification-heavy arm more.
- Arm selection for reruns (`38c7769`).

**Changed**
- Multi-job orchestration (`e64f527`). Each job's task lists the other failed jobs
  with the first error block of each log (bounded, untrusted). A job broken by a
  later repair (`REGRESSED`) gets one fix-up attempt on the combined change; only a
  verified fix-up triggers re-verification.

**Evidence**
- Round 1, commit `8cd5bce`, 39 tasks × 3 repetitions: CI Repair 107/117, Pi 113/117.
  The differences were all on multi-job tasks or at the budget limit (45, 53, 57).
  CI Repair's own verdict matched the grader in all 117 trials. It used a median of
  5 calls (Pi 8), but its median wall time was 60 s against Pi's 25 s.
- Round 2, commit `38c7769`, the 7 multi-job tasks: 16/21 → 20/21 (task 57: 0/3 → 3/3).
  Against Pi's round 1 the estimated total is 111/117 versus 113/117. This mixes
  commits and is an estimate.

**Known limits.** 75 of each arm's 117 trials are difficulty 0, and both arms solve
all of them, so the set separates the agents only on a few tasks. The 39 tasks come
from 13 repositories. Pi's time excludes grading, while CI Repair's time includes
its own verification.

### Stage U2: replay fidelity on a real CI dataset (2026-09-24 … 09-25)

**Motivation.** A static audit of LCA CI builds repair (68 tasks) found that only 32
could be reconstructed. About half of the failed jobs were blocked by gaps in the
reconstructor, not by unsafe semantics.

**Added**
- Evaluation of a subset of GitHub expressions, mapping matrices, static step
  conditions, `ubuntu-20.04`, custom shells, and the `pre-commit` and `pox` actions.
  A warm-up step prepares tox/nox/pre-commit environments offline (`5f128bf`).
- `eval/lca_coverage.py` and Pi tool routing (`eval/pi/docker-tools.ts`, `1c7de4f`).
- A setup network and environment provided by the operator, e.g. a frozen PyPI
  mirror, kept out of the image and hashed into its tag (`e37ecac`).
- `eval/lca_replay.py` and `lca-replay.yml` (`9cad47f`): a task is `USABLE` only if
  setup succeeds, the failure reproduces offline, and the reference diff passes the
  verifier.

**Fixed**
- Replay images behave like a checked-out hosted runner: a real Git checkout pruned
  to the failing commit, non-interactive apt, the warm-up keeps only tool
  environments, and a higher pids limit for builds (`eda5d5b`).
- PEP 517 build requirements are installed offline from a wheelhouse
  (`d3473a3`, `ce9ec80`, `fad880a`).
- The CI/replay error comparison ignores the `stdout:`/`stderr:` prefixes that
  actions add (`8f5a802`).
- LCA replay harness paths and mirror access (`fd15b61`, `971bcc7`, `90de54f`).

**Evidence.** Static coverage went from 32 to 61 of 68 tasks. On hosted runners, usable
tasks went from 30 to 39 of 68 (run `36124065326`). Of the 49 usable jobs, 37 replay
an error line from their CI log. The remaining 29 tasks are blocked by the tasks
themselves, for example tests that need network access or coverage gates.

### Stage U1: focus and first live acceptance (2026-09-23 … 09-24)

**Fixed** (`a0a3377`)
- Webhook repairs run on the main thread so deadline signals work.
- Continued steps keep shell exit behavior, and the container job's default shell
  is used.
- Unsuccessful jobs that did not fail (for example timed out) count against run
  coverage.
- Path policy is checked again before publication.

**Removed** (`1822a92`)
- The standalone synthetic, historical and LCA benchmark runners, their fixtures and
  their experiment files. The measured conclusions are kept under 0.4.0 below; the
  code and data remain at revision `a0a3377`.
  Reason: the product had become the automatic lifecycle, and those runners measured
  the older single-job path.

**Evidence** (`8fda4e8`, [docs/acceptance.md](docs/acceptance.md)). A real three-job
failure on a disposable public repository went through signed webhook, redelivery
deduplication, the stale-source check, cumulative repair (2 repaired,
1 `FIXED_BY_PRIOR`), the `REVIEW` and then `ALLOW` publication gates, and an automatic
draft PR whose branch passed real CI. Model use: 7 calls, $0.0039.

## [0.4.0] — 2026-09-22 — automatic repair lifecycle

Commits `09afeb6`…`74c0d04`. The first half hardened the 0.3 path and built evaluation
harnesses; the second half turned the manual tool into an automatic service.

**Motivation.** Up to 0.3 every run needed an operator to supply the image, the
commands and one job. Real failures span several jobs, and publishing
automatically requires decisions that a model must not make.

**Added: evaluation**
- Synthetic cases with hidden oracles and correct, overfit and no-op controls
  (`f95e034`, `e66119a`).
- A paired baseline against upstream mini-SWE-agent with a common external grader
  (`ead865b`).
- Pinned historical failures (`a6a312b`) and the first LCA selected-check replays
  (`50f9506`).

**Added: automatic lifecycle**
- Operator policy (`c33b8b3`): strict YAML loaded from outside every repository
  under repair, with `ALLOW`/`REVIEW`/`DENY` for patch categories, size, triggers,
  models and budgets. Network, secrets and auto-merge are fixed to `DENY`.
- `RepairAgent` (`a4af771`) separates "the agent submits" from "the system stops".
  Submission is a request to a deterministic gate. A silent verifier probe can end
  the session as soon as a patch verifies. Step, cost and wall-time limits are
  reported separately (`agent_exit`, `stop_reason`).
- Environment reconstruction (`ad80903`): a hashed spec per failed job, derived from
  the workflow, and a content-addressed image built from its setup steps.
  Unsupported semantics fail closed.
- `collect_run` (`ad31efb`) collects every failed job of a run attempt.
- `ci-repair-run` (`5cf6828`) processes a run's jobs in `needs` order, reproduces
  each baseline, skips jobs a prior repair already fixed, accumulates one patch and
  re-verifies every job.
- A publication gate and automatic draft PRs (`e7f919c`).
- A signed `workflow_run` webhook with an idempotent SQLite queue and canonical
  re-admission (`f5e6dd8`).
- Replay-plan builder for operator review (`9b413e2`).

**Changed**
- Each verification command gets its own fresh container (`06f3528`).
- The context keeps structured, bounded error evidence (`8313837`).
- Local, server and GitHub Actions share one check script (`b60c755`).
- Container platform variables come from inside Docker (`53090c6`); a macOS host had
  otherwise triggered BSD `sed` guidance.

**Fixed**
- Verification hardening and reuse of the upstream model adapters (`09afeb6`).
- Probes stage into a throwaway index, so the agent's `git diff` stays intact, and
  diffs are taken against the recorded baseline commit (`24bce9b`).

**Evidence**

| Experiment | CI Repair | Upstream mini | Conclusion |
|---|---|---|---|
| 6 synthetic cases × 3 (09-19) | 18/18, 73 calls | 18/18, 107 calls | No success difference on easy cases |
| 2 historical more-itertools defects (09-19) | 2/2, 17 calls | 2/2, 20 calls | No demonstrated advantage |
| LCA tasks 24 and 107, selected checks (09-22) | 2/2, 13 calls | 2/2, 28 calls | Local replay only, not the official score |

All trials requested `deepseek/deepseek-flash`, a provider alias rather than a
pinned backend version. The historical cases used public fixes and hindsight-informed
checks; the LCA pilot had no hidden oracle, so its false-PASS rate is unknown, not
zero. These cohorts must not be pooled into one success rate. The protocol
(`docs/evaluation.md`), experiment records (`docs/experiments/`) and inputs
(`benchmarks/`) remain at
[`a0a3377`](https://github.com/0xJieREN/ci-repair/tree/a0a3377c69e04c21fb933d398546a9a5e482bb9d).

**Known limits.** At release, the lifecycle had only been exercised with simulated
GitHub responses, and the evaluation cohorts were too small to pool.

## [0.3.0] — 2026-09-18 — provenance and draft PR publication

Commits `57ebfcd`…`64860c1` (the version bump landed in `6b0ccc5`).

**Added**
- Pinned PR checkout provenance: explicit head checkout, historical merge parents,
  and rejection of forks and `pull_request_target`. Reports record a verified patch
  SHA-256 (`57ebfcd`).
- `ci-repair-pr prepare/publish` (`6b0ccc5`): a local verified commit, then a
  creation-only, resumable draft PR. Publication refuses changed evidence, a moved
  base or a dirty checkout.

**Verification** (`64860c1`). 53 tests, including four Docker cases. Publication was
tested against real local bare remotes. PR API responses were simulated, and no live
PR was created.

## [0.2.0] — 2026-09-18 — real GitHub Actions input

Commits `a1c0a34`…`df1ec51`.

**Added**
- `ci-repair-github` (`a1c0a34`) imports one failed job: a pinned checkout, the raw
  log with its hash, and run, attempt and job provenance carried into the repair
  context.
- A manually triggered failing workflow as a fixture (`931e711`).

**Fixed**
- Log capture when the log contains terminal escape sequences; `gh` 2.101.0
  refused them (`819ce0b`).

**Verification** (`df1ec51`). 33 tests in CI. A live fixture run was imported, and a
scripted-model repair passed fresh verification. No paid model calls were made.

## [0.1.0] — 2026-09-17 — local repair contract

Commits `ae8095d`…`6308035`.

**Motivation.** Build the smallest trustworthy repair loop: reuse a proven agent
loop instead of writing one, and never let the agent decide that it succeeded.

**Added**
- The design contract (`ae8095d`). Input is a clean Git HEAD, a real failure log,
  explicit failing and regression commands, a prepared image and allowed source
  prefixes. Commands come from the operator and are never inferred from logs.
- The pipeline (`1b6d8fc`):
  1. Archive the tree.
  2. Reproduce the failure in a disposable container.
  3. Run one bounded mini-SWE-agent 2.4.6 attempt in another container with no
     network, no mounts and dropped capabilities.
  4. Extract a binary diff.
  5. Apply it to the original archive in a fresh container and rerun the failing
     and regression commands.
  6. Persist all evidence locally.
- Docker CI, dotenv configuration and a DeepSeek model setup
  (`a8d42b2`, `75d7a6e`).
- A project rule to stop Colima after every local Docker session (`6308035`).

**Verification** (`3eaf567`). A live `deepseek/deepseek-flash` run repaired the
`examples/buggy` off-by-one: 3 calls, about $0.00076, and both the original and the
regression checks passed in a fresh container.

**Known limits.** One attempt and one job only, operator-supplied everything, no
GitHub integration. Explicitly deferred: webhooks, repair PRs, retries, model
comparisons.

## Design decisions that persisted

| Decision | Since | Rationale | Revisited |
|---|---|---|---|
| Reuse mini-SWE-agent's loop instead of writing one | 0.1.0 | Small surface; upstream-comparable | U3: a general agent (Pi) matched it on success. The measured gap came from orchestration, not from the loop |
| Only fresh-container verification can make a run PASS | 0.1.0 | The agent's own claim is not evidence | Never; U3 showed verdict = grader in 117/117 trials |
| One fresh container per verification command | 0.4.0 | No state leaks between checks | U4: identical commands run once, with the evidence still per check |
| Policy lives outside every repository under repair | 0.4.0 | A branch cannot relax its own rules | — |
| Unsupported workflow semantics fail closed | 0.4.0 | Wrong replay is worse than no replay | U2: widened the subset without weakening this |
| One job at a time; later jobs see earlier repairs | 0.4.0 | Deterministic, attributable per-job patches | U3: added sibling context and fix-up; a single session over all jobs is the open alternative |
| Evaluate on hosted runners with a frozen package index | U2/U3 | Replays must fail as the recorded CI did, on steady CPU | — |
