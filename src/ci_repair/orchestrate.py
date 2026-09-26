"""Run-level orchestration: several failed jobs -> one cumulative, independently verified patch.

Jobs are processed in a deterministic, `needs`-aware order. Each earlier repair becomes the
candidate state for later jobs; a job already fixed by that candidate state never starts an
agent. Every addressed job is finally re-verified with the single cumulative patch, applied
to the original snapshot, in fresh containers with that job's reconstructed environment.
"""

import argparse
import hashlib
import json
import os
import subprocess
import tarfile
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from ci_repair.agent import StopReason
from ci_repair.context import evidence_overlap, failure_evidence
from ci_repair.github import CollectionError, load_context
from ci_repair.pipeline import Config, run_test, verify_patch, write_json
from ci_repair.pipeline import run as run_pipeline
from ci_repair.plan import clean_head
from ci_repair.policy import Policy, PolicyError, Verdict, load_policy
from ci_repair.reconstruct import (
    UNSUPPORTED,
    build_environment,
    load_workflow,
    reconstruct,
    replay_commands,
)
from ci_repair.workspace import command, snapshot, workspace

BASELINE_UNREPRODUCED = (0, -1, 124, 126, 127, 137)
GIT_IDENTITY = [
    "-c",
    "user.name=ci-repair",
    "-c",
    "user.email=ci-repair@localhost",
    "-c",
    "commit.gpgsign=false",
    "-c",
    "core.hooksPath=/dev/null",
]
FIXED_DATE = {
    "GIT_AUTHOR_DATE": "2000-01-01T00:00:00Z",
    "GIT_COMMITTER_DATE": "2000-01-01T00:00:00Z",
}


def order_jobs(entries: list[dict], doc: dict | None) -> tuple[list[dict], list[str]]:
    """Dependencies first (transitive `needs`), then workflow position, name and job ID."""
    jobs = (doc or {}).get("jobs", {}) if isinstance(doc, dict) else {}
    keys = list(jobs)

    def needs(key):
        seen, stack = set(), [key]
        while stack:
            value = jobs.get(stack.pop(), {})
            direct = value.get("needs", []) if isinstance(value, dict) else []
            for item in [direct] if isinstance(direct, str) else direct or []:
                if isinstance(item, str) and item not in seen:
                    seen.add(item)
                    stack.append(item)
        return seen

    def rank(entry):
        key = entry.get("job_key")
        position = keys.index(key) if key in keys else len(keys)
        return (position, entry["job_name"], entry["job_id"])

    pending = sorted(entries, key=rank)
    ordered, explanation = [], []
    while pending:
        ready = [
            e
            for e in pending
            if not any(
                other is not e and other.get("job_key") in needs(e.get("job_key"))
                for other in pending
            )
        ]
        if not ready:  # a cycle cannot come from a valid workflow; stay deterministic anyway
            ready = pending[:1]
        chosen = ready[0]
        dependencies = sorted(needs(chosen.get("job_key")) & {o.get("job_key") for o in ordered})
        explanation.append(
            f"{chosen['job_name']} ({chosen['job_id']})"
            + (f" after {', '.join(dependencies)}" if dependencies else "")
        )
        ordered.append(chosen)
        pending.remove(chosen)
    return ordered, explanation


def init_candidate(archive: Path, path: Path) -> str:
    """A local Git repository holding the original tree; later jobs see earlier repairs."""
    path.mkdir()
    with tarfile.open(archive) as tar:
        tar.extractall(path, filter="data")
    command(["git", "init", "-q"], cwd=path)
    # -f: every archived file is tracked upstream, even if it matches .gitignore.
    command(["git", "add", "-A", "-f"], cwd=path)
    git_commit(path, "original failing commit")
    return command(["git", "rev-parse", "HEAD"], cwd=path).decode().strip()


def git_commit(path: Path, message: str):
    subprocess.run(
        ["git", *GIT_IDENTITY, "commit", "-q", "--allow-empty", "-m", message],
        cwd=path,
        check=True,
        capture_output=True,
        env={**os.environ, **FIXED_DATE},
        timeout=60,
    )


SIBLING_BUDGET = 2000


def job_task(meta: dict, prior: list[str], others: list[dict] | None = None) -> str:
    task = (
        f"GitHub Actions job {meta['job_name']!r} failed in run {meta['run_id']} "
        f"(step: {', '.join(meta.get('failed_steps') or ['unknown'])})."
    )
    if prior:
        task += f" Changes already committed for earlier jobs in this run: {', '.join(prior)}."
    notes, remaining = [], SIBLING_BUDGET
    for other in others or []:
        # Jobs of one run often fail for one cause; each session otherwise sees only its log.
        excerpt = sibling_excerpt(other["log"])[: max(0, min(600, remaining))]
        remaining -= len(excerpt)
        notes.append(f"- {other['job_name']!r} (step: {other['step']}): {excerpt}")
    if notes:
        task += (
            "\nOther failed jobs in this run, possibly with the same cause "
            "(untrusted log excerpts):\n" + "\n".join(notes)
        )
    return task


