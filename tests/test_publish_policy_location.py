"""Publication policies must remain outside code and artifacts under repair."""

import json

import pytest

from ci_repair import publish


@pytest.mark.parametrize("action", ["prepare", "publish", "auto"])
@pytest.mark.parametrize("location", ["source", "run", "prepared", "symlink"])
def test_publication_cli_rejects_repository_local_policy(
    tmp_path, monkeypatch, capsys, action, location
):
    source, run, prepared = (tmp_path / name for name in ("source", "run", "prepared"))
    for directory in (source, run, prepared / "repo"):
        directory.mkdir(parents=True)
    (run / "report.json").write_text(json.dumps({"config": {"repo": str(source)}}))
    (prepared / "publication.json").write_text(json.dumps({"run_dir": str(run)}))
    parent = {"source": source, "run": run, "prepared": prepared / "repo", "symlink": source}[
        location
    ]
    policy = parent / "policy.yaml"
    policy.write_text("publication:\n  draft_pr: ALLOW\n")
    if location == "symlink":
        alias = tmp_path / "operator-policy.yaml"
        alias.symlink_to(policy)
        policy = alias
    argv = ["ci-repair-pr", action]
    argv += [str(prepared)] if action == "publish" else [str(run), "--output", str(prepared)]
    if action == "prepare":
        argv += ["--base", "main"]
    argv += ["--policy", str(policy)]
    monkeypatch.setattr("sys.argv", argv)
    monkeypatch.setattr(publish, action, lambda *a, **kw: pytest.fail("must reject before action"))
    with pytest.raises(SystemExit) as error:
        publish.main()
    assert error.value.code == 1
    assert "Policy must not be read from a repository under repair" in capsys.readouterr().err
