"""Committed snapshots and disposable upstream Docker environments."""

import hashlib
import shlex
import subprocess
import tarfile
from contextlib import contextmanager
from functools import cached_property
from pathlib import Path

from minisweagent.environments.docker import DockerEnvironment


def command(args: list[str], *, cwd: Path | None = None, timeout: int = 60) -> bytes:
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, timeout=timeout).stdout


def checkout_commit(repo: Path, sha: str):
    """Avoid another network fetch when the shallow clone already contains this commit."""
    try:
        command(["git", "cat-file", "-e", f"{sha}^{{commit}}"], cwd=repo)
    except subprocess.CalledProcessError:
        command(["git", "fetch", "--depth=1", "origin", sha], cwd=repo, timeout=180)
    command(["git", "checkout", "--detach", sha], cwd=repo)


def check_archive(repo: Path, archive: Path, sha: str):
    """git archive can silently omit/transform files via export-ignore/export-subst."""
    entries = command(["git", "ls-tree", "-rz", sha], cwd=repo).split(b"\0")
    expected = {}
    for entry in filter(None, entries):
        info, name = entry.split(b"\t", 1)
        mode, _, oid = info.decode().split()
        expected[name.decode(errors="surrogateescape")] = (mode, oid)
    seen = set()
    with tarfile.open(archive) as tar:
        for member in tar:
            if member.isdir():
                continue
            if member.name not in expected:
                raise ValueError("Archive contains an unexpected file")
            mode, oid = expected[member.name]
            if member.issym() and mode == "120000":
                data = member.linkname.encode(errors="surrogateescape")
                blob = hashlib.new(
                    "sha1" if len(oid) == 40 else "sha256",
                    b"blob " + str(len(data)).encode() + b"\0" + data,
                )
            elif member.isfile() and mode in ("100644", "100755"):
                if bool(member.mode & 0o111) != (mode == "100755"):
                    raise ValueError("Archive changed file permissions")
                blob = hashlib.new(
                    "sha1" if len(oid) == 40 else "sha256",
                    b"blob " + str(member.size).encode() + b"\0",
                )
                with tar.extractfile(member) as stream:
                    while chunk := stream.read(65536):
                        blob.update(chunk)
            else:
                raise ValueError("Archive changed a file type")
            if blob.hexdigest() != oid:
                raise ValueError(
                    "Archive transformed committed content; export-subst is unsupported"
                )
            seen.add(member.name)
    if seen != set(expected):
        raise ValueError("Archive omitted committed files; export-ignore is unsupported")


def snapshot(repo: Path, archive: Path) -> str:
    root = Path(command(["git", "rev-parse", "--show-toplevel"], cwd=repo).decode().strip())
    if root.resolve() != repo.resolve():
        raise ValueError("Repository must be its Git root")
    if command(["git", "status", "--porcelain", "--untracked-files=all"], cwd=repo):
        raise ValueError("Repository must be clean, including untracked files")
    tree = command(["git", "ls-tree", "-r", "HEAD"], cwd=repo)
    if any(line.startswith(b"160000 ") for line in tree.splitlines()):
        raise ValueError("Submodules are not supported in v0.1")
    sha = command(["git", "rev-parse", "HEAD"], cwd=repo).decode().strip()
    command(["git", "archive", "--format=tar", f"--output={archive}", sha], cwd=repo)
    check_archive(repo, archive, sha)
    return sha


class Sandbox(DockerEnvironment):
    """Keep mini's execution API; make teardown synchronous and portable."""

    @cached_property
    def container_platform(self) -> dict:
        values = self.checked(
            "uname -s && uname -n && uname -r && uname -v && uname -m"
        ).splitlines()
        return dict(zip(("system", "node", "release", "version", "machine"), values, strict=True))

    def get_template_vars(self, **kwargs) -> dict:
        # Upstream DockerEnvironment uses host platform.uname(); templates must describe
        # the environment where commands run (especially GNU versus BSD command syntax).
        return {**self.config.model_dump(), **self.container_platform, **kwargs}

    def execute(self, action: dict, cwd: str = "", *, timeout: int | None = None) -> dict:
        seconds = timeout or self.config.timeout
        script = action.get("command", "")
        guarded = f"timeout --signal=TERM --kill-after=2s {seconds}s bash -lc {shlex.quote(script)}"
        result = super().execute({**action, "command": guarded}, cwd, timeout=seconds + 5)
        if result["returncode"] in (124, 137):
            result["exception_info"] = "Command timed out or was killed inside the container"
        return result

    def cleanup(self):
        if getattr(self, "container_id", None):
            container_id, self.container_id = self.container_id, None
            subprocess.run(
                [self.config.executable, "rm", "-f", container_id],
                capture_output=True,
                timeout=30,
                check=False,
            )

    def copy(self, source: Path, destination: str):
        command(["docker", "cp", str(source), f"{self.container_id}:{destination}"])

    def checked(self, script: str) -> str:
        result = self.execute({"command": script})
        if result["returncode"] != 0:
            raise RuntimeError(f"Sandbox setup failed: {result['output'][-2000:]}")
        return result["output"]


@contextmanager
def workspace(archive: Path, image: str, timeout: int, lifetime: int):
    env = Sandbox(
        image=image,
        cwd="/workspace",
        timeout=timeout,
        container_timeout=f"{lifetime + 60}s",
        run_args=[
            "--rm",
            "--network=none",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--memory=2g",
            "--cpus=2",
            "--pids-limit=128",
        ],
    )
    try:
        env.copy(archive, "/tmp/source.tar")
        env.checked(
            "command -v timeout >/dev/null && tar -xf /tmp/source.tar -C /workspace && "
            "git init -q && git add -A && "
            "git -c user.name=ci-repair -c user.email=ci-repair@localhost "
            "commit -qm baseline"
        )
        yield env
    finally:
        env.cleanup()


PROBE_INDEX = "/tmp/ci-repair-probe.index"


def extract_patch(env: Sandbox, base: str = "HEAD", *, probe: bool = False) -> bytes:
    """Binary diff of the workspace (including untracked files) against the baseline commit.

    probe=True stages into a throwaway index so the agent's own `git diff`/`git status`
    view is unchanged while a session is still running.
    """
    exec_env = []
    if probe:
        env.checked(f"cp .git/index {PROBE_INDEX} && GIT_INDEX_FILE={PROBE_INDEX} git add -A")
        exec_env = ["-e", f"GIT_INDEX_FILE={PROBE_INDEX}"]
    else:
        env.checked("git add -A")
    return command(
        [
            "docker",
            "exec",
            *exec_env,
            "-w",
            "/workspace",
            env.container_id,
            "git",
            "diff",
            "--cached",
            "--binary",
            base,
            "--",
        ]
    )
