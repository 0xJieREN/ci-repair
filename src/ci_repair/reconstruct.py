"""Reconstruct a failed GitHub Actions job's replay environment from pinned evidence.

Inputs are the workflow at the failing commit, API job metadata (name, step conclusions,
runner labels) and the job log. All are untrusted: they select among a small, explicit
supported subset of GitHub Actions semantics and are never executed on the host. Anything
outside that subset yields UNSUPPORTED (or REVIEW_REQUIRED for known fidelity gaps) with
reasons, instead of a guessed execution.
"""

import hashlib
import itertools
import json
import re
import shlex
import subprocess
import time
from pathlib import Path

import yaml

from ci_repair.plan import WorkflowLoader, relative_directory, workflow_bytes
from ci_repair.policy import Policy, Verdict

SUPPORTED, REVIEW, UNSUPPORTED = "SUPPORTED", "REVIEW_REQUIRED", "UNSUPPORTED"
EXPR = re.compile(r"\$\{\{\s*(.*?)\s*\}\}", re.DOTALL)
RUNNERS = {
    "ubuntu-latest": ("ubuntu-24.04", "x64"),
    "ubuntu-24.04": ("ubuntu-24.04", "x64"),
    "ubuntu-22.04": ("ubuntu-22.04", "x64"),
    "ubuntu-24.04-arm": ("ubuntu-24.04", "arm64"),
    "ubuntu-22.04-arm": ("ubuntu-22.04", "arm64"),
}
# Approximate hosted-runner bases when no toolchain action or job container pins one.
RUNNER_BASES = {"ubuntu-24.04": "buildpack-deps:noble", "ubuntu-22.04": "buildpack-deps:jammy"}
TOOLCHAIN_IMAGES = {
    "python": "python:{version}-bookworm",
    "node": "node:{version}-bookworm",
    "go": "golang:{version}-bookworm",
}
JOB_KEYS = {
    "name",
    "runs-on",
    "steps",
    "strategy",
    "env",
    "defaults",
    "container",
    "services",
    "if",
    "needs",
    "outputs",
    "permissions",
    "concurrency",
    "timeout-minutes",
    "continue-on-error",
    "environment",
    "uses",
    "with",
    "secrets",
}
STEP_KEYS = {
    "id",
    "name",
    "uses",
    "run",
    "with",
    "env",
    "if",
    "shell",
    "working-directory",
    "continue-on-error",
    "timeout-minutes",
}
NOOP_ACTIONS = {"actions/cache", "actions/upload-artifact"}
CHECKOUT_INPUTS = {
    "fetch-depth",
    "persist-credentials",
    "clean",
    "fetch-tags",
    "show-progress",
    "lfs",
    "submodules",
    "set-safe-directory",
}
STATE_FILES = re.compile(r"\b(GITHUB_ENV|GITHUB_PATH|GITHUB_OUTPUT|GITHUB_STATE)\b")
API_FRAME_STEPS = {"Set up job", "Complete job", "Initialize containers", "Stop containers"}


class Problems:
    """Collect every blocker and review reason, not only the first."""

    def __init__(self):
        self.blockers: list[str] = []
        self.reviews: list[str] = []

    def block(self, reason: str):
        if reason not in self.blockers:
            self.blockers.append(reason)

    def review(self, reason: str):
        if reason not in self.reviews:
            self.reviews.append(reason)

    def status(self) -> str:
        return UNSUPPORTED if self.blockers else REVIEW if self.reviews else SUPPORTED


