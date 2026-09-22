"""Deterministic operator policy: code, not the model, decides ALLOW / REVIEW / DENY.

The policy is operator configuration loaded from outside every repository under repair.
A failing branch can therefore never edit or relax the rules that govern its own repair.
"""

import copy
import fnmatch
import hashlib
import math
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath

import yaml


class Verdict(str, Enum):
    ALLOW = "ALLOW"
    REVIEW = "REVIEW"
    DENY = "DENY"


SEVERITY = {Verdict.ALLOW: 0, Verdict.REVIEW: 1, Verdict.DENY: 2}


@dataclass(frozen=True)
class Decision:
    verdict: Verdict
    reasons: tuple[str, ...] = ()

    @property
    def allowed(self) -> bool:
        return self.verdict is Verdict.ALLOW

    def to_dict(self) -> dict:
        return {"verdict": self.verdict.value, "reasons": list(self.reasons)}


def combine(*decisions: Decision) -> Decision:
    """The strictest verdict wins; every reason is kept for the report."""
    verdict = max((d.verdict for d in decisions), key=lambda v: SEVERITY[v], default=Verdict.ALLOW)
    reasons = tuple(dict.fromkeys(r for d in decisions for r in d.reasons))
    return Decision(verdict, reasons)


class PolicyError(ValueError):
    """The policy file is malformed, untrusted or requests an unsupported relaxation."""


# Every key must be listed here; unknown keys are rejected so typos never widen access.
DEFAULTS = {
    "version": 1,
    "repositories": [],
    "triggers": {"events": ["push", "workflow_dispatch", "pull_request"]},
    "models": {"allowed": ["*"], "default": None},
    "budget": {
        "max_model_calls": 30,
        "max_cost_usd": 1.0,
        "max_wall_seconds": 600,
        "max_command_seconds": 60,
        "max_setup_seconds": 900,
    },
    "patch": {
        "allowed_paths": ["."],
        "max_changed_files": {"review": 5, "deny": 20},
        "max_changed_lines": {"review": 200, "deny": 1000},
        "categories": {
            "workflows": "DENY",
            "lockfiles": "REVIEW",
            "dependency_manifests": "REVIEW",
            "tests": "REVIEW",
            "generated": "DENY",
            "config": "REVIEW",
            "binary": "REVIEW",
        },
    },
    "sandbox": {"setup_network": "ALLOW", "repair_network": "DENY", "secrets": "DENY"},
    "publication": {"draft_pr": "REVIEW", "require_human_review": False, "auto_merge": "DENY"},
    "repair": {"early_stop": True, "max_rejected_submissions": 2},
}
# Capabilities this implementation cannot enforce safely are fixed; relaxing them fails closed.
FIXED = {
    ("sandbox", "repair_network"): "DENY",
    ("sandbox", "secrets"): "DENY",
    ("publication", "auto_merge"): "DENY",
}
REPOSITORY_KEYS = {"name", "branches", "allowed_paths"}

# Never changeable by a repair, whatever the policy says.
PROTECTED = (".git/*", ".ci-repair/*", "ci-repair-policy*", "*/ci-repair-policy*")
CATEGORIES = {
    "workflows": (".github/*", ".gitlab-ci.yml", ".circleci/*", "azure-pipelines.yml"),
    "lockfiles": (
        "*.lock",
        "*-lock.json",
        "*-lock.yaml",
        "go.sum",
        "requirements*.lock",
        "npm-shrinkwrap.json",
    ),
    "dependency_manifests": (
        "pyproject.toml",
        "setup.py",
        "setup.cfg",
        "requirements*.txt",
        "requirements*.in",
        "package.json",
        "Cargo.toml",
        "go.mod",
        "Gemfile",
        "*.gemspec",
        "pom.xml",
        "build.gradle*",
        "environment.yml",
    ),
    "generated": (
        "*.min.js",
        "*.min.css",
        "*_pb2.py",
        "*_pb2_grpc.py",
        "*.pb.go",
        "*.generated.*",
        "dist/*",
        "build/*",
        "vendor/*",
        "node_modules/*",
        "*.egg-info/*",
    ),
    "config": (
        "Dockerfile",
        "*.dockerfile",
        "Makefile",
        "tox.ini",
        "pytest.ini",
        "mypy.ini",
        "ruff.toml",
        ".ruff.toml",
        ".flake8",
        ".pylintrc",
        ".pre-commit-config.yaml",
        ".editorconfig",
        ".gitattributes",
        ".gitignore",
        "tsconfig*.json",
        ".eslintrc*",
        "eslint.config.*",
        ".prettierrc*",
        "noxfile.py",
        "conftest.py",
    ),
}
TEST_DIRS = {"test", "tests", "__tests__", "spec", "specs", "testing"}
TEST_FILES = (
    "test_*.py",
    "*_test.py",
    "*_test.go",
    "*.test.js",
    "*.test.ts",
    "*.test.jsx",
    "*.test.tsx",
    "*.spec.js",
    "*.spec.ts",
    "*_spec.rb",
    "conftest.py",
)


