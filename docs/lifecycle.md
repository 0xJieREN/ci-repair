# Repair lifecycle (v0.4)

This is the entry point for the automatic system. Details live in
[policy](policy.md), [environment reconstruction](environment.md) and
[PR publication](pull-requests.md). The earlier manual paths (`ci-repair`,
`ci-repair-plan`, `ci-repair-pr prepare/publish`) still work unchanged.

```text
CI failure (workflow_run completed/failure)
 → trigger      ci-repair-webhook: signature, filter, dedup, enqueue (executes nothing)
 → admission    worker re-reads canonical API state + policy (attempt, head, fork, branch)
 → collect      all failed jobs of the attempt, one pinned checkout, per-job logs/steps
 → reconstruct  workflow + job metadata + log → hashed spec: SUPPORTED/REVIEW/UNSUPPORTED
 → build        setup steps once in a disposable container → content-addressed image
 → policy       budgets, model, allowed paths, patch categories (outside the agent)
 → repair       per job, ordered; baseline on original; skip if prior patch fixes it
 → stop         submit gate / verified early stop / limits → agent_exit + stop_reason
 → verify       every addressed job re-verified with the cumulative patch, fresh containers
 → gate         ALLOW / REVIEW / DENY from evidence + current policy
 → Draft PR     only on ALLOW (auto) or by an operator (REVIEW); never merged
```

## How the existing architecture was extended

Before this version the pipeline was: operator inputs → `snapshot` → baseline
in a fresh container → one `DefaultAgent` run → `extract_patch` →
`verify_patch` in fresh containers → local report; `github.collect` imported one
job; `plan` extracted one static step; `publish` made a creation-only draft PR
after a local review. Those modules are reused, not replaced:

| Concern | Module | Change |
|---|---|---|
| Policy | `policy.py` (new) | Strict YAML, `ALLOW/REVIEW/DENY`, budgets, categories |
| Stop decision | `agent.py` (new), `pipeline.py` | `RepairAgent` subclasses mini's loop; gate + probes |
| Environment | `reconstruct.py` (new) | Uses `plan.py` loader/path helpers; builds images |
| Collection | `github.py` | `collect_run`; `collect` shares the same helpers |
| Multi-job | `orchestrate.py` (new) | Uses `pipeline.run` and `verify_patch` per job |
| Publication | `publish.py` | `publication_gate`, `auto`; prepare/publish kept |
| Trigger | `webhook.py` (new) | stdlib HTTP + SQLite queue, single worker |

The independent fresh-environment verifier remains the only thing that can
make a run `PASS`. The agent never decides that a repair succeeded.

## Stop mechanism

"The agent thinks it is done" and "the system ends the repair" are separate:

- **Submission is a request.** The mini sentinel
  (`echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT`) calls a deterministic gate:
  extract the patch (empty → accept as no patch), apply policy (DENY → reject with
  reasons), then run the failing and regression commands in fresh containers.
  A rejected submission returns bounded feedback to the agent; after
  `repair.max_rejected_submissions` rejections the attempt ends.
- **Verifier-triggered early stop.** After each step that changed the patch, the
  same gate runs as a silent probe (at most `repair.max_probes`, cached by patch
  digest). When it passes, the system stops the agent immediately, so a correct
  patch cannot be overwritten and no further model calls are spent. Probes use a
  throwaway Git index, so the agent's own `git diff` view is unaffected.
- **Limits** are reported separately: step, cost and wall time.

`agent_exit` records why the loop ended: `SUBMITTED`, `SUBMITTED_NO_PATCH`,
`EARLY_STOP_VERIFIED`, `SUBMISSION_REJECTED`, `STEP_LIMIT`, `COST_LIMIT`,
`WALL_TIME_LIMIT`, `FORMAT_ERROR`. `stop_reason` records the system outcome:

| stop_reason | Meaning |
|---|---|
| `VERIFIED_PASS` | independent verification passed (even if a limit was hit) |
| `NO_PATCH` | nothing changed |
| `POLICY_DENIED` | model, patch or publication refused by policy |
| `BASELINE_NOT_REPRODUCED` | replay did not fail like CI (not an agent failure) |
| `VERIFICATION_FAILED` | patch exists but failed verification |
| `UNSUPPORTED_ENVIRONMENT` | workflow outside the supported subset, or setup failed |
| `STALE_SOURCE` | collected evidence no longer matches the source |
| `STEP_LIMIT` / `COST_LIMIT` / `WALL_TIME_LIMIT` | budget ended an unsuccessful attempt |
| `EXECUTION_ERROR` | infrastructure/model error; `error_phase` says where |

