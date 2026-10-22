"""Run-level paths with containers and the agent replaced by a file-level fake.

Each replayed check is a predicate on the source tree, and each scripted "agent" edits the
candidate tree. Everything else is real: Git candidate state, reconstruction, ordering,
budgets, policy and the run report. tests/test_orchestrate.py covers the same flow on Docker.
"""

import json
import subprocess
import tarfile
import tempfile
from contextlib import contextmanager
from pathlib import Path

import pytest
from test_orchestrate import WORKFLOW, collection

from ci_repair import orchestrate
from ci_repair.orchestrate import repair_run
from ci_repair.pipeline import write_json
from ci_repair.policy import Policy

POLICY = {"repositories": [{"name": "owner/repo", "branches": ["main"], "allowed_paths": ["src/"]}]}


def value(tree: Path) -> int:
    return int((tree / "src/app.py").read_text().split("=")[1])


# A marker in each workflow step's script -> (job name, does the check pass on this tree?).
CHECKS = {
    "value == 1": ("lint", lambda tree: value(tree) == 1),
    "value > 0": ("unit", lambda tree: value(tree) > 0),
    "value == 0": ("zero", lambda tree: value(tree) == 0),
    "assert flag": ("other", lambda tree: "True" in (tree / "src/other.py").read_text()),
}


def check(script: str, tree: Path) -> dict:
    """A record shaped like pipeline.run_test's; a failure repeats the job's CI log line.

    A regression command chains several steps; it passes only if every step passes.
    """
    steps = [v for marker, v in CHECKS.items() if marker in script]
    assert steps, script
    failed = [job for job, passes in steps if not passes(tree)]
    return {
        "command": script,
        "returncode": 1 if failed else 0,
        "output": f"AssertionError in {failed[0]}" if failed else "ok",
        "exception_info": "",
    }


@contextmanager
def extracted(archive: Path):
    with tempfile.TemporaryDirectory() as directory:
        with tarfile.open(archive) as tar:
            tar.extractall(directory, filter="data")
        yield Path(directory)


def git(tree: Path, *args: str) -> bytes:
    return subprocess.run(["git", *args], cwd=tree, check=True, capture_output=True).stdout


def set_value(number: int):
    return lambda tree: (tree / "src/app.py").write_text(f"value = {number}\n")


def set_flag(tree: Path):
    (tree / "src/other.py").write_text("flag = True\n")


def no_change(tree: Path):
    pass


class World:
    """Fakes for the container-backed calls orchestrate makes; records what ran."""

    def __init__(self, *edits):
        self.edits = list(edits)
        self.agents = []  # cfg of every pipeline (agent) run, in order
        self.verified = []  # failing command of every fresh verification

    def model(self):
        return self.edits[len(self.agents)]

    def build(self, spec, archive, job_dir, policy):
        return {"status": "BUILT", "image_id": "sha256:fake"}

    @contextmanager
    def workspace(self, archive, image, *timeouts):
        with extracted(archive) as tree:
            yield tree

    def run_test(self, tree, script, path):
        record = check(script, tree)
        write_json(path, record)
        return record

    def verify_patch(self, cfg, archive, image, patch):
        """Fresh tree from the original snapshot + the patch, like pipeline.verify_patch."""
        self.verified.append(cfg.failing_command)
        with extracted(archive) as tree:
            if patch.read_bytes():
                git(tree, "apply", "--binary", str(patch))
            failing = check(cfg.failing_command, tree)
            if cfg.regression_command == cfg.failing_command:
                regression = {**failing, "same_as": "failing"}
            else:
                regression = check(cfg.regression_command, tree)
        write_json(cfg.output / "failing.json", failing)
        write_json(cfg.output / "regression.json", regression)
        verified = failing["returncode"] == regression["returncode"] == 0
        return {"status": "PASS" if verified else "FAIL", "verified": verified,
                "tests": [failing, regression]}  # fmt: skip

    def pipeline(self, cfg, edit, policy):
        """Baseline on the candidate (unless already shown), the scripted edit, then
        verification of its patch."""
        self.agents.append(cfg)
        cfg.output.mkdir(parents=True)
        usage = {"model_calls": 3, "estimated_cost_usd": 0.01, "agent_steps": 3,
                 "wall_seconds": 5.0, "models_used": ["fake/model"]}  # fmt: skip
        if not cfg.failure_reproduced:
            baseline = check(cfg.failing_command, cfg.repo)
            write_json(cfg.output / "baseline.json", baseline)
            if baseline["returncode"] == 0:
                return {"status": "BASELINE_NOT_REPRODUCED", "verified": False, "usage": usage}
        edit(cfg.repo)
        tests = [check(cfg.failing_command, cfg.repo), check(cfg.regression_command, cfg.repo)]
        patch = git(cfg.repo, "diff", "--binary")
        git(cfg.repo, "checkout", "--", ".")
        (cfg.output / "patch.diff").write_bytes(patch)
        if not patch:
            return {"status": "NO_PATCH", "verified": False, "stop_reason": "NO_PATCH",
                    "usage": usage}  # fmt: skip
        verified = all(t["returncode"] == 0 for t in tests)
        return {"status": "PASS" if verified else "FAIL", "verified": verified,
                "stop_reason": "VERIFIED_PASS" if verified else "VERIFICATION_FAILED",
                "tests": tests, "usage": usage}  # fmt: skip


