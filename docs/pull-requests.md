# PR provenance and draft repair publication

v0.3 added explicit PR checkout provenance and a two-step publication command.
v0.4 adds a deterministic publication gate and `ci-repair-pr auto` (see below).
Fork PRs, `pull_request_target`, automatic merge and automatic retries remain
unsupported.

## Collect a PR run

A PR workflow may check out its branch head or GitHub's temporary merge commit.
Do not assume that the run's head SHA or today's `refs/pull/N/merge` is what the
failed job executed. Read the checkout step in the selected job's logs, then
supply its exact 40-character SHA:

```sh
uv run ci-repair-github OWNER/REPO RUN_ID --job-id JOB_ID \
  --checkout-sha EXACT_TESTED_SHA --output runs/pr-import
```

The run must identify exactly one same-repository PR. For head checkout, the
provided SHA must equal the recorded PR head. For merge checkout, the GitHub
commit API must report exactly the recorded base and head, in that order, as
its parents. The collector pins that immutable commit; it does not resolve the
current merge ref. Missing provenance, fork identity, and mismatched parents
fail closed. Specifying the checkout SHA is an operator assertion about what
the job executed; the tool does not parse arbitrary checkout scripts or prove
that a workflow used the default checkout.

Repair with the existing `ci-repair --ci-context ...` command. Reports retain
checkout kind, PR number, original head/base SHAs and branch names. Every new
repair report also records the SHA-256 digest of its exact patch.

## Prepare a reviewable repair PR

```sh
uv run ci-repair-pr prepare runs/verified-repair \
  --base ORIGINAL_FAILED_BRANCH --output runs/publication
```

Preparation requires an independently verified PASS report, passing original
and regression checks for every addressed job, a matching nonempty patch digest
and CI source provenance. It checks
that the remote target branch still equals the verified SHA, then creates a
new private local checkout, applies the patch, validates source paths/file
modes, and creates one local commit. It writes `publication.json`, `patch.diff`
and `body.md`. No tests or repository programs are executed on the host, and
preparation makes no remote writes. Existing directories are not overwritten.

The target is the failed branch: for a PR input, this is the original PR's head
branch, so the repair PR is stacked onto that branch. Only head-verified repairs
can be published. A merge-verified patch must first be applied and independently
verified against the original PR head; automatic transfer/reverification is not
implemented. Older reports without patch digests must also be verified again.

The report/manifest/digest checks detect accidental stale or changed artifacts;
they are not signatures and do not authenticate reports from untrusted parties.
Only use locally produced evidence. Review the prepared patch and PR body.

## Publish explicitly

```sh
uv run ci-repair-pr publish runs/publication
```

This checks the prepared commit, parent, clean checkout, diff digest, original
run evidence and current remote base again. It pushes to a deterministic repair
branch with a creation-only Git lease (the expected remote ref is absent).
Existing branches with different commits are never overwritten. Then it creates
a **draft** PR with a concise verification summary. Credentials, full logs,
trajectories, test command strings and local filesystem paths are not included
in the generated PR body.

Retry with the same preparation directory after an interrupted publish. If the
remote branch and open PR match, the existing PR is returned. Closed/conflicting
PRs are not duplicated. If a branch was pushed but PR creation failed, it remains
available for inspection; there is no destructive rollback. If the target moves,
recollect and reverify instead of rebasing a supposedly verified patch.

The base is checked before and after push. GitHub offers no transaction spanning
branch push, base-branch changes and PR creation, so a final concurrent base
update is still possible; the draft needs normal review and GitHub CI. It is
never automatically merged.

## Verification boundaries

Local tests use actual Git commits and a bare remote for preparation, push,
conflict and retry behavior; only GitHub's PR HTTP boundary is simulated. Docker
tests continue to exercise the real agent loop and independent verifier without
paid model calls. An actual GitHub draft PR is created only by the explicit
`publish` command, not as a side effect of running the test suite.

Sources checked 2026-09-18:
[GitHub PR workflow semantics](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#pull_request),
[workflow-run metadata](https://docs.github.com/en/rest/actions/workflow-runs),
[gh draft PR creation](https://cli.github.com/manual/gh_pr_create).

## Publication gate (v0.4)

`publication_gate` combines verified evidence with the current
[policy](policy.md) into ALLOW / REVIEW / DENY. Inputs: patch categories and
size, `publication.draft_pr`/`require_human_review`, the trigger allowlist for
repository/event/branch, the stop reason, and, for orchestrated runs, each job's
environment status, architecture mismatch and baseline-vs-CI-log evidence.
Manual single-job runs are always REVIEW because their environment was
configured by hand.

| Verdict | `prepare` | `publish` (operator) | `auto` |
|---|---|---|---|
| ALLOW | yes | yes | prepares and creates the draft PR |
| REVIEW | yes | yes: the operator is the reviewer | stops with `REVIEW_REQUIRED`, no remote writes |
| DENY | refused | refused | stops with `DENIED` |

`publish` re-evaluates the gate with the current policy; `publication.json`,
`published.json` and the PR body record the verdict and reasons. The PR body
also lists per-job results, final verification, environment fidelity, stop
reason, model calls and estimated cost. `auto` targets the run's head branch;
merge-checkout PR repairs stay unpublishable.
