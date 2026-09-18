import sys

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
    monkeypatch.setattr(cli, "run", lambda *args: {"status": "PASS", "verified": True})
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


def test_upstream_factory_constructs_tool_adapters_without_api_calls(monkeypatch):
    from minisweagent.models import get_model

    monkeypatch.setenv("OPENROUTER_API_KEY", "dummy-only")
    for adapter in ("litellm", "openrouter"):
        model = get_model("anthropic/test", config={"model_class": adapter})
        assert model.config.set_cache_control == "default_end"
        assert model.config.model_name == "anthropic/test"
