"""build_environment's decisions with a scripted `docker`: network, failures, cleanup, cache.

The real image build runs in the Docker tests (test_reconstruct.py, test_orchestrate.py);
these check what is asked of Docker, which those tests cannot observe.
"""

import json
import subprocess

import pytest
from test_reconstruct import spec_for

from ci_repair import reconstruct
from ci_repair.policy import Policy
from ci_repair.reconstruct import PROBE, WHEELHOUSE, build_environment


class Docker:
    """Answers each docker subcommand like a daemon would; records every call."""

    def __init__(self, *, cached=False, arch="amd64", setup_returncode=0, bootstrap_returncode=0):
        self.calls: list[list[str]] = []
        self.images = {"ci-repair-env"} if cached else set()
        self.arch = arch
        self.setup_returncode = setup_returncode
        self.bootstrap_returncode = bootstrap_returncode

    def __call__(self, args, *, timeout=120, check=True):
        self.calls.append(args)
        returncode, out = 0, b""
        if args[:3] == ["image", "inspect", "--format={{.Id}}"]:
            known = any(args[3].startswith(i) for i in self.images)
            returncode, out = (0, b"sha256:env\n") if known else (1, b"")
        elif args[:3] == ["image", "inspect", "--format={{json .Config.Labels}}"]:
            out = json.dumps({"ci-repair.provenance": json.dumps({"status": "BUILT"})}).encode()
        elif args[:2] == ["image", "inspect"]:
            info = {"Id": "sha256:base", "RepoDigests": ["b@sha256:1"], "Os": "linux",
                    "Architecture": self.arch}  # fmt: skip
            out = json.dumps([info]).encode()
        elif args[0] == "create":
            out = b"container-1\n"
        elif args[0] == "commit":
            self.images.add(args[-1])
        elif args[0] == "exec" and "/tmp/ci-repair-setup.sh" in args:
            returncode = self.setup_returncode
        elif args[0] == "exec" and reconstruct.BOOTSTRAP in args:
            returncode = self.bootstrap_returncode
        elif args[0] == "exec" and WHEELHOUSE in args:
            out = b"2\n"
        elif args[0] == "exec" and PROBE in args:
            out = b"python3=Python 3.12.1\n"
        if check and returncode:
            raise subprocess.CalledProcessError(returncode, ["docker", *args])
        return subprocess.CompletedProcess(["docker", *args], returncode, out, b"")

    def find(self, name):
        return [c for c in self.calls if c[0] == name]


@pytest.fixture
def build(tmp_path, monkeypatch):
    specs = {}

    def run(docker, policy=None, **kwargs):
        monkeypatch.setattr(reconstruct, "docker", docker)
        key = json.dumps(policy.data if policy else None, sort_keys=True)
        if key not in specs:
            specs[key] = spec_for(tmp_path / f"spec-{len(specs)}", policy=policy)
        spec = specs[key]
        archive = tmp_path / "source.tar"
        archive.write_bytes(b"tar")
        output = tmp_path / "env"
        output.mkdir(exist_ok=True)
        return build_environment(spec, archive, output, policy or Policy(), **kwargs), output

    return run


def test_a_successful_build_commits_a_labelled_offline_ready_image(build):
    docker = Docker()
    built, output = build(docker)
    assert built["status"] == "BUILT" and built["image_id"] == "sha256:env"
    create = docker.find("create")[0]
    assert create[create.index("--network") + 1] == "bridge"
    assert "--security-opt=no-new-privileges" in create
    assert not any(a in ("-v", "--volume", "--mount") for a in create)  # no host mounts
    # Setup ran the reconstructed setup steps, then warm-up and the wheelhouse.
    assert "pip install -r requirements.txt" in (output / "setup.sh").read_text()
    assert (output / "warm-up.sh").exists() and built["wheelhouse_files"] == 2
    assert built["tool_versions"] == {"python3": "Python 3.12.1"}
    label = docker.find("commit")[0]
    assert any(a.startswith("LABEL ci-repair.provenance=") for a in label)
    assert docker.calls[-1] == ["rm", "-f", "container-1"]


def test_setup_without_network_skips_warm_up_and_online_checkout(build):
    docker = Docker()
    built, output = build(docker, Policy({"sandbox": {"setup_network": "DENY"}}))
    assert built["status"] == "BUILT" and built["setup_network"] == "none"
    create = docker.find("create")[0]
    assert create[create.index("--network") + 1] == "none"
    checkout = next(c for c in docker.calls if reconstruct.CHECKOUT in c)
    assert "CI_REPAIR_REPOSITORY=" in checkout  # no remote to fetch history from
    assert not (output / "warm-up.sh").exists() and "wheelhouse_files" not in built


@pytest.mark.parametrize("failure", ["setup_returncode", "bootstrap_returncode"])
def test_a_failed_setup_commits_nothing_and_removes_the_container(build, failure):
    docker = Docker(**{failure: 2})
    built, output = build(docker)
    assert built["status"] == "SETUP_FAILED" and built["setup_returncode"] == 2
    assert not docker.find("commit") and "image_id" not in built
    assert docker.calls[-1] == ["rm", "-f", "container-1"]
    assert (output / "setup.log").exists()


def test_a_docker_error_mid_build_still_removes_the_container(build):
    docker = Docker()
    original = docker.__call__

    def failing(args, **kwargs):
        if args[0] == "cp":
            raise subprocess.CalledProcessError(1, ["docker", *args])
        return original(args, **kwargs)

    with pytest.raises(subprocess.CalledProcessError):
        build(failing)
    assert docker.calls[-1] == ["rm", "-f", "container-1"]


def test_an_existing_image_for_the_same_spec_is_reused(build):
    docker = Docker(cached=True)
    built, _ = build(docker)
    assert built["reused"] is True and built["status"] == "BUILT"
    assert not docker.find("create")


def test_setup_variants_never_share_an_image_tag(build):
    plain, _ = build(Docker())
    mirrored, _ = build(Docker(), setup_env={"PIP_INDEX_URL": "http://mirror"})
    assert mirrored["tag"].startswith(plain["tag"] + "-s")
    assert mirrored["setup_env_keys"] == ["PIP_INDEX_URL"]


def test_an_architecture_mismatch_is_recorded(build):
    built, _ = build(Docker(arch="arm64"))
    assert built["architecture_mismatch"] == {"runner": "x64", "replay": "arm64"}
    assert "architecture_mismatch" not in build(Docker())[0]


def test_unsupported_specs_are_never_built(monkeypatch, tmp_path):
    spec = spec_for(tmp_path / "unsupported", "on: push\njobs: {test: {runs-on: windows-latest}}")
    monkeypatch.setattr(reconstruct, "docker", lambda *a, **k: pytest.fail("no docker"))
    with pytest.raises(ValueError, match="unsupported"):
        build_environment(spec, tmp_path / "source.tar", tmp_path, Policy())
