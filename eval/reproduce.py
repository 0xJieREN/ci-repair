"""Recompute every table of the evaluation record from the archived evidence.

No model, container or network is used: everything is read from `eval/results/`,
copied out of the hosted runs by `archive.py`. Each table prints in the form
`eval/README.md` shows it, followed by the figures its text cites.

    uv run python eval/reproduce.py              # print
    uv run python eval/reproduce.py --check      # fail if eval/README.md disagrees

`tests/test_eval_results.py` runs the check in the unit gate.
"""

import argparse
import collections
import json
import math
import os
import random
import statistics
import sys
from pathlib import Path

# lca_replay imports the pipeline, and with it mini, which greets on stdout.
os.environ.setdefault("MSWEA_SILENT_STARTUP", "1")

import audit  # noqa: E402
import trajectories  # noqa: E402
from lca_replay import summarize as replay_summary  # noqa: E402

RESULTS = Path(__file__).parent / "results"
README = Path(__file__).parent / "README.md"
REPLAY = "lca-replay-36124065326"
ROUND1 = ["compare-36149891145"]
ROUND2 = ["compare-36246418676"]
# The first round 3 run lost 41 trials to an exhausted provider balance; the second reran
# every repetition of those 18 tasks and supersedes the first task by task.
ROUND3 = ["compare-36399826597", "compare-36405797931"]
ROUND4 = ["compare-36415596025"]
INTERVENTION = ["compare-36970961464"]
INTERVENTION_BASE = ["compare-36405797931"]


def trials(runs: list[str], arm: str, tasks=None, repetition=None) -> list[dict]:
    found = audit.trials([RESULTS / run for run in runs])
    return [
        result
        for (task, a, rep), (_, result) in sorted(found.items())
        if a == arm and (tasks is None or task in tasks) and repetition in (None, rep)
    ]


def directories(runs: list[str], arm: str = "ci-repair", tasks=None) -> list[Path]:
    found = audit.trials([RESULTS / run for run in runs])
    return [d for (task, a, _), (d, _) in sorted(found.items()) if a == arm and task in tasks]


