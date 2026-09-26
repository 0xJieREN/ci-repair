"""Paired comparison of CI Repair and the Pi coding agent on usable LCA tasks.

Both arms get the same replay images, the same model (deepseek/deepseek-flash, provider
default thinking), the same per-failed-job budget and no network; both are graded by the
same external check: the arm's final patch, applied to the original tree, must pass every
failed job's failing and regression commands in fresh containers, and must not be denied
by the patch policy (e.g. workflow edits). The arm's own verdict is recorded, not trusted.

- ci-repair: the production run path (`repair_run`) on the task packaged as a collection.
- pi: one Pi session over all failed jobs, tools routed into a network-less container
  built from the first job's image (eval/pi/docker-tools.ts).

    uv run --with pyarrow python eval/compare.py --output runs/compare \\
        --env-file .env --network container:pypi-wayback --mirror http://localhost:8080 \\
        --tasks 4,82 --repetitions 3
    uv run --with pyarrow python eval/compare.py --output runs/compare --summarize
"""

import argparse
import collections
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from lca_coverage import dataset, job_specs, runner_hint
from lca_replay import checkout, mirror_env

from ci_repair.context import failure_evidence
from ci_repair.orchestrate import order_jobs, repair_run
from ci_repair.pipeline import Config, verify_patch, write_json
from ci_repair.policy import Policy, Verdict, categorize
from ci_repair.reconstruct import (
    UNSUPPORTED,
    build_environment,
    load_workflow,
    replay_commands,
)
from ci_repair.workspace import extract_patch, snapshot, workspace

MODEL = "deepseek/deepseek-flash"
ARMS = ("ci-repair", "pi")
# Budgets per failed job; Pi's single session gets the sum over the task's jobs.
CALLS_PER_JOB = 30
WALL_PER_JOB = 1200
COMMAND_SECONDS = 600
PRICE = json.loads((Path(__file__).parents[1] / "config/deepseek-pricing.json").read_text())[MODEL]
PI = Path(__file__).parent / "pi"
PI_CLI = PI / "node_modules/@earendil-works/pi-coding-agent/dist/cli.js"


def policy() -> Policy:
    return Policy(
        {
            "models": {"allowed": [MODEL], "default": MODEL},
            "budget": {
                "max_model_calls": CALLS_PER_JOB,
                "max_cost_usd": 1.0,
                "max_wall_seconds": WALL_PER_JOB,
                "max_command_seconds": COMMAND_SECONDS,
                "max_setup_seconds": 1800,
            },
            "repair": {"max_jobs": 10},
        }
    )


def cost(prompt: int, cached: int, completion: int) -> float:
    return (
        (prompt - cached) * PRICE["input_cost_per_token"]
        + cached * PRICE["cache_read_input_token_cost"]
        + completion * PRICE["output_cost_per_token"]
    )


# --- Task preparation (once per task, shared by both arms and all repetitions) ---------


