"""Reviewed local check replays from LCA; not the official full-workflow score."""

import argparse
import hashlib
import json
import urllib.request
from dataclasses import replace
from importlib.metadata import version
from pathlib import Path

from ci_repair.cli import make_model
from ci_repair.pipeline import Config, run, run_test, verify_patch, write_json
from ci_repair.upstream import run_upstream, upstream_prompts
from ci_repair.workspace import command, snapshot, workspace

DATASET = "JetBrains-Research/lca-ci-builds-repair"
REVISION = "ebf12dad7a97c3c0cdc38705403d8bc8ddde47dc"
PARQUET_SHA256 = "0b690ed61eef63f74be425df0010e04afb61ab3821d992a32b76c7c50e81f282"
# Curated from original failed commands, before any model trials. No commands
# from downloaded workflows or logs are evaluated automatically.
RECIPES = {
    24: {
        "repo": "canonical/cloud-init",
        "check": "/opt/cloud-init/bin/python -m ruff cloudinit/ tests/ tools/ packages/bddeb packages/brpm conftest.py setup.py",
        "regression": "/opt/cloud-init/bin/python -m ruff cloudinit/ tests/ tools/ packages/bddeb packages/brpm conftest.py setup.py",
        "diagnostics": ["F401", "typing.Optional", "Found 1 error"],
        "allow": ("cloudinit/", "tests/", "tools/", "packages/"),
        "category": "lint",
    },
    107: {
        "repo": "encode/httpx",
        "check": "PATH=/opt/httpx/bin:$PATH scripts/check",
        "regression": "PATH=/opt/httpx/bin:$PATH scripts/check",
        "diagnostics": [
            'Unexpected keyword argument "verify"',
            'Name "verify" is not defined',
            'Name "cert" is not defined',
            'Unexpected keyword argument "cert"',
            "Found 4 errors in 1 file",
        ],
        "allow": ("httpx/",),
        "category": "type-check",
    },
}


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_dataset(destination: Path) -> dict[int, dict]:
    import pyarrow.parquet as pq  # Optional: uv run --with pyarrow ...

    url = f"https://huggingface.co/datasets/{DATASET}/resolve/{REVISION}/data/python/test-00000-of-00001.parquet"
    with urllib.request.urlopen(url, timeout=60) as response:
        data = response.read(5_000_001)
    if digest(data) != PARQUET_SHA256:
        raise ValueError("Dataset checksum mismatch")
    destination.write_bytes(data)
    rows = pq.read_table(destination).to_pylist()
    if len(rows) != 68 or len({r["id"] for r in rows}) != 68:
        raise ValueError("Unexpected LCA dataset population")
    return {row["id"]: row for row in rows}


def expected_failure(result: dict, diagnostics: list[str]) -> bool:
    return (
        result.get("returncode") == 1
        and not result.get("exception_info")
        and all(d in result.get("output", "") for d in diagnostics)
    )


def prepare(row: dict, recipe: dict, output: Path, image: str) -> Config:
    if f"{row['repo_owner']}/{row['repo_name']}" != recipe["repo"]:
        raise ValueError("Dataset repository differs from reviewed recipe")
    output.mkdir(parents=True)
    write_json(output / "dataset-row.json", row)  # Host only, contains reference diff.
    repo = output / "repo"
    command(["git", "init", "-q", str(repo)])
    for sha in (row["sha_fail"], row["sha_success"]):
        command(
            [
                "git",
                "-C",
                str(repo),
                "fetch",
                "-q",
                "--depth=1",
                f"https://github.com/{recipe['repo']}.git",
                sha,
            ],
            timeout=180,
        )
    command(
        [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-C",
            str(repo),
            "checkout",
            "-q",
            "--detach",
            row["sha_fail"],
        ]
    )
    patch = (output / "reference.patch").resolve()
    patch.write_text(row["diff"])
    command(["git", "apply", "--index", "--binary", str(patch)], cwd=repo)
    if command(["git", "write-tree"], cwd=repo) != command(
        ["git", "rev-parse", row["sha_success"] + "^{tree}"], cwd=repo
    ):
        raise ValueError("Dataset reference diff does not produce the fixed source tree")
    # This is a newly created fixture, never the user's working tree.
    command(["git", "reset", "--hard", row["sha_fail"]], cwd=repo)
    archive = (output / "source.tar").resolve()
    sha = snapshot(repo, archive)
    with workspace(archive, image, 90, 150) as env:
        baseline = run_test(env, recipe["check"], output / "baseline.json")
    if not expected_failure(baseline, recipe["diagnostics"]):
        raise ValueError(f"LCA {row['id']}: expected failure not reproduced; inspect baseline.json")
    log = output / "failure.log"
    log.write_text("\n\n".join(f"{entry['step_name']}\n{entry['log']}" for entry in row["logs"]))
    cfg = Config(
        repo.resolve(),
        log.resolve(),
        (output / "gold").resolve(),
        image,
        recipe["check"],
        recipe["regression"],
        allowed_paths=recipe["allow"],
        command_seconds=90,
        task="Repair the CI failure described in the supplied logs. Preserve program behavior and the existing checks. Do not disable lint or type checking.",
    )
    cfg.output.mkdir()
    gold = verify_patch(cfg, archive, image, patch)
    if not gold.get("verified"):
        raise ValueError(f"LCA {row['id']}: reference fix rejected")
    write_json(
        output / "admission.json",
        {
            "id": row["id"],
            "repository": recipe["repo"],
            "commit": sha,
            "reference_commit": row["sha_success"],
            "image_id": image,
            "row_sha256": digest(json.dumps(row, sort_keys=True).encode()),
            "source_sha256": digest(archive.read_bytes()),
            "log_sha256": digest(log.read_bytes()),
            "baseline": "EXPECTED_FAIL",
            "reference": "PASS",
            "recipe": recipe,
            "scope": "selected-check-local-replay",
            "official_full_ci_success": None,
            "hidden_oracle": None,
        },
    )
    return cfg


