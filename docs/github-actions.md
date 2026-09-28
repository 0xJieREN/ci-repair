# Collect failed GitHub Actions runs

`ci-repair-github` uses read-only GitHub API access through an authenticated
`gh` CLI. It pins the checkout to the commit tested by one run attempt and
records logs and metadata before any repair starts. Collection itself runs no
repository commands, starts no container and makes no model calls.

## Current run-level path

```sh
uv run ci-repair-github OWNER/REPO RUN_ID --output runs/import
uv run ci-repair-run runs/import --policy ~/ci-repair-policy.yaml \
  --env-file .env --output runs/repair
```

Collection takes every job whose completed conclusion is `failure`, using
one pinned checkout. The `run.json` manifest contains the attempt, source SHA,
workflow path and each job's log and step metadata. Timed-out or cancelled jobs
are recorded separately; they prevent a run-level PASS and publication. Existing
output directories are never overwritten. Use `--attempt NUMBER` for an earlier
rerun; otherwise collection pins the latest attempt visible at its start.

Supported events are same-repository `push`, `workflow_dispatch` and
`pull_request`. Forks and `pull_request_target` are refused. For a PR run, the
collector derives the checkout SHA from each failed job log only when all of
them agree; otherwise provide an exact `--checkout-sha` after reviewing the
job logs. See [PR provenance](pull-requests.md). A missing or mismatched SHA
fails closed rather than using today's mutable merge ref.

The [environment reconstructor](environment.md) derives commands and setup
from the pinned workflow plus job metadata. It supports a deliberate subset of
Actions syntax and marks uncertain or unsupported jobs for review; raw log text
never becomes an executable command.

## Live collection fixture

`.github/workflows/repair-fixture.yml` is a manual-only, intentionally failing
workflow on `examples/buggy`. It never runs on push or PR and uses no model key.
Dispatch it and inspect its run ID:

```sh
gh workflow run repair-fixture.yml --repo 0xJieREN/ci-repair
gh run list --repo 0xJieREN/ci-repair --workflow repair-fixture.yml
uv run ci-repair-github 0xJieREN/ci-repair RUN_ID --output runs/github-smoke
```

The example policy allows only `examples/buggy/src/` for this repository. Local
tests simulate GitHub PR responses and never create an actual PR; live
publication was exercised once in the [acceptance run](acceptance.md).