def scalar(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return "" if value is None else str(value)


def render(value, ctx: dict, problems: Problems, where: str):
    """Evaluate the small expression subset; anything else blocks reconstruction."""
    if isinstance(value, dict):
        return {k: render(v, ctx, problems, where) for k, v in value.items()}
    if isinstance(value, list):
        return [render(v, ctx, problems, where) for v in value]
    if not isinstance(value, str):
        return value

    def substitute(match):
        expr = match.group(1)
        if re.fullmatch(r"secrets\.[\w-]+|github\.token", expr):
            problems.block(f"{where}: secrets are unavailable to repairs")
            return ""
        scoped = re.fullmatch(r"(matrix|env)\.([A-Za-z_][\w-]*)", expr)
        if scoped:
            scope = ctx[scoped[1]]
            if scoped[2] not in scope:
                problems.block(f"{where}: undefined {expr}")
                return ""
            item = scope[scoped[2]]
            if isinstance(item, (dict, list)):
                problems.block(f"{where}: non-scalar {expr}")
                return ""
            return scalar(item)
        if expr in ctx["literals"]:
            return ctx["literals"][expr]
        problems.block(f"{where}: unsupported expression {expr[:60]!r}")
        return ""

    return EXPR.sub(substitute, value)


def expand_matrix(matrix, problems: Problems) -> list[dict]:
    if matrix is None:
        return [{}]
    if not isinstance(matrix, dict):
        problems.block("strategy.matrix must be static")
        return []
    base = {k: v for k, v in matrix.items() if k not in ("include", "exclude")}
    if not all(
        isinstance(v, list) and all(not isinstance(i, (dict, list)) for i in v)
        for v in base.values()
    ):
        problems.block("strategy.matrix values must be static scalar lists")
        return []
    combos = (
        [dict(zip(base, values)) for values in itertools.product(*base.values())] if base else []
    )
    for entry in matrix.get("exclude", []) or []:
        combos = [c for c in combos if not all(c.get(k) == v for k, v in entry.items())]
    originals = [dict(c) for c in combos]
    for entry in matrix.get("include", []) or []:
        if not isinstance(entry, dict):
            problems.block("strategy.matrix.include must list mappings")
            return []
        # GitHub: extend every original combination whose original values are not
        # overwritten; otherwise the entry becomes a new combination.
        added = False
        for combo, original in zip(combos, originals):
            if all(original.get(k, v) == v for k, v in entry.items()):
                combo.update(entry)
                added = True
        if not added:
            combos.append(dict(entry))
    return combos


def display_name(step: dict, rendered_name: str | None) -> str:
    if rendered_name:
        return rendered_name
    if "uses" in step:
        return f"Run {step['uses']}"
    lines = [line for line in str(step.get("run", "")).splitlines() if line.strip()]
    return f"Run {lines[0].strip()}" if lines else "Run"


def map_api_steps(displays: list[str], api_steps: list[dict]) -> dict[int, dict]:
    """Ordered match of workflow steps to API steps (which add frame steps).

    Returns the matched prefix; steps after the first unmatched one stay unknown.
    """
    api = [
        s
        for s in api_steps
        if s.get("name") not in API_FRAME_STEPS
        and not str(s.get("name", "")).startswith(("Post ", "Pre "))
    ]
    mapping = {}
    cursor = 0
    for index, display in enumerate(displays):
        while cursor < len(api):
            name = str(api[cursor].get("name", ""))
            cursor += 1
            if name == display or (len(name) >= 12 and display.startswith(name.rstrip(". "))):
                mapping[index] = api[cursor - 1]
                break
        else:
            break
    return mapping


def version_from_file(repo_files: dict, path: str, problems: Problems, where: str) -> str | None:
    text = repo_files.get(path)
    if text is None:
        problems.block(f"{where}: version file {path} not found")
        return None
    if path.endswith("go.mod"):
        match = re.search(r"^go\s+(\d+\.\d+(?:\.\d+)?)\s*$", text, re.MULTILINE)
    else:
        match = re.fullmatch(r"\s*v?(\d+(?:\.\d+){0,2})\s*", text)
    if not match:
        problems.block(f"{where}: unrecognized version in {path}")
        return None
    return match.group(1)


def image_version(version: str, problems: Problems, where: str) -> str | None:
    if re.fullmatch(r"\d+(?:\.\d+){0,2}", version):
        return version
    if re.fullmatch(r"\d+(?:\.x)+", version):
        return version.split(".x")[0]
    if version in ("lts/*", "lts"):
        return "lts"
    problems.block(f"{where}: unsupported version specifier {version!r}")
    return None


def translate_action(
    step: dict, inputs: dict, repo_files: dict, problems: Problems, where: str
) -> dict:
    """Map an allowlisted action to a toolchain choice and/or a setup script."""
    uses = step["uses"]
    name, _, ref = uses.partition("@")
    if not ref or name.startswith(("./", "docker://")) or name.count("/") < 1:
        problems.block(f"{where}: unsupported action {uses}")
        return {}
    if name == "actions/checkout":
        unknown = set(inputs) - CHECKOUT_INPUTS
        if unknown:
            problems.block(f"{where}: checkout inputs {sorted(unknown)} are not reproduced")
        if scalar(inputs.get("submodules", "false")) not in ("false", ""):
            problems.block(f"{where}: submodules are not supported")
        if scalar(inputs.get("lfs", "false")) not in ("false", ""):
            problems.block(f"{where}: Git LFS is not supported")
        if scalar(inputs.get("fetch-depth", "1")) != "1":
            problems.review(f"{where}: full Git history is not reproduced")
        return {"checkout": True}
    if name in NOOP_ACTIONS:
        return {}
    tool = {
        "actions/setup-python": "python",
        "actions/setup-node": "node",
        "actions/setup-go": "go",
    }.get(name)
    if tool:
        key = f"{tool}-version"
        version = scalar(inputs.get(key)) or None
        if version is None and inputs.get(f"{key}-file"):
            version = version_from_file(repo_files, scalar(inputs[f"{key}-file"]), problems, where)
        elif version is None and tool == "python" and ".python-version" in repo_files:
            version = version_from_file(repo_files, ".python-version", problems, where)
        if version is None:
            problems.block(f"{where}: {name} requires a static version")
            return {}
        if tool == "python" and "architecture" in inputs:
            problems.block(f"{where}: explicit Python architecture is not supported")
        tag = image_version(version, problems, where)
        return {"toolchain": (tool, tag)} if tag else {}
    if name == "astral-sh/setup-uv":
        version = scalar(inputs.get("version", ""))
        if version and not re.fullmatch(r"\d+\.\d+\.\d+", version):
            problems.block(f"{where}: setup-uv version must be exact")
            return {}
        spec = f"uv=={version}" if version else "uv"
        if not version:
            problems.review(f"{where}: uv version is not pinned; recorded after build")
        return {
            "script": f"python3 -m pip install --quiet --root-user-action=ignore {shlex.quote(spec)}"
        }
    if name == "pnpm/action-setup":
        version = scalar(inputs.get("version", ""))
        if not re.fullmatch(r"\d+(?:\.\d+){0,2}", version):
            problems.block(f"{where}: pnpm/action-setup requires a static version")
            return {}
        return {"script": f"npm install --global --silent pnpm@{shlex.quote(version)}"}
    problems.block(f"{where}: action {name} is outside the supported subset")
    return {}


def step_command(step: dict) -> str:
    """One Actions step as a self-contained shell command (each step is a fresh shell)."""
    flags = {
        "bash": "bash --noprofile --norc -eo pipefail -c",
        "sh": "sh -e -c",
        None: "bash -e -c",
    }
    shell = flags[step["shell"]]
    env = " ".join(f"{k}={shlex.quote(v)}" for k, v in sorted(step["env"].items()))
    run = f"{shell} {shlex.quote(step['script'])}"
    body = f"env {env} {run}" if env else run
    if step["working_directory"] != ".":
        body = f"cd -- {shlex.quote(step['working_directory'])} && {body}"
    command = f"( {body} )"
    # Tolerate the child shell's exit, without disabling its own errexit behavior.
    return f"{command} || true" if step.get("continue_on_error") else command


def replay_commands(spec: dict) -> tuple[str, str]:
    failing = step_command(spec["failing"])
    later = spec["regression"]
    regression = " && ".join(step_command(s) for s in later) if later else failing
    return failing, regression


def parse_log(log: str) -> dict:
    """Untrusted provenance hints from the job log header; never used to select commands."""
    clean = re.sub(r"^﻿?\d{4}-\d\d-\d\dT[\d:.]+Z ", "", log, flags=re.MULTILINE)
    result = {}
    image = re.search(r"^Image: (\S+)\s*\nVersion: (\S+)", clean, re.MULTILINE)
    if image:
        result["runner_image"] = image[1][:40]
        result["runner_image_version"] = image[2][:40]
    result["actions"] = dict(
        re.findall(r"Download action repository '([^']{1,120})' \(SHA:([0-9a-f]{40})\)", clean)[:20]
    )
    versions = re.findall(r"Successfully set up (CPython|PyPy) \(([\w.+-]{1,20})\)", clean)
    if versions:
        result["python"] = versions[-1][1]
    return result


def load_workflow(repo: Path, sha: str, path: str) -> tuple[dict, bytes]:
    raw = workflow_bytes(repo, sha, path)
    if any(isinstance(t, yaml.tokens.AliasToken) for t in yaml.scan(raw)):
        raise ValueError("Workflow aliases are not supported")
    doc = yaml.load(raw, Loader=WorkflowLoader)
    if not isinstance(doc, dict) or not isinstance(doc.get("jobs"), dict):
        raise ValueError("Expected workflow jobs")
    return doc, raw


def select_job(doc: dict, job_name: str, ctx_base: dict, problems: Problems):
    """Find the unique (job key, matrix combination) whose rendered name is job_name."""
    candidates = []
    for key, job in doc["jobs"].items():
        if not isinstance(job, dict):
            continue
        strategy = job.get("strategy") or {}
        scratch = Problems()
        combos = expand_matrix(
            strategy.get("matrix") if isinstance(strategy, dict) else None, scratch
        )
        for combo in combos or [{}]:
            ctx = {**ctx_base, "matrix": combo, "env": {}}
            if "name" in job:
                name = render(job["name"], ctx, Problems(), "job.name")
            elif combo:
                name = f"{key} ({', '.join(scalar(v) for v in combo.values())})"
            else:
                name = key
            if name == job_name:
                candidates.append((key, job, combo, scratch))
    if len(candidates) != 1:
        problems.block(
            f"cannot uniquely map job {job_name!r} to the workflow ({len(candidates)} matches)"
        )
        return None
    key, job, combo, scratch = candidates[0]
    for reason in scratch.blockers:
        problems.block(reason)
    return key, job, combo


def reconstruct(
    repo: Path,
    sha: str,
    workflow_path: str,
    job_meta: dict,
    log: str,
    policy: Policy,
    *,
    repository: str = "",
    event: str = "",
) -> dict:
    """Derive a replay specification without executing anything."""
    problems = Problems()
    doc, raw = load_workflow(repo, sha, workflow_path)
    literals = {
        "runner.os": "Linux",
        "github.sha": sha,
        "github.repository": repository,
        "github.workspace": "/workspace",
        "github.event_name": event,
    }
    spec = {
        "schema_version": 1,
        "source": {
            "commit": sha,
            "workflow_path": workflow_path,
            "workflow_sha256": hashlib.sha256(raw).hexdigest(),
            "job_name": job_meta["name"],
        },
        "log_provenance_untrusted": parse_log(log),
    }
    selected = select_job(doc, job_meta["name"], {"literals": literals}, problems)
    if selected is None:
        return finish(spec, problems)
    key, job, combo = selected
    spec["source"].update(job_key=key, matrix=combo)
    for unknown in sorted(set(job) - JOB_KEYS):
        problems.block(f"job.{unknown} is not supported")
    for feature in ("uses", "services", "environment", "secrets", "with"):
        if feature in job:
            problems.block(f"job.{feature} is not supported")
    runs_on = render(
        job.get("runs-on"), {"literals": literals, "matrix": combo, "env": {}}, problems, "runs-on"
    )
    labels = runs_on if isinstance(runs_on, list) else [runs_on]
    runner = RUNNERS.get(labels[0]) if len(labels) == 1 and isinstance(labels[0], str) else None
    if runner is None:
        problems.block(f"runner {labels!r} is outside the supported hosted Ubuntu labels")
        runner = ("ubuntu-24.04", "x64")
    hint = spec["log_provenance_untrusted"].get("runner_image")
    if labels[0] == "ubuntu-latest" and hint in ("ubuntu-22.04", "ubuntu-24.04"):
        runner = (hint, runner[1])  # the log says which image ubuntu-latest resolved to
    literals["runner.arch"] = "ARM64" if runner[1] == "arm64" else "X64"
    spec["runner"] = {"labels": labels, "image": runner[0], "arch": runner[1]}

    ctx = {"literals": literals, "matrix": combo, "env": {}}
    env = {}
    for scope, where in ((doc.get("env"), "workflow.env"), (job.get("env"), "job.env")):
        if scope is None:
            continue
        if not isinstance(scope, dict):
            problems.block(f"{where} must be a static mapping")
            continue
        ctx["env"] = dict(env)
        env.update({str(k): scalar(render(v, ctx, problems, where)) for k, v in scope.items()})
    defaults = {}
    for scope in (doc, job):
        run_defaults = (scope.get("defaults") or {}).get("run", {})
        defaults.update(render(run_defaults, ctx, problems, "defaults.run"))
    base_env = {
        "CI": "true",
        "GITHUB_ACTIONS": "true",
        "RUNNER_OS": "Linux",
        "RUNNER_ARCH": literals["runner.arch"],
        "GITHUB_WORKSPACE": "/workspace",
        "GITHUB_SHA": sha,
        **({"GITHUB_REPOSITORY": repository} if repository else {}),
    }

    container = job.get("container")
    if isinstance(container, dict):
        extra = set(container) - {"image", "env"}
        if extra:
            problems.block(f"job.container {sorted(extra)} are not supported")
        env.update({str(k): scalar(v) for k, v in (container.get("env") or {}).items()})
        container = container.get("image")
    if container is not None:
        container = render(container, ctx, problems, "job.container")
        if not isinstance(container, str) or not re.fullmatch(r"[\w./:@-]{1,200}", container):
            problems.block("job.container must be a static image reference")
            container = None
        # Actions defaults to sh for run steps inside a job container.
        defaults.setdefault("shell", "sh")

    steps = job.get("steps")
    if not isinstance(steps, list) or not all(isinstance(s, dict) for s in steps):
        problems.block("expected a static step list")
        return finish(spec, problems)
    names = [
        render(s.get("name"), ctx, Problems(), "step.name") if "name" in s else None for s in steps
    ]
    displays = [display_name(s, n) for s, n in zip(steps, names)]
    api = map_api_steps(displays, job_meta.get("steps") or [])
    failed_names = set(job_meta.get("failed_steps") or [])
    failed = [i for i, s in api.items() if s.get("conclusion") == "failure"]
    if not failed:
        failed = [i for i, d in enumerate(displays) if d in failed_names]
    if len(failed) != 1:
        problems.block("cannot uniquely identify the failed step")
        return finish(spec, problems)
    failing_index = failed[0]
    spec["source"].update(step_number=failing_index + 1, step_name=displays[failing_index])

    toolchains, setup, later = [], [], []
    repo_files = read_version_files(repo, sha)
    checkout_seen = False
    for index, step in enumerate(steps):
        where = f"steps[{index + 1}]"
        after = index > failing_index
        # A later step only has to be replayable to serve as a regression check.
        local = Problems()
        kind, record = translate_step(
            step,
            where,
            index == failing_index,
            after,
            ctx,
            env,
            base_env,
            defaults,
            api,
            index,
            repo_files,
            local,
        )
        if after and local.blockers:
            problems.review(f"{where}: not replayed ({local.blockers[0]})")
            continue
        for reason in local.blockers:
            problems.block(reason)
        for reason in local.reviews:
            problems.review(reason)
        if kind == "toolchain":
            toolchains.append(record)
        elif kind == "checkout":
            if checkout_seen:
                problems.block(f"{where}: multiple checkouts are not supported")
            checkout_seen = True
        elif kind == "shell":
            if index < failing_index:
                setup.append(record)
            elif index == failing_index:
                spec["failing"] = record
            else:
                later.append(record)
    if not checkout_seen:
        problems.block("job does not use actions/checkout; source layout is unknown")

    distinct = {tool for tool, _ in toolchains}
    if container:
        base, fidelity = container, "job-container"
        if toolchains:
            problems.review(
                "setup actions inside a job container are approximated by the container"
            )
    elif len(distinct) > 1:
        problems.block(f"multiple language toolchains {sorted(distinct)} are not supported")
        base, fidelity = None, "unsupported"
    elif toolchains:
        tool, tag = toolchains[-1]
        base, fidelity = TOOLCHAIN_IMAGES[tool].format(version=tag), "toolchain-image"
        spec["toolchain"] = {tool: tag}
    else:
        base, fidelity = RUNNER_BASES[runner[0]], "approximate-runner"
        problems.review("hosted-runner preinstalled software is approximated, not reproduced")
    spec.update(
        base_image=base,
        fidelity=fidelity,
        platform=f"linux/{'arm64' if runner[1] == 'arm64' else 'amd64'}",
        setup=setup,
        regression=later,
    )
    if Verdict(policy.data["sandbox"]["setup_network"]) is not Verdict.ALLOW and setup:
        problems.review("setup steps need network, which policy does not allow automatically")
    return finish(spec, problems)


def translate_step(
    step, where, failing, after, ctx, env, base_env, defaults, api, index, repo_files, problems
):
    """Return (kind, record): kind is skip, toolchain, checkout or shell."""
    for unknown in sorted(set(step) - STEP_KEYS):
        problems.block(f"{where}.{unknown} is not supported")
    if "if" in step:
        conclusion = api.get(index, {}).get("conclusion")
        if after:
            problems.block("conditional step after the failure")
        elif conclusion == "skipped":
            return "skip", None
        elif conclusion not in ("success", "failure"):
            problems.block(f"{where}: cannot determine whether the conditional step ran")
    if after and step.get("continue-on-error") not in (None, False):
        problems.block("continue-on-error after the failure")
    step_env = dict(env)
    if step.get("env") is not None:
        if not isinstance(step["env"], dict):
            problems.block(f"{where}.env must be a static mapping")
        else:
            ctx["env"] = dict(step_env)
            step_env.update(
                {
                    str(k): scalar(render(v, ctx, problems, f"{where}.env"))
                    for k, v in step["env"].items()
                }
            )
    ctx["env"] = step_env
    if "uses" in step:
        if after:
            if step["uses"].partition("@")[0] not in NOOP_ACTIONS:
                problems.block(f"action {step['uses']}")
            return "skip", None
        inputs = render(step.get("with") or {}, ctx, problems, f"{where}.with")
        action = translate_action(step, inputs, repo_files, problems, where)
        if "toolchain" in action:
            return "toolchain", action["toolchain"]
        if action.get("checkout"):
            return "checkout", None
        if "script" in action:
            return "shell", shell_step(
                action["script"], ".", "sh", {**base_env, **step_env}, f"{where}.uses"
            )
        return "skip", None
    if not isinstance(step.get("run"), str):
        problems.block(f"{where}: expected run or uses")
        return "skip", None
    script = render(step["run"], ctx, problems, f"{where}.run")
    if STATE_FILES.search(script):
        problems.block(f"{where}: GITHUB_ENV/PATH/OUTPUT/STATE propagation is not supported")
    shell = step.get("shell", defaults.get("shell"))
    if shell not in (None, "bash", "sh"):
        problems.block(f"{where}: shell {shell!r} is not supported")
        shell = None
    try:
        directory = relative_directory(
            scalar(
                render(
                    step.get("working-directory", defaults.get("working-directory", ".")),
                    ctx,
                    problems,
                    where,
                )
            )
        )
    except ValueError:
        problems.block(f"{where}: working directory escapes the repository")
        directory = "."
    tolerate_failure = step.get("continue-on-error") not in (None, False)
    if tolerate_failure:
        if failing:
            problems.block(f"{where}: continue-on-error on the failing step")
        if step["continue-on-error"] is not True:
            problems.block(f"{where}: continue-on-error must be a static boolean")
    record = shell_step(script, directory, shell, {**base_env, **step_env}, where)
    if tolerate_failure:
        record["continue_on_error"] = True
    return "shell", record


def shell_step(script: str, directory: str, shell, env: dict, source: str) -> dict:
    return {
        "source": source,
        "script": script,
        "working_directory": directory,
        "shell": shell,
        "env": env,
    }


def finish(spec: dict, problems: Problems) -> dict:
    spec["status"] = problems.status()
    spec["unsupported"] = problems.blockers
    spec["review_reasons"] = problems.reviews
    body = {k: v for k, v in spec.items() if k != "spec_sha256"}
    spec["spec_sha256"] = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
    return spec


def read_version_files(repo: Path, sha: str) -> dict:
    files = {}
    for path in (".python-version", ".nvmrc", ".node-version", "go.mod"):
        result = subprocess.run(
            ["git", "show", f"{sha}:{path}"], cwd=repo, capture_output=True, timeout=30
        )
        if result.returncode == 0 and len(result.stdout) < 65536:
            files[path] = result.stdout.decode(errors="replace")
    return files


BOOTSTRAP = """set -e
missing=""
for tool in bash git timeout tar; do command -v "$tool" >/dev/null 2>&1 || missing="$missing $tool"; done
if [ -n "$missing" ]; then
  if command -v apt-get >/dev/null 2>&1; then
    apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends bash git coreutils tar ca-certificates >/dev/null
  elif command -v apk >/dev/null 2>&1; then
    apk add --no-cache bash git coreutils tar >/dev/null
  else
    echo "replay image lacks:$missing" >&2; exit 90
  fi
fi
mkdir -p /workspace && tar -xf /tmp/source.tar -C /workspace && rm -f /tmp/source.tar
"""
PROBE = (
    "for t in python3 node go uv pnpm npm; do command -v $t >/dev/null 2>&1 && "
    'printf "%s=%s\\n" "$t" "$($t --version 2>&1 | head -n1)"; done; true'
)


def docker(
    args: list[str], *, timeout: int = 120, check: bool = True
) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, timeout=timeout, check=check)


