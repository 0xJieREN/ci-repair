import hashlib
import json

import pytest

from ci_repair import github
from ci_repair.github import CollectionError, collect, load_context, select_job

SHA = "a" * 40
LOG = b"AssertionError: expected 14, got 9\n"


def job(job_id=12):
    return {
        "id": job_id,
        "name": "test",
        "status": "completed",
        "conclusion": "failure",
        "run_id": 7,
        "head_sha": SHA,
        "steps": [{"name": "unit", "conclusion": "failure"}],
    }


def run_data(**overrides):
    return {
        "status": "completed",
        "conclusion": "failure",
        "run_attempt": 2,
        "event": "push",
        "head_repository": {"full_name": "owner/repo"},
        "repository": {"id": 1},
        "head_sha": SHA,
        "html_url": "https://github.com/owner/repo/actions/runs/7",
        "name": "CI",
        **overrides,
    }


def fake_api(monkeypatch, *, run=None, jobs=None):
    calls = []

    def api(endpoint):
        calls.append(endpoint)
        if endpoint.endswith("/logs"):
            return LOG
        if "/jobs?" in endpoint:
            return json.dumps({"jobs": jobs if jobs is not None else [job()]}).encode()
        return json.dumps(run or run_data()).encode()

    monkeypatch.setattr(github, "api", api)
    return calls


def fake_git(monkeypatch):
    calls = []

    def command(args, **kwargs):
        calls.append(args)
        if args[:3] == ["git", "rev-parse", "HEAD"]:
            return SHA.encode()
        return b""

    monkeypatch.setattr(github, "command", command)
    monkeypatch.setattr(
        github,
        "checkout_commit",
        lambda repo, sha: command(["git", "checkout", "--detach", sha], cwd=repo),
    )
    return calls


def test_collect_pins_attempt_commit_and_log(tmp_path, monkeypatch):
    calls = fake_api(monkeypatch)
    git = fake_git(monkeypatch)
    output = tmp_path / "collected"
    result = collect("owner/repo", 7, output)
    assert result["run_attempt"] == 2
    assert any("/attempts/2/jobs?" in c for c in calls)
    assert ["git", "checkout", "--detach", SHA] in git
    assert (output / "failure.log").read_bytes() == LOG
    assert result["failed_steps"] == ["unit"]
    assert load_context(output / "ci-context.json", SHA, LOG)["job_id"] == 12
    assert output.stat().st_mode & 0o077 == 0


@pytest.mark.parametrize(
    "overrides",
    [
        {"status": "in_progress"},
        {"conclusion": "success"},
        {"event": "pull_request"},
        {"head_repository": {"full_name": "fork/repo"}},
        {"head_sha": "--evil"},
    ],
)
def test_reject_unsupported_runs_before_checkout(tmp_path, monkeypatch, overrides):
    fake_api(monkeypatch, run=run_data(**overrides))
    git = fake_git(monkeypatch)
    with pytest.raises(CollectionError):
        collect("owner/repo", 7, tmp_path / "collected")
    assert not git


def test_multiple_jobs_require_selection():
    with pytest.raises(CollectionError, match="--job-id"):
        select_job([job(12), job(13)], None)
    assert select_job([job(12), job(13)], 13)["id"] == 13
    with pytest.raises(CollectionError):
        select_job([job()], 999)


def test_attempt_pagination(tmp_path, monkeypatch):
    calls = []

    def api(endpoint):
        calls.append(endpoint)
        if endpoint.endswith("/logs"):
            return LOG
        if "/jobs?" in endpoint:
            batch = [{**job(i), "conclusion": "success"} for i in range(100)]
            return json.dumps({"jobs": batch if endpoint.endswith("page=1") else [job()]}).encode()
        return json.dumps(run_data()).encode()

    monkeypatch.setattr(github, "api", api)
    fake_git(monkeypatch)
    assert collect("owner/repo", 7, tmp_path / "out", attempt=1)["run_attempt"] == 1
    assert any("/attempts/1/jobs?per_page=100&page=2" in c for c in calls)


def test_missing_logs_leave_no_completed_manifest(tmp_path, monkeypatch):
    fake_api(monkeypatch)
    original = github.api
    monkeypatch.setattr(
        github, "api", lambda path: b"" if path.endswith("/logs") else original(path)
    )
    with pytest.raises(CollectionError, match="empty"):
        collect("owner/repo", 7, tmp_path / "out")
    assert not (tmp_path / "out/ci-context.json").exists()