def grade_candidate(cfg: Config, report: dict, output: Path) -> dict:
    output.mkdir()
    patch = cfg.output / "patch.diff"
    if report["status"] in ("ERROR", "TIMEOUT", "BASELINE_NOT_REPRODUCED"):
        return {"status": report["status"], "verified": False}
    if not patch.exists() or not patch.read_bytes():
        return {"status": "NO_PATCH", "verified": False}
    admission = json.loads((cfg.repo.parent / "admission.json").read_text())
    if (
        report.get("commit") != admission["commit"]
        or report.get("image_id") != cfg.image
        or report.get("patch_sha256") != digest(patch.read_bytes())
    ):
        return {"status": "ERROR", "verified": False}
    return verify_patch(
        replace(cfg, output=output), cfg.repo.parent / "source.tar", cfg.image, patch
    )


def save_scores(output: Path, scores: list[dict], planned: int):
    write_json(output / "scores.json", scores)
    write_json(
        output / "summary.json",
        {
            "complete": len(scores) == planned,
            "planned_trials": planned,
            "completed_trials": len(scores),
            "selected_check_passes": sum(s["selected_check_success"] for s in scores),
            "total_model_calls": sum(s["model_calls"] for s in scores),
            "estimated_cost_usd": sum(s["estimated_cost_usd"] for s in scores),
            "official_full_ci_success": None,
            "hidden_oracle": None,
            "false_pass_rate": None,
        },
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--image", default="ci-repair-lca:local")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--controls", action="store_true")
    mode.add_argument("--model")
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--steps", type=int, default=15)
    parser.add_argument("--max-total-calls", type=int)
    args = parser.parse_args()
    if args.steps <= 0 or (
        args.model and (args.max_total_calls is None or args.steps * 4 > args.max_total_calls)
    ):
        parser.error("Four trials require a positive step limit and sufficient --max-total-calls")
    args.output.mkdir(parents=True, exist_ok=False, mode=0o700)
    rows = load_dataset(args.output / "dataset.parquet")
    image = command(["docker", "image", "inspect", "--format={{.Id}}", args.image]).decode().strip()
    project = Path(__file__).resolve().parents[2]
    metadata = {
        "pyarrow_version": version("pyarrow"),
        "dataset": DATASET,
        "revision": REVISION,
        "parquet_sha256": PARQUET_SHA256,
        "code_commit": command(["git", "rev-parse", "HEAD"], cwd=project).decode().strip(),
        "code_dirty": bool(command(["git", "status", "--porcelain"], cwd=project)),
        "upstream": upstream_prompts()[1],
        "project_lock_sha256": digest((project / "uv.lock").read_bytes()),
        "environment_lock_sha256": digest(
            (project / "benchmarks/lca/requirements.lock").read_bytes()
        ),
        "recipes": RECIPES,
        "image_id": image,
        "model": args.model,
        "steps": args.steps,
        "max_total_calls": args.max_total_calls,
        "cost_per_trial": 0.1,
        "wall_seconds": 240,
        "command_seconds": 90,
        "scope": "selected-check-local-replay",
        "official_full_ci_success": None,
        "hidden_oracle": None,
    }
    write_json(args.output / "experiment.json", metadata)
    save_scores(args.output, [], 8 if args.controls else 4)
    fixtures = {
        i: prepare(rows[i], recipe, args.output / "fixtures" / str(i), image)
        for i, recipe in RECIPES.items()
    }
    if args.env_file:
        from dotenv import load_dotenv

        load_dotenv(args.env_file, override=True)
    scores = []
    for index, (i, fixture) in enumerate(fixtures.items()):
        runners = ["ci-repair", "upstream"] if index % 2 == 0 else ["upstream", "ci-repair"]
        for runner in runners:
            candidates = ["correct", "noop"] if args.controls else ["model"]
            for candidate in candidates:
                path = args.output / "trials" / f"{i}-{runner}-{candidate}"
                cfg = replace(
                    fixture,
                    output=(path / "repair").resolve(),
                    steps=args.steps,
                    cost=0.1,
                    wall_seconds=240,
                )
                if args.controls:
                    from ci_repair.evaluate import control_model

                    model = control_model(
                        {"schema_version": 2, "reference_patch": rows[i]["diff"]}, candidate
                    )
                else:
                    model = make_model(args.model, wall_seconds=240)
                report = (run if runner == "ci-repair" else run_upstream)(cfg, model)
                verdict = grade_candidate(cfg, report, path / "grade")
                trajectory = json.loads((cfg.output / "trajectory.json").read_text())
                score = {
                    "id": i,
                    "runner": runner,
                    "candidate": candidate,
                    "selected_check_success": verdict.get("verified") is True,
                    "gate_status": verdict["status"],
                    "runner_status": report["status"],
                    "agent_exit_status": trajectory["info"].get("exit_status"),
                    "model_calls": report.get("model_calls", 0),
                    "estimated_cost_usd": report.get("estimated_cost_usd", 0),
                    "duration_seconds": report["duration_seconds"],
                }
                scores.append(score)
                save_scores(args.output, scores, 8 if args.controls else 4)
                print(score, flush=True)
                if report["status"] == "ERROR":
                    return 1
                if args.controls and score["selected_check_success"] != (candidate == "correct"):
                    raise ValueError("Unexpected control result")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
