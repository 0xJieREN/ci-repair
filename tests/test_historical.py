"""Historical inputs must remain pinned and reproduce the intended defect."""

from contextlib import nullcontext
from pathlib import Path

import pytest

from ci_repair.evaluate import control_model, load_case
from ci_repair.historical import prepare_historical, validate_historical

CASES = Path(__file__).parents[1] / "benchmarks/historical"


@pytest.mark.parametrize("path", sorted(CASES.glob("*.json")))
def test_historical_manifests_are_valid(path):
    case = load_case(path)
    assert case["schema_version"] == 2
    with pytest.raises(ValueError, match="correct and noop"):
        control_model(case, "overfit")


@pytest.mark.parametrize(
    "field,value",
    [
        ("base_commit", "main"),
        ("fix_commit", "HEAD"),
        ("repository", "file:///tmp/repo"),
        ("allowed_paths", ["../"]),
        ("reference_patch", "tampered"),
    ],
)
def test_historical_manifest_rejects_unpinned_or_tampered_inputs(field, value):
    case = load_case(next(CASES.glob("*.json")))
    case[field] = value
    with pytest.raises(ValueError):
        validate_historical(case)


def test_upstream_reference_mismatch_rejected_before_docker(tmp_path, monkeypatch):
    case = load_case(next(CASES.glob("*.json")))
    monkeypatch.setattr("ci_repair.historical.command", lambda *a, **kw: b"wrong upstream diff")
    monkeypatch.setattr("ci_repair.historical.workspace", lambda *a: pytest.fail("Docker started"))
    with pytest.raises(ValueError, match="pinned upstream"):
        prepare_historical(case, tmp_path / "fixture", "image", 60)


@pytest.mark.parametrize(
    "result",
    [
        {"returncode": 1, "output": "ModuleNotFoundError: missing dependency"},
        {"returncode": 124, "output": "timeout"},
        {"returncode": 0, "output": "passed"},
    ],
)
def test_environment_failures_are_not_admitted_as_historical_bugs(tmp_path, monkeypatch, result):
    case = load_case(next(CASES.glob("*.json")))
    monkeypatch.setattr(
        "ci_repair.historical.command", lambda *a, **kw: case["reference_patch"].encode()
    )
    monkeypatch.setattr("ci_repair.historical.snapshot", lambda *a: case["base_commit"])
    monkeypatch.setattr("ci_repair.historical.workspace", lambda *a: nullcontext(None))
    monkeypatch.setattr("ci_repair.historical.run_test", lambda *a: result)
    with pytest.raises(ValueError, match="failure not reproduced"):
        prepare_historical(case, tmp_path / "fixture", "image", 60)