def build_environment(spec: dict, archive: Path, output: Path, policy: Policy) -> dict:
    """Run allowlisted setup once in a disposable container and commit the replay image.

    Setup is the only phase that may use the network (policy sandbox.setup_network); it
    receives no credentials or host mounts. Repair and verification later run offline.
    """
    if spec["status"] == UNSUPPORTED:
        raise ValueError("Cannot build an unsupported environment")
    tag = f"ci-repair-env:{spec['spec_sha256'][:16]}"
    provenance = {"tag": tag, "spec_sha256": spec["spec_sha256"], "base_image": spec["base_image"]}
    existing = docker(["image", "inspect", "--format={{.Id}}", tag], check=False)
    if existing.returncode == 0:
        # Content-addressed by spec (source SHA, workflow hash, base, setup): reuse is exact.
        cached = json.loads(
            docker(["image", "inspect", "--format={{json .Config.Labels}}", tag]).stdout or b"{}"
        )
        provenance.update(json.loads(cached.get("ci-repair.provenance", "{}")))
        provenance.update(image_id=existing.stdout.decode().strip(), reused=True)
        return provenance
    base = spec["base_image"]
    if docker(["image", "inspect", base], check=False).returncode != 0:
        docker(["pull", "--platform", spec["platform"], base], timeout=900)
    info = json.loads(docker(["image", "inspect", base]).stdout)[0]
    provenance.update(
        base_image_id=info["Id"],
        base_image_digests=info.get("RepoDigests", []),
        platform=f"{info.get('Os')}/{info.get('Architecture')}",
    )
    arch = {"amd64": "x64", "arm64": "arm64"}.get(
        info.get("Architecture"), info.get("Architecture")
    )
    if arch != spec["runner"]["arch"]:
        provenance["architecture_mismatch"] = {"runner": spec["runner"]["arch"], "replay": arch}
    network = "bridge" if policy.data["sandbox"]["setup_network"] == "ALLOW" else "none"
    seconds = int(policy.data["budget"]["max_setup_seconds"])
    container = (
        docker(
            [
                "create",
                "--network",
                network,
                "--security-opt=no-new-privileges",
                "--memory=4g",
                "--cpus=2",
                "--pids-limit=512",
                "--entrypoint",
                "",
                base,
                "sleep",
                str(seconds + 120),
            ]
        )
        .stdout.decode()
        .strip()
    )
    setup_log = output / "setup.log"
    started = time.monotonic()
    try:
        docker(["start", container])
        docker(["cp", str(archive), f"{container}:/tmp/source.tar"])
        result = docker(["exec", container, "sh", "-c", BOOTSTRAP], timeout=600, check=False)
        log = result.stdout + result.stderr
        if result.returncode == 0 and spec["setup"]:
            script = (
                "set -e\ncd /workspace\n" + "\n".join(step_command(s) for s in spec["setup"]) + "\n"
            )
            (output / "setup.sh").write_text(script)
            docker(["cp", str(output / "setup.sh"), f"{container}:/tmp/ci-repair-setup.sh"])
            result = docker(
                [
                    "exec",
                    "-w",
                    "/workspace",
                    container,
                    "timeout",
                    f"{seconds}s",
                    "bash",
                    "/tmp/ci-repair-setup.sh",
                ],
                timeout=seconds + 30,
                check=False,
            )
            log += result.stdout + result.stderr
        setup_log.write_bytes(log)
        provenance.update(
            setup_returncode=result.returncode,
            setup_seconds=time.monotonic() - started,
            setup_log_sha256=hashlib.sha256(log).hexdigest(),
            setup_network=network,
        )
        if result.returncode != 0:
            provenance["status"] = "SETUP_FAILED"
            return provenance
        tools = docker(["exec", container, "sh", "-c", PROBE], check=False).stdout.decode(
            errors="replace"
        )
        provenance["tool_versions"] = dict(
            line.split("=", 1) for line in tools.splitlines() if "=" in line
        )
        docker(["stop", "-t", "1", container], timeout=60)
        label = json.dumps({k: v for k, v in provenance.items() if k != "tag"}, sort_keys=True)
        docker(
            [
                "commit",
                "--change",
                "ENTRYPOINT []",
                "--change",
                'CMD ["sleep", "infinity"]',
                "--change",
                "WORKDIR /workspace",
                "--change",
                f"LABEL ci-repair.provenance={json.dumps(label)}",
                container,
                tag,
            ],
            timeout=600,
        )
        provenance["image_id"] = (
            docker(["image", "inspect", "--format={{.Id}}", tag]).stdout.decode().strip()
        )
        provenance["status"] = "BUILT"
        return provenance
    finally:
        docker(["rm", "-f", container], check=False)