def _match(path: str, patterns) -> bool:
    name = PurePosixPath(path).name
    return any(
        fnmatch.fnmatchcase(path, p) or ("/" not in p and fnmatch.fnmatchcase(name, p))
        for p in patterns
    )


def categorize(path: str) -> list[str]:
    found = [name for name, patterns in CATEGORIES.items() if _match(path, patterns)]
    parts = PurePosixPath(path).parts
    if any(part.lower() in TEST_DIRS for part in parts[:-1]) or _match(path, TEST_FILES):
        found.append("tests")
    return found


def safe_prefix(prefix: str) -> bool:
    if prefix == ".":
        return True
    p = PurePosixPath(prefix)
    return bool(p.parts) and not p.is_absolute() and ".." not in p.parts and p.parts[0] != ".git"


def within(path: str, prefixes) -> bool:
    p = PurePosixPath(path)
    if not path or p.is_absolute() or ".." in p.parts:
        return False
    return any(
        prefix == "." or path == prefix.rstrip("/") or path.startswith(prefix.rstrip("/") + "/")
        for prefix in prefixes
    )


def patch_stats(patch: bytes) -> dict:
    lines = patch.splitlines()
    return {
        "changed_lines": sum(
            line.startswith((b"+", b"-")) and not line.startswith((b"+++", b"---"))
            for line in lines
        ),
        "binary": any(
            line.startswith(b"GIT binary patch") or line.startswith(b"Binary files")
            for line in lines
        ),
        "bytes": len(patch),
    }


def _strict_merge(defaults, value, where: str):
    if isinstance(defaults, dict):
        if not isinstance(value, dict):
            raise PolicyError(f"{where} must be a mapping")
        unknown = set(value) - set(defaults)
        if unknown:
            raise PolicyError(f"Unknown policy keys at {where}: {sorted(unknown)}")
        merged = copy.deepcopy(defaults)
        for key, item in value.items():
            merged[key] = _strict_merge(defaults[key], item, f"{where}.{key}")
        return merged
    if isinstance(defaults, bool):
        if not isinstance(value, bool):
            raise PolicyError(f"{where} must be a boolean")
        return value
    if isinstance(defaults, (int, float)) and not isinstance(defaults, bool):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise PolicyError(f"{where} must be a number")
        if not math.isfinite(value) or value <= 0:
            raise PolicyError(f"{where} must be finite and positive")
        return value
    if isinstance(defaults, list):
        if not isinstance(value, list):
            raise PolicyError(f"{where} must be a list")
        return value
    if defaults is None or isinstance(defaults, str):
        if value is not None and not isinstance(value, str):
            raise PolicyError(f"{where} must be a string")
        return value
    raise PolicyError(f"Unsupported policy value at {where}")


def _verdict(value, where: str) -> Verdict:
    try:
        return Verdict(value)
    except ValueError:
        raise PolicyError(f"{where} must be ALLOW, REVIEW or DENY") from None


