"""Real mini agent loop and deterministic model; host commands are harmless echoes."""

import json
from pathlib import Path

import pytest
from minisweagent.environments.local import LocalEnvironment
from minisweagent.models import get_model
from minisweagent.models.test_models import make_output

from ci_repair import pipeline
from ci_repair.agent import AgentExit, GateResult, RepairAgent, StopReason, final_stop_reason
from ci_repair.pipeline import Config, make_gate
from ci_repair.policy import Policy

SUBMIT = "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"


def model(*commands, cost=0.01):
    return get_model(
        "deterministic",
        config={
            "model_class": "deterministic",
            "cost_per_call": cost,
            "outputs": [make_output("step", [{"command": c}], cost=cost) for c in commands],
        },
    )


def agent(commands, gate, **kwargs):
    return RepairAgent(
        model(*commands, cost=kwargs.pop("cost", 0.01)),
        LocalEnvironment(),
        gate=gate,
        system_template="system",
        instance_template="{{task}}",
        **{"step_limit": 10, "cost_limit": 1.0, **kwargs},
    )


class Gate:
    def __init__(self, *, submit=(), probe=()):
        self.results = {"submit": list(submit), "probe": list(probe)}
        self.calls = []

    def __call__(self, kind):
        self.calls.append(kind)
        queue = self.results[kind]
        return queue.pop(0) if queue else GateResult(False)


def test_accepted_submission_ends_run():
    gate = Gate(submit=[GateResult(True)])
    a = agent(["echo edit", SUBMIT], gate, early_stop=False)
    a.run("task")
    assert a.exit_status() == AgentExit.SUBMITTED
    assert gate.calls == ["submit"]
    assert a.submissions == 1


def test_rejected_submission_returns_feedback_then_stops_at_limit():
    gate = Gate(submit=[GateResult(False, "verification failed: boom")])
    a = agent([SUBMIT, "echo retry", SUBMIT, "echo unused"], gate, early_stop=False)
    a.run("task")
    assert a.exit_status() == AgentExit.SUBMISSION_REJECTED
    assert a.rejected_submissions == 2
    assert any("verification failed: boom" in json.dumps(m) for m in a.messages)
    assert a.n_calls == 3


def test_verified_probe_stops_before_agent_can_continue_editing():
    gate = Gate(probe=[GateResult(False), GateResult(True)])
    a = agent(["echo explore", "echo correct-fix", "echo break-it", SUBMIT], gate)
    a.run("task")
    assert a.exit_status() == AgentExit.EARLY_STOP_VERIFIED
    assert a.n_calls == 2  # the third, patch-breaking action is never requested
    assert a.steps_executed == 2


@pytest.mark.parametrize(
    "kwargs,expected",
    [
        ({"step_limit": 2}, AgentExit.STEP_LIMIT),
        ({"cost_limit": 0.015}, AgentExit.COST_LIMIT),
    ],
)
def test_limits_are_distinguished(kwargs, expected):
    a = agent(["echo a", "echo b", "echo c", SUBMIT], Gate(), early_stop=False, **kwargs)
    a.run("task")
    assert a.exit_status() == expected


@pytest.mark.parametrize(
    "verified,denied,patch,exit_status,expected",
    [
        (True, False, True, "STEP_LIMIT", StopReason.VERIFIED_PASS),
        (False, True, True, "SUBMITTED", StopReason.POLICY_DENIED),
        (False, False, True, "STEP_LIMIT", StopReason.STEP_LIMIT),
        (False, False, False, "COST_LIMIT", StopReason.COST_LIMIT),
        (False, False, False, "SUBMITTED_NO_PATCH", StopReason.NO_PATCH),
        (False, False, True, "SUBMITTED", StopReason.VERIFICATION_FAILED),
        (False, False, True, "SUBMISSION_REJECTED", StopReason.VERIFICATION_FAILED),
        (False, False, True, None, StopReason.VERIFICATION_FAILED),
    ],
)
def test_final_stop_reason(verified, denied, patch, exit_status, expected):
    result = final_stop_reason(
        verified=verified, policy_denied=denied, patch=patch, agent_exit=exit_status
    )
    assert result is expected


class PatchEnv:
    def __init__(self):
        self.files = "src/a.py\0"

    def checked(self, script):
        return self.files


def gate_fixture(tmp_path, monkeypatch, patches, verdicts, policy=None):
    cfg = Config(tmp_path, tmp_path / "log", tmp_path / "out", "img", "one", "all")
    cfg.output.mkdir()
    patches = list(patches)
    verified = []
    monkeypatch.setattr(pipeline, "extract_patch", lambda *a, **k: patches.pop(0))

    def verify(config, archive, image, patch_path):
        verified.append(patch_path.read_bytes())
        ok = verdicts.pop(0)
        return {
            "verified": ok,
            "status": "PASS" if ok else "FAIL",
            "changed_files": ["src/a.py"],
            "tests": [
                {
                    "command": "one",
                    "returncode": 0 if ok else 1,
                    "output": "E: bad",
                    "duration_seconds": 1,
                }
            ]
            + (
                [{"command": "all", "returncode": 0, "output": "", "duration_seconds": 1}]
                if ok
                else []
            ),
        }

    monkeypatch.setattr(pipeline, "verify_patch", verify)
    state = {"gates": {}, "probes": 0, "command_seconds": 0.0}
    env = PatchEnv()
    gate = make_gate(cfg, policy or Policy(), Path("a.tar"), "img", env, state)
    return gate, verified, state, env


def test_gate_caches_by_digest_and_explains_failures(tmp_path, monkeypatch):
    gate, verified, state, _ = gate_fixture(
        tmp_path, monkeypatch, [b"+a\n", b"+a\n", b"+b\n"], [False, True]
    )
    assert not gate("probe").accept
    rejected = gate("submit")  # same digest: cached, no second verifier run
    assert not rejected.accept
    assert "returncode=1" in rejected.feedback and "E: bad" in rejected.feedback
    assert gate("probe").accept is True
    assert verified == [b"+a\n", b"+b\n"]
    assert state["probes"] == 2


def test_gate_rejects_policy_denied_patch_without_verifier(tmp_path, monkeypatch):
    gate, verified, state, env = gate_fixture(tmp_path, monkeypatch, [b"+x\n"], [])
    env.files = ".github/workflows/ci.yml\0"
    result = gate("submit")
    assert not result.accept
    assert "workflows" in result.feedback
    assert verified == []


def test_gate_empty_patch_submission_ends_without_verification(tmp_path, monkeypatch):
    gate, verified, _, _ = gate_fixture(tmp_path, monkeypatch, [b"", b""], [])
    assert gate("probe").accept is False
    result = gate("submit")
    assert result.accept and result.exit is AgentExit.SUBMITTED_NO_PATCH
    assert verified == []


def test_probe_budget_is_capped(tmp_path, monkeypatch):
    policy = Policy({"repair": {"max_probes": 1}})
    gate, verified, _, _ = gate_fixture(
        tmp_path, monkeypatch, [b"+a\n", b"+b\n", b"+b\n"], [False, True], policy
    )
    gate("probe")
    assert gate("probe").accept is False  # cap reached, no verifier run
    assert gate("submit").accept is True  # explicit submissions are always checked
    assert len(verified) == 2
