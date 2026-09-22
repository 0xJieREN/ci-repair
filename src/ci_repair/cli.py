"""Explicit operator inputs, provider credentials remain on the host."""

import argparse
import os
from datetime import datetime, timezone
from pathlib import Path

from ci_repair.pipeline import Config, run
from ci_repair.policy import PolicyError, load_policy


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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo", type=Path, nargs="?")
    parser.add_argument("--plan", type=Path, help="Explicitly reviewed replay plan")
    parser.add_argument("--failure-log", type=Path)
    parser.add_argument("--ci-context", type=Path, help="Collected GitHub Actions manifest")
    parser.add_argument("--image", help="Prepared local Docker image with git and bash")
    parser.add_argument("--test", help="Original failing command")
    parser.add_argument("--regression")
    parser.add_argument(
        "--allow", action="append", default=None, help="Allowed source prefix; repeatable"
    )
    parser.add_argument(
        "--env-file", type=Path, help="Explicit local dotenv file (never committed)"
    )
    parser.add_argument(
        "--policy", type=Path, help="Operator policy YAML outside the repository (default: builtin)"
    )
    parser.add_argument("--model", help="Upstream adapter model name (default: policy default)")
    parser.add_argument(
        "--model-class",
        choices=("litellm", "openrouter"),
        default="litellm",
        help="mini-SWE-agent tool-calling adapter",
    )
    parser.add_argument("--output", type=Path, default=None)
    # Requested budgets; the policy maximum applies when omitted or exceeded.
    parser.add_argument("--steps", type=int, help="Maximum model calls")
    parser.add_argument("--cost", type=float, help="Maximum estimated USD")
    parser.add_argument("--wall-seconds", type=int)
    parser.add_argument("--command-seconds", type=int)
    args = parser.parse_args()
    if args.plan:
        if any(
            (
                args.repo,
                args.failure_log,
                args.ci_context,
                args.image,
                args.test,
                args.regression,
                args.allow,
            )
        ):
            parser.error("--plan cannot be combined with explicit repair inputs")
        from ci_repair.plan import load_plan

        try:
            inputs = load_plan(args.plan)
        except (ValueError, OSError, KeyError) as exc:
            parser.error(str(exc))
    else:
        if not all((args.repo, args.failure_log, args.image, args.test, args.regression)):
            parser.error("Provide repo, --failure-log, --image, --test and --regression, or --plan")
        inputs = dict(
            repo=args.repo.resolve(),
            failure_log=args.failure_log.resolve(),
            image=args.image,
            failing_command=args.test,
            regression_command=args.regression,
            allowed_paths=tuple(args.allow or ["src/"]),
            ci_context=args.ci_context,
        )
    try:
        policy = load_policy(args.policy, untrusted_roots=[inputs["repo"]])
    except (PolicyError, OSError) as exc:
        parser.error(f"Policy: {exc}")
    model_name = args.model or policy.data["models"]["default"]
    if not model_name:
        parser.error("Provide --model or a policy models.default")
    requested = dict(
        steps=args.steps,
        cost=args.cost,
        wall_seconds=args.wall_seconds,
        command_seconds=args.command_seconds,
    )
    effective = policy.budget(requested)["effective"]
    config = Config(
        **inputs,
        output=(
            args.output or Path("runs") / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        ).resolve(),
        # Unset requests become the policy maximum; run() records requested/max/effective.
        **{k: v if v is not None else effective[k] for k, v in requested.items()},
    )
    try:
        config.validate()
    except ValueError as exc:
        parser.error(str(exc))
    if args.env_file:
        if not args.env_file.is_file():
            parser.error("Environment file does not exist")
        from dotenv import load_dotenv

        load_dotenv(args.env_file, override=True)
    model = make_model(model_name, args.model_class, effective["wall_seconds"])
    report = run(config, model, policy)
    print(f"{report['status']} ({report['stop_reason']}): {config.output / 'report.json'}")
    return 0 if report["verified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
