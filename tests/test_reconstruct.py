import subprocess

import pytest
from test_workspace import commit

from ci_repair.policy import Policy
from ci_repair.reconstruct import (
    REVIEW,
    SUPPORTED,
    UNSUPPORTED,
    expand_matrix,
    parse_log,
    reconstruct,
    replay_commands,
    step_command,
)
from ci_repair.workspace import command

WORKFLOW = """name: CI
on: push
env:
  GLOBAL: one
jobs:
  test:
    runs-on: ubuntu-latest
    env:
      JOB: two
    defaults:
      run:
        working-directory: pkg
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'
          cache: pip
      - name: install
        run: pip install -r requirements.txt
      - name: unit tests
        shell: bash
        env:
          STEP: three
        run: python -m pytest -q
      - name: lint
        run: ruff check .
      - uses: actions/upload-artifact@v4
        with: {name: report, path: out}
"""


def repo_with(tmp_path, workflow, extra=None):
    repo = tmp_path / "repo"
    (repo / ".github/workflows").mkdir(parents=True)
    (repo / ".github/workflows/ci.yml").write_text(workflow)
    for name, text in (extra or {}).items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text)
    command(["git", "init", "-q", str(repo)])
    commit(repo)
    return repo, command(["git", "rev-parse", "HEAD"], cwd=repo).decode().strip()


def job(name="test", failed="unit tests", steps=None):
    return {"name": name, "failed_steps": [failed], "steps": steps or []}


def spec_for(tmp_path, workflow=WORKFLOW, meta=None, policy=None, extra=None, log=""):
    repo, sha = repo_with(tmp_path, workflow, extra)
    return reconstruct(
        repo,
        sha,
        ".github/workflows/ci.yml",
        meta or job(),
        log,
        policy or Policy(),
        repository="o/r",
        event="push",
    )


def test_supported_job_resolves_toolchain_setup_failing_and_regression(tmp_path):
    spec = spec_for(tmp_path)
    assert spec["status"] == SUPPORTED, spec["unsupported"] + spec["review_reasons"]
    assert spec["base_image"] == "python:3.12-bookworm"
    assert spec["fidelity"] == "toolchain-image"
    assert [s["script"] for s in spec["setup"]] == ["pip install -r requirements.txt"]
    failing = spec["failing"]
    assert failing["shell"] == "bash" and failing["working_directory"] == "pkg"
    assert {"GLOBAL": "one", "JOB": "two", "STEP": "three", "CI": "true"}.items() <= failing[
        "env"
    ].items()
    assert [s["script"] for s in spec["regression"]] == ["ruff check ."]
    assert spec["source"]["step_number"] == 4
    assert len(spec["spec_sha256"]) == 64
    assert spec_for(tmp_path / "again")["spec_sha256"] == spec["spec_sha256"]


def test_step_commands_preserve_env_directory_and_errexit(tmp_path):
    (tmp_path / "pkg").mkdir()
    step = {
        "script": 'test "$FOO" = "a b"\npwd > where\nfalse\ntouch not-reached',
        "working_directory": "pkg",
        "shell": "bash",
        "env": {"FOO": "a b"},
    }
    result = subprocess.run(["bash", "-c", step_command(step)], cwd=tmp_path)
    assert result.returncode != 0
    assert (tmp_path / "pkg/where").read_text().strip().endswith("pkg")
    assert not (tmp_path / "pkg/not-reached").exists()
    failing, regression = replay_commands({"failing": step, "regression": []})
    assert regression == failing


def test_matrix_job_is_selected_by_rendered_name(tmp_path):
    workflow = WORKFLOW.replace(
        "    runs-on: ubuntu-latest\n",
        "    runs-on: ${{ matrix.os }}\n    strategy:\n      matrix:\n"
        "        python: ['3.11', '3.12']\n        os: [ubuntu-22.04]\n",
    ).replace("python-version: '3.12'", "python-version: ${{ matrix.python }}")
    spec = spec_for(tmp_path, workflow, job("test (3.11, ubuntu-22.04)"))
    assert spec["status"] == SUPPORTED, spec["unsupported"]
    assert spec["base_image"] == "python:3.11-bookworm"
    assert spec["source"]["matrix"] == {"python": "3.11", "os": "ubuntu-22.04"}
    assert spec["runner"]["image"] == "ubuntu-22.04"
    assert spec_for(tmp_path / "x", workflow, job("test (3.10, ubuntu-22.04)"))["status"] == (
        UNSUPPORTED
    )


