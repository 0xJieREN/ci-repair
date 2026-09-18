"""Evaluator controls verify the metric, not an LLM's capabilities."""

import os
from pathlib import Path

import pytest

from ci_repair.evaluate import control_model, evaluate_case, load_case, summarize

CASES = Path(__file__).parents[1] / "benchmarks/cases"


def test_oracle_errors_are_not_counted_as_safe_accepts():
    scores = [
        dict(
            accepted=True,
            gate_status="PASS",
            oracle_status="ERROR",
            repair_success=False,
            false_pass=False,
        ),
        dict(
            accepted=True,
            gate_status="PASS",
            oracle_status="FAIL",
            repair_success=False,
            false_pass=True,
        ),
    ]
    summary = summarize(scores)
    assert summary["errors"] == 1
    assert summary["false_passes"] == 1
    assert summary["false_pass_rate_among_evaluated_accepts"] == 1


def test_case_ids_cannot_escape_output(tmp_path):
    case = tmp_path / "case.json"
    case.write_text('{"schema_version": 1, "id": "../escape"}')
    with pytest.raises(ValueError, match="Invalid"):
        load_case(case)


@pytest.mark.docker
@pytest.mark.skipif(os.getenv("CI_REPAIR_DOCKER_TESTS") != "1", reason="Docker opt-in required")
@pytest.mark.parametrize(
    "case_path,candidate,gate,oracle",
    [
        *[(p, "correct", "PASS", "PASS") for p in sorted(CASES.glob("*.json"))],
        (CASES / "inclusive-range.json", "overfit", "PASS", "FAIL"),
        (CASES / "inclusive-range.json", "noop", "NO_PATCH", "NOT_RUN"),
    ],
)
def test_evaluation_controls(tmp_path, case_path, candidate, gate, oracle):
    case = load_case(case_path)
    score = evaluate_case(
        case, tmp_path / "case", "ci-repair-demo:local", control_model(case, candidate)
    )
    assert score["gate_status"] == gate
    assert score["oracle_status"] == oracle
    assert score["repair_success"] is (candidate == "correct")
    assert score["false_pass"] is (candidate == "overfit")
    assert not (tmp_path / "case/repo/oracle.py").exists()
    trajectory = (tmp_path / "case/repair/trajectory.json").read_text()
    assert "assert total(-2, 2)" not in trajectory
