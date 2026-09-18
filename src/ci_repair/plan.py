"""Reviewable replay plans for simple, static GitHub Actions shell steps."""

import argparse
import hashlib
import json
import shlex
from pathlib import Path, PurePosixPath

import yaml

from ci_repair.github import load_context
from ci_repair.workspace import command


class WorkflowLoader(yaml.SafeLoader):
    """Ambiguous YAML keys must not silently replace earlier workflow settings."""

    def construct_mapping(self, node, deep=False):
        result = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in result:
                raise ValueError("Duplicate workflow key")
            result[key] = self.construct_object(value_node, deep=deep)
        return result


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def relative_directory(value: str) -> str:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or path.parts[:1] == (".git",):
        raise ValueError("Working directory must stay inside the repository")
    return str(path)


def clean_head(repo: Path) -> str:
    root = command(["git", "rev-parse", "--show-toplevel"], cwd=repo).decode().strip()
    if Path(root).resolve() != repo.resolve():
        raise ValueError("Expected repository root")
    if command(["git", "status", "--porcelain", "--untracked-files=all"], cwd=repo):
        raise ValueError("Repository must be clean")
    return command(["git", "rev-parse", "HEAD"], cwd=repo).decode().strip()


def workflow_bytes(repo: Path, sha: str, path: str) -> bytes:
    posix = PurePosixPath(path)
    if (
        posix.parts[:2] != (".github", "workflows")
        or len(posix.parts) != 3
        or posix.suffix not in (".yml", ".yaml")
    ):
        raise ValueError("Workflow must be a file under .github/workflows")
    raw = command(["git", "show", f"{sha}:{path}"], cwd=repo)
    if len(raw) > 256000:
        raise ValueError("Workflow exceeds the supported size")
    return raw


def draft(collection: Path, workflow: str | None = None) -> dict:
    repo = (collection / "repo").resolve()
    log = (collection / "failure.log").resolve()
    ci_path = (collection / "ci-context.json").resolve()
    sha = clean_head(repo)
    ci = load_context(ci_path, sha, log.read_bytes())
    metadata = json.loads(ci_path.read_text())
    recorded_workflow = metadata.get("workflow_path")
    if workflow and recorded_workflow and workflow != recorded_workflow:
        raise ValueError("Workflow path differs from the collected run")
    workflow = workflow or recorded_workflow
    if not workflow:
        raise ValueError("Older collections require an explicit --workflow path")
    raw = workflow_bytes(repo, sha, workflow)
    # YAML aliases and expressions require semantics outside this deliberately small subset.
    if any(isinstance(t, yaml.tokens.AliasToken) for t in yaml.scan(raw)):
        raise ValueError("Workflow aliases are not supported")
    doc = yaml.load(raw, Loader=WorkflowLoader)
    if not isinstance(doc, dict) or not isinstance(doc.get("jobs"), dict):
        raise ValueError("Expected workflow jobs")
    matches = [
        (key, job)
        for key, job in doc["jobs"].items()
        if isinstance(job, dict) and job.get("name", key) == ci["job_name"]
    ]
    if len(matches) != 1:
        raise ValueError("Cannot uniquely map collected job to a static workflow job")
    key, job = matches[0]
    steps = job.get("steps", [])
    if not isinstance(steps, list) or not all(isinstance(s, dict) for s in steps):
        raise ValueError("Expected a static step list")
    failed = [
        (i, s)
        for i, s in enumerate(steps)
        if s.get("name", "Run " + str(s.get("run", ""))) in ci["failed_steps"]
    ]
    if len(failed) != 1 or not isinstance(failed[0][1].get("run"), str):
        raise ValueError("Cannot uniquely map failed step to a shell command")
    index, step = failed[0]
    blockers = []
    for feature in (
        "strategy",
        "services",
        "uses",
        "needs",
        "container",
        "env",
        "environment",
        "if",
    ):
        if feature in job:
            blockers.append(f"job.{feature}")
    if "env" in doc:
        blockers.append("workflow.env")
    relevant = {"defaults": doc.get("defaults"), "job": job}
    if "${{" in json.dumps(relevant):
        blockers.append("dynamic expressions")
    if job.get("runs-on") not in ("ubuntu-latest", "ubuntu-22.04", "ubuntu-24.04"):
        blockers.append("nonstandard runner")
    for i, prior in enumerate(steps[: index + 1]):
        for feature in ("env", "if", "continue-on-error"):
            if feature in prior:
                blockers.append(f"step[{i + 1}].{feature}")
    defaults = {}
    for scope in (doc, job):
        defaults.update(scope.get("defaults", {}).get("run", {}))
    directory = relative_directory(
        step.get("working-directory", defaults.get("working-directory", "."))
    )
    shell = step.get("shell", defaults.get("shell"))
    if shell not in (None, "bash"):
        blockers.append("non-bash shell")
    return {
        "schema_version": 1,
        "reviewed": False,
        "unsupported": sorted(set(blockers)),
        "source": {
            "repo": str(repo),
            "commit": sha,
            "failure_log": str(log),
            "log_sha256": digest(log.read_bytes()),
            "ci_context": str(ci_path),
            "workflow_path": workflow,
            "workflow_sha256": digest(raw),
        },
        "selection": {"job_key": key, "step_number": index + 1, "step_name": step.get("name")},
        "evidence_untrusted": {
            "runner": job.get("runs-on"),
            "shell": shell or "bash (implicit -e)",
            "preceding_steps": steps[:index],
            "command_source": f"{workflow}:jobs.{key}.steps[{index}].run",
        },
        "execution": {
            "image": None,
            "working_directory": directory,
            "failing_command": step["run"],
            "regression_command": None,
            "allowed_paths": [],
        },
        "review_required": [
            "prepared image and preceding setup",
            "regression command",
            "allowed source paths",
            "command and directory",
        ],
    }


