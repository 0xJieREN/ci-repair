# LCA selected-check pilot (2026-09-22)

The first external CI dataset pilot completed four DeepSeek trials. Both
runners produced **2/2 patches passing the selected local checks**. All four
patches matched the dataset's reference diffs after ignoring Git index-hash
abbreviation differences. This is not the official full-workflow LCA score.

## Dataset and protocol

- [LCA CI builds repair](https://huggingface.co/datasets/JetBrains-Research/lca-ci-builds-repair),
  default/test revision `ebf12dad7a97c3c0cdc38705403d8bc8ddde47dc`.
- IDs 24 (cloud-init, Ruff unused import) and 107 (HTTPX, mypy arguments/names),
  preselected for explicit failure diagnostics and local environment feasibility.
  See the [dataset comparison](../benchmark-selection-2026-09-22.md).
- Clean implementation commit `50f950622049959e5967d7b11f1c7f23e242cf9e`.
- Same full failing source, archived CI logs, prepared image, model adapter,
  allowed paths, and independent fresh verifiers for each paired trial.
- Stock upstream mini-SWE-agent prompts versus CI Repair's orchestration; runner
  order alternates between cases. One repetition per case/runner.
- `deepseek/deepseek-flash`, returned model identifier `deepseek-flash`. The
  identifier is an alias, not evidence of an immutable provider model version.
- 15 calls/trial, 60 calls total authorized; USD 0.10 per-trial cost threshold,
  240-second runner limit and 90-second command limit. Retries disabled.

## Results

| LCA ID | Runner | Selected check | Calls | Estimated USD | Agent exit |
|---|---|---|---:|---:|---|
| 24 | CI Repair | PASS | 4 | 0.000524 | Submitted |
| 24 | upstream | PASS | 13 | 0.003499 | Submitted |
| 107 | upstream | PASS | 15 | 0.003462 | LimitsExceeded |
| 107 | CI Repair | PASS | 9 | 0.001556 | Submitted |

Total: **41/60 calls**, estimated **USD 0.009040776**. There were no paid pilot
retries. CI Repair used 13 calls total and upstream used 28. CI Repair submitted
both tasks explicitly; upstream submitted one and left a passing patch when its
budget expired in the other. Selected-check success grades the final patch,
including a patch left at a step limit, following the existing evaluator policy.

No hidden oracle exists in this replay. `official_full_ci_success`, hidden
oracle results and false-PASS rate remain **null / unavailable**, not zero.
Repeating the same check in a fresh container checks reproducibility, not
independent semantic correctness. Matching the reference patch is a post-run
audit, not a hidden test and not the acceptance rule supplied to the model.

## Verification and evidence

[Machine-readable evidence](2026-09-22-lca-deepseek.json) includes the dataset
revision/hash, recipes, code/environment lock hashes, image ID, source/log/patch
hashes, upstream prompt provenance, admissions, budgets and per-trial outcomes.
Full local artifacts remain under `runs/lca-deepseek-20260922-01` (ignored).

Before paid calls, both original failures and both reference fixes were checked
again. Earlier admission ran eight correct/no-op controls across both runners:
four correct patches passed and four no-op runs were rejected. The first free
admission attempt exposed a missing checker PATH; it failed closed, was fixed,
and was not counted as a model failure or dataset defect.

The final audit confirmed identical paired source archives and serialized model
configurations, reported call counts matching trajectories, and patch hashes
matching repair reports. No configured credential values were found in the
artifacts or execution log. Colima was stopped after each Docker batch and
independently verified `Stopped`. The implementation's [GitHub CI](
https://github.com/0xJieREN/ci-repair/actions/runs/35690413809) passed **123 tests**.
No upstream patches, issues, comments or benchmark submissions were published.

## Interpretation

These are two small, diagnostically explicit CI failures across two projects,
not a random sample of LCA's 68 cases. Reference solutions are public, so model
familiarity is possible. Calls/costs from one paired repetition do not establish
a general efficiency advantage. LCA's native evaluation runs complete hosted
workflows; this local adaptation checks only the reviewed Ruff target list and
HTTPX's original `scripts/check`, without the remaining workflow jobs/matrices.

The useful result is that the project now consumes a specialized external CI
dataset with provenance and reproducible local checks. Next expansion should
pre-register a stratified sample and separately evaluate full-workflow coverage,
rather than reporting these two selected checks as a benchmark-wide repair rate.
