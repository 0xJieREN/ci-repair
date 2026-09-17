"""Explicit operator inputs, provider credentials remain on the host."""

import argparse
import os
from datetime import datetime, timezone
from pathlib import Path

from ci_repair.pipeline import Config, run


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo", type=Path)
    parser.add_argument("--failure-log", type=Path, required=True)
    parser.add_argument(
        "--image", required=True, help="Prepared local Docker image with git and bash"
    )
    parser.add_argument("--test", required=True, help="Original failing command")
    parser.add_argument("--regression", required=True)
    parser.add_argument(
        "--allow", action="append", default=None, help="Allowed source prefix; repeatable"
    )
    parser.add_argument(
        "--env-file", type=Path, help="Explicit local dotenv file (never committed)"
    )
    parser.add_argument("--model", required=True, help="LiteLLM provider/model")
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--steps", type=int, default=30)
    parser.add_argument("--cost", type=float, default=1.0)
    parser.add_argument("--wall-seconds", type=int, default=600)
    parser.add_argument("--command-seconds", type=int, default=60)
    args = parser.parse_args()
    config = Config(
        repo=args.repo.resolve(),
        failure_log=args.failure_log.resolve(),
        output=(
            args.output or Path("runs") / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        ).resolve(),
        image=args.image,
        failing_command=args.test,
        regression_command=args.regression,
        allowed_paths=tuple(args.allow or ["src/"]),
        steps=args.steps,
        cost=args.cost,
        wall_seconds=args.wall_seconds,
        command_seconds=args.command_seconds,
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
    # Never put secrets in model_kwargs: mini serializes those into trajectory.json.
    if os.getenv("ANTHROPIC_AUTH_TOKEN") and not os.getenv("ANTHROPIC_API_KEY"):
        os.environ["ANTHROPIC_API_KEY"] = os.environ["ANTHROPIC_AUTH_TOKEN"]
    os.environ["MSWEA_MODEL_RETRY_STOP_AFTER_ATTEMPT"] = "1"
    from minisweagent.models.litellm_model import LitellmModel

    model = LitellmModel(
        model_name=args.model,
        cost_tracking="default",
        observation_template="returncode={{output.returncode}}\n{{output.output[:12000]}}\n"
        "{% if output.output|length > 12000 %}[output truncated]{% endif %}"
        "{{output.exception_info}}",
        model_kwargs={"timeout": min(90, args.wall_seconds), "num_retries": 0, "max_tokens": 4096},
    )
    report = run(config, model)
    print(f"{report['status']}: {config.output / 'report.json'}")
    return 0 if report["verified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
