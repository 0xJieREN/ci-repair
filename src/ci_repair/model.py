"""Model adapter construction; provider credentials remain on the host."""

import os


def make_model(name: str, model_class: str = "litellm", wall_seconds: int = 600):
    # Never put secrets in model_kwargs: mini serializes those into trajectory.json.
    if os.getenv("ANTHROPIC_AUTH_TOKEN") and not os.getenv("ANTHROPIC_API_KEY"):
        os.environ["ANTHROPIC_API_KEY"] = os.environ["ANTHROPIC_AUTH_TOKEN"]
    os.environ["MSWEA_MODEL_RETRY_STOP_AFTER_ATTEMPT"] = "1"
    from minisweagent.models import get_model

    model_kwargs = {"max_tokens": 4096}
    if model_class == "litellm":
        model_kwargs.update(timeout=min(90, wall_seconds), num_retries=0)
    return get_model(
        name,
        config={
            "model_class": model_class,
            "cost_tracking": "default",
            # Preserve failure tails without feeding unbounded command output into the model.
            "observation_template": "returncode={{output.returncode}}\n"
            "{% if output.output|length <= 12000 %}{{output.output}}{% else %}"
            "{{output.output[:6000]}}\n[output truncated]\n{{output.output[-6000:]}}{% endif %}"
            "\n{{output.exception_info}}",
            "model_kwargs": model_kwargs,
            # Read at call time, not the upstream class's import-time environment default.
            "litellm_model_registry": os.getenv("LITELLM_MODEL_REGISTRY_PATH"),
        },
    )
