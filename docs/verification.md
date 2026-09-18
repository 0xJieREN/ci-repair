# Local and server acceptance checks

GitHub upload is not a prerequisite for verification. The repository's Actions
workflow now delegates to the same script used locally and on a Linux server:

```sh
# macOS: starts Colima and stops it on success, failure or interruption
bash scripts/check.sh --colima

# Linux/server: uses the already running Docker daemon
bash scripts/check.sh

# Quick checks without Docker (not the full acceptance gate)
bash scripts/check.sh --unit
```

Prerequisites: Git, uv, Bash and Docker; the macOS managed mode also needs Colima.
Run from a checkout of this repository. A server can clone the repository and
execute the same command through SSH or its existing job scheduler. No deployed
application, GitHub token, model key or paid inference is required for this gate.
Initial dependency/image downloads need network access; cached layers and locked
uv dependencies are reused on subsequent checks.

The full script performs `uv sync --locked`, Ruff checks, a Docker image build,
all pytest tests including Docker integration, and `git diff --check`. A failing
step stops the gate. Its temporary mini-SWE configuration is removed on exit.
GitHub Actions only supplies checkout, uv and the hosted runner; the verification
steps themselves have one source of truth in `scripts/check.sh`.

The Linux/server command does not stop a shared Docker service. It removes test
containers; `--colima` additionally stops the local VM as this project's policy
requires. Do not use the Colima-managed mode for a shared remote Docker daemon.

## What is actually verified

- Native mini-SWE agent loop with its deterministic model adapter, real Docker
  workspaces, baseline failure, source patch, independent fresh verification.
- Patch boundary enforcement, command timeout, snapshot content integrity,
  CI provenance and invalid/tampered run evidence.
- Real local Git commits, bare-remote pushes, creation-only branch behavior and
  publication retries. GitHub PR HTTP responses are controlled fixtures.

A repaired target repository's verification already runs in local Docker via
`ci-repair`: original failure first, then the explicitly configured regression
command. The same CLI runs on a Linux server. Its test dependencies must be in
the supplied image; no GitHub-hosted runner is required. Prepared images now
require GNU `timeout` in addition to Bash/Git (the demo image includes it).

Neither gate automatically discovers every command in arbitrary workflow YAML.
For another repository, configure the original test command, regression command,
image, working directory, dependency versions and necessary fixtures explicitly.
Keep secrets out of repair containers. Tests needing external services need a
separately designed trusted test setup; the default sandbox has no network.

## Limits of local simulation

Same script means the same checks, not an identical machine. macOS/ARM and
GitHub's Linux/x86 runners can differ. For platform-sensitive code, run the gate
on a Linux/x86 server with the required image and runtime versions. A complete
GitHub runner image, event permissions, branch protection, GitHub API behavior,
OIDC and hosted services are not recreated by these tests. Real PR publication
still needs a deliberate live acceptance step.

`act` is optional if you specifically want to execute workflow YAML locally.
It is not needed for this repository's gate: its runner images differ from
GitHub's and some Actions features are unsupported. No act dependency was added.
See the official [runner guidance](https://nektosact.com/usage/runners.html) and
[unsupported features](https://nektosact.com/not_supported.html).
