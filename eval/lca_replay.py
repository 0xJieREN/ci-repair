"""Dynamic replay of LCA tasks: can CI Repair reproduce each failure and judge its fix?

For every failed job of one task: reconstruct the spec, build the replay image (setup
installs through a PyPI mirror frozen at the dataset's date, like the official LCA
workflows), run the failing command offline and require the CI failure to reproduce,
then apply the dataset's reference diff and require the fresh-container verifier to
pass. A task is USABLE for agent comparison only if every failed job passes all three.
No model is called.

    uv run --with pyarrow python eval/lca_replay.py --list --output runs/lca [IDS]
    uv run --with pyarrow python eval/lca_replay.py --task 82 --output runs/lca \\
        --network lca --mirror http://pypi-wayback:8080
    uv run --with pyarrow python eval/lca_replay.py --summarize runs/lca/tasks
"""

import argparse
import collections
import json
import re
import subprocess
import urllib.parse
from pathlib import Path

from lca_coverage import dataset, job_specs, task_status

from ci_repair.context import evidence_overlap
from ci_repair.orchestrate import BASELINE_UNREPRODUCED
from ci_repair.pipeline import Config, run_test, verify_patch
from ci_repair.policy import Policy
from ci_repair.reconstruct import UNSUPPORTED, build_environment, replay_commands
from ci_repair.workspace import command, snapshot, workspace

SETUP_SECONDS = 1800
COMMAND_SECONDS = 1200
MIRROR_DATE = re.compile(r"PIP_INDEX_URL:\s*\S+/(\d{4}-\d\d-\d\d)")


def checkout(row: dict, path: Path):
    url = f"https://github.com/{row['repo_owner']}/{row['repo_name']}.git"
    command(["git", "init", "-q", str(path)])
    command(["git", "fetch", "-q", "--depth=1", url, row["sha_fail"]], cwd=path, timeout=900)
    command(["git", "checkout", "-q", "--detach", "FETCH_HEAD"], cwd=path, timeout=300)


def mirror_env(row: dict, mirror: str | None) -> dict:
    """The official workflows point pip and uv at PyPI as of the failure's date."""
    if not mirror:
        return {}
    found = MIRROR_DATE.search(row["workflow"])
    date = found[1] if found else row["commit_date"][:10]
    host = urllib.parse.urlsplit(mirror).hostname
    return {
        "PIP_INDEX_URL": f"{mirror}/{date}",
        "UV_INDEX_URL": f"{mirror}/{date}",
        # pip trusts plain HTTP only on localhost, which the official workflows use.
        "PIP_TRUSTED_HOST": host,
        "UV_INSECURE_HOST": host,
    }


def replay_job(entry, archive, reference, directory, policy, network, setup_env) -> dict:
    spec = entry["spec"]
    result = {"job": entry["job"], "step": entry["step"], "static": spec["status"]}
    (directory / "failure.log").write_text(entry["log"])
    built = build_environment(
        spec, archive, directory, policy, network=network, setup_env=setup_env
    )
    result["build"] = {
        k: built.get(k)
        for k in (
            "status",
            "setup_returncode",
            "setup_seconds",
            "warm_up_returncode",
            "warm_up_seconds",
            "architecture_mismatch",
        )
    } | {"fidelity": spec["fidelity"], "base_image": spec["base_image"]}
    if built.get("status") != "BUILT":
        return {**result, "status": "SETUP_FAILED"}
    image = built["image_id"]
    failing, regression = replay_commands(spec)
    try:
        with workspace(archive, image, COMMAND_SECONDS, COMMAND_SECONDS + 60) as env:
            baseline = run_test(env, failing, directory / "baseline.json")
        reproduced = baseline["returncode"] not in BASELINE_UNREPRODUCED and not baseline.get(
            "exception_info"
        )
        result["baseline"] = {
            "returncode": baseline["returncode"],
            "matches_ci_log": evidence_overlap(entry["log"], baseline.get("output", "")),
            "seconds": round(baseline["duration_seconds"], 1),
        }
        if not reproduced:
            return {**result, "status": "BASELINE_NOT_REPRODUCED"}
        (directory / "reference").mkdir()
        config = Config(
            repo=directory,
            failure_log=directory / "failure.log",
            output=directory / "reference",
            image=image,
            failing_command=failing,
            regression_command=regression,
            allowed_paths=(".",),
            command_seconds=COMMAND_SECONDS,
            wall_seconds=COMMAND_SECONDS + 60,
        )
        verdict = verify_patch(config, archive, image, reference)
        result["reference"] = {
            "status": verdict["status"],
            "returncodes": [t["returncode"] for t in verdict.get("tests", [])],
        }
        if not verdict.get("verified"):
            return {**result, "status": "REFERENCE_FAILED"}
        return {**result, "status": "USABLE"}
    finally:
        subprocess.run(["docker", "image", "rm", "-f", built["tag"]], capture_output=True)


