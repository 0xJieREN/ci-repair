import subprocess

import pytest

from ci_repair import workspace


def commit(repo):
    workspace.command(["git", "add", "."], cwd=repo)
    workspace.command(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@localhost",
            "commit",
            "-qm",
            "fixture",
        ],
        cwd=repo,
    )


@pytest.mark.parametrize("attribute", ["export-ignore", "export-subst"])
def test_snapshot_rejects_archive_attributes_changing_the_tree(tmp_path, attribute):
    repo = tmp_path / "repo"
    workspace.command(["git", "init", str(repo)])
    (repo / ".gitattributes").write_text(f"file.txt {attribute}\n")
    (repo / "file.txt").write_text("$Format:%H$\n")
    commit(repo)
    with pytest.raises(ValueError, match=attribute):
        workspace.snapshot(repo, tmp_path / "source.tar")


def test_snapshot_preserves_symlinks_and_executable_files(tmp_path):
    repo = tmp_path / "repo"
    workspace.command(["git", "init", str(repo)])
    script = repo / "script.sh"
    script.write_text("echo ok\n")
    script.chmod(0o755)
    (repo / "link").symlink_to("script.sh")
    commit(repo)
    assert len(workspace.snapshot(repo, tmp_path / "source.tar")) == 40


@pytest.mark.parametrize("available", [True, False])
def test_checkout_fetches_only_missing_commits(tmp_path, monkeypatch, available):
    calls = []

    def command(args, **kwargs):
        calls.append(args)
        if args[1] == "cat-file" and not available:
            raise subprocess.CalledProcessError(1, args)
        return b""

    monkeypatch.setattr(workspace, "command", command)
    workspace.checkout_commit(tmp_path, "a" * 40)
    assert any(c[1] == "fetch" for c in calls) is not available
    assert calls[-1] == ["git", "checkout", "--detach", "a" * 40]


def test_sandbox_templates_use_cached_container_platform():
    from types import SimpleNamespace

    env = workspace.Sandbox.__new__(workspace.Sandbox)
    env.config = SimpleNamespace(model_dump=lambda: {"cwd": "/workspace"})
    calls = []

    def checked(script):
        calls.append(script)
        return "Linux\ncontainer\n6.1\nLinux kernel\naarch64\n"

    env.checked = checked
    assert env.get_template_vars()["system"] == "Linux"
    assert env.get_template_vars()["machine"] == "aarch64"
    assert len(calls) == 1