def load_plan(path: Path) -> dict:
    plan = json.loads(path.read_text())
    if plan.get("schema_version") != 1 or plan.get("reviewed") is not True:
        raise ValueError("Plan must be version 1 and explicitly reviewed")
    if plan.get("unsupported"):
        raise ValueError("Unsupported workflow features; use explicit CLI configuration")
    source, execution = plan["source"], plan["execution"]
    repo, log, ci = (Path(source[k]) for k in ("repo", "failure_log", "ci_context"))
    sha = clean_head(repo)
    if sha != source["commit"] or digest(log.read_bytes()) != source["log_sha256"]:
        raise ValueError("Plan source or log changed; generate a new plan")
    load_context(ci, sha, log.read_bytes())
    if digest(workflow_bytes(repo, sha, source["workflow_path"])) != source["workflow_sha256"]:
        raise ValueError("Workflow differs from the plan")
    # Recompute feature support rather than trusting an edited list of blockers.
    fresh = draft(ci.parent, source["workflow_path"])
    if fresh["unsupported"] or fresh["selection"] != plan["selection"]:
        raise ValueError("Plan no longer matches a supported workflow selection")
    if fresh["source"] != source:
        raise ValueError("Plan input paths differ from the collected source")
    for field in ("image", "failing_command", "regression_command", "working_directory"):
        if not isinstance(execution.get(field), str) or not execution[field].strip():
            raise ValueError(f"Plan requires {field}")
    allowed = execution.get("allowed_paths")
    if not isinstance(allowed, list) or not allowed or not all(isinstance(p, str) for p in allowed):
        raise ValueError("Plan requires explicit allowed_paths")
    directory = relative_directory(execution["working_directory"])

    def in_directory(script):
        shell = fresh["evidence_untrusted"]["shell"]
        flags = "--noprofile --norc -eo pipefail" if shell == "bash" else "-e"
        return f"cd -- {shlex.quote(directory)} && bash {flags} -c {shlex.quote(script)}"

    return {
        "repo": repo,
        "failure_log": log,
        "ci_context": ci,
        "image": execution["image"],
        "failing_command": in_directory(execution["failing_command"]),
        "regression_command": in_directory(execution["regression_command"]),
        "allowed_paths": tuple(allowed),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("collection", type=Path)
    parser.add_argument("--workflow", help="Required for older collections without workflow_path")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    plan = draft(args.collection, args.workflow)
    # Never overwrite an operator's reviewed plan.
    with args.output.open("x") as stream:
        args.output.chmod(0o600)
        stream.write(json.dumps(plan, indent=2) + "\n")
    print(f"Draft saved: {args.output}; review required before execution")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
