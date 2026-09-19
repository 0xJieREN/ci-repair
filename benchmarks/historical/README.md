# Historical failure seed corpus

These are full upstream repository snapshots, not extracted or rewritten toy
implementations. They reproduce reported library bugs as local CI checks. We do
not claim to have captured the original failed GitHub Actions runs.

| Case | Primary source | Defect |
|---|---|---|
| `more-itertools-split-empty` | [Upstream PR #1253](https://github.com/more-itertools/more-itertools/pull/1253) | Empty input with `maxsplit=0` yields an empty group in three split helpers. |
| `more-itertools-zip-once` | [Upstream PR #1278](https://github.com/more-itertools/more-itertools/pull/1278) | `zip_broadcast` acquires an input iterator twice, breaking single-use iterables. |

Each JSON pins the original parent commit, the upstream fix commit, and the
exact implementation-only diff with its SHA-256. Preparation fetches those
commits and checks that their diff matches before running anything in Docker.
The upstream source tree is not modified to insert our reproduction tests.
Reference diffs retain the upstream MIT license in `LICENSE.more-itertools`.
The public and hidden Python checks are written for this corpus.

Only the original Git archive, public reproduction, task description, and
regression command reach the repair workspace. Upstream fix history, reference
patch, and hidden oracle stay outside it. The `correct` deterministic control
intentionally receives the reference patch; it measures harness correctness,
not model ability. The `noop` control must not be accepted. Historical `overfit`
controls are not implemented; the synthetic corpus retains its overfit control.

Admission requires the exact public failure diagnostic and an independently
failing hidden oracle. A reference patch must then pass the public check, the
entire upstream unittest suite, and the hidden oracle in separate fresh
containers. The oracle covers additional inputs and related behavior, but is
not a proof of correctness and is derived with knowledge of the historical fix.

## Reproduce locally or on a Linux server

Use the existing `ci-repair-demo:local` image (Python 3.12, Bash, Git, GNU timeout).
No additional Python dependencies are needed for these two cases. GitHub access
is needed on the host to fetch source; execution containers have no network.
The evaluator records the resolved image ID, source commit, and case/log hashes.
Case manifests are trusted executable benchmark configuration: review new ones
before running them, just as you would review a test script.

```bash
# macOS only: always stop the VM on exit, including failed tests.
trap 'colima stop' EXIT
colima start --cpu 2 --memory 4 --disk 20

docker build -t ci-repair-demo:local -f examples/Dockerfile examples
uv run python -m ci_repair.evaluate benchmarks/historical \
  --output runs/historical-correct --runner both --control correct \
  --steps 3 --wall-seconds 180 --command-seconds 60
uv run python -m ci_repair.evaluate benchmarks/historical \
  --output runs/historical-noop --runner both --control noop \
  --steps 3 --wall-seconds 180 --command-seconds 60
```

On Linux omit the Colima lines. Output directories must be new. Normal model
experiments use the same CLI with `--model`, `--env-file`, and an explicit
`--max-total-calls` ceiling. Do not count controls as paid model repair results.

This first seed has two cases from one dependency-free project. It establishes
full-repository replay, not benchmark diversity or a success-rate estimate.
Future expansion should add other projects and dependency/lint/type/build
failures, recording installation and environment constraints per case.
