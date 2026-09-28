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
commands to pass in fresh containers, and the patch policy not to deny it; an
arm's own verdict is only recorded. Cost is computed for both from provider token
counts with `config/deepseek-pricing.json`. Each invocation appends its commit,
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
