"""Read-only GitHub Actions ingestion, using the operator's gh authentication."""

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path

from ci_repair.workspace import checkout_commit, command


class CollectionError(ValueError):
    """An Actions run cannot safely be used as a repair input."""


def api(endpoint: str) -> bytes:
    args = ["gh", "api", "--hostname", "github.com", endpoint]
    if endpoint.endswith("/logs"):
        # Capture to a private file, never render raw log escape sequences to a terminal.
        args.append("--allow-escape-sequences")
    try:
        return command(args, timeout=120)
    except subprocess.CalledProcessError as exc:
        raise CollectionError(
            f"GitHub request failed: {endpoint}; check gh authentication and log availability"
        ) from exc


def select_job(jobs: list[dict], job_id: int | None) -> dict:
    failed = [j for j in jobs if j["status"] == "completed" and j["conclusion"] == "failure"]
    if job_id is not None:
        failed = [j for j in failed if j["id"] == job_id]
    if len(failed) != 1:
        choices = ", ".join(
            f"{j['id']} ({j['name']})" for j in jobs if j["conclusion"] == "failure"
        )
        raise CollectionError(
            f"Select exactly one failed job with --job-id; candidates: {choices or 'none'}"
        )
    return failed[0]


def resolve_source(repository: str, run: dict, checkout_sha: str | None) -> tuple[str, dict]:
    """PR checkout SHA is supplied from the selected job's checkout log, not today's merge ref."""
    event = run["event"]
    if event not in ("push", "workflow_dispatch", "pull_request"):
        raise CollectionError("Unsupported event; pull_request_target is never executed")
    if run["head_repository"]["full_name"].lower() != repository.lower():
        raise CollectionError("Cross-repository runs are not supported")
    head = run["head_sha"]
    sha = checkout_sha or head
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise CollectionError("Run has an invalid commit SHA")
    if event != "pull_request":
        if sha != head:
            raise CollectionError("Checkout SHA must match the run for non-PR events")
        return sha, {"checkout_kind": "head", "head_branch": run.get("head_branch")}
    prs = run.get("pull_requests", [])
    if len(prs) != 1 or checkout_sha is None:
        raise CollectionError(
            "PR runs require one associated PR and explicit --checkout-sha from the job log"
        )
    pr = prs[0]
    repository_id = run.get("repository", {}).get("id")
    if repository_id is None or pr["head"].get("repo", {}).get("id") != repository_id:
        raise CollectionError("Fork PRs or missing repository identity are not supported")
    if pr["head"]["sha"] != head:
        raise CollectionError("PR head does not match the run")
    kind = "head"
    if sha != head:
        commit = json.loads(api(f"repos/{repository}/commits/{sha}"))
        parents = [parent["sha"] for parent in commit["parents"]]
        if parents != [pr["base"]["sha"], head]:
            raise CollectionError("Merge commit parents do not match the recorded PR base and head")
        kind = "merge"
    return sha, {
        "checkout_kind": kind,
        "head_branch": pr["head"]["ref"],
        "pull_request": {
            "number": pr["number"],
            "head_sha": head,
            "base_sha": pr["base"]["sha"],
            "base_branch": pr["base"]["ref"],
        },
    }


def fetch_run(repository: str, run_id: int, attempt: int | None) -> tuple[dict, int]:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise CollectionError("Repository must be owner/name on github.com")
    if run_id <= 0 or (attempt is not None and attempt <= 0):
        raise CollectionError("Run and attempt numbers must be positive")
    base = f"repos/{repository}/actions/runs/{run_id}"
    run = json.loads(api(base if attempt is None else f"{base}/attempts/{attempt}"))
    if run["status"] != "completed" or run["conclusion"] != "failure":
        raise CollectionError("Only completed failed runs are supported")
    return run, attempt or run["run_attempt"]


def list_jobs(repository: str, run_id: int, attempt: int) -> list[dict]:
    base = f"repos/{repository}/actions/runs/{run_id}"
    jobs = []
    page = 1
    while True:
        batch = json.loads(api(f"{base}/attempts/{attempt}/jobs?per_page=100&page={page}"))["jobs"]
        jobs.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return jobs


