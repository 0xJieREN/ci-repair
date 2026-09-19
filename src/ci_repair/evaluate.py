"""Small synthetic evaluation corpus with an oracle hidden from repair containers."""

import argparse
import hashlib
import json
import math
import platform
import re
import shlex
import time
from dataclasses import replace
from importlib.metadata import version
from pathlib import Path

from ci_repair.cli import make_model
from ci_repair.pipeline import Config, run, run_test, verify_patch, write_json
from ci_repair.upstream import run_upstream, upstream_prompts
from ci_repair.workspace import command, snapshot, workspace


def load_case(path: Path) -> dict:
    case = json.loads(path.read_text())
    if case.get("schema_version") != 1 or not re.fullmatch(r"[a-z0-9-]+", case.get("id", "")):
        raise ValueError("Invalid evaluation case")
    for field in ("category", "broken", "correct", "overfit", "public", "oracle"):
        if not isinstance(case.get(field), str) or not case[field].strip():
            raise ValueError(f"Missing case field: {field}")
    return case


def control_model(case: dict, candidate: str):
    """Known answers test the evaluator, never measure model repair ability."""
    from minisweagent.models import get_model
    from minisweagent.models.test_models import make_output

    script = "true"
    if candidate != "noop":
        script = f"printf %s {shlex.quote(case[candidate])} > src/app.py"
    return get_model(
        "deterministic",
        config={
            "model_class": "deterministic",
            "cost_per_call": 0,
            "outputs": [
                make_output("evaluation control", [{"command": cmd}], cost=0)
                for cmd in (script, "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT")
            ],
        },
    )


def prepare_case(case: dict, output: Path, image: str, *, command_seconds=60) -> Config:
    """One clean source commit and one captured failure, reused by every paired trial."""
    output.mkdir(parents=True, exist_ok=False)
    repo = output / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src/app.py").write_text(case["broken"])
    (repo / "test_public.py").write_text(case["public"])
    (repo / ".gitignore").write_text("__pycache__/\n")
    command(["git", "init", "-q", str(repo)])
    command(["git", "-c", "core.autocrlf=false", "add", "."], cwd=repo)
    command(
        [
            "env",
            "GIT_AUTHOR_DATE=2000-01-01T00:00:00+0000",
            "GIT_COMMITTER_DATE=2000-01-01T00:00:00+0000",
            "git",
            "-c",
            "user.name=Eval",
            "-c",
            "user.email=eval@localhost",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-qm",
            "case",
        ],
        cwd=repo,
    )
    archive = output / "source.tar"
    sha = snapshot(repo, archive.resolve())
    with workspace(archive, image, command_seconds, command_seconds + 30) as env:
        baseline = run_test(env, "python test_public.py", output / "baseline.json")
    if baseline.get("exception_info") or baseline["returncode"] in (0, -1, 124, 126, 127, 137):
        raise ValueError(f"Evaluation baseline not reproduced: {case['id']}")
    log = output / "failure.log"
    log.write_text(baseline["output"])
    write_json(
        output / "fixture.json",
        {
            "case": case["id"],
            "commit": sha,
            "image_id": image,
            "case_sha256": hashlib.sha256(json.dumps(case, sort_keys=True).encode()).hexdigest(),
            "log_sha256": hashlib.sha256(log.read_bytes()).hexdigest(),
        },
    )
    return Config(
        repo.resolve(),
        log.resolve(),
        (output / "unused").resolve(),
        image,
        "python test_public.py",
        "python -m compileall -q src && python test_public.py",
        command_seconds=command_seconds,
        task=case.get("task", ""),
    )


