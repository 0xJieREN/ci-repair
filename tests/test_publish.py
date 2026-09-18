"""Use real local Git repositories and a bare remote; mock only GitHub transport."""

import json

import pytest

from ci_repair import publish as pub
from ci_repair.workspace import command


@pytest.fixture
def publication(tmp_path, monkeypatch):
    repo = tmp_path / "source"
    command(["git", "init", "-b", "main", str(repo)])
    (repo / "src").mkdir()
    (repo / "src/code.py").write_text("value = 1\n")
    command(["git", "add", "."], cwd=repo)
    command(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@localhost",
            "commit",
            "-m",
            "baseline",
        ],
        cwd=repo,
    )
    sha = command(["git", "rev-parse", "HEAD"], cwd=repo).decode().strip()
    remote = tmp_path / "remote.git"
    command(["git", "clone", "--bare", str(repo), str(remote)])
    (repo / "src/code.py").write_text("value = 2\n")
    patch = command(["git", "diff", "--binary"], cwd=repo)
    command(["git", "restore", "."], cwd=repo)
    run = tmp_path / "run"
    run.mkdir()
    report = {
        "status": "PASS",
        "verified": True,
        "commit": sha,
        "patch_sha256": pub.digest(patch),
        "changed_files": ["src/code.py"],
        "config": {"allowed_paths": ["src/"]},
        "tests": [{"returncode": 0}, {"returncode": 0}],
        "github_actions": {
            "repository": "owner/repo",
            "commit": sha,
            "checkout_kind": "head",
            "head_branch": "main",
            "run_id": 7,
            "run_attempt": 1,
            "job_id": 12,
        },
    }
    (run / "patch.diff").write_bytes(patch)
    (run / "report.json").write_text(json.dumps(report))
    calls = []
    prs = []

    def transport(args, **kwargs):
        calls.append(args)
        if args[:3] == ["gh", "repo", "clone"]:
            return command(["git", "clone", "--no-checkout", str(remote), args[4]])
        if args[:3] == ["gh", "pr", "list"]:
            return json.dumps(prs).encode()
        if args[:3] == ["gh", "pr", "create"]:
            head = command(["git", "rev-parse", "HEAD"], cwd=kwargs["cwd"]).decode().strip()
            prs.append(
                {"url": "https://github.com/owner/repo/pull/1", "state": "OPEN", "headRefOid": head}
            )
            return b"https://github.com/owner/repo/pull/1\n"
        return command(
            [str(remote) if a == "https://github.com/owner/repo.git" else a for a in args], **kwargs
        )

    monkeypatch.setattr(pub, "command", transport)
    return run, tmp_path / "prepared", remote, repo, report, calls, prs


def test_prepare_and_publish_draft_is_resumable(publication):
    run, out, remote, repo, report, calls, prs = publication
    plan = pub.prepare(run, out, "main")
    # Preparation has made a real local commit but made no remote changes.
    assert command(["git", "ls-remote", str(remote), "refs/heads/ci-repair/*"]) == b""
    assert (out / "repo/src/code.py").read_text() == "value = 2\n"
    assert (repo / "src/code.py").read_text() == "value = 1\n"
    assert pub.publish(out) == "https://github.com/owner/repo/pull/1"
    assert pub.publish(out) == prs[0]["url"]
    assert sum(c[:3] == ["gh", "pr", "create"] for c in calls) == 1
    create = next(c for c in calls if c[:3] == ["gh", "pr", "create"])
    assert "--draft" in create and "--body-file" in create
    assert (
        command(["git", "ls-remote", str(remote), f"refs/heads/{plan['branch']}"])
        .decode()
        .startswith(plan["commit"])
    )


@pytest.mark.parametrize("mutation", ["fail", "digest", "tests", "merge", "paths"])
def test_invalid_evidence_cannot_prepare(publication, mutation):
    run, out, remote, repo, report, calls, prs = publication
    if mutation == "fail":
        report["status"] = "FAIL"
    if mutation == "digest":
        report["patch_sha256"] = "wrong"
    if mutation == "tests":
        report["tests"][1]["returncode"] = 1
    if mutation == "merge":
        report["github_actions"]["checkout_kind"] = "merge"
    if mutation == "paths":
        report["config"]["allowed_paths"] = ["other/"]
    (run / "report.json").write_text(json.dumps(report))
    with pytest.raises(pub.PublicationError):
        pub.prepare(run, out, "main")
    assert not any(c[:2] == ["git", "push"] for c in calls)


