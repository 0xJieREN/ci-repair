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
  SHAs, `setup-python`'s resolved version, also read from `pythonLocation`). Logs
  never select commands; they only pin `ubuntu-latest` and a Python range such as
  `3.x` (or no version) to the image CI actually used.
- Version files from the commit (`.python-version`, `.nvmrc`, `.node-version`, `go.mod`).

## Supported subset

| Area | Supported | Otherwise |
|---|---|---|
| Job selection | unique job whose rendered name (including static matrix `key (v1, v2)` naming for scalar values) equals the API job name | UNSUPPORTED |
| Matrix | static lists of scalars or mappings, `include`/`exclude` per GitHub rules | UNSUPPORTED (dynamic matrices) |
| Expressions | GitHub semantics for literals, `!`, `&&`/`\|\|` (operand values), `==`/`!=` (case-insensitive strings, number coercion), comparisons, property/index access (missing → null), `contains`/`startsWith`/`endsWith`; contexts `matrix`, `env` and `runner.os/arch`, `github.sha/repository/workspace/event_name` | UNSUPPORTED: other contexts or properties (`steps`, `needs`, `github.ref`, …), other functions (`hashFiles`, `fromJSON`, …); secrets always |
| Runner | `ubuntu-latest` (resolved from the log), `ubuntu-20.04` (historical), `ubuntu-22.04/24.04`, `-arm` variants | UNSUPPORTED |
| Environment | workflow → job → step `env`; job `container.env` | secrets block |
| Shell | default (`bash -e`), `bash` (`-eo pipefail`), `sh -e`, custom `bash\|sh [flags] {0}`; `defaults.run` precedence | UNSUPPORTED |
| Working directory | static, inside the repository | UNSUPPORTED |
| Container | static `container:` image (becomes the base image) | credentials/options/services UNSUPPORTED |
| Actions | `actions/checkout` (default inputs), `setup-python`/`setup-node`/`setup-go` (static or file versions; `setup-python` without a version → the logged or runner default Python, REVIEW), `astral-sh/setup-uv`, `pnpm/action-setup`, `pre-commit/action` and `paolorechia/pox` (install at build time, then run), `actions/cache` and `upload-artifact` (no-ops; inputs not evaluated) | UNSUPPORTED |
| Conditions before the failure | the API step conclusion (skipped/ran); without one, the evaluated `if:` | UNSUPPORTED if not evaluable |
| Steps after the failure | `run` steps and tool actions whose `if:` holds once the failure is fixed (`success()`/`always()` true, `failure()` false) become the regression check | not replayed → REVIEW |
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
| `approximate-runner` | `buildpack-deps:noble/jammy/focal` | REVIEW: hosted-runner preinstalled software is not reproduced |

## Build and provenance

`build_environment` pulls the base (if absent) for the runner's platform, starts a
disposable container without credentials, host mounts or Docker socket (network
only if `sandbox.setup_network: ALLOW`), extracts the verified source snapshot
into `/workspace`, installs missing `bash/git/timeout/tar` via apt/apk, and makes
the image behave like a hosted runner where that is cheap: apt answers yes and has
package lists, and root images without one get a `sudo` passthrough. Like
`actions/checkout`, `/workspace` then becomes a Git repository at the failing commit,
fetched from GitHub with the workflow's `fetch-depth` (a local commit without
network or access). It runs the setup steps under `budget.max_setup_seconds`, probes
tool versions, and commits `ci-repair-env:<spec hash>-r<recipe>`. Setup-created
files (virtualenvs, `node_modules`, editable installs) become part of the replay
baseline, never of the patch.

Tools such as tox, nox and pre-commit create their environments on first use,
which needs network that replay does not have. With setup network, the build
therefore **warms up**: it runs the failing and regression commands once and ignores
their results. Only tool environments survive (`.tox`, `.nox`, `.venv`, `venv`,
`.eggs`, `node_modules`, `*.egg-info` at the top level, and caches outside the
workspace); other new top-level entries such as reports or build output are removed,
and every replay workspace restores tracked files from the snapshot. The warm-up's
return code, duration and log digest are recorded.

Finally the build drops remotes and every ref the failing commit cannot reach and
prunes their objects. Setup steps like `git fetch --unshallow` keep working, but a
replay never contains commits or tags created after the failure.

An operator can attach setup to a specific Docker network and pass it extra
environment (for example a package mirror); neither is committed to the image, and
their digest is part of the tag.

Recorded: spec SHA-256 (workflow hash, source SHA, job/matrix, base, all steps),
base image ID and repo digests, actual platform, architecture mismatch, tool
versions, setup log SHA-256, network mode and duration. An existing image with
the same spec hash and build recipe is reused. A failed setup gives `UNSUPPORTED_ENVIRONMENT`.

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
