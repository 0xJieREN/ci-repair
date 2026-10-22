"""Copy the evidence behind the evaluation record out of a downloaded hosted run.

Hosted runs upload one artifact per task, and GitHub deletes artifacts after 90 days.
This keeps what the record's tables are computed from: every trial's `result.json`
and `patch.diff`, CI Repair's run and job reports, baselines and mini trajectories,
the run's `experiment.jsonl`, and for replay runs each task's result and upstream
reference diff. Collections, images, build logs and Pi's event streams stay out.

    gh run download 36405797931 --dir runs/compare-gh-36405797931
    uv run python eval/archive.py runs/compare-gh-36405797931 eval/results/compare-36405797931

Per-task artifact directories (`compare-task-45/tasks/45/...`) are merged into one
`tasks/` tree, the layout `compare.py --summarize` reads.
"""

import argparse
import shutil
import sys
from pathlib import Path

from ci_repair.diagnostics import has_credential

KEEP = (
    "tasks/*/*/*/result.json",
    "tasks/*/*/*/patch.diff",
    "tasks/*/ci-repair/*/run/report.json",
    "tasks/*/ci-repair/*/run/jobs/*/baseline.json",
    "tasks/*/ci-repair/*/run/jobs/*/*/report.json",
    "tasks/*/ci-repair/*/run/jobs/*/*/baseline.json",
    "tasks/*/ci-repair/*/run/jobs/*/*/trajectory.json",
)


def sources(run: Path) -> dict[Path, Path]:
    """Map each kept file to its path inside the archive."""
    found = {}
    for task in sorted(run.glob("compare-task-*")):
        for pattern in KEEP:
            for path in task.glob(pattern):
                found[path.relative_to(task)] = path
    for name in ("experiment.jsonl", "summary.md"):
        for path in run.glob(f"*-summary/{name}"):
            found[Path(name)] = path
    for task in sorted(run.glob("lca-task-*")):
        for name in ("result.json", "reference.diff"):
            if (task / name).exists():
                found[Path(task.name) / name] = task / name
    return found


def archive(run: Path, output: Path) -> int:
    found = sources(run)
    if not found:
        raise SystemExit(f"{run}: no compare-task-* or lca-task-* directories")
    # Trajectories hold model input and output: refuse this host's credential values and
    # token formats. Assignments such as `token = ...` are the repaired projects' code.
    leaks = [str(p) for p in found.values() if has_credential(p.read_text(errors="replace"))]
    if leaks:
        raise SystemExit("Possible credentials, nothing copied:\n" + "\n".join(leaks))
    for relative, path in found.items():
        (output / relative).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, output / relative)
    return len(found)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("run", type=Path, help="Directory from `gh run download`")
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    print(f"{archive(args.run, args.output)} files into {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
