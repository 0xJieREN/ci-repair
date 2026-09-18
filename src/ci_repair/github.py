"""Read-only GitHub Actions ingestion, using the operator's gh authentication."""

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path

from ci_repair.workspace import command


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


def collect(
    repository: str,
    run_id: int,
    output: Path,
    *,
    job_id: int | None = None,
    attempt: int | None = None,
) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise CollectionError("Repository must be owner/name on github.com")
    if run_id <= 0 or (attempt is not None and attempt <= 0):
        raise CollectionError("Run and attempt numbers must be positive")
    if output.exists():
        raise CollectionError("Output already exists; choose a new directory")
    base = f"repos/{repository}/actions/runs/{run_id}"
    latest = json.loads(api(base))
    attempt = attempt or latest["run_attempt"]
    run = json.loads(api(f"{base}/attempts/{attempt}"))
    if run["status"] != "completed" or run["conclusion"] != "failure":
        raise CollectionError("Only completed failed runs are supported")
    if run["event"] not in ("push", "workflow_dispatch"):
        raise CollectionError(
            "v0.2 supports push/workflow_dispatch only; PR merge commits need explicit handling"
        )
    if run["head_repository"]["full_name"].lower() != repository.lower():
        raise CollectionError("Cross-repository runs are not supported")
    sha = run["head_sha"]
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise CollectionError("Run has an invalid commit SHA")
    jobs = []
    page = 1
    while True:
        batch = json.loads(api(f"{base}/attempts/{attempt}/jobs?per_page=100&page={page}"))["jobs"]
        jobs.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    job = select_job(jobs, job_id)
    if job["run_id"] != run_id or job["head_sha"] != sha:
        raise CollectionError("Job does not match the selected run commit")
    log = api(f"repos/{repository}/actions/jobs/{job['id']}/logs")
    if not log.strip():
        raise CollectionError("Failed job log is empty or unavailable")
    metadata = {
        "schema_version": 1,
        "repository": repository,
        "commit": sha,
        "run_id": run_id,
        "run_attempt": attempt,
        "run_url": run["html_url"],
        "event": run["event"],
        "workflow": run["name"],
        "job_id": job["id"],
        "job_name": job["name"],
        "failed_steps": [
            s["name"] for s in job.get("steps", []) if s.get("conclusion") == "failure"
        ],
        "log_sha256": hashlib.sha256(log).hexdigest(),
    }
    output.mkdir(parents=True, mode=0o700)
    # A failed collection remains inspectable but never gets a completion manifest.
    (output / "failure.log").write_bytes(log)
    checkout = output / "repo"
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
    command(["git", "fetch", "--depth=1", "origin", sha], cwd=checkout, timeout=180)
    command(["git", "checkout", "--detach", sha], cwd=checkout)
    actual = command(["git", "rev-parse", "HEAD"], cwd=checkout).decode().strip()
    if actual != sha:
        raise CollectionError("Checkout does not match the failing commit")
    (output / "ci-context.json").write_text(json.dumps(metadata, indent=2) + "\n")
    return metadata


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
    return {field: metadata[field] for field in fields}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repository", help="owner/name on github.com")
    parser.add_argument("run_id", type=int)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--job-id", type=int)
    parser.add_argument("--attempt", type=int, help="Default: latest attempt at collection start")
    args = parser.parse_args()
    try:
        result = collect(
            args.repository,
            args.run_id,
            args.output.resolve(),
            job_id=args.job_id,
            attempt=args.attempt,
        )
    except (CollectionError, subprocess.SubprocessError, OSError) as exc:
        # gh errors can contain auth details: expose controlled errors only.
        message = str(exc) if isinstance(exc, CollectionError) else type(exc).__name__
        parser.exit(1, f"Collection failed: {message}\n")
    print(
        f"Collected {result['repository']}@{result['commit']} job {result['job_id']} into {args.output}"
    )
    return 0
