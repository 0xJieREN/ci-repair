"""GitHub webhook intake and a single-worker repair queue (stdlib HTTP server + SQLite).

The webhook payload is only a hint that a run finished. Admission is decided again from
canonical GitHub API state (latest attempt, unchanged branch head, same repository, policy)
before any collection, repair or publication happens.
"""

import argparse
import hashlib
import hmac
import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ci_repair import github
from ci_repair.policy import Policy, PolicyError, load_policy

MAX_BODY = 2 * 1024 * 1024
LEASE_SECONDS = 4 * 3600
SCHEMA = """
CREATE TABLE IF NOT EXISTS deliveries (
  delivery_id TEXT PRIMARY KEY, received_at REAL NOT NULL, outcome TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS repairs (
  key TEXT PRIMARY KEY,
  repository TEXT NOT NULL, run_id INTEGER NOT NULL, attempt INTEGER NOT NULL,
  branch TEXT, status TEXT NOT NULL, result TEXT,
  created_at REAL NOT NULL, updated_at REAL NOT NULL, lease_until REAL
);
"""


def verify_signature(secret: bytes, body: bytes, header: str | None) -> bool:
    if not secret or not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret, body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix("sha256="))


class Store:
    """SQLite state: unique run keys and delivery IDs make intake idempotent."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None, check_same_thread=False, timeout=30)
        self.lock = threading.Lock()
        self.db.executescript(SCHEMA)

    def execute(self, sql: str, params=()):
        with self.lock:
            return self.db.execute(sql, params).fetchall()

    def record_delivery(self, delivery_id: str, outcome: str) -> bool:
        with self.lock:
            cursor = self.db.execute(
                "INSERT OR IGNORE INTO deliveries VALUES (?, ?, ?)",
                (delivery_id, time.time(), outcome),
            )
            return cursor.rowcount == 1

    def enqueue(self, repository: str, run_id: int, attempt: int, branch: str | None) -> bool:
        key = f"{repository.lower()}#{run_id}#{attempt}"
        now = time.time()
        with self.lock:
            cursor = self.db.execute(
                "INSERT OR IGNORE INTO repairs VALUES (?, ?, ?, ?, ?, 'queued', NULL, ?, ?, NULL)",
                (key, repository, run_id, attempt, branch, now, now),
            )
            return cursor.rowcount == 1

    def claim(self) -> dict | None:
        """Oldest queued repair whose repository/branch has no running repair."""
        now = time.time()
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                # A crashed worker must not leave a branch locked forever, nor silently rerun.
                self.db.execute(
                    "UPDATE repairs SET status='interrupted', updated_at=? "
                    "WHERE status='running' AND lease_until < ?",
                    (now, now),
                )
                row = self.db.execute(
                    "SELECT key, repository, run_id, attempt, branch FROM repairs r "
                    "WHERE status='queued' AND NOT EXISTS (SELECT 1 FROM repairs o "
                    "WHERE o.status='running' AND lower(o.repository)=lower(r.repository) "
                    "AND o.branch IS r.branch) ORDER BY created_at, key LIMIT 1"
                ).fetchone()
                if row:
                    self.db.execute(
                        "UPDATE repairs SET status='running', updated_at=?, lease_until=? "
                        "WHERE key=?",
                        (now, now + LEASE_SECONDS, row[0]),
                    )
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
        if not row:
            return None
        return dict(zip(("key", "repository", "run_id", "attempt", "branch"), row))

    def finish(self, key: str, status: str, result: dict):
        self.execute(
            "UPDATE repairs SET status=?, result=?, updated_at=?, lease_until=NULL WHERE key=?",
            (status, json.dumps(result, default=str), time.time(), key),
        )

    def requeue(self, key: str) -> bool:
        with self.lock:
            cursor = self.db.execute(
                "UPDATE repairs SET status='queued', updated_at=? "
                "WHERE key=? AND status IN ('interrupted', 'failed')",
                (time.time(), key),
            )
            return cursor.rowcount == 1


@dataclass
class Intake:
    code: int
    outcome: str


def intake(store: Store, secret: bytes, headers, body: bytes, policy: Policy) -> Intake:
    """Authenticate, filter and enqueue; never executes anything."""
    if not verify_signature(secret, body, headers.get("X-Hub-Signature-256")):
        return Intake(401, "invalid signature")
    delivery = headers.get("X-GitHub-Delivery") or ""
    if not delivery or len(delivery) > 100:
        return Intake(400, "missing delivery id")
    event = headers.get("X-GitHub-Event")
    try:
        payload = json.loads(body)
    except ValueError:
        return Intake(400, "invalid JSON")
    outcome = "ignored"
    if event == "ping":
        outcome = "pong"
    elif event == "workflow_run" and payload.get("action") == "completed":
        run = payload.get("workflow_run") or {}
        repository = (payload.get("repository") or {}).get("full_name", "")
        head_repository = (run.get("head_repository") or {}).get("full_name", "")
        decision = policy.check_trigger(
            repository=repository,
            event=str(run.get("event", "")),
            branch=run.get("head_branch"),
            fork=head_repository.lower() != repository.lower(),
        )
        if run.get("conclusion") != "failure":
            outcome = "ignored: not a failure"
        elif not decision.allowed:
            outcome = "ignored: " + "; ".join(decision.reasons)
        elif not isinstance(run.get("id"), int) or not isinstance(run.get("run_attempt"), int):
            return Intake(400, "invalid run identity")
        elif store.enqueue(repository, run["id"], run["run_attempt"], run.get("head_branch")):
            outcome = "queued"
        else:
            outcome = "duplicate run"
    if not store.record_delivery(delivery, outcome):
        return Intake(200, "duplicate delivery")
    return Intake(202 if outcome == "queued" else 200, outcome)


def canonical_check(item: dict, policy: Policy) -> tuple[str | None, dict]:
    """Re-derive admission from the GitHub API; return (skip reason or None, run)."""
    repository, run_id = item["repository"], item["run_id"]
    try:
        run, _ = github.fetch_run(repository, run_id, item["attempt"])
    except github.CollectionError as exc:
        return f"not repairable: {exc}", {}
    latest = json.loads(github.api(f"repos/{repository}/actions/runs/{run_id}"))
    if latest["run_attempt"] != item["attempt"]:
        return "stale: a newer run attempt exists", run
    fork = run["head_repository"]["full_name"].lower() != repository.lower()
    decision = policy.check_trigger(
        repository=repository, event=run["event"], branch=run.get("head_branch"), fork=fork
    )
    if not decision.allowed:
        return "policy: " + "; ".join(decision.reasons), run
    if run["event"] == "pull_request":
        prs = run.get("pull_requests") or []
        if len(prs) != 1:
            return "not repairable: PR run without exactly one pull request", run
        live = json.loads(github.api(f"repos/{repository}/pulls/{prs[0]['number']}"))
        head = live["head"]["sha"] if live.get("state") == "open" else None
    else:
        branch = json.loads(github.api(f"repos/{repository}/branches/{run['head_branch']}"))
        head = branch["commit"]["sha"]
    if head != run["head_sha"]:
        return "stale: the branch moved after this run", run
    return None, run


def default_repair(item: dict, directory: Path, policy: Policy) -> dict:
    """collect -> orchestrate -> publication gate -> draft PR (only on ALLOW)."""
    from ci_repair.cli import make_model
    from ci_repair.orchestrate import repair_run
    from ci_repair.publish import auto

    github.collect_run(
        item["repository"], item["run_id"], directory / "collection", attempt=item["attempt"]
    )
    model_name = policy.data["models"]["default"]
    if not model_name or not policy.check_model(model_name).allowed:
        return {"status": "POLICY_DENIED", "reason": "no allowed default model"}
    wall = int(policy.data["budget"]["max_wall_seconds"])
    report = repair_run(
        directory / "collection",
        directory / "repair",
        model_factory=lambda: make_model(model_name, wall_seconds=wall),
        policy=policy,
    )
    result = {"repair_status": report["status"], "stop_reason": report.get("stop_reason")}
    if report.get("verified"):
        result["publication"] = auto(directory / "repair", directory / "publication", policy)
    return result


def process_one(
    store: Store, policy: Policy, state_dir: Path, repair=default_repair
) -> dict | None:
    item = store.claim()
    if item is None:
        return None
    try:
        reason, _ = canonical_check(item, policy)
        if reason:
            result, status = {"skipped": reason}, "skipped"
        else:
            directory = state_dir / "runs" / item["key"].replace("/", "__").replace("#", "-")
            directory.mkdir(parents=True, exist_ok=False, mode=0o700)
            result, status = repair(item, directory, policy), "done"
    except Exception as exc:
        # External error text can include credentials; persist only the type.
        result, status = {"error_type": type(exc).__name__}, "failed"
    store.finish(item["key"], status, result)
    return {**item, "status": status, "result": result}


def make_handler(store: Store, secret: bytes, policy: Policy, wake: threading.Event):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            if self.path != "/webhook":
                return self.reply(404, "not found")
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                return self.reply(411, "length required")
            if length < 0 or length > MAX_BODY:
                return self.reply(413, "payload too large")
            result = intake(store, secret, self.headers, self.rfile.read(length), policy)
            if result.outcome == "queued":
                wake.set()
            self.reply(result.code, result.outcome)

        def reply(self, code: int, message: str):
            body = json.dumps({"result": message}).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):  # noqa: A002 - upstream signature
            pass  # request lines can contain nothing useful beyond the stored outcome

    return Handler


def worker(store: Store, policy: Policy, state_dir: Path, wake: threading.Event, stop):
    while not stop.is_set():
        if process_one(store, policy, state_dir) is None:
            wake.wait(timeout=30)
            wake.clear()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    serve = sub.add_parser("serve", help="Receive webhooks and process the queue")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8080)
    serve.add_argument("--no-worker", action="store_true", help="Only enqueue")
    work = sub.add_parser("work", help="Process queued repairs")
    work.add_argument("--once", action="store_true")
    requeue = sub.add_parser("requeue", help="Requeue an interrupted or failed repair")
    requeue.add_argument("key")
    for command_parser in (serve, work, requeue):
        command_parser.add_argument("--state-dir", type=Path, required=True)
        command_parser.add_argument("--policy", type=Path, required=True)
        command_parser.add_argument("--env-file", type=Path)
    args = parser.parse_args()
    try:
        policy = load_policy(args.policy, untrusted_roots=[args.state_dir])
    except (PolicyError, OSError) as exc:
        parser.error(f"Policy: {exc}")
    if args.env_file:
        from dotenv import load_dotenv

        load_dotenv(args.env_file, override=True)
    state_dir = args.state_dir.resolve()
    store = Store(state_dir / "state.sqlite")
    if args.action == "requeue":
        return 0 if store.requeue(args.key) else 1
    if args.action == "work":
        while (result := process_one(store, policy, state_dir)) is not None:
            print(json.dumps(result, default=str), flush=True)
            if args.once:
                break
        return 0
    secret = os.environ.get("CI_REPAIR_WEBHOOK_SECRET", "").encode()
    if len(secret) < 16:
        parser.error("Set CI_REPAIR_WEBHOOK_SECRET (at least 16 bytes) in the environment")
    wake, stop = threading.Event(), threading.Event()
    server = ThreadingHTTPServer((args.host, args.port), make_handler(store, secret, policy, wake))
    if not args.no_worker:
        threading.Thread(
            target=worker, args=(store, policy, state_dir, wake, stop), daemon=True
        ).start()
    print(f"Listening on http://{args.host}:{server.server_address[1]}/webhook", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        wake.set()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
