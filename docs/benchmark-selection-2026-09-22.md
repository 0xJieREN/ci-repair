# Public benchmark selection (2026-09-22)

Use **JetBrains Research's Long Code Arena CI builds repair** as the first
external CI dataset, starting with a clearly labeled local replay subset.
It supplies actual failed-step logs, full repository commit pairs, workflow
metadata, and reference diffs; these match CI Repair's log-driven input model.

| Candidate | Fit and decision |
|---|---|
| [LCA CI builds repair](https://huggingface.co/datasets/JetBrains-Research/lca-ci-builds-repair) | Best initial fit: specialized CI tasks, compact public dataset, explicit failed logs and commit pairs. Current pinned `default/test` has 68 rows. Its official runner pushes to GitHub; our local subset is not that official score. |
| [CI-Repair-Bench](https://github.com/RabeyaMuna/CI-REPAIR-BENCH) | Strong thematic fit and richer failure categories. Official execution requires GitHub setup/forks. Defer a full run until release statistics and reproduction constraints are reconciled. |
| [SWE-bench Verified](https://github.com/SWE-bench/SWE-bench) | Useful later for general issue repair with an established evaluation harness. Its issue-driven task does not directly test CI log diagnosis or whole-workflow recovery. |
| [BugsInPy](https://github.com/soarsmu/BugsInPy) | Real Python bugs with checkout/compile/test commands. Useful as a secondary functional-repair corpus, with older runtime/environment setup to reproduce. |
| [GitBug-Actions](https://github.com/gitbugactions/gitbugactions) | Strong local workflow reproduction tooling through act; adopting its collection/reproduction stack is a larger integration than importing an existing small CI corpus. |

## Checked releases, rather than paper counts

LCA revision `ebf12dad7a97c3c0cdc38705403d8bc8ddde47dc` contains 68 rows in
`data/python/test-00000-of-00001.parquet` (SHA-256
`0b690ed61eef63f74be425df0010e04afb61ab3821d992a32b76c7c50e81f282`).
Its separate `old` split has a different population and is not included.

The [CI-Repair-Bench paper](https://arxiv.org/abs/2604.27148) reports 567 instances
and 103 repositories, while its dataset card says 567 and 105. The downloaded
Parquet at Hugging Face revision `8cf0d4cce6f31995f6e1cf141937fc6e516572b8`
actually contains **565 rows and 146 distinct `(repo_owner, repo_name)` pairs**.
This is an observed release mismatch, not a claim that its cases are invalid.
It is a reason to audit the exact artifact before reporting benchmark-wide
numbers. Metadata and the inspected LCA data are retained locally under
`runs/dataset-research-20260922`.

## First subset and evaluation boundary

Preselected **LCA 24 (canonical/cloud-init, unused import/Ruff)** and
**LCA 107 (encode/httpx, mypy call arguments/undefined names)** for two projects,
two failure categories, explicit tool versions and no GPU/external-service
requirement in the selected checks. Selection occurred before model trials.
These are engineering smoke tests, not a random or representative sample.

We preserve the dataset's complete original failing repositories, reference
diffs, and archived logs. The reference diff must reconstruct the pinned fixed
Git tree. Model containers receive no reference diff or future Git history.
The recipe runs the full logged Ruff target list for 24 and the original
`scripts/check` for 107. In both cases a separate fresh verifier repeats the
same check to detect transient/container state dependence; this is **not an
independent hidden semantic oracle**.

The LCA workflows include matrices and PyPI time-machine services. We instead
use a reviewed Docker environment with historical checker versions and a
hash-locked dependency set, executing without network access. Our acceptance
criterion is the selected check's failure-to-pass transition, not all jobs,
platforms, docs, tests, installation and coverage stages of the original CI.
Consequently `official_full_ci_success`, hidden-oracle results, and false-PASS
rates are unavailable. Do not combine these scores with the synthetic or
hindsight-oracle historical corpora.