def test_moved_base_stops_publication(publication):
    run, out, remote, repo, report, calls, prs = publication
    pub.prepare(run, out, "main")
    command(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@localhost",
            "commit",
            "--allow-empty",
            "-m",
            "base moved",
        ],
        cwd=repo,
    )
    command(["git", "push", str(remote), "main"], cwd=repo)
    with pytest.raises(pub.PublicationError, match="moved"):
        pub.publish(out)
    assert not any(c[:2] == ["git", "push"] for c in calls)


def test_changed_patch_after_prepare_is_rejected(publication):
    run, out, *_ = publication
    pub.prepare(run, out, "main")
    (run / "patch.diff").write_bytes(b"changed")
    with pytest.raises(pub.PublicationError, match="Patch"):
        pub.publish(out)


def test_existing_remote_branch_is_not_overwritten(publication):
    run, out, remote, repo, report, calls, prs = publication
    plan = pub.prepare(run, out, "main")
    command(["git", "push", str(remote), f"HEAD:refs/heads/{plan['branch']}"], cwd=repo)
    with pytest.raises(pub.PublicationError, match="overwrite"):
        pub.publish(out)
    assert not any(c[:2] == ["git", "push"] for c in calls)


def test_dirty_prepared_checkout_is_rejected(publication):
    run, out, *_ = publication
    pub.prepare(run, out, "main")
    (out / "repo/src/code.py").write_text("changed")
    with pytest.raises(pub.PublicationError, match="dirty"):
        pub.publish(out)


def test_wrong_base_rejected(publication):
    run, out, *_ = publication
    with pytest.raises(pub.PublicationError, match="original failed branch"):
        pub.prepare(run, out, "other")


def test_closed_pr_is_not_duplicated(publication):
    run, out, remote, repo, report, calls, prs = publication
    pub.prepare(run, out, "main")
    pub.publish(out)
    prs[0]["state"] = "CLOSED"
    with pytest.raises(pub.PublicationError, match="closed"):
        pub.publish(out)
    assert sum(c[:3] == ["gh", "pr", "create"] for c in calls) == 1


def test_target_pr_head_movement_is_rejected(publication, monkeypatch):
    run, out, remote, repo, report, calls, prs = publication
    report["github_actions"]["pull_request"] = {"number": 8}
    (run / "report.json").write_text(json.dumps(report))
    monkeypatch.setattr(
        pub,
        "api",
        lambda path: json.dumps(
            {
                "state": "open",
                "head": {"sha": "b" * 40, "ref": "main", "repo": {"full_name": "owner/repo"}},
            }
        ).encode(),
    )
    with pytest.raises(pub.PublicationError, match="moved"):
        pub.prepare(run, out, "main")


def test_base_moving_during_push_stops_pr_creation(publication, monkeypatch):
    run, out, remote, repo, report, calls, prs = publication
    pub.prepare(run, out, "main")
    original = pub.require_base
    checks = []

    def changed(url, base, expected):
        checks.append(base)
        if len(checks) > 1:
            raise pub.PublicationError("Target branch moved")
        original(url, base, expected)

    monkeypatch.setattr(pub, "require_base", changed)
    with pytest.raises(pub.PublicationError, match="moved"):
        pub.publish(out)
    assert prs == []


@pytest.mark.docker
def test_real_verifier_report_can_be_prepared_and_published(publication, monkeypatch):
    import hashlib
    import os

    if os.getenv("CI_REPAIR_DOCKER_TESTS") != "1":
        pytest.skip("set CI_REPAIR_DOCKER_TESTS=1")
    from test_docker import scripted_model

    from ci_repair.pipeline import Config
    from ci_repair.pipeline import run as repair

    run_dir, out, remote, repo, report, calls, prs = publication
    log = run_dir / "failure.log"
    log.write_text("AssertionError: expected value 2")
    metadata = {
        **report["github_actions"],
        "schema_version": 1,
        "log_sha256": hashlib.sha256(log.read_bytes()).hexdigest(),
        "run_url": "https://github.com/owner/repo/actions/runs/7",
        "event": "push",
        "workflow": "CI",
        "job_name": "test",
        "failed_steps": ["unit"],
    }
    manifest = run_dir / "ci-context.json"
    manifest.write_text(json.dumps(metadata))
    cfg = Config(
        repo,
        log,
        run_dir / "actual",
        "ci-repair-demo:local",
        "python -c 'from src.code import value; assert value == 2'",
        "python -c 'from src.code import value; assert isinstance(value, int)'",
        ci_context=manifest,
    )
    result = repair(cfg, scripted_model("printf 'value = 2\\n' > src/code.py"))
    assert result["status"] == "PASS", result
    pub.prepare(cfg.output, out, "main")
    assert pub.publish(out) == "https://github.com/owner/repo/pull/1"
