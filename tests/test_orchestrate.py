import hashlib
import json
import os

import pytest
from test_workspace import commit

from ci_repair.orchestrate import order_jobs, repair_run, summarize
from ci_repair.policy import Policy
from ci_repair.workspace import command

WORKFLOW = """on: push
jobs:
  lint:
    runs-on: ubuntu-latest
    container: ci-repair-demo:local
    steps:
      - uses: actions/checkout@v4
      - name: check
        run: python -c 'from src.app import value; assert value == 1'
  unit:
    runs-on: ubuntu-latest
    container: ci-repair-demo:local
    steps:
      - uses: actions/checkout@v4
      - name: test
        run: python -c 'from src.app import value; assert value > 0'
  other:
    runs-on: ubuntu-latest
    container: ci-repair-demo:local
    steps:
      - uses: actions/checkout@v4
      - name: test
        run: python -c 'from src.other import flag; assert flag'
"""


def collection(tmp_path, workflow=WORKFLOW, jobs=(("lint", "check"), ("unit", "test"))):
    root = tmp_path / "collected"
    repo = root / "repo"
    (repo / ".github/workflows").mkdir(parents=True)
    (repo / ".github/workflows/ci.yml").write_text(workflow)
    (repo / "src").mkdir()
    (repo / "src/__init__.py").write_text("")
    (repo / "src/app.py").write_text("value = 0\n")
    (repo / "src/other.py").write_text("flag = False\n")
    (repo / ".gitignore").write_text("__pycache__/\n")
    command(["git", "init", "-q", str(repo)])
    commit(repo)
    sha = command(["git", "rev-parse", "HEAD"], cwd=repo).decode().strip()
    entries = []
    for number, (name, step) in enumerate(jobs, 1):
        directory = root / "jobs" / str(number)
        directory.mkdir(parents=True)
        log = f"AssertionError in {name}\n".encode()
        (directory / "failure.log").write_bytes(log)
        (directory / "ci-context.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "repository": "owner/repo",
                    "commit": sha,
                    "run_id": 7,
                    "run_attempt": 1,
                    "run_url": "https://github.com/owner/repo/actions/runs/7",
                    "event": "push",
                    "workflow": "CI",
                    "workflow_path": ".github/workflows/ci.yml",
                    "job_id": number,
                    "job_name": name,
                    "failed_steps": [step],
                    "job_steps": [],
                    "log_sha256": hashlib.sha256(log).hexdigest(),
                }
            )
        )
        entries.append({"job_id": number, "job_name": name, "path": f"jobs/{number}"})
    (root / "run.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "repository": "owner/repo",
                "commit": sha,
                "run_id": 7,
                "run_attempt": 1,
                "run_url": "https://github.com/owner/repo/actions/runs/7",
                "event": "push",
                "workflow": "CI",
                "workflow_path": ".github/workflows/ci.yml",
                "checkout_kind": "head",
                "head_branch": "main",
                "jobs": entries,
            }
        )
    )
    return root


def test_order_respects_needs_then_workflow_position():
    doc = {
        "jobs": {"test": {"needs": "build"}, "lint": {}, "build": {}, "e2e": {"needs": ["test"]}}
    }
    entries = [
        {"job_id": 4, "job_name": "e2e", "job_key": "e2e"},
        {"job_id": 1, "job_name": "test", "job_key": "test"},
        {"job_id": 3, "job_name": "build", "job_key": "build"},
        {"job_id": 2, "job_name": "lint", "job_key": "lint"},
        {"job_id": 5, "job_name": "mystery", "job_key": None},
    ]
    ordered, explanation = order_jobs(entries, doc)
    assert [e["job_name"] for e in ordered] == ["lint", "build", "test", "e2e", "mystery"]
    assert "test (1) after build" in explanation
    assert order_jobs(list(reversed(entries)), doc)[0] == ordered  # input order is irrelevant


