# CI Repair

CI Repair uses mini-SWE-agent to repair failed GitHub Actions runs. It collects
all failed jobs from a pinned run attempt, reconstructs supported CI environments,
repairs them in order, verifies one cumulative patch in fresh Docker containers,
and can create a gated **draft** PR. It never merges automatically.

```text
workflow_run failure → signed webhook → canonical GitHub admission
→ collect failed jobs → reconstruct environments → bounded repair
→ independent verification → policy gate → draft PR or review
```

The [repair lifecycle](docs/lifecycle.md) describes the current system. It is an
alpha: local tests cover the workflow, but a live webhook-to-draft-PR acceptance
run remains to be done. Unsupported workflow features fail closed.

## Run the automatic flow

Requires Python 3.12+, [uv](https://docs.astral.sh/uv/), Git,
[GitHub CLI](https://cli.github.com/) authentication, and Docker. On macOS use
Colima for the Docker engine. Keep the operator policy outside every repository
being repaired; keep model credentials private in the ignored `.env` file.

```sh
uv sync --locked
cp -n config/policy.example.yaml ~/ci-repair-policy.yaml
cp -n .env.example .env
# Edit the repository, branch, path, model and budget rules in the policy.
# Put the DeepSeek key in .env; both files must stay private.
export CI_REPAIR_WEBHOOK_SECRET=...  # the GitHub webhook secret
uv run ci-repair-webhook serve --state-dir ~/ci-repair-state \
  --policy ~/ci-repair-policy.yaml --env-file .env
```

Subscribe the GitHub webhook to **Workflow runs**, and put the listener behind
TLS. The example policy sets `publication.draft_pr: ALLOW` for the intentional
fixture. Review permissions and secrets in the target repository's branch
workflows before using it elsewhere; set the gate to `REVIEW` if publication
should require an operator. See [policy](docs/policy.md),
[environment reconstruction](docs/environment.md) and
[publication](docs/pull-requests.md).

The same run can be processed manually without the webhook:

```sh
uv run ci-repair-github OWNER/REPO RUN_ID --all-jobs --output runs/import
uv run ci-repair-run runs/import --policy ~/ci-repair-policy.yaml \
  --env-file .env --output runs/repair
uv run ci-repair-pr auto runs/repair --policy ~/ci-repair-policy.yaml \
  --output runs/publication
```

The original single-job `ci-repair` and review-only `ci-repair-plan` commands
remain available for operator-controlled experiments. The
[GitHub Actions guide](docs/github-actions.md) includes an intentionally failing
fixture for a live collection smoke test.

## Verify changes

```sh
bash scripts/check.sh --unit    # fast checks without Docker
bash scripts/check.sh --colima  # macOS: full gate; stops Colima on exit
bash scripts/check.sh           # Linux/server: full gate with running Docker
```

The full gate runs locked dependency sync, lint and format checks, Docker
integration tests, and Git diff checks. GitHub Actions uses the same script.
No model API key or paid inference is needed. See
[verification boundaries](docs/verification.md) for what local tests simulate.

The repository retains the deliberately broken `examples/buggy` fixture because
the local repair tests and manual GitHub workflow use it. Local run outputs and
`.env` are Git-ignored. `config/deepseek-pricing.json` provides estimates, not
provider billing receipts. Prior benchmark conclusions are condensed in
[research results](docs/research-results.md); the old research code and data
remain retrievable from Git history.
