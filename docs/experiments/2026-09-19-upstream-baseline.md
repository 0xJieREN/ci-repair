# Upstream comparison — 2026-09-19

Both runners repaired all six synthetic cases on all three repetitions. This
experiment found **no repair-success advantage** for CI Repair on this corpus.
CI Repair used fewer calls and lower estimated cost in this run, with lower mean
runner wall time. These descriptive differences are not evidence of superiority
on real CI failures or an isolated benefit of structured context.

## Protocol

- Code: `53090c6db4a80726188f2efbc45b165ef02ad27f`, clean at experiment start.
- Model requested: `deepseek/deepseek-flash`; every response reported
  `deepseek-flash`. This is an alias, not proof of an immutable backend snapshot.
- Upstream: mini-SWE-agent 2.4.6 DefaultAgent; stock mini.yaml agent prompts.
  Config SHA-256: `b539a8965f5bf41dd0f32daafa5d2db22581923d13812fa378fd09569b9d6b0c`.
- Docker image ID:
  `sha256:856d917966bdb61bacffa19eab3d78549a42d190f6b1eb3eda5c6ae0cbed6a4c`.
- Six fixed cases × two runners × three repetitions = 36 valid trials / 18 pairs.
- Per trial: at most **8 model calls**, estimated cost threshold $0.10,
  runner wall budget 180 seconds, command timeout 30 seconds.
- Exact same fixture commit, real failure log, model adapter settings, image,
  task specification and allowed paths per pair; runner order alternated.
- Same external public gate and hidden oracle for both runners; CI Repair also
  retained its internal verifier. All 36 runs ended with Submitted, not a limit exit.
- See [evaluation contract](../evaluation.md) for the shared sandbox/adapters,
  metric definitions and deliberate differences from an untouched mini CLI.

## Results

| Runner | Success | False PASS | Errors | Mean calls | Mean estimated USD | Mean runner seconds | Mean grader seconds |
|---|---:|---:|---:|---:|---:|---:|---:|
| ci-repair | 18/18 | 0/18 accepted | 0 | 4.0556 | 0.000720 | 4.8342 | 0.9036 |
| upstream | 18/18 | 0/18 accepted | 0 | 5.9444 | 0.001853 | 7.8665 | 0.8974 |

All 18 pairs succeeded on both sides; no pairs were excluded. Mean end-to-end
trial time, including the common grader, was 5.7378 and 8.7639 seconds respectively.
Fixture preparation is excluded. Valid-run calls were 73 vs 107; estimated costs
were $0.012961776 vs $0.033357420, totaling **180 calls / $0.046319196**.
No inference is made from the small difference in patch size (3.17 vs 3.11 changed
lines on average).

The sanitized [per-trial CSV](2026-09-19-upstream-trials.csv) contains the source
identities and measurements used for this table. Full local evidence remains in
`runs/paired-deepseek-20260919-02/`: experiment manifest, original snapshots,
trajectories, patches, public/hidden verification outputs and incremental summary.
Those local artifacts were not uploaded to GitHub.

## Invalidated pilot and budget accounting

The initial batch used upstream DockerEnvironment template variables, which
reported the macOS **host** platform even though commands ran in Linux containers.
The stock prompt therefore included incorrect macOS/BSD sed guidance. The batch
was stopped and invalidated rather than pooled with corrected trials. Its local
record is `runs/paired-deepseek-20260919-01/invalidated.json`.

The shared Sandbox now queries platform information inside the container and
caches it. A unit regression and actual upstream-trajectory Docker assertions
check that Linux is reported and macOS guidance is absent.

The aborted pilot consumed 71 attempted calls, including the interrupted request.
The approved total ceiling was 360. The corrected experiment therefore used an
8-call limit for every trial, reserving at most 288 further calls. Actual combined
usage was **71 + 180 = 251 calls**, within the original ceiling. Pilot estimated
cost was $0.021217788; combined recorded estimate was $0.067536984. Interrupted
requests may not return usage, and estimates are not provider billing receipts.
The private call ledger is `runs/paired-budget-20260919.json`.

## Verification and interpretation

- Local full gate and GitHub Linux CI: **108 tests passed**.
- All trial source archives matched their original fixture archive byte-for-byte.
- Serialized model configurations matched across all 36 trials; patch digests and
  model call counters matched runner reports and trajectories.
- No credential string from the explicit `.env` was found in the two batches' run files or
  captured console logs. Secrets and raw artifacts remain Git-ignored.
- Colima was stopped after each local Docker session and confirmed Stopped.

The sample is too easy to distinguish repair quality: it shows a ceiling effect.
Repeating six cases three times does not create 36 independent problems. Prompt
length/style, context structure, internal verification and model/provider caching
can all influence calls, cost and time; this was not a causal ablation.

The next useful step is a frozen, more diverse set of real historical failures,
including lint, type checking and dependency/configuration issues where the
allowed edit policy is explicit. Start with a small curated addition, then expand
toward 20–50 cases. Run the same paired protocol before tuning prompts, adding
retries, memory, or multi-agent behavior. Collector/replay-plan usefulness and
production reliability were not measured by this synthetic experiment.