@pytest.mark.parametrize(
    "statuses,finals,expected",
    [
        (["REPAIRED", "FIXED_BY_PRIOR"], ["PASS", "PASS"], "PASS"),
        (["REPAIRED", "REPAIR_FAILED"], ["PASS", None], "PARTIAL"),
        (["REGRESSED", "REPAIRED"], ["FAIL", "PASS"], "FAIL"),
        (["UNSUPPORTED_ENVIRONMENT"], [None], "UNSUPPORTED_ENVIRONMENT"),
        (["BASELINE_NOT_REPRODUCED"], [None], "FAIL"),
    ],
)
def test_run_status(statuses, finals, expected):
    results = [
        {"status": s, **({"final_verification": f} if f else {})} for s, f in zip(statuses, finals)
    ]
    assert summarize(results, b"patch", {"verdict": "ALLOW"})["status"] == expected
    assert summarize(results, b"patch", {"verdict": "DENY"})["status"] == "PATCH_REJECTED"


def test_moved_checkout_is_stale_source(tmp_path):
    root = collection(tmp_path)
    (root / "repo/new.txt").write_text("moved")
    commit(root / "repo")
    report = repair_run(
        root, tmp_path / "out", model_factory=lambda: pytest.fail("no agent"), policy=Policy()
    )
    assert report["stop_reason"] == "STALE_SOURCE"
    assert json.loads((tmp_path / "out/report.json").read_text())["status"] == "ERROR"


def test_unsupported_jobs_never_build_or_start_agents(tmp_path):
    root = collection(tmp_path, WORKFLOW.replace("container: ci-repair-demo:local", "services: {}"))
    report = repair_run(
        root,
        tmp_path / "out",
        model_factory=lambda: pytest.fail("no agent"),
        policy=Policy(),
        build=lambda *args: pytest.fail("no build"),
    )
    assert report["status"] == "UNSUPPORTED_ENVIRONMENT"
    assert [j["status"] for j in report["jobs"]] == ["UNSUPPORTED_ENVIRONMENT"] * 2
    assert any("services" in r for r in report["jobs"][0]["environment"]["reasons"])


@pytest.mark.docker
@pytest.mark.skipif(os.getenv("CI_REPAIR_DOCKER_TESTS") != "1", reason="Docker opt-in required")
def test_multi_job_run_accumulates_one_verified_patch(tmp_path):
    from test_docker import scripted_model

    root = collection(tmp_path, jobs=(("other", "test"), ("unit", "test"), ("lint", "check")))
    models = [
        scripted_model("sed -i 's/value = 0/value = 1/' src/app.py"),  # lint (first by position)
        scripted_model("sed -i 's/flag = False/flag = True/' src/other.py"),  # other
    ]
    started = []

    def factory():
        started.append(True)
        return models[len(started) - 1]

    output = tmp_path / "out"
    policy = Policy({"repositories": [{"name": "owner/repo", "allowed_paths": ["src/"]}]})
    report = repair_run(root, output, model_factory=factory, policy=policy)
    try:
        assert report["status"] == "PASS", report
        assert [(j["job_name"], j["status"]) for j in report["jobs"]] == [
            ("lint", "REPAIRED"),
            ("unit", "FIXED_BY_PRIOR"),
            ("other", "REPAIRED"),
        ]
        assert len(started) == 2  # unit never started an agent
        assert sorted(report["changed_files"]) == ["src/app.py", "src/other.py"]
        assert all(j["final_verification"] == "PASS" for j in report["jobs"])
        assert len(report["tests"]) == 6
        patch = (output / "patch.diff").read_bytes()
        assert report["patch_sha256"] == hashlib.sha256(patch).hexdigest()
        assert "value = 0" in (root / "repo/src/app.py").read_text()  # input untouched
    finally:
        for job in report["jobs"]:
            tag = f"ci-repair-env:{job['environment']['spec_sha256'][:16]}"
            command(["docker", "image", "rm", "-f", tag])