def install(monkeypatch, *edits) -> World:
    fake = World(*edits)
    for name in ("workspace", "run_test", "verify_patch"):
        monkeypatch.setattr(orchestrate, name, getattr(fake, name))
    monkeypatch.setattr(orchestrate, "run_pipeline", fake.pipeline)
    return fake


@pytest.fixture
def world(monkeypatch):
    return lambda *edits: install(monkeypatch, *edits)


def run(tmp_path, fake, jobs, policy=None):
    return repair_run(
        collection(tmp_path, jobs=jobs),
        tmp_path / "out",
        model_factory=fake.model,
        policy=policy or Policy(POLICY),
        build=fake.build,
    )


def statuses(report):
    return {j["job_name"]: j["status"] for j in report["jobs"]}


def test_a_job_fixed_by_an_earlier_repair_starts_no_agent(tmp_path, world):
    fake = world(set_value(1))
    report = run(tmp_path, fake, (("lint", "check"), ("unit", "test")))
    assert report["status"] == "PASS" and report["verified"], report
    assert statuses(report) == {"lint": "REPAIRED", "unit": "FIXED_BY_PRIOR"}
    assert len(fake.agents) == 1
    # Each job keeps its own verification evidence; none of it is recomputed at the end.
    assert len(report["tests"]) == 4 and len(fake.verified) == 1
    assert report["changed_files"] == ["src/app.py"]
    assert report["policy_decision"]["verdict"] == "ALLOW"
    assert report["usage"]["model_calls"] == 3 and report["usage"]["models_used"] == ["fake/model"]
    assert json.loads((tmp_path / "out/report.json").read_text()) == report
    assert "value = 1" in (tmp_path / "out/patch.diff").read_text()


def test_independent_jobs_accumulate_one_patch(tmp_path, world, caplog):
    fake = world(set_value(1), set_flag)
    with caplog.at_level("INFO", logger="ci_repair"):
        report = run(tmp_path, fake, (("lint", "check"), ("other", "test")))
    assert sum("repair agent started" in m for m in caplog.messages) == 2
    assert any(m.endswith(": REPAIRED") for m in caplog.messages)
    assert report["status"] == "PASS", report
    assert statuses(report) == {"lint": "REPAIRED", "other": "REPAIRED"}
    assert sorted(report["changed_files"]) == ["src/app.py", "src/other.py"]
    # The second agent sees the first repair, both in its tree and in its task.
    assert "earlier jobs in this run: lint" in fake.agents[1].task
    # other: checked against lint's repair first; then both jobs are verified on the final
    # patch, because neither job's earlier evidence came from the final tree.
    assert ["flag" in c for c in fake.verified] == [True, False, True]


