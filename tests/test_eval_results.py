"""The evaluation record against its archived evidence, and the scoring rule behind it.

eval/results/ holds what the hosted runs recorded; these tests recompute the record's
tables from it, so a number in eval/README.md cannot drift from the data.
"""

import sys
from pathlib import Path

import pytest

EVAL = Path(__file__).parents[1] / "eval"
sys.path.insert(0, str(EVAL))

import compare  # noqa: E402
import reproduce  # noqa: E402
import trajectories  # noqa: E402
from audit import signals  # noqa: E402


@pytest.fixture(scope="module")
def output():
    return reproduce.reproduce()


def test_every_table_in_the_record_matches_the_archive(output):
    assert reproduce.missing_rows(output, reproduce.README.read_text()) == []


def test_the_archived_recomputation_is_current(output):
    # Regenerate with: uv run python eval/reproduce.py > eval/results/reproduced.md
    assert output == (reproduce.RESULTS / "reproduced.md").read_text()


def test_the_check_reports_a_row_the_record_does_not_show():
    assert reproduce.missing_rows("| a | 1 |\n| b | 2 |\ntext\n", "| a | 1 |\n") == ["| b | 2 |"]


def test_p90_is_the_nearest_rank():
    assert reproduce.p90(list(range(1, 11))) == 9
    assert reproduce.p90([5.0]) == 5.0


@pytest.fixture
def grade(tmp_path, monkeypatch):
    """compare.grade with each job's fresh-container verdict scripted."""

    trials = iter(range(100))

    def run(patch, statuses, changed, environments=("SUPPORTED",)):
        trial = tmp_path / str(next(trials))  # grade creates each job's directory afresh
        trial.mkdir()
        verdicts = iter(statuses)
        monkeypatch.setattr(
            compare,
            "verify_patch",
            lambda *args: {"status": next(verdicts), "changed_files": changed},
        )
        jobs = [
            {"job_id": i, "job": f"job{i}", "image": "img", "failing": "f", "regression": "r",
             "environment": environments[i % len(environments)]}
            for i in range(len(statuses))
        ]  # fmt: skip
        return compare.grade({"jobs": jobs, "archive": "source.tar"}, patch, trial)

    return run


PATCH = b"diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n@@ -1 +1 @@\n-x\n+y\n"


def test_a_pass_needs_every_job_checked_in_a_fresh_container(grade):
    result = grade(PATCH, ["PASS", "PASS"], ["src/a.py"])
    assert result["passed"] and result["auto_publishable"] and result["patch_policy"] == "ALLOW"
    assert result["changed_lines"] == 2 and not result["touches_tests"]
    assert not grade(PATCH, ["PASS", "FAIL"], ["src/a.py"])["passed"]


def test_no_patch_is_not_scored_as_a_pass(grade):
    assert grade(b"", [], []) == {"passed": False, "reason": "no patch"}


def test_policy_review_passes_but_is_not_auto_publishable(grade):
    result = grade(PATCH, ["PASS"], ["tests/test_a.py"])
    assert result["passed"] and result["patch_policy"] == "REVIEW"
    assert result["touches_tests"] and not result["auto_publishable"]
    unsupported = grade(PATCH, ["PASS"], ["src/a.py"], environments=("REVIEW_REQUIRED",))
    assert unsupported["passed"] and not unsupported["auto_publishable"]


def test_a_denied_patch_fails_even_when_the_checks_pass(grade):
    result = grade(PATCH, ["PASS"], [".github/workflows/ci.yml"])
    assert result["checks_passed"] and not result["passed"] and result["patch_policy"] == "DENY"


def test_audit_signals_count_added_suppressions_and_net_assertions():
    diff = "+++ b/t.py\n+    assert x  # noqa\n-    assert y\n-    assert z\n--- a/t.py\n"
    assert signals(diff) == (1, -1)


def test_session_commands_come_from_tool_calls():
    trajectory = {
        "messages": [
            {"tool_calls": [{"function": {"arguments": '{"command": "git reflog"}'}}]},
            {"tool_calls": [{"function": {"arguments": "not json"}}]},
            {"content": "no call"},
        ]
    }
    assert trajectories.commands(trajectory) == ["git reflog", "not json"]