The existing `status` field (`PASS`, `FAIL`, `NO_PATCH`, `PATCH_REJECTED`,
`BASELINE_NOT_REPRODUCED`, `TIMEOUT`, `ERROR`, plus `POLICY_DENIED`) is kept for
report consumers. Reports also record `budget.requested`,
`budget.policy_max`, `budget.effective`, `budget.clamped` and `usage`
(model requested, models reported by the provider, model calls, agent steps,
submissions, probes, estimated cost, command seconds, wall seconds).

## Multi-job runs

```sh
uv run ci-repair-github OWNER/REPO RUN_ID --all-jobs --output runs/run-import
uv run ci-repair-run runs/run-import --policy ~/ci-repair/policy.yaml --env-file .env
uv run ci-repair-pr auto runs/run-<timestamp> --policy ~/ci-repair/policy.yaml \
  --output runs/publication
```

Order is deterministic and explained in `report.order`: transitive `needs`
first, then workflow position, job name and job ID. For each job:
reconstruct → build → baseline on the **original** source → if the cumulative
patch already passes this job's checks, mark `FIXED_BY_PRIOR` (no agent) →
otherwise repair on a candidate repository that contains earlier repairs →
commit. Finally every `REPAIRED`/`FIXED_BY_PRIOR` job is re-verified with the one
cumulative patch against the original snapshot; a job broken by a later patch
becomes `REGRESSED` and the run fails. Run status: `PASS` (every failed job
verified), `PARTIAL`, `FAIL`, `UNSUPPORTED_ENVIRONMENT`, `PATCH_REJECTED`,
`ERROR`. Only `PASS` is publishable, as one draft PR.

## Webhook service

```sh
export CI_REPAIR_WEBHOOK_SECRET=...   # same secret as the GitHub webhook / App
uv run ci-repair-webhook serve --state-dir /var/lib/ci-repair \
  --policy /etc/ci-repair/policy.yaml --env-file /etc/ci-repair/model.env
# or: serve --no-worker, plus `work --once` from cron; `requeue KEY` after review
```

Subscribe the webhook (or GitHub App) to **Workflow runs**. Put the listener
behind TLS. Intake verifies `X-Hub-Signature-256` in constant time, caps bodies
at 2 MiB, accepts only `workflow_run`/`completed`/`failure` for allowlisted
repositories, branches and events, and stores delivery IDs and a unique
`repository#run#attempt` key, so redeliveries never start a second repair.
The worker ignores payload details and re-checks via the API: the attempt is
still the latest, the branch (or open PR) head still equals the run's
`head_sha`, the head repository is not a fork, and policy admits it. One repair
runs per repository/branch; an expired lease becomes `interrupted` and is never
rerun silently. Stored errors contain only exception types.

GitHub API access uses `gh` authentication (`GH_TOKEN` works, including an
installation token minted for a GitHub App). Minting App tokens is not built in.

## Human review, reassessed

The deterministic gates now cover what the local pre-publish review used to
check by hand: verification evidence, digest, paths/modes, change categories,
size, provenance, base branch movement and PR body hygiene. The Draft PR is the
review interface. What still needs a human, and therefore yields `REVIEW`:
changes to tests, lockfiles, dependency manifests or config; large or binary
patches; approximated environments, architecture mismatch or a replayed
failure that differs from the CI log; manual single-job runs; untrusted
repositories/branches; and any policy that requests review.

One risk no deterministic check removes: pushing a repair branch runs the
repository's `push` workflows on agent-written code, with whatever secrets and
token permissions those workflows have. The built-in policy therefore keeps
`publication.draft_pr: REVIEW`; enable `ALLOW` only for repositories whose
branch workflows have minimal permissions and no sensitive secrets.

## Intentionally unsupported (fail closed)

- Automatic merge (fixed `DENY`), network or secrets during repair/verification.
- Fork PRs and `pull_request_target`; publication of merge-checkout PR repairs
  (the report is kept; transfer to the PR head is not implemented).
- GitHub Actions semantics outside [the supported subset](environment.md):
  services, reusable workflows, arbitrary actions, `GITHUB_ENV`/`OUTPUT`
  propagation, most expressions, non-Linux runners, multiple toolchains.
- Repository-local policy files (a PR could edit its own rules); submodules,
  LFS and full Git history in replay.
- Automatic retries, run-level budgets across jobs beyond `repair.max_jobs`,
  distributed workers, App token minting, notifications.

## What has and has not been exercised

Automated tests exercise the real mini loop, real Docker builds from
reconstructed specs, multi-job accumulation, early stop, probes, the HTTP
intake and the SQLite queue, and publication against a real bare Git remote.
GitHub API responses and PR creation are simulated in tests. As of this version
no live webhook delivery, live multi-job run or automatic live draft PR has been
performed; do that as a deliberate acceptance step on a low-privilege repository.