class Policy:
    def __init__(self, data: dict | None = None, *, source: str = "builtin", raw: bytes = b""):
        self.data: dict = _strict_merge(DEFAULTS, data or {}, "policy")
        self.source = source
        self.digest = hashlib.sha256(raw or repr(sorted(self.data.items())).encode()).hexdigest()
        self._validate()

    def _validate(self):
        d = self.data
        if d["version"] != 1:
            raise PolicyError("Only policy version 1 is supported")
        for (section, key), fixed in FIXED.items():
            if d[section][key] != fixed:
                raise PolicyError(f"{section}.{key} can only be {fixed} in this version")
        for section, key in (("sandbox", "setup_network"), ("publication", "draft_pr")):
            _verdict(d[section][key], f"{section}.{key}")
        for name, value in d["patch"]["categories"].items():
            _verdict(value, f"patch.categories.{name}")
        for limit in ("max_changed_files", "max_changed_lines"):
            if d["patch"][limit]["review"] > d["patch"][limit]["deny"]:
                raise PolicyError(f"patch.{limit}.review must not exceed deny")
        for key in ("max_model_calls", "max_wall_seconds", "max_command_seconds"):
            if not float(d["budget"][key]).is_integer():
                raise PolicyError(f"budget.{key} must be an integer")
        if not isinstance(d["repair"]["max_rejected_submissions"], int):
            raise PolicyError("repair.max_rejected_submissions must be an integer")
        self._paths(d["patch"]["allowed_paths"], "patch.allowed_paths")
        if not all(isinstance(m, str) and m for m in d["models"]["allowed"]):
            raise PolicyError("models.allowed must list model name patterns")
        if not all(isinstance(e, str) for e in d["triggers"]["events"]):
            raise PolicyError("triggers.events must be strings")
        seen = set()
        for entry in d["repositories"]:
            if not isinstance(entry, dict) or set(entry) - REPOSITORY_KEYS or "name" not in entry:
                raise PolicyError("repositories entries need name and optional branches/paths")
            name = entry["name"]
            if not isinstance(name, str) or name.count("/") != 1 or name.lower() in seen:
                raise PolicyError("repositories names must be unique owner/name values")
            seen.add(name.lower())
            branches = entry.get("branches", ["*"])
            if not isinstance(branches, list) or not all(isinstance(b, str) for b in branches):
                raise PolicyError("repositories branches must be a list of patterns")
            if "allowed_paths" in entry:
                self._paths(entry["allowed_paths"], f"repositories[{name}].allowed_paths")

    @staticmethod
    def _paths(paths, where):
        if not isinstance(paths, list) or not paths:
            raise PolicyError(f"{where} must be a nonempty list")
        for prefix in paths:
            if not isinstance(prefix, str) or not safe_prefix(prefix):
                raise PolicyError(f"Unsafe path prefix in {where}: {prefix!r}")

    # --- Trigger and model admission -------------------------------------------------

    def repository(self, name: str) -> dict | None:
        for entry in self.data["repositories"]:
            if entry["name"].lower() == name.lower():
                return entry
        return None

    def check_trigger(
        self, *, repository: str, event: str, branch: str | None, fork: bool = False
    ) -> Decision:
        if event == "pull_request_target":
            return Decision(Verdict.DENY, ("pull_request_target is never executed",))
        if fork:
            return Decision(Verdict.DENY, ("fork pull requests are not trusted",))
        entry = self.repository(repository)
        if entry is None:
            return Decision(Verdict.DENY, (f"repository {repository} is not in the policy",))
        if event not in self.data["triggers"]["events"]:
            return Decision(Verdict.DENY, (f"event {event} is not an allowed trigger",))
        branches = entry.get("branches", ["*"])
        if not branch or not any(fnmatch.fnmatchcase(branch, b) for b in branches):
            return Decision(Verdict.DENY, (f"branch {branch} is not trusted for {repository}",))
        return Decision(Verdict.ALLOW)

    def check_model(self, name: str) -> Decision:
        if any(fnmatch.fnmatchcase(name, p) for p in self.data["models"]["allowed"]):
            return Decision(Verdict.ALLOW)
        return Decision(Verdict.DENY, (f"model {name} is not allowed by policy",))

    def allowed_paths(self, repository: str | None = None) -> tuple[str, ...]:
        entry = self.repository(repository) if repository else None
        return tuple((entry or {}).get("allowed_paths") or self.data["patch"]["allowed_paths"])

    # --- Budgets: requested -> policy maximum -> effective ---------------------------

    def budget(self, requested: dict) -> dict:
        """Clamp requested budgets to policy maxima; never silently raise them."""
        limits = self.data["budget"]
        mapping = {
            "steps": "max_model_calls",
            "cost": "max_cost_usd",
            "wall_seconds": "max_wall_seconds",
            "command_seconds": "max_command_seconds",
        }
        maximum = {key: limits[name] for key, name in mapping.items()}
        effective = {}
        clamped = []
        for key, limit in maximum.items():
            value = requested.get(key)
            if value is None:
                effective[key] = limit
            elif value > limit:
                effective[key] = limit
                clamped.append(key)
            else:
                effective[key] = value
        for key in ("steps", "wall_seconds", "command_seconds"):
            effective[key] = int(effective[key])
        return {
            "requested": requested,
            "policy_max": maximum,
            "effective": effective,
            "clamped": clamped,
        }

    # --- Patch risk ------------------------------------------------------------------

    def check_patch(self, paths: list[str], patch: bytes, allowed_paths) -> Decision:
        if not paths or not patch:
            return Decision(Verdict.DENY, ("empty patch",))
        rules = self.data["patch"]
        decisions = []
        for path in paths:
            if _match(path, PROTECTED):
                decisions.append(Decision(Verdict.DENY, (f"{path}: protected path",)))
                continue
            if not within(path, allowed_paths):
                decisions.append(Decision(Verdict.DENY, (f"{path}: outside allowed paths",)))
                continue
            for category in categorize(path):
                verdict = Verdict(rules["categories"][category])
                if verdict is not Verdict.ALLOW:
                    decisions.append(Decision(verdict, (f"{path}: {category}",)))
        stats = patch_stats(patch)
        if stats["binary"]:
            verdict = Verdict(rules["categories"]["binary"])
            decisions.append(Decision(verdict, ("binary patch",)))
        for measure, value in (
            ("max_changed_files", len(paths)),
            ("max_changed_lines", stats["changed_lines"]),
        ):
            if value > rules[measure]["deny"]:
                decisions.append(Decision(Verdict.DENY, (f"{measure}: {value}",)))
            elif value > rules[measure]["review"]:
                decisions.append(Decision(Verdict.REVIEW, (f"{measure}: {value}",)))
        return combine(Decision(Verdict.ALLOW), *decisions)

    def agent_summary(self, allowed_paths) -> dict:
        """What the agent may know to avoid wasted attempts; enforcement stays outside."""
        categories = self.data["patch"]["categories"]
        return {
            "allowed_source_prefixes": list(allowed_paths),
            "forbidden_changes": sorted(k for k, v in categories.items() if v == "DENY"),
            "changes_requiring_human_review": sorted(
                k for k, v in categories.items() if v == "REVIEW"
            ),
            "network": "disabled",
            "secrets": "unavailable",
        }

    def to_dict(self) -> dict:
        return {"source": self.source, "sha256": self.digest}


def load_policy(path: Path | None, *, untrusted_roots=()) -> Policy:
    """Load operator policy; refuse files that live inside any repository under repair."""
    if path is None:
        return Policy()
    resolved = path.resolve()
    for root in untrusted_roots:
        if resolved.is_relative_to(Path(root).resolve()):
            raise PolicyError("Policy must not be read from a repository under repair")
    raw = resolved.read_bytes()
    from ci_repair.plan import WorkflowLoader

    try:
        data = yaml.load(raw, Loader=WorkflowLoader)
    except (yaml.YAMLError, ValueError) as exc:
        raise PolicyError(f"Invalid policy YAML: {exc}") from None
    return Policy(data or {}, source=str(resolved), raw=raw)
