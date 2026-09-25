"""Static coverage of LCA CI builds repair by CI Repair's environment reconstructor.

For each task the failing repository is fetched blobless at `sha_fail`, and every failed
job is passed to `ci_repair.reconstruct.reconstruct`. No model call, container or build
happens; SUPPORTED/REVIEW_REQUIRED is therefore an upper bound on replayable tasks.

    uv run --with pyarrow python eval/lca_coverage.py --output runs/lca-coverage

Adaptations to the dataset (LCA records per-step logs, not API job metadata):
- The failing step is identified by its API step number (step 1 is "Set up job").
- LCA log file names drop "/" from job names; the unique rendered name is recovered.
- LCA logs omit "Set up job", so `ubuntu-latest` is resolved from the commit date.
- Step conclusions are unknown, so earlier `if:` conditions must be evaluable.
"""

import argparse
import collections
import hashlib
import json
import re
import subprocess
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pyarrow.parquet as pq

from ci_repair.policy import Policy
from ci_repair.reconstruct import (
    Problems,
    display_name,
    expand_matrix,
    load_workflow,
    reconstruct,
    render,
    scalar,
    select_job,
)

REVISION = "ebf12dad7a97c3c0cdc38705403d8bc8ddde47dc"
PARQUET_URL = (
    "https://huggingface.co/datasets/JetBrains-Research/lca-ci-builds-repair/resolve/"
    f"{REVISION}/data/python/test-00000-of-00001.parquet"
)
PARQUET_SHA256 = "0b690ed61eef63f74be425df0010e04afb61ab3821d992a32b76c7c50e81f282"


def dataset(path: Path | None, cache: Path) -> list[dict]:
    if path is None:
        path = cache / "lca-ci-builds-repair.parquet"
        if not path.exists():
            urllib.request.urlretrieve(PARQUET_URL, path)
    if hashlib.sha256(path.read_bytes()).hexdigest() != PARQUET_SHA256:
        raise SystemExit(f"{path} is not the pinned LCA revision {REVISION}")
    return pq.read_table(path).to_pylist()


def fetch(repos: Path, row: dict) -> str | None:
    path = repos / str(row["id"])
    if (path / ".git").exists():
        return None
    path.mkdir(parents=True, exist_ok=True)
    url = f"https://github.com/{row['repo_owner']}/{row['repo_name']}.git"
    for args in (
        ["git", "init", "-q"],
        ["git", "fetch", "-q", "--depth=1", "--filter=blob:none", url, row["sha_fail"]],
    ):
        result = subprocess.run(args, cwd=path, capture_output=True, timeout=600)
        if result.returncode != 0:
            return result.stderr.decode(errors="replace").strip().splitlines()[-1][:200]
    return None


def norm(text: str) -> str:
    return re.sub(r"[^0-9a-z]", "", text.lower())


def real_job_name(doc: dict, file_job: str, literals: dict) -> str:
    names = set()
    for key, job in doc["jobs"].items():
        if not isinstance(job, dict):
            continue
        strategy = job.get("strategy") if isinstance(job.get("strategy"), dict) else {}
        for combo in expand_matrix(strategy.get("matrix"), Problems()) or [{}]:
            ctx = {"literals": literals, "matrix": combo, "env": {}}
            if "name" in job:
                names.add(render(job["name"], ctx, Problems(), "name"))
            elif combo and not any(isinstance(v, dict) for v in combo.values()):
                names.add(f"{key} ({', '.join(scalar(v) for v in combo.values())})")
            else:
                names.add(key)
    matches = [n for n in names if isinstance(n, str) and n.replace("/", "") == file_job]
    return matches[0] if len(matches) == 1 else file_job


def runner_hint(commit_date: str) -> str:
    """ubuntu-latest meant ubuntu-22.04 from December 2022 to January 2025."""
    image = "ubuntu-22.04" if "2022-12" <= commit_date[:7] <= "2025-01" else "ubuntu-24.04"
    return f"Image: {image}\nVersion: lca-commit-date\n"


def failed_step(doc, job_name, number, file_step, literals) -> tuple[str | None, str | None]:
    selected = select_job(doc, job_name, {"literals": literals}, Problems())
    if selected is None:
        return None, "job name does not map uniquely"
    _, job, combo = selected
    steps = [s for s in job.get("steps") or [] if isinstance(s, dict)]
    ctx = {"literals": literals, "matrix": combo, "env": {}}
    displays = [
        display_name(s, render(s.get("name"), ctx, Problems(), "name") if "name" in s else None)
        for s in steps
    ]
    index = number - 2
    if 0 <= index < len(displays) and norm(displays[index]).startswith(norm(file_step)[:20]):
        return displays[index], None
    matches = [d for d in displays if norm(d) == norm(file_step)]
    if len(matches) == 1:
        return matches[0], None
    return None, "failed step does not map to the workflow"