def sibling_excerpt(log: str) -> str:
    evidence = failure_evidence(log)
    blocks = evidence["error_blocks"]
    text = blocks[0]["text"] if blocks else evidence["raw_excerpt"][-400:]
    return " ".join(text.split())


def repair_run(
    collection: Path,
    output: Path,
    *,
    model_factory: Callable[[], object],
    policy: Policy,
    build=build_environment,
) -> dict:
    manifest = json.loads((collection / "run.json").read_text())
    if manifest.get("schema_version") != 1:
        raise ValueError("Unsupported run manifest")
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    repo = (collection / "repo").resolve()
    report = {
        "schema_version": 2,
        "kind": "run",
        "status": "ERROR",
        "verified": False,
        "policy": policy.to_dict(),
        "github_actions": {
            k: manifest[k]
            for k in (
                "repository",
                "commit",
                "run_id",
                "run_attempt",
                "run_url",
                "event",
                "workflow",
                "checkout_kind",
                "head_branch",
                "pull_request",
            )
            if k in manifest
        },
        "jobs": [],
        "other_unsuccessful_jobs": manifest.get("other_unsuccessful_jobs", []),
    }
    try:
        return _repair_run(collection, output, repo, manifest, report, model_factory, policy, build)
    except CollectionError as exc:
        report.update(status="ERROR", stop_reason=StopReason.STALE_SOURCE.value, error=str(exc))
    except Exception as exc:
        report.update(
            status="ERROR",
            stop_reason=StopReason.EXECUTION_ERROR.value,
            error_type=type(exc).__name__,
        )
    write_json(output / "report.json", report)
    return report


