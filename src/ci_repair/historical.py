"""Pinned upstream repositories, reproduced without rewriting their source tree."""

import hashlib
import json
import re
import shlex
from pathlib import Path

from ci_repair.pipeline import Config, run_test, write_json
from ci_repair.workspace import command, snapshot, workspace


def validate_historical(case: dict):
    if not re.fullmatch(r"https://github\.com/[\w.-]+/[\w.-]+\.git", case.get("repository", "")):
        raise ValueError("Historical repository must be an HTTPS GitHub repository")
    for field in ("base_commit", "fix_commit"):
        if not re.fullmatch(r"[0-9a-f]{40}", case.get(field, "")):
            raise ValueError(f"Historical {field} must be a full commit ID")
    for field in (
        "public",
        "oracle",
        "baseline_diagnostic",
        "regression_command",
        "reference_patch",
        "source_url",
        "task",
    ):
        if not isinstance(case.get(field), str) or not case[field].strip():
            raise ValueError(f"Missing historical case field: {field}")
    if hashlib.sha256(case["reference_patch"].encode()).hexdigest() != case.get(
        "reference_patch_sha256"
    ):
        raise ValueError("Reference patch digest mismatch")
    paths = case.get("allowed_paths")
    if (
        not isinstance(paths, list)
        or not paths
        or any(not isinstance(p, str) or not re.fullmatch(r"[\w-]+(?:/[\w-]+)*/", p) for p in paths)
    ):
        raise ValueError("Historical allowed_paths must be relative directory prefixes")


def python_command(script: str) -> str:
    return f"python -c {shlex.quote(script)}"


def prepare_historical(case: dict, output: Path, image: str, command_seconds: int) -> Config:
    validate_historical(case)
    output.mkdir(parents=True, exist_ok=False)
    repo = output / "repo"
    command(["git", "init", "-q", str(repo)])
    # Fetch immutable commits only; repository code is executed only inside Docker.
    for sha in (case["base_commit"], case["fix_commit"]):
        command(["git", "-C", str(repo), "fetch", "-q", "--depth=1", case["repository"], sha])
    actual = command(
        [
            "git",
            "-C",
            str(repo),
            "diff",
            case["base_commit"],
            case["fix_commit"],
            "--",
            *case["allowed_paths"],
        ]
    ).decode()
    if actual != case["reference_patch"]:
        raise ValueError("Reference patch does not match pinned upstream commits")
    command(
        [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-C",
            str(repo),
            "checkout",
            "-q",
            "--detach",
            case["base_commit"],
        ]
    )
    archive = output / "source.tar"
    sha = snapshot(repo, archive.resolve())
    failing = python_command(case["public"])
    with workspace(archive, image, command_seconds, command_seconds + 30) as env:
        baseline = run_test(env, failing, output / "baseline.json")
    if (
        baseline.get("exception_info")
        or baseline["returncode"] != 1
        or case["baseline_diagnostic"] not in baseline["output"]
    ):
        raise ValueError(f"Historical failure not reproduced: {case['id']}")
    # The hidden oracle must also detect the original bug, independently of the public check.
    with workspace(archive, image, command_seconds, command_seconds + 30) as env:
        oracle = run_test(env, python_command(case["oracle"]), output / "baseline-oracle.json")
    if (
        oracle.get("exception_info")
        or oracle["returncode"] != 1
        or "AssertionError" not in oracle["output"]
    ):
        raise ValueError(f"Historical oracle does not detect the original bug: {case['id']}")
    log = output / "failure.log"
    log.write_text(baseline["output"])
    write_json(
        output / "fixture.json",
        {
            "case": case["id"],
            "commit": sha,
            "image_id": image,
            "repository": case["repository"],
            "fix_commit": case["fix_commit"],
            "case_sha256": hashlib.sha256(json.dumps(case, sort_keys=True).encode()).hexdigest(),
            "log_sha256": hashlib.sha256(log.read_bytes()).hexdigest(),
        },
    )
    return Config(
        repo.resolve(),
        log.resolve(),
        (output / "unused").resolve(),
        image,
        failing,
        case["regression_command"],
        allowed_paths=tuple(case["allowed_paths"]),
        command_seconds=command_seconds,
        task=case["task"],
    )