def job_specs(repo: Path, row: dict, policy: Policy) -> list[dict]:
    """One entry per failed job: job, step, log and either a spec or an unsupported reason.

    Raises for an unreadable workflow; the caller records that as a task-level status.
    """
    repository = f"{row['repo_owner']}/{row['repo_name']}"
    doc, _ = load_workflow(repo, row["sha_fail"], row["workflow_path"])
    literals = {
        "runner.os": "Linux",
        "github.sha": row["sha_fail"],
        "github.repository": repository,
        "github.workspace": "/workspace",
        "github.event_name": "push",
    }
    by_job = collections.defaultdict(list)
    for log in row["logs"]:
        job, _, rest = log["step_name"].rpartition("/")
        match = re.fullmatch(r"(\d+)_(.*)\.txt", rest)
        by_job[job].append((int(match[1]), match[2], log["log"]))
    entries = []
    for file_job, steps in by_job.items():
        number, file_step, log = min(steps)
        job_name = real_job_name(doc, file_job, literals)
        entry = {"job": job_name, "step": file_step, "log": log, "spec": None}
        display, problem = failed_step(doc, job_name, number, file_step, literals)
        if problem:
            entry["problem"] = problem
        else:
            entry["spec"] = reconstruct(
                repo,
                row["sha_fail"],
                row["workflow_path"],
                {"name": job_name, "failed_steps": [display], "steps": None},
                runner_hint(row["commit_date"]) + log,
                policy,
                repository=repository,
                event="push",
            )
        entries.append(entry)
    return entries


def task_status(statuses) -> str:
    """A task is only as replayable as its least replayable failed job."""
    statuses = set(statuses)
    for status in ("UNSUPPORTED", "REVIEW_REQUIRED", "SUPPORTED"):
        if status in statuses:
            return status
    return "UNSUPPORTED"


def audit(repos: Path, row: dict, policy: Policy) -> dict:
    record = {
        "id": row["id"],
        "repository": f"{row['repo_owner']}/{row['repo_name']}",
        "difficulty": row["difficulty"],
        "commit_date": row["commit_date"],
        "workflow_path": row["workflow_path"],
        "jobs": [],
    }
    if row.get("fetch_error"):
        return {**record, "status": "SOURCE_UNAVAILABLE", "reason": row["fetch_error"]}
    try:
        entries = job_specs(repos / str(row["id"]), row, policy)
    except Exception as exc:  # noqa: BLE001 - every failure mode is part of the audit
        reason = f"{type(exc).__name__}: {str(exc)[:160]}"
        return {**record, "status": "WORKFLOW_UNREADABLE", "reason": reason}
    for entry in entries:
        spec = entry["spec"]
        if spec is None:
            job = {"status": "UNSUPPORTED", "unsupported": [entry["problem"]], "review_reasons": []}
        else:
            job = {
                "status": spec["status"],
                "fidelity": spec.get("fidelity"),
                "base_image": spec.get("base_image"),
                "unsupported": spec["unsupported"],
                "review_reasons": spec["review_reasons"],
            }
        record["jobs"].append({"job": entry["job"], "step": entry["step"], **job})
    record["status"] = task_status(j["status"] for j in record["jobs"])
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--parquet", type=Path, help="Local copy of the pinned dataset file")
    args = parser.parse_args()
    repos = args.output / "repos"
    repos.mkdir(parents=True, exist_ok=True)
    rows = dataset(args.parquet, args.output)
    with ThreadPoolExecutor(8) as pool:
        for row, error in zip(rows, pool.map(lambda r: fetch(repos, r), rows)):
            row["fetch_error"] = error
    records = [audit(repos, row, Policy()) for row in rows]
    (args.output / "coverage.json").write_text(json.dumps(records, indent=2) + "\n")
    table = collections.defaultdict(collections.Counter)
    for record in records:
        table[record["difficulty"]][record["status"]] += 1
    for difficulty in sorted(table):
        print(f"difficulty {difficulty}: {dict(table[difficulty])}")
    print(f"total: {dict(collections.Counter(r['status'] for r in records))}")


if __name__ == "__main__":
    main()
