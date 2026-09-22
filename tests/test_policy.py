from pathlib import Path

import pytest

from ci_repair.policy import Policy, PolicyError, Verdict, categorize, load_policy

PATCH = b"--- a/src/a.py\n+++ b/src/a.py\n-x = 1\n+x = 2\n"


def test_example_policy_is_valid():
    policy = load_policy(Path(__file__).parents[1] / "config/policy.example.yaml")
    assert policy.data["publication"]["draft_pr"] == "ALLOW"
    assert len(policy.digest) == 64


@pytest.mark.parametrize(
    "data,match",
    [
        ({"sandbox": {"repair_network": "ALLOW"}}, "repair_network"),
        ({"sandbox": {"secrets": "ALLOW"}}, "secrets"),
        ({"publication": {"auto_merge": "ALLOW"}}, "auto_merge"),
        ({"budget": {"max_cost_usd": float("inf")}}, "finite"),
        ({"budget": {"max_model_calls": 0}}, "positive"),
        ({"budget": {"max_model_calls": 2.5}}, "integer"),
        ({"budget": {"max_tokens": 1}}, "Unknown"),
        ({"patch": {"categories": {"tests": "MAYBE"}}}, "ALLOW, REVIEW"),
        ({"patch": {"allowed_paths": ["../outside"]}}, "Unsafe"),
        ({"patch": {"max_changed_files": {"review": 9, "deny": 2}}}, "exceed"),
        ({"repositories": [{"name": "a/b", "admin": True}]}, "repositories"),
        ({"version": 2}, "version"),
    ],
)
def test_invalid_or_relaxing_policies_fail_closed(data, match):
    with pytest.raises(PolicyError, match=match):
        Policy(data)


def test_policy_inside_repository_under_repair_is_refused(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "policy.yaml").write_text("version: 1\n")
    with pytest.raises(PolicyError, match="repository under repair"):
        load_policy(repo / "policy.yaml", untrusted_roots=[repo])
    with pytest.raises(PolicyError, match="Duplicate|Invalid"):
        (tmp_path / "dup.yaml").write_text("version: 1\nversion: 1\n")
        load_policy(tmp_path / "dup.yaml")


@pytest.mark.parametrize(
    "path,categories",
    [
        ("src/app.py", []),
        (".github/workflows/ci.yml", ["workflows"]),
        ("uv.lock", ["lockfiles"]),
        ("web/package-lock.json", ["lockfiles"]),
        ("pyproject.toml", ["dependency_manifests"]),
        ("tests/test_app.py", ["tests"]),
        ("src/pkg/app_test.go", ["tests"]),
        ("src/api_pb2.py", ["generated"]),
        ("tox.ini", ["config"]),
    ],
)
def test_categories(path, categories):
    assert categorize(path) == categories


@pytest.mark.parametrize(
    "paths,verdict",
    [
        (["src/a.py"], Verdict.ALLOW),
        (["tests/test_a.py"], Verdict.REVIEW),
        (["uv.lock"], Verdict.REVIEW),
        ([".github/workflows/ci.yml"], Verdict.DENY),
        ([".ci-repair/policy.yaml"], Verdict.DENY),
        ([".git/config"], Verdict.DENY),
        (["../escape.py"], Verdict.DENY),
        (["src/a.py", ".github/workflows/ci.yml"], Verdict.DENY),
    ],
)
def test_patch_decisions(paths, verdict):
    assert Policy().check_patch(paths, PATCH, (".",)).verdict is verdict


def test_allowed_paths_and_size_limits():
    policy = Policy({"patch": {"max_changed_lines": {"review": 1, "deny": 3}}})
    assert policy.check_patch(["lib/a.py"], PATCH, ("src/",)).verdict is Verdict.DENY
    assert policy.check_patch(["src/a.py"], PATCH, ("src/",)).verdict is Verdict.REVIEW
    big = PATCH + b"+y\n+z\n"
    decision = policy.check_patch(["src/a.py"], big, ("src/",))
    assert decision.verdict is Verdict.DENY
    assert "max_changed_lines: 4" in decision.reasons
    assert policy.check_patch([], b"", ("src/",)).verdict is Verdict.DENY
    binary = b"diff --git a/src/x b/src/x\nGIT binary patch\n"
    assert policy.check_patch(["src/x"], binary, ("src/",)).verdict is Verdict.REVIEW


def test_budget_requested_policy_effective():
    policy = Policy({"budget": {"max_model_calls": 10, "max_cost_usd": 0.5}})
    budget = policy.budget({"steps": 30, "cost": 0.2, "wall_seconds": None})
    assert budget["effective"]["steps"] == 10
    assert budget["effective"]["cost"] == 0.2
    assert budget["effective"]["wall_seconds"] == 600
    assert budget["clamped"] == ["steps"]
    assert budget["requested"]["steps"] == 30


def test_trigger_and_model_admission():
    policy = Policy(
        {
            "repositories": [{"name": "Owner/Repo", "branches": ["main", "release/*"]}],
            "models": {"allowed": ["deepseek/*"]},
        }
    )
    ok = dict(repository="owner/repo", event="push", branch="release/1")
    assert policy.check_trigger(**ok).allowed
    assert policy.check_trigger(**{**ok, "branch": "feature"}).verdict is Verdict.DENY
    assert policy.check_trigger(**{**ok, "repository": "x/y"}).verdict is Verdict.DENY
    assert policy.check_trigger(**{**ok, "event": "pull_request_target"}).verdict is Verdict.DENY
    assert policy.check_trigger(**{**ok, "event": "schedule"}).verdict is Verdict.DENY
    assert policy.check_trigger(**ok, fork=True).verdict is Verdict.DENY
    assert policy.check_model("deepseek/deepseek-flash").allowed
    assert not policy.check_model("openai/gpt").allowed