def p90(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[math.ceil(0.9 * len(ordered)) - 1]  # nearest rank


def passed(rows: list[dict]) -> int:
    return sum(bool(r["passed"]) for r in rows)


def calls(rows: list[dict]) -> str:
    values = [r["model_calls"] for r in rows]
    return f"{statistics.median(values):.0f} ({statistics.mean(values):.1f})"


def cost(rows: list[dict]) -> str:
    return f"${statistics.mean(r['cost_usd'] for r in rows):.4f}"


def times(rows: list[dict]) -> str:
    t = [r["agent_seconds"] for r in rows]
    return f"{statistics.median(t):.0f} / {statistics.mean(t):.0f} / {p90(t):.0f} s"


def table(header: list[str], rows: list[list]) -> list[str]:
    def line(cells):
        return " ".join(["|", *(f"{c} |" if str(c) else "|" for c in cells)])

    return [line(header), "|---|" + "---:|" * (len(header) - 1), *map(line, rows)]


def per_task(rows: list[dict]) -> dict[int, list[bool]]:
    found = collections.defaultdict(list)
    for r in rows:
        found[r["id"]].append(bool(r["passed"]))
    return found


def replay() -> list[str]:
    records = {
        r["id"]: r
        for r in (json.loads(p.read_text()) for p in (RESULTS / REPLAY).glob("*/result.json"))
    }
    summary = replay_summary(RESULTS / REPLAY).splitlines()
    usable = [j for r in records.values() if r["status"] == "USABLE" for j in r["jobs"]]
    matched = sum(bool(j["baseline"].get("matches_ci_log")) for j in usable)
    return [
        f"## LCA dynamic replay (run {REPLAY.rsplit('-', 1)[1]})",
        "",
        *summary[2 : summary.index("", 2)],
        "",
        f"- Usable jobs: {len(usable)}; replaying an error line from their CI log: {matched}",
    ]


def round1() -> list[str]:
    tasks = json.loads(_replay_index())
    columns = [trials(ROUND1, "ci-repair"), trials(ROUND1, "pi")]

    def by_difficulty(rows):
        cells = []
        for difficulty in range(4):
            subset = [r for r in rows if tasks[str(r["id"])]["difficulty"] == difficulty]
            cells.append(f"{passed(subset)}/{len(subset)}")
        return ", ".join(cells)

    rows = [
        ["Trials passed", *(f"{passed(c)}/{len(c)} ({100 * passed(c) / len(c):.1f}%)" for c in columns)],
        ["Difficulty 0 / 1 / 2 / 3", *(by_difficulty(c) for c in columns)],
        ["Model calls, median (mean)", *(calls(c) for c in columns)],
        ["Estimated cost, mean per trial", *(cost(c) for c in columns)],
        ["Agent time, median", *(f"{statistics.median(r['agent_seconds'] for r in c):.0f} s" for c in columns)],
    ]  # fmt: skip
    ours, pi = (per_task(c) for c in columns)
    differences = {t: statistics.mean(ours[t]) - statistics.mean(pi[t]) for t in ours}
    differing = [f"{t} ({sum(ours[t])}/3 against {sum(pi[t])}/3)" for t in ours if differences[t]]
    low, high = bootstrap(differences, {t: tasks[str(t)]["repository"] for t in differences})
    verdicts = collections.Counter(
        (r["own_verdict"] == "PASS") == bool(r["passed"]) for r in columns[0]
    )
    return [
        "## Round 1",
        "",
        *table(["", "CI Repair", "Pi"], rows),
        "",
        f"- Tasks that differ: {', '.join(differing)}",
        f"- Mean per-task difference: {100 * statistics.mean(differences.values()):.1f} points; "
        f"95% bootstrap interval clustered by repository: [{low:.1f}, {high:.1f}]",
        f"- CI Repair's own verdict matched the grader in {verdicts[True]} of "
        f"{len(columns[0])} trials",
    ]


def bootstrap(differences: dict[int, float], repository: dict[int, str]) -> tuple[float, float]:
    """Percentile interval of the mean task difference, resampling whole repositories."""
    clusters = collections.defaultdict(list)
    for task, difference in differences.items():
        clusters[repository[task]].append(difference)
    names = sorted(clusters)
    rng = random.Random(0)
    means = []
    for _ in range(10_000):
        sample = [d for name in rng.choices(names, k=len(names)) for d in clusters[name]]
        means.append(100 * statistics.mean(sample))
    means.sort()
    return means[int(0.025 * len(means))], means[int(0.975 * len(means)) - 1]


def _replay_index() -> str:
    records = (json.loads(p.read_text()) for p in (RESULTS / REPLAY).glob("*/result.json"))
    return json.dumps({str(r["id"]): r for r in records})


def round2() -> list[str]:
    multi = sorted({r["id"] for r in trials(ROUND2, "ci-repair")})
    columns = [
        trials(ROUND1, "ci-repair", multi),
        trials(ROUND2, "ci-repair", multi),
        trials(ROUND1, "pi", multi),
    ]
    tasks = [per_task(c) for c in columns]
    rows, unchanged = [], []
    for task in multi:
        if all(all(t[task]) for t in tasks):
            unchanged.append(task)
        else:
            rows.append([task, *(f"{sum(t[task])}/{len(t[task])}" for t in tasks)])
    total = lambda t, keys: f"{sum(sum(t[k]) for k in keys)}/{sum(len(t[k]) for k in keys)}"  # noqa: E731
    rows.append([", ".join(map(str, unchanged)), *(total(t, unchanged) for t in tasks)])
    rows.append(["**Total**", *(f"{passed(c)}/{len(c)}" for c in columns)])
    rows.append(["Model calls, median (mean)", *(calls(c) for c in columns)])
    rows.append(["Estimated cost, mean per trial", *(cost(c) for c in columns)])
    header = ["Task", "CI Repair round 1", "CI Repair round 2", "Pi round 1"]
    return ["## Round 2", "", *table(header, rows)]


def round3() -> list[str]:
    columns = [trials(ROUND1, "ci-repair"), trials(ROUND3, "ci-repair"), trials(ROUND1, "pi")]
    rows = [
        ["Passed", *(passed(c) for c in columns)],
        ["Time per trial, median / mean / p90", *(times(c) for c in columns)],
        ["Total time", *(f"{sum(r['agent_seconds'] for r in c):,.0f} s" for c in columns)],
        ["Model calls, median (mean)", *(calls(c) for c in columns)],
        ["Estimated cost, mean per trial", *(cost(c) for c in columns)],
    ]
    header = ["117 trials", "CI Repair round 1 (`8cd5bce`)", "CI Repair round 3", "Pi round 1"]
    multi = {r["id"] for r in trials(ROUND2, "ci-repair")}
    before, after = (
        [r for r in trials(runs, "ci-repair") if r["id"] in multi] for runs in (ROUND1, ROUND3)
    )
    return [
        "## Round 3",
        "",
        *table(header, rows),
        "",
        f"- Multi-job trials: mean time {statistics.mean(r['agent_seconds'] for r in before):.0f}"
        f" s -> {statistics.mean(r['agent_seconds'] for r in after):.0f} s; passed "
        f"{passed(before)} -> {passed(after)} of {len(after)}",
    ]


def round4() -> list[str]:
    columns = [
        trials(ROUND3, "ci-repair", repetition=1),
        trials(ROUND4, "ci-repair"),
        trials(ROUND1, "pi", repetition=1),
    ]
    rows = [
        ["Passed", *(passed(c) for c in columns)],
        ["Time per trial, median / mean / p90", *(times(c) for c in columns)],
        ["Model calls, mean", *(f"{statistics.mean(r['model_calls'] for r in c):.1f}" for c in columns)],
    ]  # fmt: skip
    header = [
        "39 trials",
        "CI Repair round 3, repetition 1",
        "CI Repair round 4",
        "Pi round 1, repetition 1",
    ]
    run = RESULTS / ROUND4[0]
    pipeline = [
        (json.loads(p.read_text()), json.loads(p.with_name("report.json").read_text()))
        # Repairs and fix-ups both run the pipeline, and with it a baseline.
        for p in sorted(run.glob("tasks/*/ci-repair/*/run/jobs/*/*/baseline.json"))
    ]
    seconds = sum(b["duration_seconds"] for b, _ in pipeline) / len(columns[1])
    waited = sum(r.get("baseline_wait_seconds", 0) for _, r in pipeline) / len(columns[1])
    prior = [
        json.loads(p.read_text()) for p in run.glob("tasks/*/ci-repair/*/run/jobs/*/baseline.json")
    ]
    totals = [
        sum(r["agent_seconds"] for r in trials(ROUND3, "ci-repair", repetition=k))
        for k in (1, 2, 3)
    ]  # fmt: skip
    return [
        "## Round 4",
        "",
        *table(header, rows),
        "",
        f"- Pipeline baselines: {len(pipeline)}; baseline {seconds:.1f} s per trial, waited "
        f"{waited:.1f} s, overlapped {seconds - waited:.1f} s "
        f"({100 * (seconds - waited) / seconds:.0f}%)",
        f"- Run-level baselines: {sum(b['duration_seconds'] for b in prior) / len(columns[1]):.1f}"
        " s per trial",
        f"- Total time: round 3 repetitions {', '.join(f'{t:,.0f}' for t in totals)} s; "
        f"round 4 {sum(r['agent_seconds'] for r in columns[1]):,.0f} s",
    ]


def audited() -> list[str]:
    references = RESULTS / REPLAY
    rounds = [(ROUND1, "ci-repair"), (ROUND3, "ci-repair"), (ROUND4, "ci-repair"), (ROUND1, "pi")]
    counts, flagged = [], []
    for runs, arm in rounds:
        table_, found = audit.count(references, [RESULTS / run for run in runs])
        counts.append(table_[arm])
        flagged += [line for line in found if line.startswith(f"- {arm} ")]
    labels = {
        "Subset of upstream": "Proper subset of upstream's files",
        "Other files": "Any file upstream did not change",
        "Tests upstream left alone": "Edits a test file upstream left alone",
        "Net assertions removed": "Removes more assertions than it adds",
    }
    rows = [[labels.get(c, c), *(row[c] for row in counts)] for c in audit.COLUMNS]
    header = [
        "Passing patches",
        "CI Repair round 1",
        "CI Repair round 3",
        "CI Repair round 4",
        "Pi round 1",
    ]
    supported = supported_tasks([*ROUND1, *ROUND3])
    verdicts = []
    for runs, arm, name in ((ROUND3, "ci-repair", "round 3"), (ROUND1, "pi", "round 1")):
        ok = [r for r in trials(runs, arm) if r["passed"]]
        # Results recorded before `patch_policy` existed keep the verdict under "policy".
        policy = [(r, r.get("patch_policy") or r["policy"]["verdict"]) for r in ok]
        review = [r for r, v in policy if v == "REVIEW"]
        allow = [r for r, v in policy if v == "ALLOW"]
        own = sum(r.get("own_verdict") == "PASS" for r in ok) if arm != "pi" else "-"
        verdicts.append(
            f"- {arm} {name}: passed {len(ok)}, own verdict PASS {own}, ALLOW {len(allow)}, "
            f"REVIEW {len(review)} ({sum(r['touches_tests'] for r in review)} edit tests), "
            f"ALLOW in a SUPPORTED environment {sum(r['id'] in supported for r in allow)}"
        )
    return ["## Audit against upstream fixes", "", *table(header, rows), "", *flagged, *verdicts]


def supported_tasks(runs: list[str]) -> set[int]:
    """Tasks whose every replayed job environment is SUPPORTED, from CI Repair's reports.

    The environment is reconstructed from the task alone, so it holds for both arms."""
    found = {}
    for path in (
        p for run in runs for p in (RESULTS / run).glob("tasks/*/ci-repair/*/run/report.json")
    ):
        jobs = json.loads(path.read_text())["jobs"]
        task = int(path.parts[path.parts.index("tasks") + 1])
        statuses = {
            (j.get("environment") or {}).get("status")
            for j in jobs
            if j["status"] != "NOT_REPLAYED"
        }
        found.setdefault(task, set()).update(statuses)
    return {task for task, statuses in found.items() if statuses == {"SUPPORTED"}}


def sessions() -> list[str]:
    runs = [*ROUND1, *ROUND2, *ROUND3, *ROUND4]
    lines = trajectories.summarize([RESULTS / run for run in runs]).splitlines()
    return ["## Job sessions (rounds 1-4)", "", *lines]


def intervention() -> list[str]:
    tasks = sorted({r["id"] for r in trials(INTERVENTION, "ci-repair")})
    columns = [trials(INTERVENTION_BASE, "ci-repair", tasks), trials(INTERVENTION, "ci-repair")]
    by_task = [per_task(c) for c in columns]
    no_patch = []
    for runs in (INTERVENTION_BASE, INTERVENTION):
        reports = [
            json.loads((d / "run/report.json").read_text()) for d in directories(runs, tasks=tasks)
        ]
        no_patch.append(
            sum(j.get("repair_status") == "NO_PATCH" for r in reports for j in r["jobs"])
        )
    rows = [[t, *(f"{sum(b[t])}/{len(b[t])}" for b in by_task)] for t in tasks]
    rows += [
        ["Total", *(f"{passed(c)}/{len(c)}" for c in columns)],
        ["`NO_PATCH` job sessions", *no_patch],
        ["Mean model calls per trial", *(f"{statistics.mean(r['model_calls'] for r in c):.1f}" for c in columns)],
        ["Mean seconds per trial", *(f"{statistics.mean(r['agent_seconds'] for r in c):.0f}" for c in columns)],
        [f"Estimated cost, {len(columns[1])} trials", *(f"${sum(r['cost_usd'] for r in c):.3f}" for c in columns)],
    ]  # fmt: skip
    return ["## The intervention", "", *table(["Task", "Round 3", "With the intervention"], rows)]


SECTIONS = (replay, round1, round2, round3, round4, audited, sessions, intervention)


def reproduce() -> str:
    return "\n\n".join("\n".join(section()) for section in SECTIONS) + "\n"


def missing_rows(output: str, readme: str) -> list[str]:
    """Table rows of the recomputed output that eval/README.md does not show verbatim."""
    shown = set(readme.splitlines())
    return [line for line in output.splitlines() if line.startswith("| ") and line not in shown]


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="Compare tables with eval/README.md")
    args = parser.parse_args()
    output = reproduce()
    if not args.check:
        print(output, end="")
        return 0
    missing = missing_rows(output, README.read_text())
    for line in missing:
        print(f"not in eval/README.md: {line}", file=sys.stderr)
    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
