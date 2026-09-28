# Project instructions

## Local Docker lifecycle

On the local macOS machine, Colima's VM holds its CPUs and memory while it runs,
so it must not stay up between tasks. Prefer `bash scripts/check.sh --colima`,
which starts Colima and stops it on exit, including failures and interrupts. If a
task needs Docker directly, start Colima once, run that task's Docker work back to
back, then run `colima stop` and verify with `colima status`, including when a
command fails or is interrupted. Removing test containers alone is not sufficient.
Paid or long experiments run on hosted runners (`compare.yml`, `lca-replay.yml`),
not locally. Linux servers and GitHub Actions use their own Docker engine and are
not affected.

## Changelog

After each feature iteration, record it in `CHANGELOG.md` in the same commit or
the commit that follows, in the existing style. Add a stage under **Unreleased**
(or a version section when `pyproject.toml` changes version) with Motivation,
Changed/Added/Fixed/Removed, Verification and Known limits, cite commit hashes,
and update the timeline row. Report measured results with their run, commit and
task set; label numbers derived from earlier data as estimates. When a later
experiment confirms or refutes an estimate, update that stage.
