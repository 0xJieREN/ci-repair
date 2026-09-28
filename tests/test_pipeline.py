import json
import subprocess
import threading
import time
from contextlib import contextmanager

import pytest

from ci_repair import pipeline
from ci_repair.pipeline import Config, build_context, paths_allowed, run
from ci_repair.workspace import snapshot


class FakeAgent:
    n_calls, cost, steps_executed, submissions, rejected_submissions = 0, 0, 0, 0, 0
    command_seconds = 0.0

    def __init__(self, *args, **kwargs):
        pass

    def run(self, task):
        return {"exit_status": "SUBMITTED"}

    def exit_status(self):
        return "SUBMITTED"

    def models_used(self):
        return []


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


class PatchedEnv:
    """Commands run on the original source until a verifier applies a patch."""

    patched = False

    def copy(self, *args):
        pass

    def cleanup(self):
        pass

    def checked(self, script):
        self.patched = self.patched or "git apply" in script
        return self.raw(script)

    def raw(self, script):
        if "--name-only" in script:
            return "src/a.py\0"
        if "--raw" in script:
            return ":100644 100644 a b M\tsrc/a.py"
        return ""


@pytest.mark.parametrize(
    "baseline,patch,paths,verify,status,phases",
    [
        (0, b"patch", "src/a.py\0", 0, "BASELINE_NOT_REPRODUCED", 2),
        (-1, b"patch", "src/a.py\0", 0, "BASELINE_NOT_REPRODUCED", 2),
        (124, b"patch", "src/a.py\0", 0, "BASELINE_NOT_REPRODUCED", 2),
        (137, b"patch", "src/a.py\0", 0, "BASELINE_NOT_REPRODUCED", 2),
        (1, b"", "src/a.py\0", 0, "NO_PATCH", 2),
        (1, b"patch", "tests/test.py\0", 0, "PATCH_REJECTED", 2),
        (1, b"patch", "src/a.py\0", 1, "FAIL", 3),
        (1, b"patch", "src/a.py\0", 0, "PASS", 4),
    ],
)
def test_orchestration(tmp_path, monkeypatch, baseline, patch, paths, verify, status, phases):
    cfg = config(tmp_path)
    opened = []
    closed = []

    class Env(PatchedEnv):
        def execute(self, action):
            code = verify if self.patched else baseline
            return {"returncode": code, "output": "test output", "exception_info": ""}

        def checked(self, script):
            super().checked(script)
            return paths if "--name-only" in script else self.raw(script)

    @contextmanager
    def factory(*args):
        env = Env()
        opened.append(env)
        try:
            yield env
        finally:
            closed.append(env)

    class Agent(FakeAgent):
        n_calls = 1
        cost = 0.01

    monkeypatch.setattr(pipeline, "workspace", factory)
    monkeypatch.setattr(pipeline, "snapshot", lambda *args: "abc")
    monkeypatch.setattr(pipeline, "command", lambda *args: b"sha256:image")
    monkeypatch.setattr(pipeline, "extract_patch", lambda *args: patch)
    monkeypatch.setattr(pipeline, "RepairAgent", Agent)
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


def test_zero_exit_with_verifier_exception_cannot_pass(tmp_path, monkeypatch):
    cfg = config(tmp_path)
    phases = []

    class Env(PatchedEnv):
        def execute(self, action):
            if not self.patched:
                return {"returncode": 1, "output": "failure", "exception_info": ""}
            return {
                "returncode": 0,
                "output": "ok",
                "exception_info": "transport failure" if "test-all" in action["command"] else "",
            }

    @contextmanager
    def factory(*args):
        phases.append(True)
        yield Env()

    Agent = FakeAgent

    monkeypatch.setattr(pipeline, "workspace", factory)
    monkeypatch.setattr(pipeline, "snapshot", lambda *args: "abc")
    monkeypatch.setattr(pipeline, "command", lambda *args: b"image")
    monkeypatch.setattr(pipeline, "extract_patch", lambda *args: b"patch")
    monkeypatch.setattr(pipeline, "RepairAgent", Agent)
    report = run(cfg, object())
    assert report["status"] == "FAIL"
    assert report["verified"] is False


@pytest.mark.parametrize("cost", [float("nan"), float("inf"), 0, -1])
def test_cost_budget_must_be_finite_and_positive(tmp_path, cost):
    from dataclasses import replace

    with pytest.raises(ValueError, match="finite"):
        replace(config(tmp_path), cost=cost).validate()


def test_report_records_requested_policy_and_effective_budget(tmp_path, monkeypatch):
    from dataclasses import replace

    from ci_repair.policy import Policy

    cfg = replace(config(tmp_path), steps=99, cost=5.0)

    def expire(*args):
        raise pipeline.RunDeadline()

    monkeypatch.setattr(pipeline, "snapshot", expire)

    class Model:
        class config:
            model_name = "deepseek/x"

    report = run(cfg, Model(), Policy({"budget": {"max_model_calls": 7}}))
    assert report["budget"]["requested"]["steps"] == 99
    assert report["budget"]["effective"]["steps"] == 7
    assert report["budget"]["effective"]["cost"] == 1.0
    assert set(report["budget"]["clamped"]) == {"steps", "cost"}
    assert report["config"]["steps"] == 7
    assert report["stop_reason"] == "WALL_TIME_LIMIT"
    assert report["usage"]["model_requested"] == "deepseek/x"