def _repair_run(collection, output, repo, manifest, report, model_factory, policy, build):
    sha = clean_head(repo)
    if sha != manifest["commit"]:
        raise CollectionError("Collected checkout no longer matches the run commit")
    report["commit"] = sha
    archive = output / "source.tar"
    snapshot(repo, archive)
    allowed = policy.allowed_paths(manifest["repository"])
    report["config"] = {"repo": str(repo), "allowed_paths": list(allowed)}
    doc = None
    if manifest.get("workflow_path"):
        doc, _ = load_workflow(repo, sha, manifest["workflow_path"])

    entries = []
    for entry in manifest["jobs"]:
        directory = collection / entry["path"]
        log = (directory / "failure.log").read_bytes()
        load_context(directory / "ci-context.json", sha, log)  # commit and log digest must match
        meta = json.loads((directory / "ci-context.json").read_text())
        spec = None
        if doc is not None:
            spec = reconstruct(
                repo,
                sha,
                manifest["workflow_path"],
                {
                    "name": meta["job_name"],
                    "failed_steps": meta["failed_steps"],
                    "steps": meta.get("job_steps"),
                },
                log.decode(errors="replace"),
                policy,
                repository=manifest["repository"],
                event=manifest["event"],
            )
        entries.append(
            {
                **entry,
                "job_key": (spec or {}).get("source", {}).get("job_key"),
                "meta": meta,
                "log_path": directory / "failure.log",
                "spec": spec,
            }
        )
    ordered, report["order"] = order_jobs(entries, doc)

    candidate = output / "candidate"
    base = init_candidate(archive, candidate)
    cumulative = output / "patch.diff"
    cumulative.write_bytes(b"")
    effective = policy.budget({})["effective"]
    usage = {"model_calls": 0, "estimated_cost_usd": 0.0, "agent_steps": 0, "models_used": set()}
    repaired_names: list[str] = []
    attempted = 0
    results = []
    for entry in ordered:
        job_dir = output / "jobs" / str(entry["job_id"])
        job_dir.mkdir(parents=True)
        result = {"job_id": entry["job_id"], "job_name": entry["job_name"]}
        results.append(result)
        spec = entry["spec"]
        if spec is None:
            result.update(status="UNSUPPORTED_ENVIRONMENT", reasons=["workflow path unknown"])
            continue
        write_json(job_dir / "environment.json", spec)
        result["environment"] = {
            k: spec.get(k) for k in ("status", "fidelity", "base_image", "spec_sha256")
        } | {"reasons": spec["unsupported"] + spec["review_reasons"]}
        if spec["status"] == UNSUPPORTED:
            result["status"] = "UNSUPPORTED_ENVIRONMENT"
            continue
        if attempted >= policy.data["repair"]["max_jobs"]:
            result["status"] = "SKIPPED_BUDGET"
            continue
        attempted += 1
        built = build(spec, archive, job_dir, policy)
        write_json(job_dir / "build.json", built)
        result["environment"].update(
            image_id=built.get("image_id"),
            base_image_digests=built.get("base_image_digests"),
            architecture_mismatch=built.get("architecture_mismatch"),
        )
        if built.get("status") not in ("BUILT", None) or not built.get("image_id"):
            result.update(status="UNSUPPORTED_ENVIRONMENT", reasons=["setup steps failed"])
            continue
        image = built["image_id"]
        failing, regression = replay_commands(spec)
        cfg = Config(
            repo=candidate,
            failure_log=entry["log_path"],
            output=job_dir / "repair",
            image=image,
            failing_command=failing,
            regression_command=regression,
            allowed_paths=allowed,
            **effective,
        )
        with workspace(archive, image, cfg.command_seconds, cfg.command_seconds + 60) as env:
            baseline = run_test(env, failing, job_dir / "baseline.json")
        if baseline["returncode"] in BASELINE_UNREPRODUCED or baseline.get("exception_info"):
            result["status"] = "BASELINE_NOT_REPRODUCED"
            continue
        result["baseline_matches_ci_log"] = evidence_overlap(
            entry["log_path"].read_text(errors="replace"), baseline.get("output", "")
        )
        if cumulative.read_bytes():
            (job_dir / "prior-check").mkdir()
            check = verify_patch(
                replace(cfg, output=job_dir / "prior-check"), archive, image, cumulative
            )
            if check.get("verified"):
                result["status"] = "FIXED_BY_PRIOR"  # no agent started
                result["config"] = cfg
                continue
        others = [
            {
                "job_name": o["job_name"],
                "step": ", ".join(o["meta"].get("failed_steps") or ["unknown"]),
                "log": o["log_path"].read_text(errors="replace"),
            }
            for o in ordered
            if o is not entry
        ]
        task = job_task(entry["meta"], repaired_names, others)
        repair = run_pipeline(replace(cfg, task=task), model_factory(), policy)
        for key in ("model_calls", "estimated_cost_usd", "agent_steps"):
            usage[key] += repair.get("usage", {}).get(key, 0)
        usage["models_used"].update(repair.get("usage", {}).get("models_used", []))
        result.update(repair_status=repair["status"], stop_reason=repair.get("stop_reason"))
        if not repair.get("verified"):
            result["status"] = "REPAIR_FAILED"
            continue
        command(
            ["git", "apply", "--index", "--binary", str(cfg.output / "patch.diff")], cwd=candidate
        )
        git_commit(candidate, f"repair {entry['job_name']}")
        cumulative.write_bytes(command(["git", "diff", "--binary", base, "HEAD"], cwd=candidate))
        repaired_names.append(entry["job_name"])
        result.update(status="REPAIRED", config=cfg)

    # A run-level PASS must not silently omit known timeouts/cancellations.
    results.extend(
        {**job, "status": "NOT_REPLAYED", "stop_reason": StopReason.VERIFICATION_FAILED.value}
        for job in manifest.get("other_unsuccessful_jobs", [])
    )
    addressed = [r for r in results if r.get("status") in ("REPAIRED", "FIXED_BY_PRIOR")]

    def verify_all(label: str, jobs: list[dict]) -> list[dict]:
        """Jobs against the one cumulative patch, in fresh containers."""
        found = []
        for result in jobs:
            cfg = result["config"]
            directory = output / label / str(result["job_id"])
            directory.mkdir(parents=True)
            final = verify_patch(replace(cfg, output=directory), archive, cfg.image, cumulative)
            result["final_verification"] = final["status"]
            found.extend(final.get("tests", []))
            if not final.get("verified"):
                result["status"] = "REGRESSED"
        return found

    tests = verify_all("verification", addressed) if cumulative.read_bytes() else []
    regressed = [r for r in addressed if r["status"] == "REGRESSED"]
    for result in regressed:
        # A later repair broke this job. One bounded fix-up on the combined change, seeing
        # the job's current failure; the final verification below still decides.
        fix_up(result, addressed, output, candidate, cumulative, base, model_factory, policy, usage)
    if any(r["fixup"]["verified"] for r in regressed):
        for result in regressed:
            if result["fixup"]["verified"]:
                result["status"] = "REPAIRED"
        # A failed fix-up stays REGRESSED; everything else is judged on the new patch.
        again = [r for r in addressed if r["status"] != "REGRESSED"]
        tests = verify_all("verification-after-fixup", again)
    patch = cumulative.read_bytes()
    report["patch_sha256"] = hashlib.sha256(patch).hexdigest()
    report["usage"] = {**usage, "models_used": sorted(usage["models_used"])}
    if patch:
        paths = command(["git", "diff", "--name-only", "-z", base, "HEAD"], cwd=candidate)
        report["changed_files"] = paths.decode().rstrip("\0").split("\0")
        decision = policy.check_patch(report["changed_files"], patch, allowed)
        report["policy_decision"] = decision.to_dict()
    for result in results:
        result.pop("config", None)
    report["jobs"] = results
    report["tests"] = tests
    report.update(summarize(results, patch, report.get("policy_decision")))
    write_json(output / "report.json", report)
    return report