def test_a_failed_fix_up_leaves_the_job_regressed(tmp_path, world):
    def break_lint(tree):
        set_flag(tree)
        set_value(2)(tree)

    fake = world(set_value(1), break_lint, no_change)
    report = run(tmp_path, fake, (("lint", "check"), ("other", "test")))
    jobs = {j["job_name"]: j for j in report["jobs"]}
    assert report["status"] == "FAIL" and report["stop_reason"] == "VERIFICATION_FAILED"
    assert jobs["lint"]["status"] == "REGRESSED"
    assert jobs["lint"]["fixup"] == {
        "repair_status": "NO_PATCH",
        "stop_reason": "NO_PATCH",
        "verified": False,
    }
    # The fix-up was given the job's current failure, not the original CI log.
    assert fake.agents[2].failure_log.read_text() == "AssertionError in lint"
    assert jobs["other"]["final_verification"] == "PASS"


def test_a_successful_fix_up_reverifies_every_job(tmp_path, world):
    def break_lint(tree):
        set_flag(tree)
        set_value(2)(tree)

    fake = world(set_value(1), break_lint, set_value(1))
    report = run(tmp_path, fake, (("lint", "check"), ("other", "test")))
    assert report["status"] == "PASS", report
    assert statuses(report) == {"lint": "REPAIRED", "other": "REPAIRED"}
    assert report["usage"]["model_calls"] == 9
    assert (tmp_path / "out/verification-after-fixup/1/failing.json").exists()
    assert (tmp_path / "out/verification-after-fixup/2/failing.json").exists()


def test_a_job_the_agent_cannot_repair_makes_the_run_partial(tmp_path, world):
    fake = world(no_change, set_value(2))
    report = run(tmp_path, fake, (("lint", "check"), ("unit", "test")))
    assert statuses(report) == {"lint": "REPAIR_FAILED", "unit": "REPAIRED"}
    assert report["status"] == "PARTIAL" and not report["verified"]
    assert report["stop_reason"] == "NO_PATCH"


def test_a_later_repair_can_fix_a_job_whose_own_attempt_failed(tmp_path, world):
    # Round 4, task 26: the first job's agent failed; the second job's repair fixed both.
    fake = world(no_change, set_value(1))
    report = run(tmp_path, fake, (("lint", "check"), ("unit", "test")))
    assert statuses(report) == {"lint": "FIXED_BY_LATER", "unit": "REPAIRED"}
    assert report["status"] == "PASS" and report["verified"], report
    lint = report["jobs"][0]
    assert lint["repair_status"] == "NO_PATCH" and lint["final_verification"] == "PASS"
    assert len(report["tests"]) == 4


def test_a_job_whose_failure_never_reproduced_is_not_credited_to_a_later_repair(
    tmp_path, world, monkeypatch
):
    fake = world(no_change, set_value(1))
    pipeline = fake.pipeline

    def provider_error_first(cfg, edit, policy):
        if not fake.agents:  # the first session fails before its baseline finishes
            fake.agents.append(cfg)
            cfg.output.mkdir(parents=True)
            return {"status": "ERROR", "verified": False, "stop_reason": "EXECUTION_ERROR"}
        return pipeline(cfg, edit, policy)

    monkeypatch.setattr(orchestrate, "run_pipeline", provider_error_first)
    report = run(tmp_path, fake, (("lint", "check"), ("unit", "test")))
    assert statuses(report) == {"lint": "REPAIR_FAILED", "unit": "REPAIRED"}
    assert report["status"] == "PARTIAL"