def test_manifest_rejects_wrong_commit_or_log(tmp_path):
    p = tmp_path / "context.json"
    p.write_text(
        json.dumps(
            {"schema_version": 1, "commit": SHA, "log_sha256": hashlib.sha256(LOG).hexdigest()}
        )
    )
    with pytest.raises(CollectionError, match="commit"):
        load_context(p, "b" * 40, LOG)
    with pytest.raises(CollectionError, match="log"):
        load_context(p, SHA, b"changed")


def test_output_is_never_overwritten(tmp_path, monkeypatch):
    fake_api(monkeypatch)
    with pytest.raises(CollectionError, match="exists"):
        collect("owner/repo", 7, tmp_path)


def test_job_must_match_commit(tmp_path, monkeypatch):
    fake_api(monkeypatch, jobs=[{**job(), "head_sha": "b" * 40}])
    with pytest.raises(CollectionError, match="commit"):
        collect("owner/repo", 7, tmp_path / "out")


def test_mismatched_context_stops_pipeline_before_sandbox(tmp_path, monkeypatch):
    from ci_repair import pipeline

    repo = tmp_path / "repo"
    repo.mkdir()
    log = tmp_path / "failure.log"
    log.write_bytes(LOG)
    manifest = tmp_path / "ci-context.json"
    manifest.write_text(json.dumps({"schema_version": 1, "commit": "b" * 40}))
    monkeypatch.setattr(pipeline, "snapshot", lambda *args: SHA)
    monkeypatch.setattr(pipeline, "command", lambda *args: b"sha256:image")

    def forbidden(*args):
        pytest.fail("mismatched context must not enter a sandbox")

    monkeypatch.setattr(pipeline, "workspace", forbidden)
    cfg = pipeline.Config(
        repo, log, tmp_path / "run", "image", "test", "regression", ci_context=manifest
    )
    result = pipeline.run(cfg, object())
    assert result["status"] == "ERROR"
    assert result["error_type"] == "CollectionError"
    assert "model_calls" not in result


def test_only_log_api_allows_raw_escape_sequences(monkeypatch):
    calls = fake_git(monkeypatch)
    github.api("repos/owner/repo/actions/jobs/12/logs")
    github.api("repos/owner/repo/actions/runs/7")
    assert "--allow-escape-sequences" in calls[0]
    assert "--allow-escape-sequences" not in calls[1]


def pr_run():
    return run_data(
        event="pull_request",
        pull_requests=[
            {
                "number": 8,
                "head": {"sha": SHA, "ref": "feature", "repo": {"id": 1}},
                "base": {"sha": "b" * 40, "ref": "main"},
            }
        ],
    )


def test_pr_requires_explicit_checkout_sha():
    with pytest.raises(CollectionError, match="checkout-sha"):
        github.resolve_source("owner/repo", pr_run(), None)
    sha, source = github.resolve_source("owner/repo", pr_run(), SHA)
    assert sha == SHA
    assert source["checkout_kind"] == "head"
    assert source["head_branch"] == "feature"
    assert source["pull_request"]["number"] == 8


def test_pr_merge_uses_recorded_parents_not_current_merge_ref(monkeypatch):
    monkeypatch.setattr(
        github,
        "api",
        lambda path: json.dumps({"parents": [{"sha": "b" * 40}, {"sha": SHA}]}).encode(),
    )
    sha, source = github.resolve_source("owner/repo", pr_run(), "c" * 40)
    assert sha == "c" * 40
    assert source["checkout_kind"] == "merge"
    monkeypatch.setattr(
        github,
        "api",
        lambda path: json.dumps({"parents": [{"sha": "d" * 40}, {"sha": SHA}]}).encode(),
    )
    with pytest.raises(CollectionError, match="parents"):
        github.resolve_source("owner/repo", pr_run(), "c" * 40)


def test_pr_merge_collection_records_different_job_and_checkout_sha(tmp_path, monkeypatch):
    fake_api(monkeypatch, run=pr_run())
    original = github.api

    def api(path):
        if "/commits/" in path:
            return json.dumps({"parents": [{"sha": "b" * 40}, {"sha": SHA}]}).encode()
        return original(path)

    monkeypatch.setattr(github, "api", api)

    def command(args, **kwargs):
        return b"c" * 40 if args[:3] == ["git", "rev-parse", "HEAD"] else b""

    monkeypatch.setattr(github, "command", command)
    monkeypatch.setattr(
        github,
        "checkout_commit",
        lambda repo, sha: command(["git", "checkout", "--detach", sha], cwd=repo),
    )
    result = collect("owner/repo", 7, tmp_path / "out", checkout_sha="c" * 40)
    assert result["commit"] == "c" * 40
    assert result["pull_request"]["head_sha"] == SHA


def test_pull_request_target_remains_rejected():
    with pytest.raises(CollectionError, match="Unsupported"):
        github.resolve_source("owner/repo", run_data(event="pull_request_target"), SHA)


