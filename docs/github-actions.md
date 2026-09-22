# v0.2: import a failed GitHub Actions job

Since v0.4, `--all-jobs` collects every failed job of the attempt (one shared
checkout, `run.json`, per-job logs and manifests with step conclusions and runner
labels) for `ci-repair-run`. For PR runs the checkout SHA may be derived from the
checkout step's log when all failed jobs agree; it is still verified against the
API exactly as an explicit `--checkout-sha`. See the [lifecycle](lifecycle.md).

The next slice after the local MVP is read-only Actions collection. Run it
manually; it does not create branches, PRs, comments, or webhook services.
The existing mini-SWE-agent loop and independent verifier remain unchanged.

## Collect

Requires `gh auth login` with read access to the source repository and Actions.
GitHub.com completed failures from same-repository `push`, `workflow_dispatch`,
and (since v0.3) explicit-SHA `pull_request` events are supported. The workflow must have tested its
reported `head_sha` with the default checkout; custom checkout refs are not
inferred. For PR head/merge provenance and fork restrictions, see
[PR collection](pull-requests.md).

```sh
uv run ci-repair-github OWNER/REPO RUN_ID --output runs/import-01
# If several jobs failed, add --job-id JOB_ID.
# To select an earlier rerun, add --attempt ATTEMPT_NUMBER.
```

The collector resolves the attempt once, lists that attempt's jobs (including
pagination), and downloads only the selected failed job's log. It creates a
fresh detached checkout of the exact run SHA under `runs/import-01/repo`, along
with `failure.log` and `ci-context.json`. The manifest records workflow/job/step
names, run link, attempt, SHA and a SHA-256 hash of the raw log. Existing output
directories are never overwritten. An incomplete collection has no completed
manifest and must be retried with a new output directory.

No repository commands, dependencies, or workflow steps are executed by the
collector. Commands and sandbox image are still explicit operator inputs.
Logs and repository content are untrusted data; review them before sending them
to a model provider. Collection itself makes no model calls and starts no Docker VM.

## Repair

Start Docker before a local repair, and always stop Colima after the experiment,
including failures. Keep the repair output outside the imported source checkout.

```sh
colima start
trap 'colima stop' EXIT INT TERM
uv run ci-repair runs/import-01/repo \
  --failure-log runs/import-01/failure.log \
  --ci-context runs/import-01/ci-context.json \
  --image YOUR_PREPARED_IMAGE \
  --model deepseek/deepseek-flash --env-file .env \
  --test 'YOUR_ORIGINAL_FAILING_COMMAND' \
  --regression 'YOUR_REGRESSION_COMMAND' \
  --allow src/ --output runs/repair-01
```

The repair pipeline checks that the manifest commit matches local HEAD and the
log hash matches the supplied file before invoking the model. The context and
report retain the CI provenance. A mismatch yields ERROR without model calls.
This guards accidental mixing of inputs, not deliberate manifest forgery.

## Repeatable live smoke test

This repository includes `repair-fixture.yml`, a **manual-only, intentionally
failing** workflow on the existing interval fixture. It never runs on pushes or
PRs and uses no model credentials. Dispatch it, obtain its run ID, then collect:

```sh
gh workflow run repair-fixture.yml --repo 0xJieREN/ci-repair
gh run list --repo 0xJieREN/ci-repair --workflow repair-fixture.yml
uv run ci-repair-github 0xJieREN/ci-repair RUN_ID --output runs/github-smoke
```

For that fixture, use the prepared `ci-repair-demo:local` image, allow
`examples/buggy/src/`, and execute tests with
`cd examples/buggy && python -m unittest discover -s tests` (add
`-k test_positive_interval` for the original failing command).

API sources checked 2026-09-18:
[workflow runs](https://docs.github.com/en/rest/actions/workflow-runs),
[attempt jobs and individual job logs](https://docs.github.com/en/rest/actions/workflow-jobs).

PR checkout provenance and explicit draft publication are available in v0.3;
see [publication](pull-requests.md). Automatic triggers and command/test selection
remain deferred.