def job_log(repository: str, run: dict, run_id: int, job: dict) -> bytes:
    if job["run_id"] != run_id or job["head_sha"] != run["head_sha"]:
        raise CollectionError("Job does not match the selected run commit")
    log = api(f"repos/{repository}/actions/jobs/{job['id']}/logs")
    if not log.strip():
        raise CollectionError("Failed job log is empty or unavailable")
    return log


def job_metadata(repository, run, run_id, attempt, sha, source, job, log) -> dict:
    return {
        "schema_version": 1,
        **source,
        "repository": repository,
        "commit": sha,
        "run_id": run_id,
        "run_attempt": attempt,
        "run_url": run["html_url"],
        "event": run["event"],
        "workflow": run["name"],
        "workflow_path": run.get("path"),
        "job_id": job["id"],
        "job_name": job["name"],
        "failed_steps": [
            s["name"] for s in job.get("steps", []) if s.get("conclusion") == "failure"
        ],
        # Step conclusions and runner labels let reconstruction decide which steps ran.
        "job_steps": [
            {k: s.get(k) for k in ("number", "name", "conclusion")} for s in job.get("steps", [])
        ],
        "job_labels": job.get("labels", []),
        "log_sha256": hashlib.sha256(log).hexdigest(),
    }


def clone_at(repository: str, checkout: Path, sha: str):
    command(
        [
            "gh",
            "repo",
            "clone",
            f"https://github.com/{repository}",
            str(checkout),
            "--",
            "--no-checkout",
            "--depth=1",
        ],
        timeout=180,
    )
    checkout_commit(checkout, sha)
    actual = command(["git", "rev-parse", "HEAD"], cwd=checkout).decode().strip()
    if actual != sha:
        raise CollectionError("Checkout does not match the failing commit")


def checkout_sha_from_log(log: bytes) -> str | None:
    """actions/checkout prints `git log -1 --format=%H` output; verified later via the API."""
    text = log.decode(errors="replace")
    found = set(
        re.findall(
            r"git log -1 --format=['\"]?%H['\"]?\s*\n(?:\S+Z )?['\"]?([0-9a-f]{40})['\"]?\s*$",
            text,
            re.MULTILINE,
        )
    )
    return found.pop() if len(found) == 1 else None


def collect(
    repository: str,
    run_id: int,
    output: Path,
    *,
    job_id: int | None = None,
    attempt: int | None = None,
    checkout_sha: str | None = None,
) -> dict:
    if output.exists():
        raise CollectionError("Output already exists; choose a new directory")
    run, attempt = fetch_run(repository, run_id, attempt)
    sha, source = resolve_source(repository, run, checkout_sha)
    job = select_job(list_jobs(repository, run_id, attempt), job_id)
    log = job_log(repository, run, run_id, job)
    metadata = job_metadata(repository, run, run_id, attempt, sha, source, job, log)
    output.mkdir(parents=True, mode=0o700)
    # A failed collection remains inspectable but never gets a completion manifest.
    (output / "failure.log").write_bytes(log)
    clone_at(repository, output / "repo", sha)
    (output / "ci-context.json").write_text(json.dumps(metadata, indent=2) + "\n")
    return metadata