def replay(row: dict, output: Path, network: str | None, mirror: str | None) -> dict:
    directory = (output / "tasks" / str(row["id"])).resolve()
    directory.mkdir(parents=True, exist_ok=False)
    record = {
        "id": row["id"],
        "repository": f"{row['repo_owner']}/{row['repo_name']}",
        "difficulty": row["difficulty"],
        "jobs": [],
    }
    policy = Policy(
        {"budget": {"max_setup_seconds": SETUP_SECONDS, "max_command_seconds": COMMAND_SECONDS}}
    )
    try:
        repo = directory / "repo"
        checkout(row, repo)
        entries = job_specs(repo, row, policy)
        record["static"] = task_status(
            e["spec"]["status"] if e["spec"] else UNSUPPORTED for e in entries
        )
        if record["static"] == UNSUPPORTED:
            record["status"] = "UNSUPPORTED"
            return record
        archive = directory / "source.tar"
        snapshot(repo, archive)
        reference = directory / "reference.diff"
        reference.write_text(row["diff"])
        setup_env = mirror_env(row, mirror)
        record["mirror"] = setup_env.get("PIP_INDEX_URL")
        for index, entry in enumerate(entries):
            job_dir = directory / "jobs" / str(index)
            job_dir.mkdir(parents=True)
            record["jobs"].append(
                replay_job(entry, archive, reference, job_dir, policy, network, setup_env)
            )
        statuses = [j["status"] for j in record["jobs"]]
        record["status"] = next((s for s in statuses if s != "USABLE"), "USABLE")
    except Exception as exc:  # noqa: BLE001 - one broken task must not hide the others
        record["status"] = "ERROR"
        record["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
    finally:
        (directory / "result.json").write_text(json.dumps(record, indent=2) + "\n")
    return record


ORDER = [
    "USABLE",
    "REFERENCE_FAILED",
    "BASELINE_NOT_REPRODUCED",
    "SETUP_FAILED",
    "UNSUPPORTED",
    "ERROR",
]


def summarize(tasks: Path) -> str:
    records = [json.loads(p.read_text()) for p in sorted(tasks.glob("*/result.json"))]
    table = collections.defaultdict(collections.Counter)
    for record in records:
        table[record["difficulty"]][record["status"]] += 1
    columns = [s for s in ORDER if any(table[d][s] for d in table)]
    lines = [
        f"LCA replay: {len(records)} tasks",
        "",
        "| Difficulty | " + " | ".join(columns) + " | Total |",
        "|---|" + "---:|" * (len(columns) + 1),
    ]
    for difficulty in sorted(table):
        row = table[difficulty]
        cells = " | ".join(str(row[c]) for c in columns)
        lines.append(f"| {difficulty} | {cells} | {sum(row.values())} |")
    totals = collections.Counter(r["status"] for r in records)
    cells = " | ".join(str(totals[c]) for c in columns)
    lines.append(f"| All | {cells} | {len(records)} |")
    lines += ["", "| Task | Repository | Status | Jobs |", "|---:|---|---|---|"]
    for record in sorted(records, key=lambda r: r["id"]):
        jobs = "; ".join(f"{j['job']}: {j['status']}" for j in record["jobs"])
        detail = jobs or record.get("error", "")
        lines.append(f"| {record['id']} | {record['repository']} | {record['status']} | {detail} |")
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--list", action="store_true", help="Print selected task IDs as JSON")
    mode.add_argument("--task", type=int)
    mode.add_argument("--summarize", type=Path, help="Directory of per-task results")
    parser.add_argument("ids", nargs="?", default="", help="Comma-separated IDs for --list")
    parser.add_argument("--output", type=Path, default=Path("runs/lca"))
    parser.add_argument("--parquet", type=Path)
    parser.add_argument("--network", help="Docker network that reaches the mirror")
    parser.add_argument("--mirror", help="PyPI wayback base URL, e.g. http://pypi-wayback:8080")
    args = parser.parse_args()
    if args.summarize:
        print(summarize(args.summarize), end="")
        return
    args.output.mkdir(parents=True, exist_ok=True)
    rows = {row["id"]: row for row in dataset(args.parquet, args.output)}
    if args.list:
        wanted = [int(i) for i in args.ids.split(",") if i.strip()] or sorted(rows)
        unknown = set(wanted) - set(rows)
        if unknown:
            raise SystemExit(f"Unknown LCA task IDs: {sorted(unknown)}")
        print(json.dumps(wanted))
        return
    record = replay(rows[args.task], args.output, args.network, args.mirror)
    print(json.dumps({k: record[k] for k in ("id", "status")}))


if __name__ == "__main__":
    main()
