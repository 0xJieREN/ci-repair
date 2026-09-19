"""Evaluator controls verify the metric, not an LLM's capabilities."""

import json
import os
import sys
from pathlib import Path

import pytest

from ci_repair.evaluate import (
    control_model,
    evaluate_case,
    load_case,
    main,
    paired_results,
    schedule,
    summarize,
)
from ci_repair.upstream import upstream_prompts

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
@pytest.mark.parametrize("runner", ["ci-repair", "upstream"])
def test_evaluation_controls(tmp_path, case_path, candidate, gate, oracle, runner):
    case = load_case(case_path)
    score = evaluate_case(
        case,
        tmp_path / "case",
        "ci-repair-demo:local",
        control_model(case, candidate),
        runner=runner,
    )
    assert score["gate_status"] == gate
    assert score["oracle_status"] == oracle
    assert score["repair_success"] is (candidate == "correct")
    assert score["false_pass"] is (candidate == "overfit")
    assert not (tmp_path / "case/input/repo/oracle.py").exists()
    trajectory = (tmp_path / "case/repair/trajectory.json").read_text()
    assert "assert total(-2, 2)" not in trajectory
    if runner == "upstream":
        prompts, _ = upstream_prompts()
        assert (
            json.loads(trajectory)["info"]["config"]["agent"]["system_template"]
            == prompts["system_template"]
        )
        assert score["runner_verified"] is None
        assert not (tmp_path / "case/repair/baseline.json").exists()
        assert not (tmp_path / "case/repair/failing.json").exists()


def test_unreproduced_baseline_is_an_evaluation_error():
    score = dict(
        accepted=False,
        gate_status="BASELINE_NOT_REPRODUCED",
        oracle_status="NOT_RUN",
        repair_success=False,
        false_pass=False,
    )
    assert summarize([score])["errors"] == 1


def test_schedule_pairs_and_alternates_runner_order():
    trials = schedule([{"id": "a"}, {"id": "b"}], ["ci-repair", "upstream"], 3)
    assert len(trials) == 12
    assert [s["runner"] for s in trials[:2]] == ["ci-repair", "upstream"]
    assert [s["runner"] for s in trials[4:6]] == ["upstream", "ci-repair"]
    assert len({(s["case"], s["runner"], s["repetition"]) for s in trials}) == 12


def test_model_plan_over_budget_rejected_before_creating_artifacts(tmp_path, monkeypatch):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "evaluate",
            str(CASES),
            "--output",
            str(tmp_path / "out"),
            "--runner",
            "both",
            "--repetitions",
            "3",
            "--steps",
            "10",
            "--model",
            "fake",
            "--max-total-calls",
            "359",
        ],
    )
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2
    assert not (tmp_path / "out").exists()


def test_errors_excluded_from_valid_rate_but_visible_in_attempt_rate():
    scores = [
        dict(
            accepted=False,
            gate_status="ERROR",
            oracle_status="NOT_RUN",
            repair_success=False,
            false_pass=False,
        ),
        dict(
            accepted=True,
            gate_status="PASS",
            oracle_status="PASS",
            repair_success=True,
            false_pass=False,
        ),
    ]
    result = summarize(scores)
    assert result["valid_trials"] == 1
    assert result["success_rate"] == 1
    assert result["success_rate_all_attempts"] == 0.5
    assert result["errors"] == 1


def test_pairing_rejects_different_inputs():
    score = dict(
        case="a",
        repetition=1,
        gate_status="PASS",
        oracle_status="PASS",
        repair_success=True,
        commit="a",
        image_id="image",
        log_sha256="log",
        case_sha256="case",
    )
    pair = [{**score, "runner": "ci-repair"}, {**score, "runner": "upstream"}]
    assert paired_results(pair)["both_success"] == 1
    pair[1]["commit"] = "b"
    with pytest.raises(ValueError, match="inputs"):
        paired_results(pair)