def prepare(row: dict, directory: Path, build) -> dict:
    """Package the task as a CI Repair collection, snapshot it and build every job image."""
    prep_file = directory / "prep.json"
    if prep_file.exists():
        return json.loads(prep_file.read_text())
    # A preparation interrupted before prep.json leaves nothing worth resuming.
    for leftover in ("collection", "build"):
        shutil.rmtree(directory / leftover, ignore_errors=True)
    (directory / "source.tar").unlink(missing_ok=True)
    collection = directory / "collection"
    repo = collection / "repo"
    checkout(row, repo)
    entries = job_specs(repo, row, policy())
    if any(e["spec"] is None or e["spec"]["status"] == UNSUPPORTED for e in entries):
        raise RuntimeError("task is not reconstructable")
    repository = f"{row['repo_owner']}/{row['repo_name']}"
    run_url = f"https://github.com/{repository}/commit/{row['sha_fail']}"
    common = {
        "schema_version": 1,
        "repository": repository,
        "commit": row["sha_fail"],
        "run_id": row["id"],
        "run_attempt": 1,
        "run_url": run_url,
        "event": "push",
        "workflow": row["workflow_name"],
        "workflow_path": row["workflow_path"],
    }
    jobs, manifest_jobs = [], []
    for index, entry in enumerate(entries, 1):
        job_dir = collection / "jobs" / str(index)
        job_dir.mkdir(parents=True)
        # The same log text reaches both arms: the job log plus the runner hint.
        log = (runner_hint(row["commit_date"]) + entry["log"]).encode()
        (job_dir / "failure.log").write_bytes(log)
        spec = entry["spec"]
        meta = {
            **common,
            "checkout_kind": "head",
            "head_branch": row["head_branch"],
            "job_id": index,
            "job_name": entry["job"],
            "failed_steps": [spec["source"]["step_name"]],
            "job_steps": None,
            "log_sha256": hashlib.sha256(log).hexdigest(),
        }
        write_json(job_dir / "ci-context.json", meta)
        manifest_jobs.append({"job_id": index, "job_name": entry["job"], "path": f"jobs/{index}"})
        failing, regression = replay_commands(spec)
        jobs.append(
            {
                "job_id": index,
                "job": entry["job"],
                "step": spec["source"]["step_name"],
                "job_key": spec["source"].get("job_key"),
                "spec": spec,
                "failing": failing,
                "regression": regression,
                "log": log.decode(errors="replace"),
            }
        )
    write_json(
        collection / "run.json",
        {
            **common,
            "checkout_kind": "head",
            "head_branch": row["head_branch"],
            "jobs": manifest_jobs,
            "other_unsuccessful_jobs": [],
        },
    )
    archive = directory / "source.tar"
    snapshot(repo, archive)
    for job in jobs:
        out = directory / "build" / str(job["job_id"])
        out.mkdir(parents=True)
        built = build(job["spec"], archive, out, policy())
        if built.get("status") not in ("BUILT", None) or not built.get("image_id"):
            raise RuntimeError(f"build failed for {job['job']}")
        job["image"], job["tag"] = built["image_id"], built["tag"]
    # The order repair_run uses: needs, then workflow position, name and ID.
    workflow, _ = load_workflow(repo, row["sha_fail"], row["workflow_path"])
    keyed = [{"job_key": j["job_key"], "job_name": j["job"], "job_id": j["job_id"]} for j in jobs]
    order = [e["job_id"] for e in order_jobs(keyed, workflow)[0]]
    prep = {
        "id": row["id"],
        "repository": repository,
        "difficulty": row["difficulty"],
        "archive": str(archive),
        "collection": str(collection),
        "order": order,
        "jobs": [{k: v for k, v in j.items() if k != "spec"} for j in jobs],
    }
    write_json(prep_file, prep)
    return prep


# --- Arms ------------------------------------------------------------------------------


def trajectory_usage(run_dir: Path) -> dict:
    """Token usage from mini trajectories (provider-reported, per model response)."""
    totals = collections.Counter()
    for path in run_dir.glob("jobs/*/repair/trajectory.json"):
        for message in json.loads(path.read_text()).get("messages", []):
            usage = (message.get("extra", {}).get("response") or {}).get("usage") or {}
            if not usage:
                continue
            details = usage.get("prompt_tokens_details") or {}
            reasoning = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
            totals["prompt"] += usage.get("prompt_tokens", 0)
            totals["cached"] += details.get("cached_tokens") or 0
            totals["completion"] += usage.get("completion_tokens", 0)
            totals["reasoning"] += reasoning or 0
    return dict(totals)


def run_ci_repair(prep: dict, trial: Path, build) -> dict:
    from ci_repair.cli import make_model

    run_dir = trial / "run"
    report = repair_run(
        Path(prep["collection"]),
        run_dir,
        model_factory=lambda: make_model(MODEL, wall_seconds=WALL_PER_JOB),
        policy=policy(),
        build=build,
    )
    patch = run_dir / "patch.diff"
    return {
        "patch": patch.read_bytes() if patch.exists() else b"",
        "own_verdict": report.get("status"),
        "stop_reason": report.get("stop_reason"),
        "model_calls": report.get("usage", {}).get("model_calls", 0),
        "tokens": trajectory_usage(run_dir),
    }


