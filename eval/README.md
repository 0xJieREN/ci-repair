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
versions and budgets to `experiment.jsonl`; finished trials are skipped on rerun.

```sh
uv run --with pyarrow python eval/compare.py --output runs/compare --env-file .env \
  --network container:pypi-wayback --mirror http://localhost:8080 \
  --tasks 4,82 --repetitions 3
uv run --with pyarrow python eval/compare.py --output runs/compare --summarize
```

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
