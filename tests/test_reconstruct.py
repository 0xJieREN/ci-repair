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
    repeated = reconstruct(
        tmp_path / "repo",
        spec["source"]["commit"],
        ".github/workflows/ci.yml",
        job(),
        "",
        Policy(),
        repository="o/r",
        event="push",
    )
    assert repeated["spec_sha256"] == spec["spec_sha256"]


def test_continue_on_error_stops_step_but_allows_next_step(tmp_path):
    workflow = WORKFLOW.replace(
        "run: pip install -r requirements.txt",
        "continue-on-error: true\n        run: |\n          false\n          touch not-reached",
    )
    spec = spec_for(tmp_path, workflow)
    assert spec["status"] == SUPPORTED
    (tmp_path / "pkg").mkdir()
    result = subprocess.run(
        ["bash", "-e", "-c", step_command(spec["setup"][0]) + "\ntouch next-step"],
        cwd=tmp_path,
    )
    assert result.returncode == 0
    assert not (tmp_path / "pkg/not-reached").exists()
    assert (tmp_path / "next-step").exists()


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
    # Without an API conclusion an evaluable condition decides; an opaque one blocks.
    assert [s["script"] for s in spec_for(tmp_path / "a", workflow)["setup"]] == [
        "pip install -r requirements.txt"
    ]
    opaque = workflow.replace("runner.os == 'Linux'", "steps.x.outputs.y == 'a'")
    spec = spec_for(tmp_path / "c", opaque)
    assert spec["status"] == UNSUPPORTED
    assert any("cannot evaluate condition" in r for r in spec["unsupported"])
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
    assert spec["setup"][0]["shell"] == "sh"
    assert spec["failing"]["shell"] == "bash"  # Explicit step choice wins.
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


@pytest.mark.parametrize(
    "expr,expected",
    [
        ("matrix.cfg.tip && 'tip-' || ''", ""),
        ("!matrix.cfg.tip", True),
        ("matrix.py == '3.9'", True),  # YAML float and string compare as numbers
        ("matrix.missing == 'ubuntu-latest'", False),  # missing properties are null
        ("matrix.missing || 'ubuntu-latest'", "ubuntu-latest"),
        ("runner.os == 'LINUX'", True),  # string comparison ignores case
        ("matrix['cfg'].os", "ubuntu-20.04"),
        ("contains('Hello', 'ell') && startsWith('abc', 'A')", True),
        ("(1 < 2) && !null", True),
    ],
)
def test_expressions_follow_github_semantics(expr, expected):
    from ci_repair.reconstruct import evaluate

    ctx = {
        "literals": {"runner.os": "Linux"},
        "matrix": {"cfg": {"tip": False, "os": "ubuntu-20.04"}, "py": 3.9},
        "env": {},
    }
    assert evaluate(expr, ctx) == expected


@pytest.mark.parametrize(
    "expr", ["hashFiles('x')", "fromJSON('[]')", "steps.a.outputs.b", "github.ref", "success()"]
)
def test_unreproducible_expressions_fail_closed(expr):
    from ci_repair.reconstruct import ExpressionError, evaluate

    with pytest.raises(ExpressionError):
        evaluate(expr, {"literals": {"github.sha": "0" * 40}})


MAPPING_MATRIX = """on: push
jobs:
  lint:
    strategy:
      matrix:
        env: [ruff, mypy]
        lint-with:
          - {tip-versions: false, os: ubuntu-20.04}
          - {tip-versions: true, os: ubuntu-latest}
    name: Check ${{ matrix.lint-with.tip-versions && 'tip-' || '' }}${{ matrix.env }}
    runs-on: ${{ matrix.lint-with.os }}
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: {python-version: '3.11'}
      - uses: actions/cache@v4
        with: {path: ~/.cache, key: "${{ hashFiles('**/*.toml') }}"}
      - name: Test
        if: ${{ !matrix.lint-with.tip-versions }}
        run: tox -e ${{ matrix.env }}
      - name: Upload on failure
        if: failure()
        run: exit 1
      - name: Always
        if: always()
        run: echo done
"""


