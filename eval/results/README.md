# Archived evaluation evidence

The data behind every table of [`../README.md`](../README.md) except LCA static
coverage. GitHub deletes run artifacts after 90 days, so the evidence is kept here.
`../reproduce.py` recomputes the tables from it; `reproduced.md` is its output.

| Directory | Hosted run | Commit | What it is | Files |
|---|---|---|---|---:|
| `lca-replay-36124065326` | [36124065326](https://github.com/0xJieREN/ci-repair/actions/runs/36124065326) | — | LCA dynamic replay, 68 tasks; each task's `result.json` and upstream `reference.diff` | 128 |
| `compare-36149891145` | [36149891145](https://github.com/0xJieREN/ci-repair/actions/runs/36149891145) | `8cd5bce` | Round 1: CI Repair and Pi, 39 tasks × 3 | 1098 |
| `compare-36246418676` | [36246418676](https://github.com/0xJieREN/ci-repair/actions/runs/36246418676) | `38c7769` | Round 2: CI Repair, the 7 multi-job tasks × 3 | 208 |
| `compare-36399826597` | [36399826597](https://github.com/0xJieREN/ci-repair/actions/runs/36399826597) | `7ab19ca` | Round 3, first run: 41 trials invalid (`Insufficient Balance`); 21 tasks kept | 894 |
| `compare-36405797931` | [36405797931](https://github.com/0xJieREN/ci-repair/actions/runs/36405797931) | `2492dd0` | Round 3, rerun of the other 18 tasks × 3 | 407 |
| `compare-36415596025` | [36415596025](https://github.com/0xJieREN/ci-repair/actions/runs/36415596025) | `2c9af64` | Round 4: CI Repair, 39 tasks × 1, baseline beside the agent | 254 |
| `compare-36970961464` | [36970961464](https://github.com/0xJieREN/ci-repair/actions/runs/36970961464) | `bf9f92c` | The intervention: tasks 45, 53, 158, 160 × 3 | 75 |

Round 3 merges its two runs task by task; the rerun supersedes the first run's
trials of the same task (`audit.trials`).

## What is kept

`../archive.py` copies these files out of a `gh run download` directory:

- every trial's `result.json` (the grader's verdict, calls, cost, time) and `patch.diff`;
- CI Repair's `run/report.json`, each job's `report.json` and `baseline.json`, and the
  mini-swe-agent `trajectory.json` of each job session (60 MB of the 76 MB);
- the run's `experiment.jsonl` and `summary.md`;
- for the replay run, each task's `result.json` and `reference.diff`.

Left out: repository archives, images, build logs, Pi's event streams.

Before copying, `archive.py` refuses a run whose files contain a credential value of
the host environment or a provider or GitHub token format (`sk-…`, `ghp_…`,
`github_pat_…`). Names such as `token = self.config['token']` in the repaired projects'
code are not credentials and stay.

```sh
gh run download 36405797931 --dir runs/compare-gh-36405797931
uv run python eval/archive.py runs/compare-gh-36405797931 eval/results/compare-36405797931
```