def test_matrix_include_and_exclude_follow_github_rules():
    matrix = {
        "fruit": ["apple", "pear"],
        "animal": ["cat", "dog"],
        "exclude": [{"fruit": "pear", "animal": "dog"}],
        "include": [{"color": "green"}, {"fruit": "apple", "shape": "circle"}, {"fruit": "fig"}],
    }
    from ci_repair.reconstruct import Problems

    combos = expand_matrix(matrix, Problems())
    assert {"fruit": "apple", "animal": "cat", "color": "green", "shape": "circle"} in combos
    assert {"fruit": "pear", "animal": "cat", "color": "green"} in combos
    assert {"fruit": "fig"} in combos
    assert len(combos) == 4


@pytest.mark.parametrize(
    "change,reason",
    [
        (("run: pip install", "run: pip install --token ${{ secrets.TOKEN }}"), "secrets"),
        (
            (
                "    runs-on: ubuntu-latest\n",
                "    runs-on: ubuntu-latest\n    services: {db: {image: postgres}}\n",
            ),
            "services",
        ),
        (("uses: actions/setup-python@v5", "uses: acme/custom@v1"), "outside the supported"),
        (
            ("run: pip install -r requirements.txt", 'run: echo "X=1" >> "$GITHUB_ENV"'),
            "GITHUB_ENV",
        ),
        (("runs-on: ubuntu-latest", "runs-on: windows-latest"), "runner"),
        (("shell: bash", "shell: pwsh"), "shell"),
        (("run: pip install", "run: pip install ${{ steps.x.outputs.y }}"), "expression"),
        (
            ("uses: actions/checkout@v4", "uses: actions/checkout@v4\n        with: {ref: main}"),
            "checkout inputs",
        ),
        (
            (
                "    runs-on: ubuntu-latest\n",
                "    runs-on: ubuntu-latest\n    uses: o/r/.github/workflows/x.yml@v1\n",
            ),
            "job.uses",
        ),
    ],
)
def test_unsupported_semantics_fail_closed(tmp_path, change, reason):
    spec = spec_for(tmp_path, WORKFLOW.replace(*change))
    assert spec["status"] == UNSUPPORTED
    assert any(reason in r for r in spec["unsupported"]), spec["unsupported"]


def test_later_unreplayable_steps_only_require_review(tmp_path):
    workflow = WORKFLOW.replace("run: ruff check .", "run: deploy --key ${{ secrets.KEY }}")
    spec = spec_for(tmp_path, workflow)
    assert spec["status"] == REVIEW
    assert spec["regression"] == []
    assert any("not replayed" in r for r in spec["review_reasons"])


def test_conditional_preceding_step_uses_api_conclusion(tmp_path):
    workflow = WORKFLOW.replace(
        "      - name: install\n", "      - name: install\n        if: runner.os == 'Linux'\n"
    )
    assert spec_for(tmp_path / "a", workflow)["status"] == UNSUPPORTED
    api = [
        {"name": "Set up job", "conclusion": "success"},
        {"name": "Run actions/checkout@v4", "conclusion": "success"},
        {"name": "Run actions/setup-python@v5", "conclusion": "success"},
        {"name": "install", "conclusion": "skipped"},
        {"name": "unit tests", "conclusion": "failure"},
        {"name": "lint", "conclusion": "skipped"},
        {"name": "Post Run actions/checkout@v4", "conclusion": "success"},
    ]
    spec = spec_for(tmp_path / "b", workflow, job(steps=api))
    assert spec["status"] == SUPPORTED, spec["unsupported"]
    assert spec["setup"] == []


