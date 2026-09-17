import json
import subprocess
from contextlib import contextmanager

import pytest

from ci_repair import pipeline
from ci_repair.pipeline import Config, build_context, paths_allowed, run
from ci_repair.workspace import snapshot


def config(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    log = tmp_path / "failure.log"
    log.write_text("AssertionError: expected 14, got 9")
    return Config(repo, log, tmp_path / "run", "image", "test-one", "test-all")


def test_context_is_bounded_and_does_not_template_log(tmp_path):
    result = build_context(config(tmp_path), "abc", "x" * 20000 + "{{ secret }}")
    assert len(result) < 17000
    assert "{{ secret }}" in result
    assert json.loads(result)["commit"] == "abc"


@pytest.mark.parametrize(
    "paths,expected",
    [
        (["src/a.py"], True),
        (["src2/a.py"], False),
        (["tests/test.py"], False),
        ([], False),
        (["src/../tests/test.py"], False),
        (["/src/a.py"], False),
    ],
)
def test_source_boundaries(paths, expected):
    assert paths_allowed(paths, ("src/",)) is expected


def test_snapshot_rejects_dirty_repo(tmp_path):
    cfg = config(tmp_path)
    subprocess.run(["git", "init", str(cfg.repo)], check=True, capture_output=True)
    (cfg.repo / "a").write_text("a")
    subprocess.run(["git", "-C", str(cfg.repo), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(cfg.repo),
            "-c",
            "user.name=test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-m",
            "initial",
        ],
        check=True,
        capture_output=True,
    )
    sha = snapshot(cfg.repo, tmp_path / "source.tar")
    assert len(sha) == 40
    (cfg.repo / "untracked").write_text("x")
    with pytest.raises(ValueError, match="clean"):
        snapshot(cfg.repo, tmp_path / "source.tar")


@pytest.mark.parametrize(
    "baseline,patch,paths,verify,status,phases",
    [
        (0, b"patch", "src/a.py\0", 0, "BASELINE_NOT_REPRODUCED", 1),
        (-1, b"patch", "src/a.py\0", 0, "BASELINE_NOT_REPRODUCED", 1),
        (1, b"", "src/a.py\0", 0, "NO_PATCH", 2),
        (1, b"patch", "tests/test.py\0", 0, "PATCH_REJECTED", 3),
        (1, b"patch", "src/a.py\0", 1, "FAIL", 3),
        (1, b"patch", "src/a.py\0", 0, "PASS", 3),
    ],
)
def test_orchestration(tmp_path, monkeypatch, baseline, patch, paths, verify, status, phases):
    cfg = config(tmp_path)
    opened = []
    closed = []

    class Env:
        def execute(self, action):
            code = baseline if self.number == 0 else verify
            return {"returncode": code, "output": "test output", "exception_info": ""}

        def copy(self, *args):
            pass

        def checked(self, script):
            if "--name-only" in script:
                return paths
            if "--raw" in script:
                return ":100644 100644 a b M\tsrc/a.py"
            return ""

    @contextmanager
    def factory(*args):
        env = Env()
        env.number = len(opened)
        opened.append(env)
        try:
            yield env
        finally:
            closed.append(env)

    class Agent:
        n_calls = 1
        cost = 0.01

        def __init__(self, *args, **kwargs):
            pass

        def run(self, task):
            return {"exit_status": "Submitted", "submission": "fixed"}

    monkeypatch.setattr(pipeline, "workspace", factory)
    monkeypatch.setattr(pipeline, "snapshot", lambda *args: "abc")
    monkeypatch.setattr(pipeline, "command", lambda *args: b"sha256:image")
    monkeypatch.setattr(pipeline, "extract_patch", lambda *args: patch)
    monkeypatch.setattr(pipeline, "DefaultAgent", Agent)
    report = run(cfg, object())
    assert report["status"] == status
    assert report["verified"] == (status == "PASS")
    assert len(opened) == len(closed) == phases
    assert json.loads((cfg.output / "report.json").read_text())["status"] == status


def test_rejects_run_directory_inside_input(tmp_path):
    cfg = config(tmp_path)
    from dataclasses import replace

    with pytest.raises(ValueError, match="outside"):
        replace(cfg, output=cfg.repo / "runs").validate()


def test_deadline_is_not_swallowed_and_report_is_saved(tmp_path, monkeypatch):
    cfg = config(tmp_path)

    def expire(*args):
        raise pipeline.RunDeadline()

    monkeypatch.setattr(pipeline, "snapshot", expire)
    report = run(cfg, object())
    assert report["status"] == "TIMEOUT"
    assert json.loads((cfg.output / "report.json").read_text())["verified"] is False
