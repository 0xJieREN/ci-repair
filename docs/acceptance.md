# Live acceptance run (2026-09-24)

One deliberate end-to-end run of the v0.4 automatic lifecycle against real
GitHub: signed webhook delivery over HTTPS, canonical admission, a three-job
failure, cumulative repair, the publication gate at `REVIEW` and then `ALLOW`,
and an automatically created draft PR. No code changes were needed; nothing
below was simulated unless marked.

## Setup

| Item | Value |
|---|---|
| ci-repair | `1822a92` (v0.4.0), local macOS arm64, Colima (aarch64) |
| Target | public [0xJieREN/ci-repair-acceptance](https://github.com/0xJieREN/ci-repair-acceptance): `contents: read` workflow, no secrets |
| Workflow | `push`; jobs `math`, `text`, `report` on `ubuntu-24.04-arm`, `setup-python` 3.12 |
| Failure | two bugs in one commit: `calc/mathutil.py` (off-by-one), `calc/text.py` (no lowercasing); `report` fails only through the first |
| Transport | `cloudflared` quick tunnel (HTTPS) → listener on `127.0.0.1`; repository webhook, `workflow_run` only, HMAC secret |
| Policy | outside both repositories; repository allowlist `main` only, `allowed_paths: [calc/]`, `max_cost_usd: 0.5`; `draft_pr` `REVIEW`, then `ALLOW` |
| Model | `deepseek/deepseek-flash` (provider reported `deepseek-flash`) |
| GitHub credential | the operator's existing `gh` OAuth token (`repo`, `workflow`, …); least privilege was **not** exercised |

The `-arm` runner is deliberate: an x64 job replayed on an arm64 host records
an architecture mismatch, which always yields `REVIEW`, so `ALLOW` could never
be reached. Only `main` is trusted so that a failing repair branch cannot
re-enter the queue.

## Results

| Check | Evidence | Result |
|---|---|---|
| Unsigned / wrong signature via public URL | HTTP 401 `invalid signature`, no state | pass |
| GitHub `ping` | 200 `pong` | pass |
| `requested` / `in_progress` actions | stored as `ignored` | pass |
| Failed run A (`220d68b`) | 202 `queued` | pass |
| GitHub redelivery of A (same GUID) | 200 `duplicate delivery`, still one repair | pass |
| Run B payload re-signed with a new delivery ID (operator-sent) | `duplicate run` | pass |
| Source lock: push `a611412`, then `work --once` | run A `skipped: stale: the branch moved after this run`; no collection | pass |
| Offline preflight (collect + reconstruct, no model) | 3/3 `SUPPORTED`, `toolchain-image`, `linux/arm64`, no review reasons | pass |
| Run B attempt 1, policy `REVIEW` | `PASS`/`VERIFIED_PASS`; gate `REVIEW` (only reason: `draft_pr is REVIEW`); `REVIEW_REQUIRED`; remote had only `main` | pass |
| Run B attempt 2 (`gh run rerun --failed`), policy `ALLOW`, worker in `serve` | `PASS`; gate `ALLOW` with no reasons; `PUBLISHED` | pass |
| Draft PR [#1](https://github.com/0xJieREN/ci-repair-acceptance/pull/1) | draft, open, base `main`, head `ci-repair/35977121527-c48255111e53`, two files | pass |
| PR commit equals verified patch | commit diff SHA-256 = `patch_sha256` `c4825511…` | pass |
| PR body | per-job table, gate, stop reason, calls, cost; no logs, commands or local paths | pass |
| Real CI on the repair branch | run `35977475892`: `math`, `text`, `report` all success | pass |
| Repair-branch run webhook | `ignored: not a failure`; nothing re-queued | pass |

Per-attempt repair detail (both attempts identical in outcome):

| Job | Status | Agent | Final verification | Baseline matches CI log |
|---|---|---|---|---|
| math | `REPAIRED` | `EARLY_STOP_VERIFIED` after 3 calls (2 in attempt 2), 1 probe | PASS | true |
| text | `REPAIRED` | `EARLY_STOP_VERIFIED` after 4 calls, 1 probe | PASS | true |
| report | `FIXED_BY_PRIOR` | not started | PASS | true |

Order followed workflow position (`math → text → report`), not job ID. Each
attempt produced the same two-file patch and six passing verification
commands. Model usage: 7 calls / $0.0039 (attempt 1) and 6 calls / $0.0037
(attempt 2), estimated from `config/deepseek-pricing.json`.

## Observations (no defect found)

- GitHub redeliveries reuse the original `X-GitHub-Delivery` GUID, so the
  delivery table catches them before the run key does.
- In the rerun, `math` landed on runner image `20260907.118.1` instead of
  `20260920.129.1`. Log provenance is part of the spec hash, so that job's
  replay image was rebuilt while the other two were reused. Reruns are not
  guaranteed to hit the same runner image.
- `serve` prints no per-repair result; the outcome is in `state.sqlite`
  (`repairs.result`) and under `runs/<key>/`.

## Not covered by this run

- Least-privilege service credentials (a repository-scoped fine-grained PAT or
  App installation token), `pull_request` events, merge-checkout provenance,
  a failing repair branch (policy denial verified only offline), expired
  leases, and x64 jobs replayed on an arm64 host.
- One run on a toy repository is evidence that the chain works end to end, not
  a measure of repair success on real projects.

## Teardown

The webhook was deleted, the tunnel and listener were stopped, and Colima was
stopped and verified. The target repository and draft PR remain as evidence.
Local state, reports and trajectories are under the ignored
`runs/acceptance-20260924/`.
