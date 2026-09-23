import hashlib
import hmac
import json
import signal
import threading
import urllib.error
import urllib.request

import pytest

from ci_repair import webhook
from ci_repair.policy import Policy

SECRET = b"s" * 32
POLICY = Policy({"repositories": [{"name": "owner/repo", "branches": ["main"]}]})
SHA = "a" * 40


def payload(**run):
    return {
        "action": "completed",
        "repository": {"full_name": "owner/repo"},
        "workflow_run": {
            "id": 7,
            "run_attempt": 1,
            "conclusion": "failure",
            "event": "push",
            "head_branch": "main",
            "head_repository": {"full_name": "owner/repo"},
            **run,
        },
    }


def headers(body, delivery="d1", event="workflow_run", secret=SECRET):
    signature = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
    return {
        "X-Hub-Signature-256": signature,
        "X-GitHub-Delivery": delivery,
        "X-GitHub-Event": event,
    }


def post(store, data, **kwargs):
    body = json.dumps(data).encode()
    return webhook.intake(store, SECRET, headers(body, **kwargs), body, POLICY)


@pytest.fixture
def store(tmp_path):
    return webhook.Store(tmp_path / "state.sqlite")


def test_signature_verification():
    body = b"{}"
    good = headers(body)["X-Hub-Signature-256"]
    assert webhook.verify_signature(SECRET, body, good)
    assert not webhook.verify_signature(SECRET, body + b" ", good)
    assert not webhook.verify_signature(SECRET, body, good.replace("sha256=", "sha1="))
    assert not webhook.verify_signature(SECRET, body, None)
    assert not webhook.verify_signature(b"", body, good)


def test_invalid_signature_is_rejected_without_state(store):
    body = json.dumps(payload()).encode()
    result = webhook.intake(store, SECRET, headers(body, secret=b"x" * 32), body, POLICY)
    assert result.code == 401
    assert store.execute("SELECT count(*) FROM deliveries")[0][0] == 0


def test_duplicate_deliveries_and_runs_enqueue_once(store):
    assert post(store, payload()).outcome == "queued"
    assert post(store, payload()).outcome == "duplicate delivery"
    assert post(store, payload(), delivery="d2").outcome == "duplicate run"
    assert post(store, payload(run_attempt=2), delivery="d3").outcome == "queued"
    assert store.execute("SELECT count(*) FROM repairs")[0][0] == 2


@pytest.mark.parametrize(
    "data,event,outcome",
    [
        (payload(conclusion="success"), "workflow_run", "not a failure"),
        (payload(event="pull_request_target"), "workflow_run", "pull_request_target"),
        (payload(head_repository={"full_name": "fork/repo"}), "workflow_run", "fork"),
        (payload(head_branch="feature"), "workflow_run", "branch"),
        (
            {
                **payload(head_repository={"full_name": "other/repo"}),
                "repository": {"full_name": "other/repo"},
            },
            "workflow_run",
            "not in",
        ),
        ({**payload(), "action": "requested"}, "workflow_run", "ignored"),
        ({"zen": "hi"}, "ping", "pong"),
        (payload(), "push", "ignored"),
    ],
)
def test_filtering_never_enqueues(store, data, event, outcome):
    result = post(store, data, event=event)
    assert outcome in result.outcome
    assert store.execute("SELECT count(*) FROM repairs")[0][0] == 0


def fake_github(monkeypatch, *, latest_attempt=1, branch_sha=SHA, fork=False, conclusion="failure"):
    run = {
        "status": "completed",
        "conclusion": conclusion,
        "run_attempt": 1,
        "event": "push",
        "head_branch": "main",
        "head_sha": SHA,
        "head_repository": {"full_name": "fork/repo" if fork else "owner/repo"},
    }

    def api(endpoint):
        if endpoint.endswith("/attempts/1"):
            return json.dumps(run).encode()
        if endpoint.endswith("/runs/7"):
            return json.dumps({**run, "run_attempt": latest_attempt}).encode()
        if "/branches/" in endpoint:
            return json.dumps({"commit": {"sha": branch_sha}}).encode()
        raise AssertionError(endpoint)

    monkeypatch.setattr(webhook.github, "api", api)


