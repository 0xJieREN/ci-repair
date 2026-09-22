import sys

import pytest

from ci_repair import cli


def test_cli_uses_upstream_factory_and_current_environment(tmp_path, monkeypatch):
    import minisweagent.models

    env = tmp_path / "model.env"
    env.write_text("LITELLM_MODEL_REGISTRY_PATH=pricing.json\n")
    seen = {}

    def factory(name, config):
        seen.update(name=name, config=config)
        return object()

    monkeypatch.setattr(minisweagent.models, "get_model", factory)
    seen_run = {}

    def run(config, model, policy):
        seen_run.update(config=config, policy=policy)
        return {"status": "PASS", "verified": True, "stop_reason": "VERIFIED_PASS"}

    monkeypatch.setattr(cli, "run", run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "ci-repair",
            str(tmp_path / "repo"),
            "--failure-log",
            str(tmp_path / "failure.log"),
            "--image",
            "image",
            "--test",
            "test",
            "--regression",
            "regression",
            "--model",
            "deepseek/test",
            "--env-file",
            str(env),
            "--output",
            str(tmp_path / "output"),
        ],
    )
    monkeypatch.setenv("LITELLM_MODEL_REGISTRY_PATH", "stale.json")
    monkeypatch.setenv("MSWEA_MODEL_RETRY_STOP_AFTER_ATTEMPT", "5")
    assert cli.main() == 0
    assert seen["name"] == "deepseek/test"
    assert seen["config"]["litellm_model_registry"] == "pricing.json"
    assert seen["config"]["model_kwargs"]["num_retries"] == 0
    assert seen["config"]["cost_tracking"] == "default"
    # Unrequested budgets become the builtin policy maxima.
    assert seen_run["config"].steps == 30
    assert seen_run["policy"].source == "builtin"


def test_cli_clamps_requested_budget_and_rejects_policy_inside_repo(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    policy = tmp_path / "policy.yaml"
    policy.write_text("budget: {max_model_calls: 5}\nmodels: {default: deepseek/x}\n")
    seen = {}
    monkeypatch.setattr(cli, "make_model", lambda *args: seen.setdefault("model", args))
    monkeypatch.setattr(
        cli,
        "run",
        lambda config, model, p: (
            seen.update(config=config, policy=p)
            or {"status": "PASS", "verified": True, "stop_reason": "VERIFIED_PASS"}
        ),
    )
    base = ["ci-repair", str(repo), "--failure-log", "log", "--image", "i", "--test", "t"]
    base += ["--regression", "r", "--output", str(tmp_path / "out"), "--steps", "50"]
    monkeypatch.setattr(sys, "argv", [*base, "--policy", str(policy)])
    assert cli.main() == 0
    assert seen["config"].steps == 50  # requested; run() applies the policy maximum
    assert seen["model"][0] == "deepseek/x"
    assert seen["policy"].budget({"steps": 50})["effective"]["steps"] == 5
    inside = repo / "policy.yaml"
    inside.write_text("version: 1\n")
    monkeypatch.setattr(sys, "argv", [*base, "--model", "m", "--policy", str(inside)])
    with pytest.raises(SystemExit):
        cli.main()


def test_upstream_factory_constructs_tool_adapters_without_api_calls(monkeypatch):
    from minisweagent.models import get_model

    monkeypatch.setenv("OPENROUTER_API_KEY", "dummy-only")
    for adapter in ("litellm", "openrouter"):
        model = get_model("anthropic/test", config={"model_class": adapter})
        assert model.config.set_cache_control == "default_end"
        assert model.config.model_name == "anthropic/test"
