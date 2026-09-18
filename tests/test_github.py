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
