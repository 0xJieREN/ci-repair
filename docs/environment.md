# CI environment reconstruction

`reconstruct.py` turns pinned evidence into a replay specification, then builds a
replay image. It never executes workflow content on the host, and it replaces
the manual image/command choice of [reviewed plans](replay-plan.md) for
`ci-repair-run` and the webhook. Reviewed plans remain available.

## Inputs (all untrusted)

- The workflow file **at the failing commit** (`run.path` from the API), parsed
  with the strict loader (duplicate keys and YAML aliases rejected).
- API job metadata: job name, per-step conclusions, runner labels.
- The job log: only bounded provenance hints (runner image and version, action
  SHAs, `setup-python`'s resolved version). Logs never select commands.
- Version files from the commit (`.python-version`, `.nvmrc`, `.node-version`, `go.mod`).

## Supported subset

| Area | Supported | Otherwise |
|---|---|---|
| Job selection | unique job whose rendered name (including static matrix `key (v1, v2)` naming) equals the API job name | UNSUPPORTED |
| Matrix | static scalar lists, `include`/`exclude` per GitHub rules | UNSUPPORTED |
| Expressions | `matrix.*`, `env.*`, `runner.os`, `runner.arch`, `github.sha`, `github.repository`, `github.workspace`, `github.event_name` | UNSUPPORTED (secrets always) |
| Runner | `ubuntu-latest` (resolved from the log), `ubuntu-22.04/24.04`, `-arm` variants | UNSUPPORTED |
| Environment | workflow → job → step `env`; job `container.env` | secrets block |
| Shell | default (`bash -e`), `bash` (`-eo pipefail`), `sh -e`; `defaults.run` precedence | UNSUPPORTED |
| Working directory | static, inside the repository | UNSUPPORTED |
| Container | static `container:` image (becomes the base image) | credentials/options/services UNSUPPORTED |
| Actions before the failure | `actions/checkout` (default inputs), `setup-python`/`setup-node`/`setup-go` (static or file versions), `astral-sh/setup-uv`, `pnpm/action-setup`, `actions/cache` and `upload-artifact` (no-ops) | UNSUPPORTED |
| Conditions before the failure | decided from the API step conclusion (skipped/ran) | UNSUPPORTED if unknown |
| Steps after the failure | unconditional `run` steps become the regression check | not replayed → REVIEW |
| State files | — | `GITHUB_ENV/PATH/OUTPUT/STATE` UNSUPPORTED |

The failing command is the failing step; the regression command is the later
replayable steps (or the failing step again when there are none). Each step is a
fresh shell with its own env and directory, like Actions. `CI=true`,
`GITHUB_ACTIONS`, `RUNNER_OS`, `RUNNER_ARCH`, `GITHUB_WORKSPACE`, `GITHUB_SHA`
and `GITHUB_REPOSITORY` are set.

## Base image and fidelity

| Fidelity | Base | Status |
|---|---|---|
| `job-container` | the job's `container` image | SUPPORTED |
| `toolchain-image` | `python:<v>-bookworm`, `node:<v>-bookworm`, `golang:<v>-bookworm` | SUPPORTED (Debian, not the Ubuntu runner) |
| `approximate-runner` | `buildpack-deps:noble/jammy` | REVIEW: hosted-runner preinstalled software is not reproduced |

## Build and provenance

`build_environment` pulls the base (if absent) for the runner's platform, starts a
disposable container without credentials, host mounts or Docker socket (network
only if `sandbox.setup_network: ALLOW`), extracts the verified source snapshot
into `/workspace`, installs missing `bash/git/timeout/tar` via apt/apk, runs the
setup steps under `budget.max_setup_seconds`, probes tool versions, and commits
`ci-repair-env:<spec hash>`. Setup-created files (virtualenvs, `node_modules`,
editable installs) become part of the replay baseline, never of the patch.

Recorded: spec SHA-256 (workflow hash, source SHA, job/matrix, base, all steps),
base image ID and repo digests, actual platform, architecture mismatch, tool
versions, setup log SHA-256, network mode and duration. An existing image with
the same spec hash is reused. A failed setup gives `UNSUPPORTED_ENVIRONMENT`.

After the baseline replay, `baseline_matches_ci_log` compares normalized error
lines (runner paths, timestamps, colors removed) with the CI log. `false`
means the replay fails differently than CI did; publication then needs review.

## Known gaps

Replay uses Debian-based language images or an approximate runner, not GitHub's
runner image. A missing base is pulled for the runner's platform (on an arm64 host
an x64 job then needs emulation); a base already present locally for another
architecture is used as-is and recorded as a mismatch (REVIEW). `ubuntu-latest` without a log hint
assumes 24.04. Unpinned setup inputs (for example `setup-uv` without a version)
are recorded after the build but are not reproducible over time. Full Git
history, submodules, LFS and caches are not reproduced.
