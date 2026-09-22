import hashlib
import json
import os
import sys

import pytest
from test_workspace import commit

from ci_repair import cli
from ci_repair.plan import draft, load_plan
from ci_repair.workspace import command

WORKFLOW = """name: CI
on: push
jobs:
  unit:
    runs-on: ubuntu-latest
    defaults:
      run:
        working-directory: package
    steps:
      - uses: actions/checkout@v4
      - name: unit tests
        shell: bash
        run: python test_public.py
"""


def collection(tmp_path, workflow=WORKFLOW):
    root = tmp_path / "collected"
    repo = root / "repo"
    (repo / ".github/workflows").mkdir(parents=True)
    (repo / ".github/workflows/ci.yml").write_text(workflow)
    command(["git", "init", "-q", str(repo)])
    commit(repo)
    sha = command(["git", "rev-parse", "HEAD"], cwd=repo).decode().strip()
    log = b"AssertionError: failed\n"
    (root / "failure.log").write_bytes(log)
    (root / "ci-context.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "commit": sha,
                "repository": "test/repo",
                "run_id": 1,
                "run_attempt": 1,
                "run_url": "https://github.com/test/repo/actions/runs/1",
                "event": "push",
                "workflow": "CI",
                "workflow_path": ".github/workflows/ci.yml",
                "job_id": 2,
                "job_name": "unit",
                "failed_steps": ["unit tests"],
                "log_sha256": hashlib.sha256(log).hexdigest(),
            }
        )
    )
    return root


def reviewed_plan(root, plan):
    plan["reviewed"] = True
    plan["execution"].update(
        image="image", regression_command="python regression.py", allowed_paths=["src/"]
    )
    path = root / "plan.json"
    path.write_text(json.dumps(plan))
    return path


def test_plan_requires_review_and_explicit_policy(tmp_path):
    root = collection(tmp_path)
    plan = draft(root)
    assert plan["execution"]["working_directory"] == "package"
    assert plan["execution"]["failing_command"] == "python test_public.py"
    assert plan["execution"]["image"] is None
    path = root / "plan.json"
    path.write_text(json.dumps(plan))
    with pytest.raises(ValueError, match="reviewed"):
        load_plan(path)
    plan["reviewed"] = True
    path.write_text(json.dumps(plan))
    with pytest.raises(ValueError, match="image"):
        load_plan(path)
    config = load_plan(reviewed_plan(root, plan))
    assert (
        config["failing_command"]
        == "cd -- package && bash --noprofile --norc -eo pipefail -c 'python test_public.py'"
    )
    assert config["allowed_paths"] == ("src/",)


@pytest.mark.parametrize("change", ["log", "commit", "dirty"])
def test_stale_plan_rejected_before_execution(tmp_path, change):
    root = collection(tmp_path)
    path = reviewed_plan(root, draft(root))
    if change == "log":
        (root / "failure.log").write_text("changed")
    else:
        (root / "repo/new.txt").write_text("changed")
        if change == "commit":
            commit(root / "repo")
    with pytest.raises(ValueError, match="changed|clean"):
        load_plan(path)


@pytest.mark.parametrize(
    "feature", ["strategy: {matrix: {python: ['3.12']}}", "services: {}", "env: {KEY: value}"]
)
def test_unsupported_features_cannot_be_cleared_in_plan(tmp_path, feature):
    root = collection(
        tmp_path,
        WORKFLOW.replace("runs-on: ubuntu-latest", "runs-on: ubuntu-latest\n    " + feature),
    )
    plan = draft(root)
    assert plan["unsupported"]
    plan["unsupported"] = []
    with pytest.raises(ValueError, match="supported"):
        load_plan(reviewed_plan(root, plan))


def test_wrong_workflow_and_duplicate_keys_rejected(tmp_path):
    root = collection(
        tmp_path,
        WORKFLOW.replace(
            "runs-on: ubuntu-latest", "runs-on: ubuntu-latest\n    runs-on: windows-latest"
        ),
    )
    with pytest.raises(ValueError, match="differs"):
        draft(root, ".github/workflows/wrong.yml")
    with pytest.raises(ValueError, match="Duplicate"):
        draft(root)


def test_cli_plan_routes_reviewed_inputs_without_executing_setup(tmp_path, monkeypatch):
    root = collection(tmp_path)
    path = reviewed_plan(root, draft(root))
    seen = {}

    def run(config, model, policy):
        seen["config"] = config
        return {"status": "PASS", "verified": True, "stop_reason": "VERIFIED_PASS"}

    monkeypatch.setattr(cli, "run", run)
    monkeypatch.setattr(cli, "make_model", lambda *args: object())
    monkeypatch.setattr(
        sys,
        "argv",
        ["ci-repair", "--plan", str(path), "--model", "fake", "--output", str(tmp_path / "run")],
    )
    assert cli.main() == 0
    assert "actions/checkout" not in seen["config"].failing_command
    assert seen["config"].repo == root / "repo"


@pytest.mark.docker
@pytest.mark.skipif(os.getenv("CI_REPAIR_DOCKER_TESTS") != "1", reason="Docker opt-in required")
def test_reviewed_plan_runs_pinned_case_in_docker(tmp_path):
    from test_docker import scripted_model

    from ci_repair.pipeline import Config, run

    # Without Bash errexit, the trailing true would hide the baseline failure.
    root = collection(
        tmp_path,
        WORKFLOW.replace(
            "run: python test_public.py", "run: |\n          python test_public.py\n          true"
        ),
    )
    repo = root / "repo"
    (repo / "package/src").mkdir(parents=True)
    (repo / "package/src/app.py").write_text("value = 0\n")
    (repo / "package/test_public.py").write_text("from src.app import value\nassert value == 1\n")
    (repo / ".gitignore").write_text("__pycache__/\n")
    commit(repo)
    metadata_path = root / "ci-context.json"
    metadata = json.loads(metadata_path.read_text())
    metadata["commit"] = command(["git", "rev-parse", "HEAD"], cwd=repo).decode().strip()
    metadata_path.write_text(json.dumps(metadata))
    plan = draft(root)
    path = reviewed_plan(root, plan)
    plan["execution"].update(
        image="ci-repair-demo:local",
        regression_command="python test_public.py",
        allowed_paths=["package/src/"],
    )
    path.write_text(json.dumps(plan))
    cfg = Config(**load_plan(path), output=tmp_path / "repair")
    result = run(cfg, scripted_model("sed -i 's/value = 0/value = 1/' package/src/app.py"))
    assert result["status"] == "PASS", result
    assert result["changed_files"] == ["package/src/app.py"]