def collect_run(
    repository: str,
    run_id: int,
    output: Path,
    *,
    attempt: int | None = None,
    checkout_sha: str | None = None,
) -> dict:
    """Collect every failed job of one run attempt, sharing one pinned checkout."""
    if output.exists():
        raise CollectionError("Output already exists; choose a new directory")
    run, attempt = fetch_run(repository, run_id, attempt)
    jobs = list_jobs(repository, run_id, attempt)
    failed = sorted(
        (j for j in jobs if j["status"] == "completed" and j["conclusion"] == "failure"),
        key=lambda j: j["id"],
    )
    if not failed:
        raise CollectionError("Run attempt has no failed jobs")
    logs = {job["id"]: job_log(repository, run, run_id, job) for job in failed}
    if checkout_sha is None and run["event"] == "pull_request":
        derived = {checkout_sha_from_log(log) for log in logs.values()}
        if len(derived) != 1 or None in derived:
            raise CollectionError("Failed jobs do not agree on one recorded checkout SHA")
        checkout_sha = derived.pop()
    sha, source = resolve_source(repository, run, checkout_sha)
    output.mkdir(parents=True, mode=0o700)
    entries = []
    for job in failed:
        directory = output / "jobs" / str(job["id"])
        directory.mkdir(parents=True)
        (directory / "failure.log").write_bytes(logs[job["id"]])
        metadata = job_metadata(repository, run, run_id, attempt, sha, source, job, logs[job["id"]])
        (directory / "ci-context.json").write_text(json.dumps(metadata, indent=2) + "\n")
        entries.append({"job_id": job["id"], "job_name": job["name"], "path": f"jobs/{job['id']}"})
    clone_at(repository, output / "repo", sha)
    manifest = {
        "schema_version": 1,
        **source,
        "repository": repository,
        "commit": sha,
        "run_id": run_id,
        "run_attempt": attempt,
        "run_url": run["html_url"],
        "event": run["event"],
        "workflow": run["name"],
        "workflow_path": run.get("path"),
        "jobs": entries,
        "other_unsuccessful_jobs": [
            {"job_id": j["id"], "job_name": j["name"], "conclusion": j["conclusion"]}
            for j in jobs
            if j["conclusion"] not in ("success", "failure", "skipped", None)
        ],
    }
    (output / "run.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def load_context(path: Path, sha: str, log: bytes) -> dict:
    metadata = json.loads(path.read_text())
    if metadata.get("schema_version") != 1:
        raise CollectionError("Unsupported CI context version")
    if metadata.get("commit") != sha:
        raise CollectionError("CI context commit does not match the repository")
    if metadata.get("log_sha256") != hashlib.sha256(log).hexdigest():
        raise CollectionError("Failure log does not match the collected CI context")
    # Never send arbitrary extra fields from a local manifest into model context.
    fields = (
        "repository",
        "commit",
        "run_id",
        "run_attempt",
        "run_url",
        "event",
        "workflow",
        "job_id",
        "job_name",
        "failed_steps",
    )
    result = {field: metadata[field] for field in fields}
    for field in ("checkout_kind", "head_branch", "pull_request", "workflow_path"):
        if field in metadata:
            result[field] = metadata[field]
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repository", help="owner/name on github.com")
    parser.add_argument("run_id", type=int)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--job-id", type=int)
    parser.add_argument(
        "--all-jobs", action="store_true", help="Collect every failed job for ci-repair-run"
    )
    parser.add_argument("--attempt", type=int, help="Default: latest attempt at collection start")
    parser.add_argument(
        "--checkout-sha", help="Exact SHA checked out by the PR job; required for PR events"
    )
    args = parser.parse_args()
    if args.all_jobs:
        if args.job_id:
            parser.error("--all-jobs cannot be combined with --job-id")
        try:
            manifest = collect_run(
                args.repository,
                args.run_id,
                args.output.resolve(),
                attempt=args.attempt,
                checkout_sha=args.checkout_sha,
            )
        except (CollectionError, subprocess.SubprocessError, OSError) as exc:
            message = str(exc) if isinstance(exc, CollectionError) else type(exc).__name__
            parser.exit(1, f"Collection failed: {message}\n")
        print(f"Collected {len(manifest['jobs'])} failed jobs into {args.output}")
        return 0
    try:
        result = collect(
            args.repository,
            args.run_id,
            args.output.resolve(),
            job_id=args.job_id,
            attempt=args.attempt,
            checkout_sha=args.checkout_sha,
        )
    except (CollectionError, subprocess.SubprocessError, OSError) as exc:
        # gh errors can contain auth details: expose controlled errors only.
        message = str(exc) if isinstance(exc, CollectionError) else type(exc).__name__
        parser.exit(1, f"Collection failed: {message}\n")
    print(
        f"Collected {result['repository']}@{result['commit']} job {result['job_id']} into {args.output}"
    )
    return 0