def test_container_job_and_runner_approximation(tmp_path):
    workflow = WORKFLOW.replace(
        "    runs-on: ubuntu-latest\n",
        "    runs-on: ubuntu-latest\n    container: python:3.11-slim\n",
    )
    spec = spec_for(tmp_path / "a", workflow)
    assert spec["base_image"] == "python:3.11-slim"
    assert spec["fidelity"] == "job-container"
    bare = WORKFLOW.replace(
        "      - uses: actions/setup-python@v5\n        with:\n          python-version: '3.12'\n"
        "          cache: pip\n",
        "",
    )
    spec = spec_for(
        tmp_path / "b",
        bare,
        log="2026-09-18T04:28:17.5Z Image: ubuntu-22.04\n"
        "2026-09-18T04:28:17.5Z Version: 20260907.1\n",
    )
    assert spec["status"] == REVIEW
    assert spec["fidelity"] == "approximate-runner"
    assert spec["base_image"] == "buildpack-deps:jammy"  # log resolved ubuntu-latest


def test_version_file_and_setup_network_policy(tmp_path):
    workflow = WORKFLOW.replace("python-version: '3.12'", "python-version-file: .python-version")
    spec = spec_for(tmp_path / "a", workflow, extra={".python-version": "3.13\n"})
    assert spec["base_image"] == "python:3.13-bookworm"
    offline = Policy({"sandbox": {"setup_network": "DENY"}})
    spec = spec_for(tmp_path / "b", policy=offline)
    assert spec["status"] == REVIEW


def test_log_provenance_is_bounded_hints():
    log = (
        "﻿2026-09-18T04:28:17.5Z Image: ubuntu-24.04\n"
        "2026-09-18T04:28:17.5Z Version: 20260907.300.1\n"
        "2026-09-18T04:28:17.8Z Download action repository 'actions/checkout@v4' "
        "(SHA:11d5960a326750d5838078e36cf38b85af677262)\n"
        "2026-09-18T04:28:19Z Successfully set up CPython (3.12.7)\n"
    )
    hints = parse_log(log)
    assert hints["runner_image"] == "ubuntu-24.04"
    assert hints["runner_image_version"] == "20260907.300.1"
    assert hints["actions"]["actions/checkout@v4"].startswith("11d5960a")
    assert hints["python"] == "3.12.7"


@pytest.mark.docker
@pytest.mark.skipif(
    __import__("os").getenv("CI_REPAIR_DOCKER_TESTS") != "1", reason="Docker opt-in required"
)
def test_reconstructed_container_job_builds_and_repairs(tmp_path):
    """Workflow -> spec -> built replay image (setup state kept) -> repair -> verify."""
    from test_docker import scripted_model

    from ci_repair.pipeline import Config, run
    from ci_repair.reconstruct import build_environment
    from ci_repair.workspace import snapshot

    workflow = """on: push
jobs:
  unit:
    runs-on: ubuntu-latest
    container: ci-repair-demo:local
    steps:
      - uses: actions/checkout@v4
      - name: prepare
        run: echo ready > /tmp/prepared && touch generated-by-setup
      - name: test
        run: test -f /tmp/prepared && python -c 'from src.app import value; assert value == 1'
      - name: regression
        run: python -c 'from src.app import value; assert isinstance(value, int)'
"""
    repo, sha = repo_with(
        tmp_path,
        workflow,
        {"src/app.py": "value = 0\n", "src/__init__.py": "", ".gitignore": "__pycache__/\n"},
    )
    spec = reconstruct(repo, sha, ".github/workflows/ci.yml", job("unit", "test"), "", Policy())
    assert spec["status"] == SUPPORTED, spec["unsupported"] + spec["review_reasons"]
    out = tmp_path / "env"
    out.mkdir()
    snapshot(repo, out / "source.tar")
    built = build_environment(spec, out / "source.tar", out, Policy())
    try:
        assert built["status"] == "BUILT", (out / "setup.log").read_text()
        assert built["setup_returncode"] == 0
        assert build_environment(spec, out / "source.tar", out, Policy())["reused"] is True
        failing, regression = replay_commands(spec)
        (tmp_path / "log").write_text("AssertionError")
        cfg = Config(
            repo, tmp_path / "log", tmp_path / "run", built["image_id"], failing, regression
        )
        report = run(cfg, scripted_model("sed -i 's/value = 0/value = 1/' src/app.py"))
        assert report["status"] == "PASS", report
        # Setup-created files are part of the replay baseline, not the repair patch.
        assert report["changed_files"] == ["src/app.py"]
    finally:
        command(["docker", "image", "rm", "-f", built["tag"]])
