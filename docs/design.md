# v0.1 design decision (historical baseline)

Current checks and boundaries: [verification](verification.md) and
[repository review](review-2026-09-18.md).

Use mini-SWE-agent 2.4.6's DefaultAgent, LitellmModel and DockerEnvironment.
Upstream inspected: https://github.com/SWE-agent/mini-swe-agent at
04d809ceab9df28f9adaed044884180159172930 (2026-09-17).
Do not implement another reasoning loop or automated GitHub repair integration.

A run takes a clean local Git HEAD, a real failure log, an explicit failing
command, an explicit regression command, a prepared Docker image and source
path prefixes allowed to change. Commands are operator configuration, never
inferred and automatically executed from untrusted logs.

1. Archive the exact committed tree; reject dirty trees and submodules.
2. Reproduce the failing command in a disposable container.
3. Run one bounded mini-SWE-agent attempt in another container.
4. Extract its binary Git diff, including untracked files.
5. Create a fresh verifier from the original archive; apply only that diff.
6. Reject changes outside the explicit source prefixes and symlink/submodule
   changes; rerun the original command, then regression tests.
7. Persist context, baseline, trajectory, patch, tests, cost and outcome locally.

Containers have no host bind mounts, credentials, Docker socket or network;
capabilities are dropped and memory, CPU and process limits apply. Dependencies
must be included in the operator-supplied image. Docker is not a complete
security boundary for hostile multi-tenant workloads. Source changes may still
cheat visible tests: PASS means the configured checks passed, not semantic proof.

Limits: one attempt; positive step, estimated-dollar, command and wall-time
budgets. Upstream cost checks occur between model calls and can overshoot by
one response; a process alarm bounds orchestration on Unix. Provider retries
are disabled by the CLI so model-call counts reflect requests. Unknown pricing
fails closed; custom providers need a LiteLLM model registry. Images are resolved
to an image ID for all phases and recorded. Runs are private, gitignored artifacts;
logs/trajectories may contain repository data and should be reviewed before sharing.

Deferred: webhooks, repair PRs, retries, automatic test selection, token-budget
accounting, model comparisons and distributed execution. No claim of production
readiness or measured repair success rate from a single demo.
