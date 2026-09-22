# Reviewed CI replay plans

Manual path. The automatic path (`ci-repair-run`, webhook) derives and builds the
environment itself; see [environment reconstruction](environment.md).

`ci-repair-plan` reads a collected source checkout and the workflow **at that
commit**, matches the collected job and failed step, and writes a draft JSON
plan. It does not execute the workflow, install dependencies, start Docker, or
call a model.

```sh
uv run ci-repair-plan runs/collected --output runs/repair-plan.json

# Older collections without workflow_path require an explicit selection:
uv run ci-repair-plan runs/collected \
  --workflow .github/workflows/ci.yml --output runs/repair-plan.json
```

The output must not already exist. The plan records source commit, log and
workflow hashes, the selected job/step, runner, shell, preceding setup steps,
and the exact workflow location of the extracted command. An existing plan is
never overwritten. Evidence and setup steps remain untrusted, review-only data.

Before execution, edit the draft:

- Supply `execution.image`: a prepared image containing Bash, GNU timeout, Git,
  the runtime and all dependencies. Prior workflow setup actions are **not** run.
- Supply `execution.regression_command` and explicit `execution.allowed_paths`.
  Allowed paths are relative to the repository root, even for nested jobs.
- Review `execution.failing_command` and `working_directory`. Each verifier runs
  in a separate fresh container; commands must include their own required setup.
- Set `reviewed` to `true` only after checking the above. Do not store credentials
  in commands or plans; runtime repair containers remain network-disabled.

```sh
uv run ci-repair --plan runs/repair-plan.json \
  --model deepseek/deepseek-flash --env-file .env --steps 30 --cost 1 \
  --output runs/repair-from-plan
```

This explicit command starts model/Docker work. Standard Docker lifecycle rules
apply: start when needed, then stop Colima and verify it stopped after the run.
Plan mode cannot be combined with manual source/image/test/path flags. Existing
manual CLI usage remains supported. Commands execute relative to the reviewed
working directory; Bash fail-fast behavior is retained, including pipefail when
`bash` was explicitly selected in the workflow.

## Deliberately limited first version

Supported: a uniquely matched static Ubuntu job, one failed shell step, default
or explicit Bash, and static working-directory defaults/overrides. The plan's
runtime/setup evidence comes from the preceding steps; it is not an inferred,
ready-made Docker image.

Matrix strategies, reusable jobs, services, job containers, dynamic expressions,
environment propagation, conditions and nonstandard runners are blocked. Missing
or ambiguous job/step mappings, YAML aliases and duplicate keys are rejected.
Unsupported features are rechecked against source before execution, even if
someone deletes the draft's blocker list. For unsupported workflows use explicit
operator configuration; this is not a general GitHub Actions interpreter.

Execution rechecks the clean source HEAD, collected CI manifest, failure log and
workflow hash. A changed source or log requires a new plan. `reviewed` is a local
operator acknowledgement, not a signature or a security boundary against someone
who can edit all local evidence.

Workflow precedence and shell behavior were checked against the official
[GitHub workflow syntax](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax).