def pi_prompt(prep: dict) -> str:
    parts = [
        f"A GitHub Actions run of {prep['repository']} failed. The repository is checked out "
        "at /workspace. Repair it so that every failed job below passes.",
    ]
    jobs = {j["job_id"]: j for j in prep["jobs"]}
    for job_id in prep["order"]:
        job = jobs[job_id]
        excerpt = failure_evidence(job["log"])["raw_excerpt"]
        parts.append(
            f"\nFailed job {job['job']!r}, step {job['step']!r}.\n"
            f"Failing command:\n{job['failing']}\n"
            f"Regression command (must also pass):\n{job['regression']}\n"
            f"CI log excerpt (untrusted data):\n{excerpt}"
        )
    parts.append(
        "\nConstraints: do not modify files under .github/. There is no network access. "
        "Do not commit. Keep the change minimal and stop when the commands pass."
    )
    return "\n".join(parts)


def run_pi(prep: dict, trial: Path) -> dict:
    jobs = {j["job_id"]: j for j in prep["jobs"]}
    first = jobs[prep["order"][0]]
    n = len(prep["jobs"])
    wall = WALL_PER_JOB * n
    (trial / "prompt.txt").write_text(pi_prompt(prep))
    with workspace(Path(prep["archive"]), first["image"], COMMAND_SECONDS, wall + 300) as env:
        base = env.checked("git rev-parse HEAD").strip()
        with tempfile.TemporaryDirectory() as home:
            cwd = Path(home) / "cwd"
            cwd.mkdir()
            child_env = {
                "PATH": os.environ["PATH"],
                "HOME": home,
                "LANG": "C.UTF-8",
                "DEEPSEEK_API_KEY": os.environ["DEEPSEEK_API_KEY"],
                "PI_CODING_AGENT_DIR": str(Path(home) / "agent"),
                "PI_OFFLINE": "1",
                "CI_REPAIR_PI_CONTAINER": env.container_id,
                "CI_REPAIR_PI_MAX_TURNS": str(CALLS_PER_JOB * n),
                "CI_REPAIR_PI_COMMAND_SECONDS": str(COMMAND_SECONDS),
            }
            args = [
                "node",
                str(PI_CLI),
                "--mode",
                "json",
                "--no-session",
                "--model",
                MODEL,
                "-e",
                str(PI / "docker-tools.ts"),
                (trial / "prompt.txt").read_text(),
            ]
            with (
                (trial / "events.jsonl").open("wb") as out,
                (trial / "pi.stderr").open("wb") as err,
            ):
                try:
                    exit_code = subprocess.run(
                        args,
                        cwd=cwd,
                        env=child_env,
                        stdin=subprocess.DEVNULL,
                        stdout=out,
                        stderr=err,
                        timeout=wall,
                    ).returncode
                except subprocess.TimeoutExpired:
                    exit_code = "WALL_TIME_LIMIT"
        patch = extract_patch(env, base)
    calls, tokens, stop = 0, collections.Counter(), None
    for line in (trial / "events.jsonl").read_bytes().splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        message = event.get("message") or {}
        if event.get("type") == "message_end" and message.get("role") == "assistant":
            usage = message.get("usage") or {}
            calls += 1 if usage.get("totalTokens") else 0
            tokens["prompt"] += usage.get("input", 0) + usage.get("cacheRead", 0)
            tokens["cached"] += usage.get("cacheRead", 0)
            tokens["completion"] += usage.get("output", 0)
            tokens["reasoning"] += usage.get("reasoning", 0)
            stop = message.get("stopReason")
    return {
        "patch": patch,
        "own_verdict": None,  # Pi does not claim verification
        "stop_reason": exit_code if exit_code != 0 else stop,
        "model_calls": calls,
        "tokens": dict(tokens),
    }


# --- Common external grader ------------------------------------------------------------