def grade(case: dict, config: Config, output: Path, report: dict) -> dict:
    """Same external acceptance gate and hidden oracle for both runners."""
    output.mkdir()
    archive = config.repo.parent / "source.tar"
    patch = config.output / "patch.diff"
    result = {"status": "NO_PATCH", "verified": False, "oracle_status": "NOT_RUN"}
    if report["status"] in ("ERROR", "TIMEOUT", "BASELINE_NOT_REPRODUCED"):
        return {**result, "status": report["status"]}
    if not patch.exists() or not patch.read_bytes():
        return result
    fixture = json.loads((config.repo.parent / "fixture.json").read_text())
    if (
        report.get("commit") != fixture["commit"]
        or report.get("image_id") != config.image
        or report.get("patch_sha256") != hashlib.sha256(patch.read_bytes()).hexdigest()
    ):
        return {**result, "status": "ERROR", "error_type": "EvidenceMismatch"}
    try:
        result.update(verify_patch(replace(config, output=output), archive, config.image, patch))
        if result["verified"]:
            oracle = output / "oracle.py"
            oracle.write_text(case["oracle"])
            with workspace(
                archive, config.image, config.command_seconds, config.command_seconds + 30
            ) as env:
                env.copy(patch, "/tmp/repair.diff")
                env.checked("git apply --index --binary /tmp/repair.diff")
                env.copy(oracle, "/tmp/oracle.py")
                test = run_test(
                    env, "PYTHONPATH=/workspace python /tmp/oracle.py", output / "oracle.json"
                )
            if test.get("exception_info") or test["returncode"] in (-1, 124, 126, 127, 137):
                result["oracle_status"] = "ERROR"
            else:
                result["oracle_status"] = "PASS" if test["returncode"] == 0 else "FAIL"
    except Exception as exc:
        result.update(status="ERROR", verified=False, error_type=type(exc).__name__)
    write_json(output / "report.json", result)
    return result


def evaluate_case(
    case: dict,
    output: Path,
    image: str,
    model,
    *,
    steps=30,
    cost=1.0,
    runner="ci-repair",
    fixture: Config | None = None,
    repetition=1,
    wall_seconds=600,
    command_seconds=60,
) -> dict:
    if runner not in ("ci-repair", "upstream"):
        raise ValueError("Unknown runner")
    output.mkdir(parents=True, exist_ok=False)
    if fixture is None:
        image = command(["docker", "image", "inspect", "--format={{.Id}}", image]).decode().strip()
        fixture = prepare_case(case, output / "input", image, command_seconds=command_seconds)
    cfg = replace(
        fixture,
        output=(output / "repair").resolve(),
        steps=steps,
        cost=cost,
        wall_seconds=wall_seconds,
        command_seconds=command_seconds,
    )
    report = (run if runner == "ci-repair" else run_upstream)(cfg, model)
    grade_start = time.monotonic()
    verdict = grade(case, cfg, output / "grade", report)
    grade_seconds = time.monotonic() - grade_start
    accepted = verdict.get("verified") is True
    patch = cfg.output / "patch.diff"
    patch_bytes = patch.read_bytes() if patch.exists() else b""
    response_models = set()
    trajectory = cfg.output / "trajectory.json"
    if trajectory.exists():
        for message in json.loads(trajectory.read_text()).get("messages", []):
            response = message.get("extra", {}).get("response", {})
            if isinstance(response, dict) and isinstance(response.get("model"), str):
                response_models.add(response["model"])
    score = {
        "case": case["id"],
        "runner": runner,
        "repetition": repetition,
        "case_sha256": hashlib.sha256(json.dumps(case, sort_keys=True).encode()).hexdigest(),
        "category": case["category"],
        "commit": report.get("commit"),
        "image_id": report.get("image_id"),
        "log_sha256": hashlib.sha256(cfg.failure_log.read_bytes()).hexdigest(),
        "runner_status": report["status"],
        "runner_verified": report.get("verified"),
        "gate_status": verdict["status"],
        "accepted": accepted,
        "oracle_status": verdict["oracle_status"],
        "repair_success": accepted and verdict["oracle_status"] == "PASS",
        "false_pass": accepted and verdict["oracle_status"] == "FAIL",
        "model_calls": report.get("model_calls", 0),
        "estimated_cost_usd": report.get("estimated_cost_usd", 0),
        "response_models": sorted(response_models),
        "repair_seconds": report["duration_seconds"],
        "grader_seconds": grade_seconds,
        "trial_seconds": report["duration_seconds"] + grade_seconds,
        "patch_bytes": len(patch_bytes),
        "patch_lines": sum(
            line.startswith((b"+", b"-")) and not line.startswith((b"+++", b"---"))
            for line in patch_bytes.splitlines()
        ),
    }
    write_json(output / "score.json", score)
    return score


def trial_error(score: dict) -> bool:
    # TIMEOUT is budget exhaustion (a failed repair), not missing infrastructure evidence.
    return (
        score["gate_status"] in ("ERROR", "BASELINE_NOT_REPRODUCED")
        or score["oracle_status"] == "ERROR"
    )


