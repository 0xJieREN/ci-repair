"""Small synthetic evaluation corpus with an oracle hidden from repair containers."""

import argparse
import hashlib
import json
import re
import shlex
from pathlib import Path

from ci_repair.cli import make_model
from ci_repair.pipeline import Config, run, run_test, write_json
from ci_repair.workspace import command, workspace


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


def evaluate_case(case: dict, output: Path, image: str, model, *, steps=30, cost=1.0) -> dict:
    output.mkdir(parents=True, exist_ok=False)
    repo = output / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src/app.py").write_text(case["broken"])
    (repo / "test_public.py").write_text(case["public"])
    (repo / ".gitignore").write_text("__pycache__/\n")
    command(["git", "init", "-q", str(repo)])
    command(["git", "add", "."], cwd=repo)
    command(
        ["git", "-c", "user.name=Eval", "-c", "user.email=eval@localhost", "commit", "-qm", "case"],
        cwd=repo,
    )
    log = output / "failure.log"
    log.write_text("Synthetic failure: python test_public.py fails on the original source.\n")
    cfg = Config(
        repo.resolve(),
        log.resolve(),
        (output / "repair").resolve(),
        image,
        "python test_public.py",
        "python -m compileall -q src && python test_public.py",
        steps=steps,
        cost=cost,
    )
    report = run(cfg, model)
    accepted = report.get("verified") is True
    oracle_passed = None
    oracle_status = "NOT_RUN"
    if accepted:
        # Hidden assertions and reference fixes are never copied into repair workspaces.
        oracle = output / "oracle.py"
        oracle.write_text(case["oracle"])
        try:
            with workspace(cfg.output / "source.tar", report["image_id"], 60, 120) as env:
                env.copy(cfg.output / "patch.diff", "/tmp/repair.diff")
                env.checked("git apply --index --binary /tmp/repair.diff")
                env.copy(oracle, "/tmp/oracle.py")
                result = run_test(
                    env, "PYTHONPATH=/workspace python /tmp/oracle.py", output / "oracle.json"
                )
            if result.get("exception_info") or result["returncode"] in (-1, 124, 126, 127, 137):
                oracle_status = "ERROR"
            else:
                oracle_passed = result["returncode"] == 0
                oracle_status = "PASS" if oracle_passed else "FAIL"
        except Exception as exc:
            oracle_status = "ERROR"
            write_json(output / "oracle.json", {"error_type": type(exc).__name__})
    patch = cfg.output / "patch.diff"
    patch_bytes = patch.read_bytes() if patch.exists() else b""
    score = {
        "case": case["id"],
        "case_sha256": hashlib.sha256(json.dumps(case, sort_keys=True).encode()).hexdigest(),
        "category": case["category"],
        "commit": report.get("commit"),
        "image_id": report.get("image_id"),
        "gate_status": report["status"],
        "accepted": accepted,
        "oracle_status": oracle_status,
        "repair_success": accepted and oracle_passed is True,
        "false_pass": accepted and oracle_passed is False,
        "model_calls": report.get("model_calls", 0),
        "estimated_cost_usd": report.get("estimated_cost_usd", 0),
        "repair_seconds": report["duration_seconds"],
        "patch_bytes": len(patch_bytes),
        "patch_lines": sum(
            line.startswith((b"+", b"-")) and not line.startswith((b"+++", b"---"))
            for line in patch_bytes.splitlines()
        ),
    }
    write_json(output / "score.json", score)
    return score


def summarize(scores: list[dict]) -> dict:
    accepted = sum(s["accepted"] for s in scores)
    errors = sum(
        s["gate_status"] in ("ERROR", "TIMEOUT", "BASELINE_NOT_REPRODUCED")
        or s["oracle_status"] == "ERROR"
        for s in scores
    )
    successes = sum(s["repair_success"] for s in scores)
    false_passes = sum(s["false_pass"] for s in scores)
    # Unknown oracle outcomes are not silently counted as safe passes.
    evaluated_accepts = sum(s["oracle_status"] in ("PASS", "FAIL") for s in scores)
    return {
        "cases": len(scores),
        "accepted": accepted,
        "errors": errors,
        "successes": successes,
        "false_passes": false_passes,
        "success_rate": successes / len(scores) if scores else None,
        "false_pass_rate_among_evaluated_accepts": false_passes / evaluated_accepts
        if evaluated_accepts
        else None,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cases", type=Path, help="Directory of fixed JSON cases")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--image", default="ci-repair-demo:local")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--control", choices=("correct", "overfit", "noop"))
    mode.add_argument("--model", help="Explicit opt-in to paid model calls, per case budgets")
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--steps", type=int, default=30)
    parser.add_argument("--cost", type=float, default=1.0)
    args = parser.parse_args()
    cases = [load_case(p) for p in sorted(args.cases.glob("*.json"))]
    if not cases or len({c["id"] for c in cases}) != len(cases):
        parser.error("Cases must be nonempty and have unique IDs")
    if args.env_file:
        if not args.env_file.is_file():
            parser.error("Environment file does not exist")
        from dotenv import load_dotenv

        load_dotenv(args.env_file, override=True)
    args.output.mkdir(parents=True, exist_ok=False)
    image = command(["docker", "image", "inspect", "--format={{.Id}}", args.image]).decode().strip()
    scores = []
    for case in cases:
        model = control_model(case, args.control) if args.control else make_model(args.model)
        score = evaluate_case(
            case, args.output / case["id"], image, model, steps=args.steps, cost=args.cost
        )
        scores.append(score)
        # Preserve completed results even if a later case is interrupted.
        write_json(
            args.output / "summary.json",
            {
                "schema_version": 1,
                "mode": args.control or "model",
                "model": args.model,
                "steps_per_case": args.steps,
                "cost_per_case": args.cost,
                "planned_cases": len(cases),
                "complete": len(scores) == len(cases),
                **summarize(scores),
                "scores": scores,
            },
        )
        print(f"{case['id']}: gate={score['gate_status']} oracle={score['oracle_status']}")
    return 1 if summarize(scores)["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
