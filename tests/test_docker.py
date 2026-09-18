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


class ScriptedModel:
    def __init__(self, script):
        self.commands = iter([script, "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"])

    def query(self, messages):
        return {
            "role": "assistant",
            "content": "scripted test action",
            "extra": {"actions": [{"command": next(self.commands)}], "cost": 0.01},
        }

    def format_message(self, **kwargs):
        return kwargs

    def format_observation_messages(self, message, outputs, template_vars=None):
        return [{"role": "user", "content": str(outputs)}]

    def get_template_vars(self):
        return {}

    def serialize(self):
        return {"info": {"test_model": "scripted, not a real model repair"}}


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
    report = run(cfg, ScriptedModel(script))
    assert report["status"] == expected, report
    assert (
        report["patch_sha256"]
        == hashlib.sha256((cfg.output / "patch.diff").read_bytes()).hexdigest()
    )
    assert "range(start, end)" in (repo / "src/ranges.py").read_text()
    assert (cfg.output / "trajectory.json").exists()