def test_mapping_matrix_expressions_and_later_conditions(tmp_path):
    spec = spec_for(tmp_path, MAPPING_MATRIX, job("Check mypy", "Test"))
    assert spec["status"] == SUPPORTED, spec["unsupported"] + spec["review_reasons"]
    assert spec["runner"]["image"] == "ubuntu-20.04"
    assert spec["failing"]["script"] == "tox -e mypy"
    # failure() is false once the failure is fixed; always() still runs as a regression check.
    assert [s["script"] for s in spec["regression"]] == ["echo done"]
    assert (
        spec_for(tmp_path / "tip", MAPPING_MATRIX, job("Check tip-mypy", "Test"))["runner"]["image"]
        == "ubuntu-24.04"
    )


def test_tool_actions_install_during_build_and_run_as_steps(tmp_path):
    workflow = WORKFLOW.replace(
        "        run: python -m pytest -q\n",
        "        uses: pre-commit/action@v3.0.0\n        with: {extra_args: --all-files -v}\n",
    ).replace("      - name: unit tests\n        shell: bash\n", "      - name: unit tests\n")
    spec = spec_for(tmp_path / "a", workflow)
    assert spec["status"] == SUPPORTED, spec["unsupported"] + spec["review_reasons"]
    assert spec["setup"][-1]["script"] == "python -m pip install pre-commit"
    assert spec["failing"]["script"].startswith("pre-commit run ")
    assert spec["failing"]["script"].endswith("--all-files -v")
    assert spec["failing"]["env"]["STEP"] == "three"
    pox = """on: push
jobs:
  fmt:
    runs-on: ubuntu-22.04
    steps:
      - uses: actions/checkout@v3
      - name: Check
        uses: paolorechia/pox@v1.0.1
        with: {tox_env: format_check}
"""
    spec = spec_for(tmp_path / "b", pox, job("fmt", "Check"))
    assert spec["status"] == REVIEW, spec["unsupported"]
    assert spec["base_image"] == "python:3.10-bookworm"  # the runner's default Python
    assert spec["setup"][0]["script"] == "python3 -m pip install tox"
    assert spec["failing"]["script"] == "python3 -m tox -e format_check"


def test_setup_python_without_version_uses_runner_default(tmp_path):
    workflow = WORKFLOW.replace(
        "        with:\n          python-version: '3.12'\n          cache: pip\n", ""
    ).replace("ubuntu-latest", "ubuntu-22.04")
    spec = spec_for(tmp_path, workflow)
    assert spec["status"] == REVIEW
    assert spec["base_image"] == "python:3.10-bookworm"
    assert any("runner default Python" in r for r in spec["review_reasons"])


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
        run: sudo sh -c 'echo ready > /tmp/prepared' && touch generated-by-setup
      - name: test
        run: |
          mkdir -p .tox && touch .tox/created-on-first-use warm-up-report.xml
          test -f /tmp/prepared && python -c 'from src.app import value; assert value == 1'
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
        # No repository to fetch: /workspace is a local commit of the failing tree.
        assert built["checkout"] == "local"
        # The warm-up ran the failing command once and kept only its tool environment.
        assert built["warm_up_returncode"] == 0
        command(
            [
                "docker",
                "run",
                "--rm",
                built["image_id"],
                "sh",
                "-c",
                "test -f /workspace/.tox/created-on-first-use"
                " && test ! -e /workspace/warm-up-report.xml"
                f' && test "$(git -C /workspace log -1 --format=%s)" = {sha}',
            ]
        )
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


def test_python_range_uses_the_version_ci_resolved(tmp_path):
    workflow = WORKFLOW.replace("python-version: '3.12'", "python-version: 3.x")
    assert spec_for(tmp_path / "a", workflow)["base_image"] == "python:3-bookworm"
    log = "##[group]Run pytest\nenv:\n  pythonLocation: /opt/hostedtoolcache/Python/3.11.7/x64\n"
    spec = spec_for(tmp_path / "b", workflow, log=log)
    assert spec["log_provenance_untrusted"]["python"] == "3.11.7"
    assert spec["base_image"] == "python:3.11-bookworm"
    # An exact workflow version is never overridden by the log.
    assert spec_for(tmp_path / "c", log=log)["base_image"] == "python:3.12-bookworm"