@pytest.mark.parametrize(
    "kwargs,reason",
    [
        ({"latest_attempt": 2}, "newer run attempt"),
        ({"branch_sha": "b" * 40}, "branch moved"),
        ({"fork": True}, "fork"),
        ({"conclusion": "success"}, "not repairable"),
    ],
)
def test_worker_rechecks_canonical_state_before_any_repair(
    store, tmp_path, monkeypatch, kwargs, reason
):
    fake_github(monkeypatch, **kwargs)
    post(store, payload())
    result = webhook.process_one(
        store, POLICY, tmp_path, repair=lambda *a: pytest.fail("must not repair")
    )
    assert result["status"] == "skipped"
    assert reason in result["result"]["skipped"]


def test_worker_repairs_once_and_records_outcome(store, tmp_path, monkeypatch):
    fake_github(monkeypatch)
    post(store, payload())
    seen = []

    def repair(item, directory, policy):
        seen.append(directory)
        return {"repair_status": "PASS", "publication": {"status": "PUBLISHED"}}

    result = webhook.process_one(store, POLICY, tmp_path, repair=repair)
    assert result["status"] == "done"
    assert seen[0].name == "owner__repo-7-1"
    assert webhook.process_one(store, POLICY, tmp_path, repair=repair) is None
    stored = store.execute("SELECT status, result FROM repairs")[0]
    assert stored[0] == "done" and json.loads(stored[1])["repair_status"] == "PASS"


def test_errors_are_recorded_by_type_and_can_be_requeued(store, tmp_path, monkeypatch):
    fake_github(monkeypatch)
    post(store, payload())

    def boom(*args):
        raise RuntimeError("token=secret")

    result = webhook.process_one(store, POLICY, tmp_path, repair=boom)
    assert result["status"] == "failed"
    assert "secret" not in json.dumps(result["result"])
    assert store.requeue("owner/repo#7#1")
    previous = tmp_path / "runs" / "owner__repo-7-1"
    (previous / "evidence.txt").write_text("first attempt")
    seen = []

    def retry(item, directory, policy):
        seen.append(directory)
        return {"repair_status": "PASS"}

    result = webhook.process_one(store, POLICY, tmp_path, repair=retry)
    assert result["status"] == "done"
    assert seen[0] != previous and seen[0].is_dir()
    assert (previous / "evidence.txt").read_text() == "first attempt"


def test_serve_runs_repairs_on_main_thread(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "sys.argv",
        [
            "ci-repair-webhook",
            "serve",
            "--port",
            "0",
            "--state-dir",
            str(tmp_path),
            "--policy",
            str(tmp_path.parent / "policy.yaml"),
        ],
    )
    monkeypatch.setattr(webhook, "load_policy", lambda *a, **kw: POLICY)
    monkeypatch.setenv("CI_REPAIR_WEBHOOK_SECRET", SECRET.decode())
    observed = []

    def check_worker(*args):
        # This is the same signal operation used by the real repair pipeline.
        previous = signal.signal(signal.SIGALRM, signal.SIG_DFL)
        signal.signal(signal.SIGALRM, previous)
        observed.append(threading.current_thread() is threading.main_thread())
        raise KeyboardInterrupt

    monkeypatch.setattr(webhook, "worker", check_worker)
    assert webhook.main() == 0
    assert observed == [True]


def test_one_running_repair_per_branch_and_expired_leases_are_not_rerun(store, monkeypatch):
    post(store, payload())
    post(store, payload(id=8), delivery="d2")
    first = store.claim()
    assert first["run_id"] == 7
    assert store.claim() is None  # same repository and branch is busy
    monkeypatch.setattr(webhook.time, "time", lambda: 10**12)
    assert store.claim()["run_id"] == 8  # stale lease released as interrupted, not rerun
    statuses = dict(store.execute("SELECT run_id, status FROM repairs"))
    assert statuses[7] == "interrupted"


def test_http_server_round_trip(store):
    wake = threading.Event()
    server = webhook.ThreadingHTTPServer(
        ("127.0.0.1", 0), webhook.make_handler(store, SECRET, POLICY, wake)
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}/webhook"
    try:
        body = json.dumps(payload()).encode()
        request = urllib.request.Request(url, body, headers(body), method="POST")
        with urllib.request.urlopen(request) as response:
            assert response.status == 202
        assert wake.is_set()
        bad = urllib.request.Request(url, body, {"X-GitHub-Delivery": "x"}, method="POST")
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(bad)
        assert error.value.code == 401
        huge = urllib.request.Request(
            url, b"x", {"Content-Length": str(webhook.MAX_BODY + 1)}, method="POST"
        )
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(huge)
        assert error.value.code == 413
    finally:
        server.shutdown()
        server.server_close()