def summarize(scores: list[dict]) -> dict:
    errors = sum(trial_error(s) for s in scores)
    successes = sum(s["repair_success"] for s in scores)
    false_passes = sum(s["false_pass"] for s in scores)
    evaluated_accepts = sum(s["oracle_status"] in ("PASS", "FAIL") for s in scores)
    valid = len(scores) - errors
    result = {
        "cases": len(scores),
        "valid_trials": valid,
        "accepted": sum(s["accepted"] for s in scores),
        "errors": errors,
        "successes": successes,
        "false_passes": false_passes,
        "success_rate": successes / valid if valid else None,
        "success_rate_all_attempts": successes / len(scores) if scores else None,
        "false_pass_rate_among_evaluated_accepts": false_passes / evaluated_accepts
        if evaluated_accepts
        else None,
    }
    for field in (
        "model_calls",
        "estimated_cost_usd",
        "repair_seconds",
        "grader_seconds",
        "trial_seconds",
        "patch_lines",
    ):
        values = [s[field] for s in scores if field in s]
        result[f"total_{field}"] = sum(values)
        result[f"avg_{field}"] = sum(values) / len(values) if values else None
    return result


def schedule(cases: list[dict], runners: list[str], repetitions: int) -> list[dict]:
    trials = []
    for rep in range(1, repetitions + 1):
        for i, case in enumerate(cases):
            order = runners if (rep - 1 + i) % 2 == 0 else list(reversed(runners))
            trials.extend(
                {"case": case["id"], "runner": runner, "repetition": rep} for runner in order
            )
    return trials


def paired_results(scores: list[dict]) -> dict:
    pairs = {}
    for score in scores:
        pairs.setdefault((score["case"], score["repetition"]), {})[score["runner"]] = score
    result = {
        "complete_pairs": 0,
        "excluded_pairs": 0,
        "ci_only_success": 0,
        "upstream_only_success": 0,
        "both_success": 0,
        "both_fail": 0,
    }
    for arms in pairs.values():
        if set(arms) != {"ci-repair", "upstream"} or any(trial_error(s) for s in arms.values()):
            result["excluded_pairs"] += 1
            continue
        a, b = arms["ci-repair"], arms["upstream"]
        if any(
            a[field] != b[field] for field in ("commit", "image_id", "log_sha256", "case_sha256")
        ):
            raise ValueError("Paired inputs do not match")
        result["complete_pairs"] += 1
        left, right = a["repair_success"], b["repair_success"]
        key = (
            "both_success"
            if left and right
            else "ci_only_success"
            if left
            else "upstream_only_success"
            if right
            else "both_fail"
        )
        result[key] += 1
    return result