def test_model_outside_policy_is_denied_before_any_work(tmp_path, monkeypatch):
    from ci_repair.policy import Policy

    monkeypatch.setattr(pipeline, "snapshot", lambda *args: pytest.fail("no work"))

    class Model:
        class config:
            model_name = "openai/gpt"

    report = run(config(tmp_path), Model(), Policy({"models": {"allowed": ["deepseek/*"]}}))
    assert report["status"] == "POLICY_DENIED"
    assert report["stop_reason"] == "POLICY_DENIED"


@pytest.mark.parametrize("regression,phases", [("test-all", 4), ("test-one", 3)])
def test_identical_checks_run_once(tmp_path, monkeypatch, regression, phases):
    from dataclasses import replace

    cfg = replace(config(tmp_path), regression_command=regression)

    class Env(PatchedEnv):
        def execute(self, action):
            code = 0 if self.patched else 1
            return {"returncode": code, "output": "out", "exception_info": ""}

    opened = []

    @contextmanager
    def factory(*args):
        opened.append(True)
        yield Env()

    monkeypatch.setattr(pipeline, "workspace", factory)
    monkeypatch.setattr(pipeline, "snapshot", lambda *args: "abc")
    monkeypatch.setattr(pipeline, "command", lambda *args: b"sha256:image")
    monkeypatch.setattr(pipeline, "extract_patch", lambda *args: b"patch")
    monkeypatch.setattr(pipeline, "RepairAgent", FakeAgent)
    report = run(cfg, object())
    assert report["status"] == "PASS"
    assert len(opened) == phases
    # Evidence keeps one record per check; an identical regression check is marked, not rerun.
    assert [t["command"] for t in report["tests"]] == ["test-one", regression]
    assert bool(report["tests"][1].get("same_as")) == (regression == "test-one")
    assert (cfg.output / "regression.json").exists()
    assert json.loads((cfg.output / "baseline.json").read_text())["returncode"] == 1


def concurrent_fixture(tmp_path, monkeypatch, agent_run):
    """The baseline command blocks until released; the agent decides when that happens."""
    release, killed = threading.Event(), threading.Event()

    class Env(PatchedEnv):
        def execute(self, action):
            deadline = time.monotonic() + 10
            while not self.patched and not release.is_set() and not killed.is_set():
                assert time.monotonic() < deadline
                time.sleep(0.01)
            code = 137 if killed.is_set() else 0 if self.patched else 1
            return {"returncode": code, "output": "out", "exception_info": ""}

        def cleanup(self):
            killed.set()

    @contextmanager
    def factory(*args):
        yield Env()

    class Agent(FakeAgent):
        def run(self, task):
            return agent_run(release)

    monkeypatch.setattr(pipeline, "workspace", factory)
    monkeypatch.setattr(pipeline, "snapshot", lambda *args: "abc")
    monkeypatch.setattr(pipeline, "command", lambda *args: b"sha256:image")
    monkeypatch.setattr(pipeline, "extract_patch", lambda *args: b"patch")
    monkeypatch.setattr(pipeline, "RepairAgent", Agent)
    return config(tmp_path), killed


def test_agent_starts_while_the_baseline_runs(tmp_path, monkeypatch):
    def agent_run(release):
        time.sleep(0.2)  # the baseline cannot finish before the agent releases it
        release.set()
        return {"exit_status": "SUBMITTED"}

    cfg, killed = concurrent_fixture(tmp_path, monkeypatch, agent_run)
    report = run(cfg, object())
    assert report["status"] == "PASS" and not killed.is_set()
    assert json.loads((cfg.output / "baseline.json").read_text())["returncode"] == 1


def test_agent_error_stops_the_pending_baseline(tmp_path, monkeypatch):
    def agent_run(release):
        raise RuntimeError("provider failed")

    cfg, killed = concurrent_fixture(tmp_path, monkeypatch, agent_run)
    started = time.monotonic()
    report = run(cfg, object())
    assert report["status"] == "ERROR" and report["error_phase"] == "agent"
    assert killed.is_set() and time.monotonic() - started < 5


def test_baseline_failure_during_the_agent_is_attributed_to_the_baseline(tmp_path, monkeypatch):
    @contextmanager
    def factory(*args):
        if threading.current_thread().name.startswith("baseline"):
            raise RuntimeError("container did not start")
        yield PatchedEnv()

    class Agent(FakeAgent):
        def __init__(self, *args, gate, **kwargs):
            self.gate = gate

        def run(self, task):
            time.sleep(0.1)
            return {"exit_status": self.gate("submit").exit}

    monkeypatch.setattr(pipeline, "workspace", factory)
    monkeypatch.setattr(pipeline, "snapshot", lambda *args: "abc")
    monkeypatch.setattr(pipeline, "command", lambda *args: b"sha256:image")
    monkeypatch.setattr(pipeline, "extract_patch", lambda *args, **kwargs: b"patch")
    monkeypatch.setattr(pipeline, "RepairAgent", Agent)
    report = run(config(tmp_path), object())
    assert report["status"] == "ERROR" and report["error_phase"] == "baseline"