def test_fork_pr_rejected_even_if_run_head_repository_is_base():
    run = pr_run()
    run["pull_requests"][0]["head"]["repo"]["id"] = 2
    with pytest.raises(CollectionError, match="Fork"):
        github.resolve_source("owner/repo", run, SHA)


def test_explicit_attempt_avoids_loading_latest_metadata(tmp_path, monkeypatch):
    calls = fake_api(monkeypatch)
    fake_git(monkeypatch)
    collect("owner/repo", 7, tmp_path / "out", attempt=1)
    assert calls[0] == "repos/owner/repo/actions/runs/7/attempts/1"
    assert "repos/owner/repo/actions/runs/7" not in calls


def run_api(monkeypatch, jobs, logs, run=None):
    calls = []

    def api(endpoint):
        calls.append(endpoint)
        if endpoint.endswith("/logs"):
            return logs[int(endpoint.split("/")[-2])]
        if "/jobs?" in endpoint:
            return json.dumps({"jobs": jobs}).encode()
        if "/commits/" in endpoint:
            return json.dumps({"parents": [{"sha": "b" * 40}, {"sha": SHA}]}).encode()
        return json.dumps(run or run_data(path=".github/workflows/ci.yml")).encode()

    monkeypatch.setattr(github, "api", api)
    return calls


def test_collect_run_gathers_every_failed_job_with_step_conclusions(tmp_path, monkeypatch):
    jobs = [
        {**job(13), "name": "lint", "labels": ["ubuntu-latest"]},
        {**job(12), "steps": [{"number": 1, "name": "unit", "conclusion": "failure"}]},
        {**job(14), "conclusion": "success"},
        {**job(15), "conclusion": "cancelled", "name": "slow"},
    ]
    run_api(monkeypatch, jobs, {12: LOG, 13: b"E lint\n"})
    fake_git(monkeypatch)
    output = tmp_path / "run"
    manifest = github.collect_run("owner/repo", 7, output)
    assert [j["job_id"] for j in manifest["jobs"]] == [12, 13]
    assert manifest["other_unsuccessful_jobs"] == [
        {"job_id": 15, "job_name": "slow", "conclusion": "cancelled"}
    ]
    context = load_context(output / "jobs/12/ci-context.json", SHA, LOG)
    assert context["job_id"] == 12
    raw = json.loads((output / "jobs/12/ci-context.json").read_text())
    assert raw["job_steps"] == [{"number": 1, "name": "unit", "conclusion": "failure"}]
    assert json.loads((output / "jobs/13/ci-context.json").read_text())["job_labels"] == [
        "ubuntu-latest"
    ]
    assert json.loads((output / "run.json").read_text())["commit"] == SHA


def test_real_checkout_log_yields_sha():
    from pathlib import Path

    log = b"2026-09-18T04:28:18.7Z [command]/usr/bin/git log -1 --format=%H\r\n"
    log += b"2026-09-18T04:28:18.7Z " + SHA.encode() + b"\r\n"
    assert github.checkout_sha_from_log(log) == SHA
    assert github.checkout_sha_from_log(b"no checkout here") is None
    real = Path(__file__).parents[1] / "runs/github-import-01/failure.log"
    if real.exists():
        assert github.checkout_sha_from_log(real.read_bytes()) == (
            "931e711c8bb7462b89791e0b77c8db996f1e2ee1"
        )


def test_collect_run_pr_derives_and_verifies_checkout_sha(tmp_path, monkeypatch):
    merge = "c" * 40
    checkout = (
        b"2026-09-18T04:28:18.7Z [command]/usr/bin/git log -1 --format=%H\n2026-09-18T04:28:18.7Z "
        + merge.encode()
        + b"\n"
    )
    run_api(monkeypatch, [job(12), job(13)], {12: checkout, 13: checkout}, run=pr_run())

    def command(args, **kwargs):
        return merge.encode() if args[:3] == ["git", "rev-parse", "HEAD"] else b""

    monkeypatch.setattr(github, "command", command)
    monkeypatch.setattr(github, "checkout_commit", lambda repo, sha: None)
    manifest = github.collect_run("owner/repo", 7, tmp_path / "run")
    assert manifest["commit"] == merge
    assert manifest["checkout_kind"] == "merge"
    other = (
        b"2026-09-18T04:28:18.7Z [command]/usr/bin/git log -1 --format=%H\n2026-09-18T04:28:18.7Z "
        + b"d" * 40
        + b"\n"
    )
    run_api(monkeypatch, [job(12), job(13)], {12: checkout, 13: other}, run=pr_run())
    with pytest.raises(CollectionError, match="agree"):
        github.collect_run("owner/repo", 7, tmp_path / "run2")
