# First historical DeepSeek experiment: 2026-09-19

Both CI Repair and the upstream mini-SWE-agent baseline produced patches that
passed the public reproduction, original upstream regression suite, and hidden
oracle for both historical cases. This means **2/2 verified patches per runner**,
not a demonstrated advantage in repair success.

## Inputs and budget

- Two complete pinned more-itertools repositories; see the
  [case provenance and admission checks](../../benchmarks/historical/README.md).
- Implementation: clean commit `a6a312b2b061904bde1193bd299fc988ad86db2c`.
- Model: `deepseek/deepseek-flash`, returned identifier `deepseek-flash`; this is
  a provider alias, not proof of an immutable backend model version.
- One repetition per case/runner, alternating runner order: four trials.
- Each trial: at most 10 model calls, USD 0.10 cost threshold, 180 seconds runner
  wall time, 60 seconds per command. Provider retries disabled. A cost threshold
  may overshoot by one response; the call cap is the hard experiment bound.
- Authorized total: 40 calls. Actual: **37 calls**, estimated **USD 0.024594576**.
  No failed pilots or retries were added to this experiment.

## Results and stopping behavior

| Case | Runner | Public + regression + oracle | Calls | Agent exit |
|---|---|---|---:|---|
| split-empty | CI Repair | PASS | 10 | LimitsExceeded |
| split-empty | upstream | PASS | 10 | LimitsExceeded |
| zip-once | upstream | PASS | 10 | LimitsExceeded |
| zip-once | CI Repair | PASS | 7 | Submitted |

The existing evaluator grades the patch remaining at the end of the bounded
run; explicit submission is not required for its `repair_success` metric.
**Three agents exhausted the call budget before submission**, although their
patches subsequently passed independent checks. Explicit completion plus a
verified patch was therefore 1/2 for CI Repair and 0/2 for upstream. This second
view describes stopping behavior and does not replace the predefined metric.

| Runner | Verified patches | Observed false PASS | Total calls | Estimated USD | Avg runner seconds |
|---|---:|---:|---:|---:|---:|
| CI Repair | 2/2 | 0 | 17 | 0.008949 | 34.19 |
| upstream | 2/2 | 0 | 20 | 0.015646 | 25.73 |

Runner time includes the CI pipeline's internal verification where applicable;
external grading adds approximately 10.98 seconds per CI trial and 10.71 seconds
per upstream trial. Call/cost differences are descriptive, not statistically
reliable estimates. The two split patches differ only in variable naming from
the reference implementation. Both zip patches reuse one acquired iterator by
inlining scalar detection, differing from the upstream fix's helper structure.

## Evidence and limits

[Machine-readable evidence](2026-09-19-historical-deepseek.json) records per-trial
scores, exit statuses, source/image/case/log/patch hashes, lock hash, installed
upstream prompt provenance and schedule. Full local artifacts remain in
`runs/historical-deepseek-20260919-01` and are ignored by Git. Verification
confirmed byte-identical source archives per pair, identical serialized model
configurations, and patch hashes matching repair reports. A scan against the
configured credential values found no matches in the experiment artifacts or
execution log. Colima was stopped and independently checked as `Stopped`.

The original upstream suites contained 907 and 914 tests at the pinned commits.
The project passed 119 local tests and the implementation's
[GitHub Linux CI](https://github.com/0xJieREN/ci-repair/actions/runs/35436868925).
No patches or messages were submitted to upstream projects.

Two cases from one pure-Python project, one repetition, hindsight-informed
hidden checks, and possible model familiarity with public fixes limit inference.
These are historical bugs reproduced as CI checks, not collected original
Actions failures. Zero observed false PASS does not establish zero false-PASS
risk. The earlier synthetic experiment must not be pooled into these results.

Next, add cases from other repositories and dependency/lint/type/build failures
before increasing repetitions or modifying prompts based on this small sample.
