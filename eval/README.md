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
