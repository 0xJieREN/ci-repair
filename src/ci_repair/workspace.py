"""Committed snapshots and disposable upstream Docker environments."""

import subprocess
from contextlib import contextmanager
from pathlib import Path

from minisweagent.environments.docker import DockerEnvironment


def command(args: list[str], *, cwd: Path | None = None, timeout: int = 60) -> bytes:
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, timeout=timeout).stdout


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
    return sha


class Sandbox(DockerEnvironment):
    """Keep mini's execution API; make teardown synchronous and portable."""

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
            "tar -xf /tmp/source.tar -C /workspace && "
            "git init -q && git add -A && "
            "git -c user.name=ci-repair -c user.email=ci-repair@localhost "
            "commit -qm baseline"
        )
        yield env
    finally:
        env.cleanup()


def extract_patch(env: Sandbox) -> bytes:
    env.checked("git add -A")
    return command(
        [
            "docker",
            "exec",
            "-w",
            "/workspace",
            env.container_id,
            "git",
            "diff",
            "--cached",
            "--binary",
            "HEAD",
            "--",
        ]
    )