def grade(prep: dict, patch: bytes, trial: Path) -> dict:
    if not patch:
        return {"passed": False, "reason": "no patch"}
    patch_path = trial / "patch.diff"
    patch_path.write_bytes(patch)
    results, changed = {}, []
    for job in prep["jobs"]:
        out = trial / "grade" / str(job["job_id"])
        out.mkdir(parents=True)
        config = Config(
            repo=out,
            failure_log=patch_path,
            output=out,
            image=job["image"],
            failing_command=job["failing"],
            regression_command=job["regression"],
            allowed_paths=(".",),
            command_seconds=COMMAND_SECONDS,
            wall_seconds=COMMAND_SECONDS + 60,
        )
        verdict = verify_patch(config, Path(prep["archive"]), job["image"], patch_path)
        changed = verdict.get("changed_files") or changed
        results[job["job"]] = verdict["status"]
    decision = policy().check_patch(changed, patch, (".",))
    passed = all(s == "PASS" for s in results.values()) and decision.verdict is not Verdict.DENY
    return {
        "passed": passed,
        "jobs": results,
        "policy": decision.to_dict(),
        "changed_files": changed,
        "changed_lines": sum(
            line.startswith((b"+", b"-")) and not line.startswith((b"+++", b"---"))
            for line in patch.splitlines()
        ),
        "touches_tests": any("tests" in categorize(p) for p in changed),
    }


# --- Experiment loop -----------------------------------------------------------------------


def trial(prep: dict, arm: str, repetition: int, directory: Path, build) -> dict:
    trial_dir = directory / arm / str(repetition)
    result_file = trial_dir / "result.json"
    if result_file.exists():
        return json.loads(result_file.read_text())
    trial_dir.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    try:
        run = (
            run_ci_repair(prep, trial_dir, build) if arm == "ci-repair" else run_pi(prep, trial_dir)
        )
        seconds = time.monotonic() - started
        graded = grade(prep, run.pop("patch"), trial_dir)
    except Exception as exc:  # noqa: BLE001 - infrastructure errors are reported, not scored
        run, seconds = {}, time.monotonic() - started
        graded = {"passed": None, "error": f"{type(exc).__name__}: {str(exc)[:300]}"}
    tokens = run.get("tokens", {})
    result = {
        "id": prep["id"],
        "arm": arm,
        "repetition": repetition,
        **{k: v for k, v in run.items() if k != "tokens"},
        "tokens": tokens,
        "cost_usd": cost(
            tokens.get("prompt", 0), tokens.get("cached", 0), tokens.get("completion", 0)
        ),
        "agent_seconds": round(seconds, 1),
        **graded,
    }
    write_json(result_file, result)
    return result


def run_task(row, output: Path, repetitions: int, build, keep_images: bool, arms=ARMS):
    directory = output / "tasks" / str(row["id"])
    directory.mkdir(parents=True, exist_ok=True)
    prep = prepare(row, directory, build)
    for repetition in range(1, repetitions + 1):
        # Alternate which arm goes first to spread provider cache and load effects.
        order = ARMS if (repetition + row["id"]) % 2 else tuple(reversed(ARMS))
        for arm in (a for a in order if a in arms):
            result = trial(prep, arm, repetition, directory, build)
            print(
                json.dumps({k: result.get(k) for k in ("id", "arm", "repetition", "passed")}),
                flush=True,
            )
    if not keep_images:
        for job in prep["jobs"]:
            subprocess.run(["docker", "image", "rm", "-f", job["tag"]], capture_output=True)


def record_experiment(output: Path, ids: list[int], repetitions: int):
    """What produced these results; appended per invocation so resumed runs stay traceable."""
    from importlib.metadata import version

    root = Path(__file__).parents[1]
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True
    ).stdout.strip()
    dirty = bool(
        subprocess.run(
            ["git", "status", "--porcelain", "--", "src", "eval"], cwd=root, capture_output=True
        ).stdout
    )
    pi = json.loads((PI / "node_modules/@earendil-works/pi-coding-agent/package.json").read_text())
    entry = {
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "commit": commit or None,
        "dirty": dirty,
        "model": MODEL,
        "thinking": "provider default (enabled) in both arms",
        "budget_per_failed_job": {
            "model_calls": CALLS_PER_JOB,
            "wall_seconds": WALL_PER_JOB,
            "command_seconds": COMMAND_SECONDS,
        },
        "repetitions": repetitions,
        "tasks": ids,
        "versions": {"mini-swe-agent": version("mini-swe-agent"), "pi": pi["version"]},
    }
    with (output / "experiment.jsonl").open("a") as stream:
        stream.write(json.dumps(entry) + "\n")