def test_custom_shell_template_keeps_its_flags(tmp_path):
    spec = spec_for(tmp_path, WORKFLOW.replace("shell: bash", "shell: sh -ex {0}"))
    assert spec["status"] == SUPPORTED, spec["unsupported"]
    step = {**spec["failing"], "script": "false\ntouch not-reached", "working_directory": "."}
    result = subprocess.run(["bash", "-c", step_command(step)], cwd=tmp_path, capture_output=True)
    assert result.returncode != 0 and b"+ false" in result.stderr
    assert not (tmp_path / "not-reached").exists()


@pytest.mark.docker
@pytest.mark.skipif(
    __import__("os").getenv("CI_REPAIR_DOCKER_TESTS") != "1", reason="Docker opt-in required"
)
def test_setup_env_reaches_setup_but_not_the_image(tmp_path):
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
        run: echo "$MIRROR_URL" > mirror-seen
      - name: test
        run: 'false'
"""
    repo, sha = repo_with(tmp_path, workflow)
    spec = reconstruct(repo, sha, ".github/workflows/ci.yml", job("unit", "test"), "", Policy())
    out = tmp_path / "env"
    out.mkdir()
    snapshot(repo, out / "source.tar")
    built = build_environment(
        spec, out / "source.tar", out, Policy(), setup_env={"MIRROR_URL": "http://mirror"}
    )
    try:
        assert built["status"] == "BUILT", (out / "setup.log").read_text()
        assert built["tag"].rsplit("-", 1)[1].startswith("s")  # never reused by plain builds
        assert built["setup_env_keys"] == ["MIRROR_URL"]
        seen = command(
            ["docker", "run", "--rm", built["image_id"], "cat", "/workspace/mirror-seen"]
        )
        assert seen.decode().strip() == "http://mirror"
        env = command(["docker", "image", "inspect", "--format={{json .Config.Env}}", built["tag"]])
        assert b"MIRROR_URL" not in env
    finally:
        command(["docker", "image", "rm", "-f", built["tag"]])


def test_checkout_depth_is_part_of_the_spec(tmp_path):
    assert spec_for(tmp_path / "a")["checkout"] == {"repository": "o/r", "fetch_depth": 1}
    full = WORKFLOW.replace(
        "uses: actions/checkout@v4", "uses: actions/checkout@v4\n        with: {fetch-depth: 0}"
    )
    spec = spec_for(tmp_path / "b", full)
    assert spec["checkout"]["fetch_depth"] == 0 and spec["status"] == SUPPORTED
    offline = spec_for(tmp_path / "c", full, policy=Policy({"sandbox": {"setup_network": "DENY"}}))
    assert any("Git history" in r for r in offline["review_reasons"])


def test_pruned_history_keeps_the_past_but_not_later_commits(tmp_path):
    from ci_repair.reconstruct import PRUNE_HISTORY

    repo = tmp_path / "workspace"
    repo.mkdir()
    command(["git", "init", "-q", str(repo)])
    (repo / "f").write_text("past\n")
    commit(repo)
    past = command(["git", "rev-parse", "HEAD"], cwd=repo).decode().strip()
    command(["git", "tag", "v1"], cwd=repo)
    (repo / "f").write_text("the fix\n")
    commit(repo)
    future = command(["git", "rev-parse", "HEAD"], cwd=repo).decode().strip()
    command(["git", "tag", "v2"], cwd=repo)
    command(["git", "remote", "add", "origin", "https://example.invalid/r"], cwd=repo)
    command(["git", "checkout", "-q", "--detach", past], cwd=repo)
    script = PRUNE_HISTORY.replace("cd /workspace", f"cd {repo}")
    subprocess.run(["bash", "-c", script], check=True, capture_output=True)
    assert command(["git", "tag"], cwd=repo).decode().split() == ["v1"]
    assert command(["git", "remote"], cwd=repo) == b""
    missing = subprocess.run(["git", "cat-file", "-e", future], cwd=repo, capture_output=True)
    assert missing.returncode != 0


def test_warm_up_keeps_only_tool_environments(tmp_path):
    from ci_repair.reconstruct import WARM_UP

    (tmp_path / "setup-created").write_text("kept\n")
    script = WARM_UP.format(commands="mkdir .tox .cov && touch report.xml pkg.egg-info").replace(
        "cd /workspace", f"cd {tmp_path}"
    )
    subprocess.run(["bash", "-c", script], check=True)
    assert sorted(p.name for p in tmp_path.iterdir()) == [".tox", "pkg.egg-info", "setup-created"]
