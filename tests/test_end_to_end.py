"""Signed webhook -> queue -> collect -> repair -> draft PR, with every external call faked.

GitHub (REST API and gh), Docker and the model are replaced. The signature check, SQLite
queue, canonical re-check, orchestrator, policy, publication gate and Git push run for real;
the push goes to a local bare repository standing in for github.com.
"""

import json
import shutil
from functools import partial

import pytest
from test_orchestrate import collection
from test_orchestrate_flow import install, set_value
from test_webhook import SECRET, headers, payload

from ci_repair import github, orchestrate, webhook
from ci_repair import model as model_module
from ci_repair import publish as pub
from ci_repair.policy import Policy
from ci_repair.workspace import command

POLICY = {
    "repositories": [{"name": "owner/repo", "branches": ["main"], "allowed_paths": ["src/"]}],
    "models": {"allowed": ["fake/*"], "default": "fake/model"},
    "publication": {"draft_pr": "ALLOW"},
}
URL = "https://github.com/owner/repo.git"


def fake_github(monkeypatch, collected, sha):
    run = {
        "id": 7,
        "run_attempt": 1,
        "status": "completed",
        "conclusion": "failure",
        "event": "push",
        "head_branch": "main",
        "head_sha": sha,
        "head_repository": {"full_name": "owner/repo"},
    }
    responses = {
        "repos/owner/repo/actions/runs/7/attempts/1": run,
        "repos/owner/repo/actions/runs/7": run,
        "repos/owner/repo/branches/main": {"commit": {"sha": sha}},
    }
    monkeypatch.setattr(github, "api", lambda path, **kwargs: json.dumps(responses[path]))

    def collect_run(repository, run_id, output, *, attempt=None, **kwargs):
        assert (repository, run_id, attempt) == ("owner/repo", 7, 1)
        shutil.copytree(collected, output)

    monkeypatch.setattr(github, "collect_run", collect_run)


def fake_remote(monkeypatch, tmp_path, repo):
    """A bare repository as github.com; gh calls answered locally and recorded."""
    remote = tmp_path / "remote.git"
    command(["git", "init", "-q", "--bare", str(remote)])
    command(["git", "push", "-q", str(remote), "HEAD:refs/heads/main"], cwd=repo)
    calls = []

    def transport(args, **kwargs):
        calls.append(args)
        if args[:3] == ["gh", "repo", "clone"]:
            return command(["git", "clone", "-q", "--no-checkout", str(remote), args[4]])
        if args[:3] == ["gh", "pr", "list"]:
            return b"[]"
        if args[:3] == ["gh", "pr", "create"]:
            return b"https://github.com/owner/repo/pull/1\n"
        return command([str(remote) if a == URL else a for a in args], **kwargs)

    monkeypatch.setattr(pub, "command", transport)
    return remote, calls


def test_a_failed_run_becomes_a_verified_draft_pr(tmp_path, monkeypatch):
    collected = collection(tmp_path / "source")  # jobs lint and unit, one cause
    repo = collected / "repo"
    sha = command(["git", "rev-parse", "HEAD"], cwd=repo).decode().strip()
    fake_github(monkeypatch, collected, sha)
    remote, calls = fake_remote(monkeypatch, tmp_path, repo)
    world = install(monkeypatch, set_value(1))
    # The webhook worker calls repair_run with the real environment builder (docker build).
    monkeypatch.setattr(
        orchestrate, "repair_run", partial(orchestrate.repair_run, build=world.build)
    )
    models = []
    monkeypatch.setattr(
        model_module, "make_model", lambda name, **kwargs: models.append(name) or world.model()
    )
    policy = Policy(POLICY)
    store = webhook.Store(tmp_path / "state/queue.sqlite")

    body = json.dumps(payload(head_sha=sha)).encode()
    accepted = webhook.intake(store, SECRET, headers(body), body, policy)
    assert (accepted.code, accepted.outcome) == (202, "queued")

    done = webhook.process_one(store, policy, tmp_path / "state")
    assert done["status"] == "done", done
    result = done["result"]
    assert result["repair_status"] == "PASS"
    assert result["publication"]["status"] == "PUBLISHED", result
    assert result["publication"]["url"] == "https://github.com/owner/repo/pull/1"
    assert models == ["fake/model"] and len(world.agents) == 1

    # The draft PR's branch holds exactly the verified patch on top of the failed commit.
    branch = next(c for c in calls if c[:2] == ["git", "push"])[-1].split(":")[-1]
    assert branch.startswith("refs/heads/ci-repair/7-")
    shown = command(["git", "show", f"{branch}:src/app.py"], cwd=remote).decode()
    assert shown == "value = 1\n"
    parent = command(["git", "rev-parse", f"{branch}^"], cwd=remote).decode().strip()
    assert parent == sha
    create = next(c for c in calls if c[:3] == ["gh", "pr", "create"])
    assert "--draft" in create

    # The queue records the outcome; the same delivery and run are not repaired twice.
    stored = store.execute("SELECT status, result FROM repairs")
    assert [(s, json.loads(r)["repair_status"]) for s, r in stored] == [("done", "PASS")]
    again = webhook.intake(store, SECRET, headers(body, delivery="d2"), body, policy)
    assert again.outcome == "duplicate run"
    assert webhook.process_one(store, policy, tmp_path / "state") is None


def test_no_allowed_default_model_stops_before_any_repair(tmp_path, monkeypatch):
    collected = collection(tmp_path / "source")
    sha = command(["git", "rev-parse", "HEAD"], cwd=collected / "repo").decode().strip()
    fake_github(monkeypatch, collected, sha)
    monkeypatch.setattr(orchestrate, "repair_run", lambda *a, **k: pytest.fail("no repair"))
    policy = Policy({**POLICY, "models": {"allowed": ["other/*"], "default": "fake/model"}})
    result = webhook.default_repair(
        {"repository": "owner/repo", "run_id": 7, "attempt": 1}, tmp_path / "run", policy
    )
    assert result == {"status": "POLICY_DENIED", "reason": "no allowed default model"}


def test_a_refused_publication_is_recorded_with_its_reason(tmp_path, monkeypatch):
    collected = collection(tmp_path / "source")
    sha = command(["git", "rev-parse", "HEAD"], cwd=collected / "repo").decode().strip()
    fake_github(monkeypatch, collected, sha)
    world = install(monkeypatch, set_value(1))
    monkeypatch.setattr(model_module, "make_model", lambda name, **kwargs: world.model())
    monkeypatch.setattr(
        orchestrate, "repair_run", partial(orchestrate.repair_run, build=world.build)
    )

    def refuse(*args):
        raise pub.PublicationError("Target branch moved")

    monkeypatch.setattr(pub, "auto", refuse)
    result = webhook.default_repair(
        {"repository": "owner/repo", "run_id": 7, "attempt": 1}, tmp_path / "run", Policy(POLICY)
    )
    assert result["repair_status"] == "PASS"
    assert result["publication"] == {"status": "NOT_PUBLISHABLE", "reason": "Target branch moved"}
