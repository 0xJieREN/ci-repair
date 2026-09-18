"""Prepare a verified patch locally; explicitly publish a creation-only draft PR."""

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path

from ci_repair.github import CollectionError, api
from ci_repair.pipeline import paths_allowed
from ci_repair.workspace import checkout_commit, command


class PublicationError(ValueError):
    pass


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def verified_run(run_dir: Path) -> tuple[dict, bytes]:
    report = json.loads((run_dir / "report.json").read_text())
    patch = (run_dir / "patch.diff").read_bytes()
    tests = report.get("tests", [])
    if (
        report.get("status") != "PASS"
        or report.get("verified") is not True
        or len(tests) != 2
        or any(t.get("returncode") != 0 or t.get("exception_info") for t in tests)
    ):
        raise PublicationError("Only independently verified PASS runs can be published")
    if not patch or report.get("patch_sha256") != digest(patch):
        raise PublicationError(
            "Patch is missing, changed, or from an older run without a digest; verify again"
        )
    ci = report.get("github_actions", {})
    if ci.get("checkout_kind") != "head" or ci.get("commit") != report.get("commit"):
        raise PublicationError(
            "Publish requires CI provenance verified on the branch head; reverify merge patches on head"
        )
    if not re.fullmatch(r"[0-9a-f]{40}", report["commit"]):
        raise PublicationError("Invalid verified commit")
    return report, patch


def target(report: dict, base: str) -> str:
    ci = report["github_actions"]
    repository = ci["repository"]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise PublicationError("Invalid GitHub repository")
    command(["git", "check-ref-format", f"refs/heads/{base}"])
    if base.startswith("-") or base != ci.get("head_branch"):
        raise PublicationError(
            "Target must be the original failed branch (PR head branch for PR runs)"
        )
    pr = ci.get("pull_request")
    if pr:
        live = json.loads(api(f"repos/{repository}/pulls/{pr['number']}"))
        if (
            live["state"] != "open"
            or live["head"]["sha"] != report["commit"]
            or live["head"]["ref"] != base
            or live["head"]["repo"]["full_name"].lower() != repository.lower()
        ):
            raise PublicationError("Original PR is closed, moved, or from a fork")
    return f"https://github.com/{repository}.git"


def remote_sha(url: str, branch: str) -> str | None:
    result = command(["git", "ls-remote", "--heads", url, f"refs/heads/{branch}"], timeout=120)
    return result.decode().split()[0] if result.strip() else None


def require_base(url: str, base: str, expected: str):
    if remote_sha(url, base) != expected:
        raise PublicationError(
            "Target branch moved or disappeared; collect and verify its current commit again"
        )


def prepare(run_dir: Path, output: Path, base: str) -> dict:
    if output.exists():
        raise PublicationError(
            "Preparation directory already exists; resume publish or use a new directory"
        )
    report, patch = verified_run(run_dir)
    url = target(report, base)
    require_base(url, base, report["commit"])
    output.mkdir(parents=True, mode=0o700)
    checkout = output / "repo"
    command(
        ["gh", "repo", "clone", url, str(checkout), "--", "--no-checkout", "--depth=1"], timeout=180
    )
    command(["git", "config", "core.hooksPath", "/dev/null"], cwd=checkout)
    checkout_commit(checkout, report["commit"])
    patch_path = output / "patch.diff"
    patch_path.write_bytes(patch)
    command(["git", "apply", "--index", "--binary", str(patch_path.resolve())], cwd=checkout)
    paths = (
        command(["git", "diff", "--cached", "--name-only", "-z"], cwd=checkout)
        .decode()
        .rstrip("\0")
        .split("\0")
    )
    raw = command(["git", "diff", "--cached", "--raw"], cwd=checkout).decode()
    if (
        sorted(paths) != sorted(report["changed_files"])
        or not paths_allowed(paths, tuple(report["config"]["allowed_paths"]))
        or any(
            mode not in ("000000", "100644", "100755")
            for line in raw.splitlines()
            for mode in line[1:].split()[:2]
        )
    ):
        raise PublicationError("Patch paths or file modes do not match the verified source changes")
    ci = report["github_actions"]
    title = f"Repair CI failure from run {ci['run_id']}"
    command(
        [
            "git",
            "-c",
            "user.name=CI Repair",
            "-c",
            "user.email=ci-repair@localhost",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-m",
            title,
        ],
        cwd=checkout,
    )
    commit = command(["git", "rev-parse", "HEAD"], cwd=checkout).decode().strip()
    diff = command(["git", "diff", "--binary", report["commit"], commit], cwd=checkout)
    branch = f"ci-repair/{ci['run_id']}-{digest(patch)[:12]}"
    if branch == base:
        raise PublicationError("Repair branch must differ from the target branch")
    body = (
        f"Apply the independently verified patch for [failed CI run {ci['run_id']}]"
        f"(https://github.com/{ci['repository']}/actions/runs/{ci['run_id']}).\n\n"
        f"Verified baseline: `{report['commit']}`. Job: `{ci['job_id']}`; "
        f"attempt: `{ci['run_attempt']}`.\n\n"
        "The original failing command and configured regression command both passed "
        "in separate fresh verification containers. Full logs and trajectories remain local.\n\n"
        "This is a draft for review; passing these checks is not proof of complete correctness.\n"
    )
    (output / "body.md").write_text(body)
    plan = {
        "schema_version": 1,
        "run_dir": str(run_dir.resolve()),
        "repository": ci["repository"],
        "base": base,
        "base_sha": report["commit"],
        "branch": branch,
        "commit": commit,
        "patch_sha256": digest(patch),
        "prepared_diff_sha256": digest(diff),
        "title": title,
    }
    (output / "publication.json").write_text(json.dumps(plan, indent=2) + "\n")
    return plan