def summarize(output: Path) -> str:
    results = [json.loads(p.read_text()) for p in output.glob("tasks/*/*/*/result.json")]
    by_arm = collections.defaultdict(list)
    for r in results:
        by_arm[r["arm"]].append(r)
    lines = [
        "| Arm | Trials | Passed | Errors | Mean calls | Mean cost USD | Mean seconds |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for arm in ARMS:
        rs = by_arm.get(arm, [])
        scored = [r for r in rs if r.get("passed") is not None]
        if not rs:
            continue
        mean = lambda key: sum(r.get(key) or 0 for r in scored) / max(len(scored), 1)  # noqa: E731
        lines.append(
            f"| {arm} | {len(rs)} | {sum(bool(r['passed']) for r in scored)} | "
            f"{len(rs) - len(scored)} | {mean('model_calls'):.1f} | {mean('cost_usd'):.4f} | "
            f"{mean('agent_seconds'):.0f} |"
        )
    # Per task: share of repetitions passed, for paired, repository-clustered analysis.
    per_task = collections.defaultdict(lambda: collections.defaultdict(list))
    for r in results:
        if r.get("passed") is not None:
            per_task[r["id"]][r["arm"]].append(bool(r["passed"]))
    lines += ["", "| Task | " + " | ".join(ARMS) + " |", "|---:|" + "---:|" * len(ARMS)]
    for task in sorted(per_task):
        cells = []
        for arm in ARMS:
            passes = per_task[task][arm]
            cells.append(f"{sum(passes)}/{len(passes)}" if passes else "—")
        lines.append(f"| {task} | " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--summarize", action="store_true")
    parser.add_argument("--tasks", default="", help="Comma-separated LCA IDs")
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--parquet", type=Path)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--network", help="Setup network, e.g. container:pypi-wayback")
    parser.add_argument("--mirror", help="PyPI wayback base URL, e.g. http://localhost:8080")
    parser.add_argument("--keep-images", action="store_true")
    parser.add_argument("--arms", default=",".join(ARMS), help="Comma-separated subset of arms")
    args = parser.parse_args()
    args.output = args.output.resolve()  # git runs inside checkouts; paths must be absolute
    if args.summarize:
        print(summarize(args.output), end="")
        return
    if args.env_file:
        from dotenv import load_dotenv

        load_dotenv(args.env_file, override=True)
    if not os.environ.get("DEEPSEEK_API_KEY"):
        raise SystemExit("DEEPSEEK_API_KEY is required (use --env-file)")
    if not PI_CLI.exists():
        raise SystemExit("Install Pi first: cd eval/pi && pnpm install --frozen-lockfile")
    args.output.mkdir(parents=True, exist_ok=True)
    rows = {row["id"]: row for row in dataset(args.parquet, args.output)}
    ids = [int(i) for i in args.tasks.split(",") if i.strip()]
    if not ids:
        raise SystemExit("--tasks is required")

    record_experiment(args.output, ids, args.repetitions)

    def build(spec, archive, out, pol):
        return build_environment(
            spec, archive, out, pol, network=args.network, setup_env=mirror_env(row, args.mirror)
        )

    for task_id in ids:
        row = rows[task_id]
        try:
            arms = tuple(a for a in args.arms.split(",") if a in ARMS)
            run_task(row, args.output, args.repetitions, build, args.keep_images, arms)
        except Exception as exc:  # noqa: BLE001 - keep going; the task is reported as an error
            write_json(
                args.output / "tasks" / str(task_id) / "error.json",
                {"id": task_id, "error": f"{type(exc).__name__}: {str(exc)[:300]}"},
            )
            print(json.dumps({"id": task_id, "error": type(exc).__name__}), flush=True)


if __name__ == "__main__":
    main()