def test_jobs_beyond_the_job_budget_are_skipped(tmp_path, world):
    fake = world(set_value(1))
    policy = Policy({**POLICY, "repair": {"max_jobs": 1}})
    report = run(tmp_path, fake, (("lint", "check"), ("other", "test")), policy)
    assert statuses(report) == {"lint": "REPAIRED", "other": "SKIPPED_BUDGET"}
    assert report["status"] == "PARTIAL" and report["stop_reason"] == "SKIPPED_BUDGET"
    assert len(fake.agents) == 1


def test_failed_setup_is_an_unsupported_environment(tmp_path, world):
    fake = world()
    fake.build = lambda *args: {"status": "SETUP_FAILED", "image_id": None}
    report = run(tmp_path, fake, (("lint", "check"),))
    assert report["status"] == "UNSUPPORTED_ENVIRONMENT"
    assert report["jobs"][0]["reasons"] == ["setup steps failed"]
    assert not fake.agents and "patch_sha256" in report


ZERO_JOB = """  zero:
    runs-on: ubuntu-latest
    container: ci-repair-demo:local
    steps:
      - uses: actions/checkout@v4
      - name: test
        run: python -c 'from src.app import value; assert value == 0'
"""


def test_a_job_passing_on_the_original_source_is_not_repaired(tmp_path, world):
    fake = world(no_change)
    root = collection(tmp_path, WORKFLOW + ZERO_JOB, jobs=(("zero", "test"),))
    report = repair_run(
        root, tmp_path / "out", model_factory=fake.model, policy=Policy(POLICY), build=fake.build
    )
    assert statuses(report) == {"zero": "BASELINE_NOT_REPRODUCED"}
    assert report["status"] == "FAIL" and report["stop_reason"] == "BASELINE_NOT_REPRODUCED"


def test_earlier_repairs_are_credited_only_for_jobs_failing_on_the_original(tmp_path, world):
    fake = world(set_value(1))
    root = collection(tmp_path, WORKFLOW + ZERO_JOB, jobs=(("lint", "check"), ("zero", "test")))
    report = repair_run(
        root, tmp_path / "out", model_factory=fake.model, policy=Policy(POLICY), build=fake.build
    )
    assert statuses(report) == {"lint": "REPAIRED", "zero": "BASELINE_NOT_REPRODUCED"}
    assert len(fake.agents) == 1
    assert report["status"] == "PARTIAL"


def test_errors_are_reported_without_their_message(tmp_path, world):
    fake = world()

    def build(*args):
        raise RuntimeError("token=secret-value")

    fake.build = build
    report = run(tmp_path, fake, (("lint", "check"),))
    assert report["status"] == "ERROR" and report["error_type"] == "RuntimeError"
    assert "secret-value" not in (tmp_path / "out/report.json").read_text()
    # The traceback stays next to the report for the operator, credentials masked.
    assert report["error_log"] == "error.log"
    private = (tmp_path / "out/error.log").read_text()
    assert "RuntimeError" in private and "secret-value" not in private


BOTH_JOB = """  both:
    runs-on: ubuntu-latest
    container: ci-repair-demo:local
    steps:
      - uses: actions/checkout@v4
      - name: first
        run: python -c 'from src.app import value; assert value > 0'
      - name: second
        run: python -c 'from src.other import flag; assert flag'
"""


def test_a_job_failing_only_in_a_later_step_after_earlier_repairs_is_repaired(tmp_path, world):
    # lint's repair makes both's failed step pass, but both's next step still fails. The
    # job failed on the original source, so its agent must run, not end unreproduced.
    fake = world(set_value(1), set_flag)
    root = collection(tmp_path, WORKFLOW + BOTH_JOB, jobs=(("lint", "check"), ("both", "first")))
    report = repair_run(
        root, tmp_path / "out", model_factory=fake.model, policy=Policy(POLICY), build=fake.build
    )
    assert statuses(report) == {"lint": "REPAIRED", "both": "REPAIRED"}, report
    assert report["status"] == "PASS"
    assert [a.failure_reproduced for a in fake.agents] == [False, True]