def publish(output: Path) -> str:
    plan = json.loads((output / "publication.json").read_text())
    if plan.get("schema_version") != 1:
        raise PublicationError("Unsupported publication plan")
    report, patch = verified_run(Path(plan["run_dir"]))
    if (
        digest(patch) != plan["patch_sha256"]
        or report["commit"] != plan["base_sha"]
        or report["github_actions"]["repository"] != plan["repository"]
    ):
        raise PublicationError("Prepared plan no longer matches the verified run")
    url = target(report, plan["base"])
    checkout = output / "repo"
    branch = plan["branch"]
    expected_branch = f"ci-repair/{report['github_actions']['run_id']}-{digest(patch)[:12]}"
    if branch != expected_branch or branch == plan["base"]:
        raise PublicationError("Unexpected repair branch")
    if command(["git", "status", "--porcelain"], cwd=checkout):
        raise PublicationError("Prepared checkout is dirty")
    if command(["git", "rev-parse", "HEAD"], cwd=checkout).decode().strip() != plan["commit"]:
        raise PublicationError("Prepared commit changed")
    parents = (
        command(["git", "rev-list", "--parents", "-n", "1", "HEAD"], cwd=checkout).decode().split()
    )
    diff = command(["git", "diff", "--binary", plan["base_sha"], "HEAD"], cwd=checkout)
    if (
        parents != [plan["commit"], plan["base_sha"]]
        or digest(diff) != plan["prepared_diff_sha256"]
    ):
        raise PublicationError("Prepared commit or patch changed")
    require_base(url, plan["base"], plan["base_sha"])
    existing = remote_sha(url, branch)
    if existing is not None and existing != plan["commit"]:
        raise PublicationError(
            "Repair branch already exists with different content; refusing to overwrite"
        )
    if existing is None:
        # Empty expected value makes this a creation-only CAS: never replace an existing ref.
        command(
            [
                "git",
                "push",
                f"--force-with-lease=refs/heads/{branch}:",
                url,
                f"{plan['commit']}:refs/heads/{branch}",
            ],
            cwd=checkout,
            timeout=180,
        )
    require_base(url, plan["base"], plan["base_sha"])
    prs = json.loads(
        command(
            [
                "gh",
                "pr",
                "list",
                "--repo",
                plan["repository"],
                "--head",
                branch,
                "--base",
                plan["base"],
                "--state",
                "all",
                "--json",
                "url,state,headRefOid",
            ]
        )
    )
    if prs:
        if len(prs) != 1 or prs[0]["state"] != "OPEN" or prs[0]["headRefOid"] != plan["commit"]:
            raise PublicationError(
                "A conflicting or closed PR already exists; refusing to duplicate"
            )
        pr_url = prs[0]["url"]
    else:
        pr_url = (
            command(
                [
                    "gh",
                    "pr",
                    "create",
                    "--repo",
                    plan["repository"],
                    "--draft",
                    "--head",
                    branch,
                    "--base",
                    plan["base"],
                    "--title",
                    plan["title"],
                    "--body-file",
                    str((output / "body.md").resolve()),
                ],
                cwd=checkout,
                timeout=120,
            )
            .decode()
            .strip()
        )
    (output / "published.json").write_text(
        json.dumps({"url": pr_url, "commit": plan["commit"]}) + "\n"
    )
    return pr_url


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    prep = sub.add_parser(
        "prepare", help="Create a local commit and reviewable PR body; no remote writes"
    )
    prep.add_argument("run_dir", type=Path)
    prep.add_argument("--base", required=True)
    prep.add_argument("--output", type=Path, required=True)
    pub = sub.add_parser("publish", help="Push the prepared branch and create/resume a draft PR")
    pub.add_argument("output", type=Path)
    args = parser.parse_args()
    try:
        if args.action == "prepare":
            plan = prepare(args.run_dir.resolve(), args.output.resolve(), args.base)
            print(
                f"Prepared {plan['commit']} in {args.output}; review patch.diff and body.md before publish"
            )
        else:
            print(publish(args.output.resolve()))
    except (
        PublicationError,
        CollectionError,
        subprocess.SubprocessError,
        OSError,
        KeyError,
        ValueError,
    ) as exc:
        msg = (
            str(exc) if isinstance(exc, (PublicationError, CollectionError)) else type(exc).__name__
        )
        parser.exit(1, f"Publication stopped: {msg}\n")
    return 0