def fix_up(result, addressed, output, candidate, cumulative, base, model_factory, policy, usage):
    cfg = result["config"]
    job_dir = output / "jobs" / str(result["job_id"])
    current = output / "verification" / str(result["job_id"])
    failure = "\n".join(
        json.loads(p.read_text()).get("output", "")
        for p in (current / "failing.json", current / "regression.json")
        if p.exists()
    )
    (job_dir / "fixup-failure.log").write_text(failure)
    others = [r["job_name"] for r in addressed if r is not result]
    task = (
        f"GitHub Actions job {result['job_name']!r} was already repaired, but the later "
        f"repairs for {', '.join(repr(o) for o in others) or 'other jobs'} made it fail again "
        "(current failure attached). Change the current code so this job passes again "
        "while keeping those other repairs working."
    )
    repair = run_pipeline(
        replace(
            cfg, output=job_dir / "fixup", failure_log=job_dir / "fixup-failure.log", task=task
        ),
        model_factory(),
        policy,
    )
    for key in ("model_calls", "estimated_cost_usd", "agent_steps"):
        usage[key] += repair.get("usage", {}).get(key, 0)
    usage["models_used"].update(repair.get("usage", {}).get("models_used", []))
    result["fixup"] = {
        "repair_status": repair["status"],
        "stop_reason": repair.get("stop_reason"),
        "verified": bool(repair.get("verified")),
    }
    if repair.get("verified"):
        command(
            ["git", "apply", "--index", "--binary", str(job_dir / "fixup" / "patch.diff")],
            cwd=candidate,
        )
        git_commit(candidate, f"fix up {result['job_name']}")
        cumulative.write_bytes(command(["git", "diff", "--binary", base, "HEAD"], cwd=candidate))


def summarize(results: list[dict], patch: bytes, decision: dict | None) -> dict:
    verified = [r for r in results if r.get("final_verification") == "PASS"]
    if decision and decision["verdict"] == Verdict.DENY.value:
        return {"status": "PATCH_REJECTED", "verified": False, "stop_reason": "POLICY_DENIED"}
    if patch and verified and len(verified) == len(results):
        return {"status": "PASS", "verified": True, "stop_reason": StopReason.VERIFIED_PASS.value}
    if any(r["status"] == "REGRESSED" for r in results):
        return {"status": "FAIL", "verified": False, "stop_reason": "VERIFICATION_FAILED"}
    if verified:
        return {"status": "PARTIAL", "verified": False, "stop_reason": first_failure(results)}
    if all(r["status"] == "UNSUPPORTED_ENVIRONMENT" for r in results):
        return {
            "status": "UNSUPPORTED_ENVIRONMENT",
            "verified": False,
            "stop_reason": "UNSUPPORTED_ENVIRONMENT",
        }
    return {"status": "FAIL", "verified": False, "stop_reason": first_failure(results)}


def first_failure(results: list[dict]) -> str:
    for r in results:
        if r.get("final_verification") != "PASS":
            if r["status"] in ("UNSUPPORTED_ENVIRONMENT", "BASELINE_NOT_REPRODUCED"):
                return r["status"]
            return r.get("stop_reason") or r["status"]
    return StopReason.EXECUTION_ERROR.value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("collection", type=Path, help="Output of ci-repair-github --all-jobs")
    parser.add_argument("--policy", type=Path, help="Operator policy YAML outside the repository")
    parser.add_argument("--model", help="Default: policy models.default")
    parser.add_argument("--model-class", choices=("litellm", "openrouter"), default="litellm")
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    collection = args.collection.resolve()
    try:
        policy = load_policy(args.policy, untrusted_roots=[collection])
    except (PolicyError, OSError) as exc:
        parser.error(f"Policy: {exc}")
    model_name = args.model or policy.data["models"]["default"]
    if not model_name or not policy.check_model(model_name).allowed:
        parser.error("Provide a model allowed by the policy")
    if args.env_file:
        from dotenv import load_dotenv

        load_dotenv(args.env_file, override=True)
    from ci_repair.cli import make_model

    wall = int(policy.data["budget"]["max_wall_seconds"])
    output = (
        args.output or Path("runs") / datetime.now(timezone.utc).strftime("run-%Y%m%dT%H%M%S%fZ")
    ).resolve()
    report = repair_run(
        collection,
        output,
        model_factory=lambda: make_model(model_name, args.model_class, wall),
        policy=policy,
    )
    print(f"{report['status']} ({report.get('stop_reason')}): {output / 'report.json'}")
    return 0 if report["verified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
