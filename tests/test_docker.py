"""Real Docker + real mini agent loop, scripted model; no paid API requests."""

import hashlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from ci_repair.pipeline import Config, run

pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(
        os.getenv("CI_REPAIR_DOCKER_TESTS") != "1", reason="set CI_REPAIR_DOCKER_TESTS=1"
    ),
]


def scripted_model(*scripts):
    from minisweagent.models import get_model
    from minisweagent.models.test_models import make_output

    return get_model(
        "deterministic",
        config={
            "model_class": "deterministic",
            "cost_per_call": 0.01,
            "outputs": [
                make_output("scripted test action", [{"command": command}], cost=0.01)
                # A rejected first submission gets feedback; the second ends the attempt.
                for command in (*scripts, *["echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"] * 2)
            ],
        },
    )


def fixture_repo(tmp_path):
    repo = tmp_path / "target"
    shutil.copytree(
        Path(__file__).parents[1] / "examples/buggy",
        repo,
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    (repo / ".gitignore").write_text("__pycache__/\n")
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-m",
            "fixture",
        ],
        check=True,
        capture_output=True,
    )
    return repo


@pytest.mark.parametrize(
    "script,expected,marker_expectation",
    [
        ("sed -i 's/range(start, end)/range(start, end + 1)/' src/ranges.py", "PASS", None),
        ("sed -i 's/, 14)/, 9)/' tests/test_ranges.py", "PATCH_REJECTED", None),
        ("true", "NO_PATCH", None),
        ("sed -i 's/range(start, end)/range(start, end + 1)/' src/ranges.py", "PASS", "! -e"),
        ("sed -i 's/range(start, end)/range(start, end + 1)/' src/ranges.py", "FAIL", "-e"),
    ],
)
def test_fresh_verifier(tmp_path, script, expected, marker_expectation):
    repo = fixture_repo(tmp_path)
    log = tmp_path / "failure.log"
    log.write_text("AssertionError: 9 != 14")
    cfg = Config(
        repo,
        log,
        tmp_path / "run",
        "ci-repair-demo:local",
        "python -m unittest discover -s tests -k test_positive_interval",
        "python -m unittest discover -s tests",
    )
    if marker_expectation:
        from dataclasses import replace

        # State outside the checkout also must not leak between verifier commands.
        cfg = replace(
            cfg,
            failing_command=cfg.failing_command + " && touch /tmp/verifier-marker",
            regression_command=f"test {marker_expectation} /tmp/verifier-marker && "
            + cfg.regression_command,
        )
    report = run(cfg, scripted_model(script))
    assert report["status"] == expected, report
    assert (
        report["patch_sha256"]
        == hashlib.sha256((cfg.output / "patch.diff").read_bytes()).hexdigest()
    )
    if expected == "PASS":
        # The system stops after the first verified step; the submit output is never needed.
        assert report["agent_exit"] == "EARLY_STOP_VERIFIED"
        assert report["usage"]["model_calls"] == 1
        assert report["verification_source"].startswith("probe")
    expected_stop = {
        "PASS": "VERIFIED_PASS",
        "PATCH_REJECTED": "POLICY_DENIED",
        "NO_PATCH": "NO_PATCH",
        "FAIL": "VERIFICATION_FAILED",
    }
    assert report["stop_reason"] == expected_stop[expected]
    assert "range(start, end)" in (repo / "src/ranges.py").read_text()
    assert (cfg.output / "trajectory.json").exists()


def test_probes_leave_the_agents_git_view_unchanged(tmp_path):
    repo = fixture_repo(tmp_path)
    log = tmp_path / "failure.log"
    log.write_text("AssertionError: 9 != 14")
    cfg = Config(
        repo,
        log,
        tmp_path / "run",
        "ci-repair-demo:local",
        "python -m unittest discover -s tests -k test_positive_interval",
        "python -m unittest discover -s tests",
    )
    model = scripted_model(
        "printf '# touched\\n' >> src/ranges.py",  # still failing: probe runs and fails
        # Only fixes the bug if the earlier edit is still visible as an unstaged change.
        "git diff --name-only | grep -qx src/ranges.py && "
        "sed -i 's/range(start, end)/range(start, end + 1)/' src/ranges.py",
    )
    report = run(cfg, model)
    assert report["status"] == "PASS", report
    assert report["usage"]["verification_probes"] == 2
    assert report["agent_exit"] == "EARLY_STOP_VERIFIED"


def test_command_timeout_terminates_container_process(tmp_path):
    from ci_repair.workspace import Sandbox

    env = Sandbox(
        image="ci-repair-demo:local",
        cwd="/workspace",
        timeout=1,
        run_args=["--rm", "--network=none"],
    )
    try:
        result = env.execute({"command": "sleep 2; touch /tmp/should-not-exist"})
        assert result["returncode"] == 124
        assert result["exception_info"]
        # If only docker exec's client died, the delayed write would still happen.
        check = env.execute({"command": "sleep 2; test ! -e /tmp/should-not-exist"}, timeout=4)
        assert check["returncode"] == 0
    finally:
        env.cleanup()
