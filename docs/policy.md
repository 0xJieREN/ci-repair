# Operator policy

Safety boundaries are decided by code from an operator-owned YAML file, not by
the model and not by prompts. See the annotated
[example](../config/policy.example.yaml). Without `--policy`, the CLIs use the
built-in defaults below; library callers that pass no policy (evaluation
corpora) keep the permissive legacy rules (paths and file modes only).

## Trust boundary

- The policy is loaded from outside every repository under repair; a path inside
  the target checkout (or a webhook state directory) is refused. Patches can
  never touch `.git/`, `.ci-repair/` or `ci-repair-policy*`, whatever the policy says.
- Repository-local policy is intentionally unsupported: a failing PR must not be
  able to relax the rules that govern its own repair.
- Unknown keys, wrong types, non-finite or non-positive numbers and duplicate
  YAML keys are errors. Capabilities this version cannot enforce are fixed:
  `sandbox.repair_network: DENY`, `sandbox.secrets: DENY`,
  `publication.auto_merge: DENY`; setting anything else fails closed.
- Reports record the policy source and SHA-256. `ci-repair-pr publish` evaluates
  the **current** policy again, so a prepared plan never carries old permissions.

## Schema (defaults)

| Key | Default | Effect |
|---|---|---|
| `repositories[]` | `[]` | `name`, `branches` (fnmatch), optional `allowed_paths`; trigger and auto-publish allowlist |
| `triggers.events` | push, workflow_dispatch, pull_request | `pull_request_target` and forks are always DENY |
| `models.allowed` / `default` | `["*"]` / none | fnmatch allowlist; default for webhook and `ci-repair-run` |
| `budget.max_model_calls` | 30 | one mini step is one model call |
| `budget.max_cost_usd` | 1.0 | estimated; upstream checks between calls |
| `budget.max_wall_seconds` / `max_command_seconds` | 600 / 60 | per repair attempt / per command |
| `budget.max_setup_seconds` | 900 | environment build |
| `patch.allowed_paths` | `["."]` | used when no repository entry overrides it |
| `patch.max_changed_files` | review 5, deny 20 | |
| `patch.max_changed_lines` | review 200, deny 1000 | |
| `patch.categories` | workflows DENY, generated DENY; lockfiles, dependency_manifests, tests, config, binary REVIEW | strictest verdict wins |
| `sandbox.setup_network` | ALLOW | network only while building the replay image |
| `publication.draft_pr` | REVIEW | ALLOW enables `ci-repair-pr auto` |
| `publication.require_human_review` | false | forces REVIEW |
| `repair.early_stop` | true | verifier probes after patch-changing steps |
| `repair.max_rejected_submissions` / `max_probes` / `max_jobs` | 2 / 10 / 5 | |

Categories are fixed path patterns in `policy.py` (for example `.github/*`,
`*.lock`, `pyproject.toml`, `tests/`, `test_*.py`, `*_pb2.py`, `dist/*`, `tox.ini`).
A change can be within `allowed_paths` and still be REVIEW or DENY by category.

## Budgets: requested → maximum → effective

CLI flags such as `--steps` are *requests*. An omitted request uses the policy
maximum; a larger one is clamped and listed in `budget.clamped`. The run records
all three values next to actual `usage`, which makes runs comparable across
agents and models.

## What the agent sees

The task context includes a short policy summary (allowed prefixes, forbidden
and review-only categories, no network or secrets) to avoid wasted attempts.
Enforcement happens only outside the agent: in the submission gate, before
verification and again at publication.