def save_summary(output: Path, manifest: dict, scores: list[dict]):
    rows = {
        runner: summarize([s for s in scores if s["runner"] == runner])
        for runner in manifest["runners"]
    }
    write_json(
        output / "summary.json",
        {
            "schema_version": 2,
            "mode": manifest["mode"],
            "model": manifest["model"],
            "planned_trials": len(manifest["schedule"]),
            "complete": len(scores) == len(manifest["schedule"]),
            **summarize(scores),
            "by_runner": rows,
            "paired": paired_results(scores),
            "scores": scores,
        },
    )
    text = "| Runner | Success / valid | Errors | False PASS | Avg calls | Avg USD | Avg runner s | Avg grader s |\n|---|---:|---:|---:|---:|---:|---:|---:|\n"
    for runner, row in rows.items():
        avg = [
            f"{row['avg_' + field]:.4f}" if row["avg_" + field] is not None else "—"
            for field in ("model_calls", "estimated_cost_usd", "repair_seconds", "grader_seconds")
        ]
        text += (
            f"| {runner} | {row['successes']}/{row['valid_trials']} | {row['errors']} | {row['false_passes']} | "
            + " | ".join(avg)
            + " |\n"
        )
    text += "\nSix synthetic cases; repeated trials are not independent new cases. See experiment.json for the comparison contract.\n"
    (output / "comparison.md").write_text(text)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cases", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--image", default="ci-repair-demo:local")
    parser.add_argument("--runner", choices=("ci-repair", "upstream", "both"), default="ci-repair")
    parser.add_argument("--repetitions", type=int, default=1)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--control", choices=("correct", "overfit", "noop"))
    mode.add_argument("--model")
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--steps", type=int, default=30)
    parser.add_argument("--cost", type=float, default=1.0)
    parser.add_argument("--wall-seconds", type=int, default=600)
    parser.add_argument("--command-seconds", type=int, default=60)
    parser.add_argument(
        "--max-total-calls",
        type=int,
        help="Required hard planned-call ceiling for model experiments",
    )
    args = parser.parse_args()
    if not all(
        math.isfinite(v) and v > 0
        for v in (args.steps, args.cost, args.wall_seconds, args.command_seconds, args.repetitions)
    ):
        parser.error("Budgets and repetitions must be finite and positive")
    cases = [load_case(p) for p in sorted(args.cases.glob("*.json"))]
    if not cases or len({c["id"] for c in cases}) != len(cases):
        parser.error("Cases must be nonempty and have unique IDs")
    runners = ["ci-repair", "upstream"] if args.runner == "both" else [args.runner]
    trials = schedule(cases, runners, args.repetitions)
    planned_calls = len(trials) * args.steps
    if args.model and (args.max_total_calls is None or planned_calls > args.max_total_calls):
        parser.error(
            f"Planned call ceiling is {planned_calls}; provide a sufficient --max-total-calls"
        )
    if args.env_file:
        if not args.env_file.is_file():
            parser.error("Environment file does not exist")
        from dotenv import load_dotenv

        load_dotenv(args.env_file, override=True)
    args.output.mkdir(parents=True, exist_ok=False, mode=0o700)
    image = command(["docker", "image", "inspect", "--format={{.Id}}", args.image]).decode().strip()
    _, upstream = upstream_prompts()
    manifest = {
        "schema_version": 2,
        "runners": runners,
        "mode": args.control or "model",
        "model": args.model,
        "steps_per_trial": args.steps,
        "cost_per_trial": args.cost,
        "wall_seconds": args.wall_seconds,
        "command_seconds": args.command_seconds,
        "planned_call_ceiling": planned_calls,
        "max_total_calls": args.max_total_calls,
        "image_id": image,
        "upstream": upstream,
        "litellm_version": version("litellm"),
        "platform": platform.platform(),
        "schedule": trials,
        "model_adapter": "DeterministicModel"
        if args.control
        else "shared LitellmModel with retries disabled",
        "comparison": "upstream DefaultAgent + stock mini.yaml prompts vs CI pipeline; shared hardened sandbox, model adapter, constraints and external grader",
    }
    project = Path(__file__).resolve().parents[2]
    manifest["code_commit"] = command(["git", "rev-parse", "HEAD"], cwd=project).decode().strip()
    manifest["code_dirty"] = bool(command(["git", "status", "--porcelain"], cwd=project))
    manifest["lock_sha256"] = hashlib.sha256((project / "uv.lock").read_bytes()).hexdigest()
    write_json(args.output / "experiment.json", manifest)
    scores = []
    save_summary(args.output, manifest, scores)
    fixtures = {
        case["id"]: prepare_case(
            case, args.output / "fixtures" / case["id"], image, command_seconds=args.command_seconds
        )
        for case in cases
    }
    by_id = {case["id"]: case for case in cases}
    for trial in trials:
        case = by_id[trial["case"]]
        model = (
            control_model(case, args.control)
            if args.control
            else make_model(args.model, wall_seconds=args.wall_seconds)
        )
        trial_id = f"{case['id']}-{trial['repetition']}-{trial['runner']}"
        score = evaluate_case(
            case,
            args.output / "trials" / trial_id,
            image,
            model,
            steps=args.steps,
            cost=args.cost,
            runner=trial["runner"],
            repetition=trial["repetition"],
            fixture=fixtures[case["id"]],
            wall_seconds=args.wall_seconds,
            command_seconds=args.command_seconds,
        )
        scores.append(score)
        save_summary(args.output, manifest, scores)
        print(
            f"{trial_id}: gate={score['gate_status']} oracle={score['oracle_status']}", flush=True
        )
        if score["runner_status"] == "ERROR" and args.model:
            # A provider/config failure should not spend the rest of the experiment budget.
            break
    return 1 if summarize(scores)["errors"] or len(scores) != len(trials) else 0


if __name__ == "__main__":
    raise SystemExit(main())
