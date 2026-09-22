"""LCA replay must fail closed on environment drift and mismatched evidence."""

import json
import sys
from types import SimpleNamespace

import pytest

from ci_repair.lca import RECIPES, expected_failure, grade_candidate, main


def test_lca_requires_all_original_diagnostics():
    messages = RECIPES[107]["diagnostics"]
    assert expected_failure({"returncode": 1, "output": "\n".join(messages)}, messages)
    assert not expected_failure({"returncode": 1, "output": messages[0]}, messages)
    assert not expected_failure({"returncode": 1, "output": "ModuleNotFoundError"}, messages)
    assert not expected_failure({"returncode": 0, "output": "\n".join(messages)}, messages)
    assert not expected_failure(
        {"returncode": 1, "output": "\n".join(messages), "exception_info": "timeout"}, messages
    )


def test_lca_checks_paid_budget_before_downloading(tmp_path, monkeypatch):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "lca",
            "--output",
            str(tmp_path / "out"),
            "--model",
            "fake",
            "--steps",
            "15",
            "--max-total-calls",
            "59",
        ],
    )
    with pytest.raises(SystemExit):
        main()
    assert not (tmp_path / "out").exists()


def test_lca_rejects_patch_provenance_mismatch_before_grading(tmp_path, monkeypatch):
    (tmp_path / "repair").mkdir()
    (tmp_path / "repair/patch.diff").write_text("candidate patch")
    (tmp_path / "admission.json").write_text(json.dumps({"commit": "base"}))
    cfg = SimpleNamespace(output=tmp_path / "repair", repo=tmp_path / "repo", image="image")
    monkeypatch.setattr(
        "ci_repair.lca.verify_patch", lambda *a: pytest.fail("Untrusted evidence graded")
    )
    result = grade_candidate(
        cfg, {"status": "CANDIDATE", "commit": "different"}, tmp_path / "grade"
    )
    assert result == {"status": "ERROR", "verified": False}


def test_lca_summary_does_not_invent_full_ci_or_hidden_oracle_scores(tmp_path):
    from ci_repair.lca import save_scores

    save_scores(
        tmp_path,
        [{"selected_check_success": True, "model_calls": 2, "estimated_cost_usd": 0.01}],
        4,
    )
    summary = json.loads((tmp_path / "summary.json").read_text())
    assert summary["complete"] is False
    assert summary["selected_check_passes"] == 1
    assert summary["official_full_ci_success"] is None
    assert summary["false_pass_rate"] is None
