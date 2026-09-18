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


def scripted_model(script):
    from minisweagent.models import get_model
    from minisweagent.models.test_models import make_output

    return get_model(
        "deterministic",
        config={
            "model_class": "deterministic",
            "cost_per_call": 0.01,
            "outputs": [
                make_output("scripted test action", [{"command": command}], cost=0.01)
                for command in (script, "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT")
            ],
        },
    )


@pytest.mark.parametrize(
    "script,expected",
    [
        ("sed -i 's/range(start, end)/range(start, end + 1)/' src/ranges.py", "PASS"),
        ("sed -i 's/, 14)/, 9)/' tests/test_ranges.py", "PATCH_REJECTED"),
        ("true", "NO_PATCH"),
    ],
)
def test_fresh_verifier(tmp_path, script, expected):
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
    report = run(cfg, scripted_model(script))
    assert report["status"] == expected, report
    assert (
        report["patch_sha256"]
        == hashlib.sha256((cfg.output / "patch.diff").read_bytes()).hexdigest()
    )
    assert "range(start, end)" in (repo / "src/ranges.py").read_text()
    assert (cfg.output / "trajectory.json").exists()


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
