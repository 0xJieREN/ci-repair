import os

from ci_repair.model import make_model


def test_factory_uses_current_environment_and_disables_retries(monkeypatch):
    import minisweagent.models

    seen = {}

    def factory(name, config):
        seen.update(name=name, config=config)
        return object()

    monkeypatch.setattr(minisweagent.models, "get_model", factory)
    monkeypatch.setenv("LITELLM_MODEL_REGISTRY_PATH", "pricing.json")
    monkeypatch.setenv("MSWEA_MODEL_RETRY_STOP_AFTER_ATTEMPT", "5")
    make_model("deepseek/test")
    assert seen["name"] == "deepseek/test"
    assert seen["config"]["litellm_model_registry"] == "pricing.json"
    assert seen["config"]["model_kwargs"]["num_retries"] == 0
    assert seen["config"]["cost_tracking"] == "default"
    assert os.environ["MSWEA_MODEL_RETRY_STOP_AFTER_ATTEMPT"] == "1"


def test_upstream_factory_constructs_tool_adapters_without_api_calls(monkeypatch):
    from minisweagent.models import get_model

    monkeypatch.setenv("OPENROUTER_API_KEY", "dummy-only")
    for adapter in ("litellm", "openrouter"):
        model = get_model("anthropic/test", config={"model_class": adapter})
        assert model.config.set_cache_control == "default_end"
        assert model.config.model_name == "anthropic/test"
