# Evaluation harness

Tools for measuring CI Repair on external CI-failure datasets. They are not part of
the installed package and are never run by the webhook or CLIs.

## LCA static coverage

`lca_coverage.py` fetches every failing repository of the pinned
[LCA CI builds repair](https://huggingface.co/datasets/JetBrains-Research/lca-ci-builds-repair)
revision blobless and asks the reconstructor whether each failed job can be
replayed. No model, container or build runs, so the result is an upper bound:
building the environment and reproducing the baseline come next.

```sh
uv run --with pyarrow python eval/lca_coverage.py --output runs/lca-coverage
```

A task counts only if all of its failed jobs are reconstructable. 2026-09-24,
68 tasks of the `default/test` split:

| Difficulty | SUPPORTED | REVIEW_REQUIRED | UNSUPPORTED | Total |
|---|---:|---:|---:|---:|
| 0 | 22 | 12 | 2 | 36 |
| 1 | 4 | 2 | 1 | 7 |
| 2 | 10 | 7 | 2 | 19 |
| 3 | 3 | 1 | 2 | 6 |
| All | 39 | 22 | 7 | 68 |

Before the reconstructor gained expression evaluation, mapping matrices, static
conditions, `ubuntu-20.04`, custom shells and the `pre-commit`/`pox` actions, the
same audit gave 25 / 7 / 36 (32 reconstructable). Remaining blockers: a Windows
runner, a dynamic `fromJson` matrix, `GITHUB_ENV`, go+python toolchains and an
action-only job without checkout.

The official LCA workflows add a `pypi_wayback` service that freezes PyPI at the
commit date; the official score runs those modified workflows on GitHub. Replays
that install today's packages can fail differently from the recorded CI, so a
build/baseline evaluation must use the same frozen index.

## LCA dynamic replay

`lca_replay.py` goes one step further for each task. For every failed job it builds
the replay image, with setup installing through the same PyPI wayback mirror the
official workflows use, frozen at the dataset's date. It then requires the failing
command to fail again offline, and requires the dataset's reference diff to pass the
fresh-container verifier. Only tasks where every failed job passes all three
(`USABLE`) are fair ground for comparing repair agents. The failure modes are
`SETUP_FAILED`, `BASELINE_NOT_REPRODUCED` and `REFERENCE_FAILED`. No model is called.

The manual workflow `.github/workflows/lca-replay.yml` runs one hosted x86 runner
per task (no secrets) and writes a summary table to the run page:

```sh
gh workflow run lca-replay.yml -f tasks=82,107       # empty: all 68 tasks
```

Hosted run [36124065326](https://github.com/0xJieREN/ci-repair/actions/runs/36124065326)
(2026-09-25, all 68 tasks):

| Difficulty | USABLE | REFERENCE_FAILED | BASELINE_NOT_REPRODUCED | SETUP_FAILED | UNSUPPORTED | ERROR | Total |
|---|---:|---:|---:|---:|---:|---:|---:|
| 0 | 25 | 4 | 1 | 2 | 2 | 2 | 36 |
| 1 | 4 | 2 | 0 | 0 | 1 | 0 | 7 |
| 2 | 9 | 5 | 1 | 2 | 2 | 0 | 19 |
| 3 | 1 | 3 | 0 | 0 | 2 | 0 | 6 |
| All | **39** | 14 | 2 | 4 | 7 | 2 | 68 |

The first full run had 30 usable tasks. Replay fidelity fixes found by these runs
(checkout history, hosted-runner apt behavior, warm-up cleanup, build pids,
offline build requirements) raised that to 39. Of the 49 usable jobs, 37 replay an
error line from their CI log. Five (beets) differed only by the `stderr:` prefix
its action adds, which the comparison now ignores. For the other seven the
heuristic finds no error line to compare; the one checked by hand (task 4)
replays the CI failure verbatim.

What still blocks the other 29 comes from the tasks, not from the replay:
tests or regression steps that need the internet (the replay is offline by
design), coverage gates that miss once network tests skip, an unpinned Git
dependency, preinstalled Rust, a 2 GB memory limit, submodules, a cache from another
job, and `sysctl` in a container.

The 39 tasks come from only 13 repositories (up to 6 each), so an agent comparison
must report results per repository and use a clustered or paired analysis, not
treat the tasks as independent.

## CI Repair versus Pi

`compare.py` runs both agents on usable tasks under the same conditions and grades
them with one external check. Each task is packaged once: collection, snapshot and
job images built through the frozen mirror. Then, per repetition, arms alternate:

- `ci-repair`: the production `repair_run` path on the task as a collection.
- `pi`: one Pi session over all failed jobs, in a network-less container from the
  first job's image, given the same commands and log excerpts.

Both use `deepseek/deepseek-flash` with the provider's default thinking (enabled;
Pi sends no effort level for this model), 30 model calls and 1200 s per failed job
(Pi's session gets the sum), and 600 s per command. Grading applies the final patch
to the original tree and requires every failed job's failing and regression
commands to pass in fresh containers, and the patch policy not to deny it. Each
result records four things separately: `checks_passed`, the arm's own verdict
(`own_accepted`, recorded and never trusted), the patch policy verdict
(`patch_policy`), and `auto_publishable`: checks passed, policy `ALLOW` and every
job environment `SUPPORTED`. The last is an upper bound, because a deployment's
trigger and publication rules apply on top. Cost is computed for both from provider
token counts with `config/deepseek-pricing.json`, over every repair and fix-up
trajectory. CI Repair's fix-up draws on what the repairs left of the run's budget
(the per-job budget times the jobs attempted), the same total Pi's session gets. Each invocation appends its commit,
versions and budgets to `experiment.jsonl`; scored trials are skipped on rerun. An
exception out of an agent loop (provider or transport failure, such as an exhausted
account balance) makes the trial an infrastructure error: it is reported under
Errors, never scored, and retried on the next invocation.

```sh
uv run --with pyarrow python eval/compare.py --output runs/compare --env-file .env \
  --network container:pypi-wayback --mirror http://localhost:8080 \
  --tasks 4,82 --repetitions 3
uv run --with pyarrow python eval/compare.py --output runs/compare --summarize
```

### Round 1: all 39 usable tasks, 3 repetitions (2026-09-25)

Hosted run [36149891145](https://github.com/0xJieREN/ci-repair/actions/runs/36149891145),
commit `8cd5bce`, mini-swe-agent 2.4.6, Pi 0.87.1; 234 trials, no infrastructure errors.

| | CI Repair | Pi |
|---|---:|---:|
| Trials passed | 107/117 (91.5%) | 113/117 (96.6%) |
| Difficulty 0 / 1 / 2 / 3 | 75/75, 10/12, 22/27, 0/3 | 75/75, 12/12, 26/27, 0/3 |
| Model calls, median (mean) | 5 (9.2) | 8 (11.0) |
| Estimated cost, mean per trial | $0.0069 | $0.0078 |
| Agent time, median | 60 s | 25 s |

- Only three tasks differ, all in Pi's favor: 45 (CI Repair 2/3), 53 (1/3), 57 (0/3).
  The mean per-task difference is −5.1 points. The 95% bootstrap interval, clustered
  by repository, is [−13.1, 0.0], so the difference is borderline.
- CI Repair's own verdict matched the external grader in all 117 trials (107 PASS,
  10 FAIL), so it never claimed an unverified fix.
- CI Repair made fewer calls and cost less. Its time includes its own baseline,
  probes and final verification, which Pi does not run.
- The differences come from multi-job tasks. On 57, repairing pyupgrade after black
  changed code that black then rejected. The final verification caught the
  regression, but nothing repaired it. On 53, two jobs share one cause, and each
  single-job session saw only its own log. 45 needed 26–30 calls per attempt, at
  the budget limit.
- Most tasks are easy for both agents. Difficulty 0 accounts for 75 of each arm's
  117 trials, so this set separates the agents only on the few harder tasks.

### Round 2: multi-job tasks after `e64f527`, 3 repetitions (2026-09-26)

Hosted run [36246418676](https://github.com/0xJieREN/ci-repair/actions/runs/36246418676),
commit `38c7769` (includes `e64f527`: sibling failure excerpts in each job's task,
and one fix-up repair for jobs broken by a later repair). CI Repair only, on the 7
multi-job tasks (26, 29, 53, 57, 60, 96, 142). Pi's code is unchanged, so its round 1
trials serve as the comparison. 21 trials, no infrastructure errors.

| Task | CI Repair round 1 | CI Repair round 2 | Pi round 1 |
|---:|---:|---:|---:|
| 53 | 1/3 | 2/3 | 3/3 |
| 57 | 0/3 | 3/3 | 3/3 |
| 26, 29, 60, 96, 142 | 15/15 | 15/15 | 15/15 |
| **Total** | 16/21 | 20/21 | 21/21 |
| Model calls, median (mean) | 12 (15.9) | 11 (14.0) | 14 (18.6) |
| Estimated cost, mean per trial | $0.0132 | $0.0098 | $0.0149 |

- 57: every trial repaired black and pyupgrade, final verification found that the
  pyupgrade change broke black, and one fix-up of black passed. All 3 trials passed,
  at 11–16 calls.
- 53: in both passing trials, repairing Core Test also fixed Linter
  (`FIXED_BY_PRIOR`). The failing trial had the sibling excerpt too, but both
  sessions used all 30 calls trying to recover the deleted `elevensports` and
  `ellentube` modules instead of removing their stale imports. This is agent
  variance, not an orchestration gap.
- The other five tasks stayed at 15/15 with similar calls, so the sibling excerpt
  did not hurt them. The verdict again matched the grader in all 21 trials.
- Combining round 2 with round 1 for the 32 unchanged tasks gives CI Repair about
  111/117 against Pi's 113/117. The remaining gap is 45 (2/3, budget-bound) and
  53 (2/3). This mixes two commits, so it is an estimate, not a fresh full run.

### Round 3: verification without redundant reruns (2026-09-28)

CI Repair only, all 39 tasks × 3 repetitions, measuring `7cee58c` (an identical
regression check runs once, the first repair reuses the run's baseline, and final
verification reuses evidence for an unchanged tree). Two hosted runs:

- [36399826597](https://github.com/0xJieREN/ci-repair/actions/runs/36399826597),
  commit `7ab19ca`. The DeepSeek account ran out of balance during the run: 41 trials
  received `Insufficient Balance` and were scored as failures by the harness of that
  commit. They are invalid (the harness now treats them as infrastructure errors).
  Only the 21 tasks with no invalid trial are kept from this run.
- [36405797931](https://github.com/0xJieREN/ci-repair/actions/runs/36405797931),
  commit `2492dd0` (v0.6.0), reran every repetition of the other 18 tasks:
  25, 26, 27, 29, 33, 35, 45, 53, 60, 96, 127, 128, 129, 130, 140, 142, 158, 160.
  No provider errors. The repair path is unchanged between the two commits; 0.6.0
  removed only the manual single-job commands and hardened the harness.

All 117 trials, paired by task and repetition with round 1:

| 117 trials | CI Repair round 1 (`8cd5bce`) | CI Repair round 3 | Pi round 1 |
|---|---:|---:|---:|
| Passed | 107 | 111 | 113 |
| Time per trial, median / mean / p90 | 60 / 227 / 761 s | 40 / 107 / 274 s | 25 / 66 / 171 s |
| Total time | 26,617 s | 12,522 s | 7,707 s |
| Model calls, median (mean) | 5 (9.2) | 5 (8.6) | 8 (11.0) |
| Estimated cost, mean per trial | $0.0069 | $0.0058 | $0.0078 |

- Total CI Repair wall time fell by 53%, as estimated from round 1's recorded
  timings (mean 108 s estimated, 107 s measured). Model calls did not change
  materially: the change touches only deterministic verification.
- Success did not regress: 111 against 107. Multi-job task 57 went from 0/3 to 3/3
  (from `e64f527`), and task 53 from 1/3 to 3/3; task 158 went from 2/3 to 1/3.
  All six failures stopped at the 30-call step limit (tasks 45, 158, 160); in round 1
  three of the ten failures were rejected by final verification.
- On the 21 multi-job trials the mean time fell from 385 s to 176 s, and all 21 passed
  (16 in round 1).
- Pi remains faster (mean 66 s against 107 s, total 7,707 s against 12,522 s) and
  passed two more trials. Pi's time still excludes grading, while CI Repair's
  includes its own verification. Task 160 fails in every arm.

### Round 4: baseline alongside the agent (2026-09-28)

Hosted run [36415596025](https://github.com/0xJieREN/ci-repair/actions/runs/36415596025),
commit `2c9af64` (S1, `82090ad`: the pipeline replays the baseline in its own container
while the agent starts; the gate waits for it before verifying anything). CI Repair
only, all 39 tasks × **1** repetition, to save cost: the saving is measured per job
from `baseline.json` and `baseline_wait_seconds` and does not need paired repetitions.
No provider errors.

| 39 trials | CI Repair round 3, repetition 1 | CI Repair round 4 | Pi round 1, repetition 1 |
|---|---:|---:|---:|
| Passed | 36 | 37 | 38 |
| Time per trial, median / mean / p90 | 46 / 101 / 273 s | 42 / 100 / 241 s | 25 / 66 / 206 s |
| Model calls, mean | 8.3 | 10.3 | 10.2 |

- Measured directly on the 43 pipeline baselines: the baseline command took 30.1 s
  per trial and the pipeline still waited 19.3 s for it, so 10.8 s per trial (36% of
  the baseline) now overlaps the agent. The estimate was at most 36 s. The agent
  usually writes its first patch within a few calls, before the baseline finishes,
  and the gate then waits for it.
- The run-level baseline now runs only after earlier repairs (3.2 s per trial).
- Total time did not measurably change (3,930 → 3,886 s). With one repetition this
  cannot resolve 10 s: the three repetitions of round 3 alone totaled 3,930, 4,522
  and 4,070 s, and round 4 made 2 more calls per trial (task 53 alone: 24 → 60 calls
  over its jobs), which the change cannot cause.
- Passes moved both ways: 45 and 158 passed, although they failed in round 3's first
  repetition, and 53 failed. Both failures (53, 160) stopped at the step limit. Task
  26 passed the grader but CI Repair itself reported `PARTIAL` at the step limit.

## What a pass means: audit against upstream fixes (2026-10-02)

The grader runs checks that live in the repository, and an agent may edit them: in
round 3, 34 of CI Repair's 111 passing patches change test files. A pass alone
therefore does not show that a check was repaired and not weakened. `audit.py` asks
an independent question of every passing patch, using the upstream fix the dataset
records for each task (`reference.diff` from `lca_replay.py`), which no agent sees:
does the patch change only files the upstream fix also changed, does it add more
suppression markers (`skip`, `xfail`, `noqa`, `type: ignore`, `pragma: no cover`)
than upstream, and does it remove more assertions than it adds? It runs on recorded
results; no model or container is used.

```sh
uv run python eval/audit.py --references runs/lca-replay runs/compare-a runs/compare-b
```

| Passing patches | CI Repair round 1 | CI Repair round 3 | CI Repair round 4 | Pi round 1 |
|---|---:|---:|---:|---:|
| Passed | 107 | 111 | 37 | 113 |
| Same files as upstream | 81 | 85 | 28 | 85 |
| Proper subset of upstream's files | 26 | 26 | 9 | 27 |
| Any file upstream did not change | 0 | 0 | 0 | 1 |
| Edits tests | 32 | 34 | 12 | 35 |
| Edits a test file upstream left alone | 0 | 0 | 0 | 0 |
| More suppressions than upstream | 0 | 0 | 0 | 1 |
| Removes more assertions than it adds | 0 | 0 | 0 | 0 |

Runs: round 1 `36149891145`, round 3 `36399826597` + `36405797931`, round 4
`36415596025`; references from replay run `36124065326`.

- Every passing CI Repair patch stays within the files of the upstream fix, and
  every test edit is to a test file the upstream fix edited too: where an agent
  changed a test, the maintainers' own fix changed that test as well.
- The one flagged patch is Pi's on task 53, repetition 2: it passed by changing two
  other extractor files and adding one suppression marker.
- Separating the verdicts for round 3: 111 passed the checks and CI Repair accepted
  the same 111; the patch policy says `ALLOW` for 74 and `REVIEW` for 37 (34 for
  test edits); 44 are also in a `SUPPORTED` environment, the upper bound for
  automatic draft PRs. Pi round 1: 113 / 75 / 45.
- Fix-up trajectories were missing from the cost of earlier rounds: one trial in
  round 3 ($0.0028, 0.4% of the round's total), three in round 2 ($0.0069) and one
  in round 4 ($0.0020), recomputed from the recorded usage. The tables above are
  unchanged at their precision.

Limits: the audit compares file sets and two line-level signals, not behaviour. A
patch inside the upstream file set can still be wrong in a way the selected checks
miss, and jobs that passed in the original run are not replayed.

## Why trials fail (2026-10-02)

Every failed trial of the four rounds was traced back to its trajectories. No new
inference; `trajectories.py` reproduces the counts from recorded runs.

```sh
uv run python eval/trajectories.py runs/compare-a runs/compare-b
```

| Failed CI Repair trials | Trials | Tasks | Cause | State |
|---|---:|---|---|---|
| A later job's repair broke an earlier one | 3 | 57 | orchestration | fixed by the fix-up (`e64f527`); 7/7 since |
| Step limit with no change in the tree | 16 | 160 (7), 53 (4), 158 (3), 45 (2) | agent | open |

One further trial (task 26, round 4) passed the grader while CI Repair reported
`PARTIAL`: the first job's session ended at the step limit, the second job's repair
also fixed the first, and a job whose repair failed is not checked against the final
patch.

The 16 open failures are one behaviour. In the sessions read in full (45, 53, 158,
160), the agent reads the failing location within its first three commands and then
never edits. It looks for another version of the code instead: Git internals, package
caches, wheels, the snapshot archive, the network, and on task 53 the environment and
filesystem for a patch file. Its reasoning says so; on task 158 it inspects `.pyc`
files in case they were compiled from the fixed source. The upstream fix was one line
on 158, four on 160 and six deleted import lines on 53, each at the location the agent
had already read.

All job sessions of the five recorded runs, provider errors excluded:

| Session outcome | Sessions | Mean calls | Searched history | Commands per session | Searched for copies | Commands per session |
|---|---:|---:|---:|---:|---:|---:|
| `NO_PATCH` | 26 | 30.0 | 25 | 4.5 | 23 | 5.7 |
| `PASS`, 15 or more calls | 38 | 20.7 | 27 | 1.7 | 31 | 2.3 |
| `PASS`, under 15 calls | 268 | 5.3 | 20 | 0.1 | 52 | 0.2 |

"Searched history" matches Git plumbing and history beyond the checkout (`reflog`,
`fsck`, `cat-file`, `rev-list`, `.git/` internals); "searched for copies" matches
package caches, wheels, `find /`, the snapshot archive and network tools. The
patterns are in `trajectories.py`; they are coarse and also match some ordinary
commands, which is why short passing sessions are not at zero.

- The behaviour belongs to the model, not to one harness. Pi's four failures (158
  once, 160 three times) show it too: no edit, 6 to 13 such commands each. Where Pi
  passes 45 and 53 it searches the same way first and edits late, within a session
  budget of 30 to 60 calls; CI Repair's sessions on 53 are two separate 30-call
  sessions that each repeat the search.
- CI Repair's task context names the run's commit, while the workspace history shows
  only the checkout and a local `baseline` commit. All 26 `NO_PATCH` sessions refer
  to that unresolvable hash in their reasoning (85 of 306 passing sessions do), and
  on 158 the agent calls it "the fix commit". Pi's prompt has no such hash and Pi
  searches anyway, so this is at most a contributing cue.
- Nothing in the container holds the fix: the archive and probe index are the
  failing snapshot, and the network is off. The search costs calls, not validity.

What this supports and what it does not: the remaining failures are not about
verification, feedback or multi-job orchestration, and a larger budget alone turns
some of them into late passes (Pi on 53). Whether telling the agent that no other
version exists, or interrupting a session that has not changed a file, turns the
search into an edit was a hypothesis; the next section tests it.

### The intervention, measured (2026-10-02)

Commit `5427d2a` removes the commit hash from the task context, states in the system
prompt that the workspace is the only copy of the code, and reminds a session once
if no file has changed after 10 model calls. Hosted run `36970961464` (commit
`bf9f92c`): tasks 45, 53, 158 and 160, CI Repair only, 3 repetitions, same model and
budget. The comparison is round 3 on the same tasks (run `36405797931`).

| Task | Round 3 | With the intervention |
|---|---:|---:|
| 45 | 2/3 | 2/3 |
| 53 | 3/3 | 3/3 |
| 158 | 1/3 | 3/3 |
| 160 | 0/3 | 0/3 |
| Total | 6/12 | 8/12 |
| `NO_PATCH` job sessions | 6 | 5 |
| Mean model calls per trial | 26.5 | 25.2 |
| Mean seconds per trial | 366 | 357 |
| Estimated cost, 12 trials | $0.317 | $0.319 |

The rule written before the run asked for at least 9 passes and at most 3 `NO_PATCH`
sessions to call the hypothesis supported, and 6 or fewer passes to call it refuted.
8 passes and 5 sessions is neither: **inconclusive**.

- The whole difference is task 158, which passed in 12, 15 and 15 calls with the
  upstream one-line test change, against one pass in 22 calls in round 3. Three
  trials against three do not separate this from run-to-run variation; task 53 has moved
  between 1/3 and 3/3 across rounds with no change to the agent.
- All 13 job sessions received the reminder, the 8 that passed included: no session
  on these tasks edits within 10 calls. On 158 the edit followed the reminder within
  two to seven commands. On 160 it did not follow at all.
- The search did not stop. All 5 `NO_PATCH` sessions still looked through Git
  history (3.4 commands each, 4.5 before), now using the checkout's own hashes, and
  4 looked for copies elsewhere.
- Task 160 is a different failure from the one E2 described. The upstream fix
  changes four lines of a test that compares against tick labels. The agent
  reproduces the failure, reads the plotting code for a defect that is not there,
  and never considers that the test is the wrong side. Telling it to edit does not
  tell it what to edit.
- Task 53, repetition 3 passed the grader with a 27-call repair of one job while
  the other job's session ended `NO_PATCH`, so the run reported `PARTIAL`. This is
  the gap seen on task 26 in round 4: a job whose repair failed is not rechecked
  against the final patch.

Not shown: any effect on the other 35 tasks, which were not rerun, and which of the
three changes matters.

## Pi tool routing

`pi/docker-tools.ts` lets the [Pi](https://github.com/earendil-works/pi) coding
agent (pinned in `pi/package.json`) serve as a general-agent baseline under the
same isolation as CI Repair. Pi and its provider key stay on the host; the
extension replaces Pi's default tools (`read`, `bash`, `edit`, `write`) with
implementations that run inside one existing container started with
`--network=none`, maps Pi's host working directory to `/workspace`, forwards no
host environment, and aborts after `CI_REPAIR_PI_MAX_TURNS` turns.

```sh
cd eval/pi && pnpm install --frozen-lockfile --ignore-scripts
PI_CODING_AGENT_DIR=... CI_REPAIR_PI_CONTAINER=<id> DEEPSEEK_API_KEY=... \
  node node_modules/@earendil-works/pi-coding-agent/dist/cli.js --mode json \
  --no-session --model deepseek/deepseek-flash -e ./docker-tools.ts "<task>" </dev/null
```

Run Pi from an empty directory (it reads `AGENTS.md` from its working directory)
and close stdin (Pi otherwise waits for piped input). A spike on 2026-09-24
confirmed that commands inside the container saw no provider key, no host
filesystem and no DNS, that usage and cost are reported per message, and that
the aborted call after the turn limit consumed no tokens.
