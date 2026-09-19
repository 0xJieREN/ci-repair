# Historical corpus admission: 2026-09-19

Two real more-itertools bug/fix pairs were replayed from their complete upstream
source commits with Python 3.12 in the shared network-disabled Docker sandbox.
The manifests and upstream provenance links are in
[`benchmarks/historical`](../../benchmarks/historical/README.md).

| Case | Original public / hidden check | Reference fix, CI / upstream | Original upstream suite after reference fix |
|---|---|---|---|
| split-empty | Both fail as expected | Both public + regression + oracle PASS | 907 tests PASS |
| zip-once | Both fail as expected | Both public + regression + oracle PASS | 914 tests PASS |

All four reference-fix trials passed. All four no-op trials returned `NO_PATCH`
and were rejected. No evaluator errors occurred. These eight deterministic
trials made **zero external model calls** and incurred no model cost. A passing
reference control is evidence that the environment and acceptance checks work;
it is not evidence that either runner can discover the fix.

The source commits, image ID, manifest hashes, captured log hashes, and per-trial
outcomes are in [the machine-readable evidence](2026-09-19-historical-controls.json).
Detailed local artifacts are under `runs/historical-correct-20260919` and
`runs/historical-noop-20260919` (ignored by Git). These admission runs used the
working implementation before its first commit; their experiment manifests
explicitly record the working tree as dirty. Colima was stopped after testing
and independently checked as `Stopped`.

This is a two-case, single-project seed with hindsight-informed oracle checks,
not a representative benchmark or an independently blind repair study. The
original hosted CI failure logs were not collected; the checks replay actual
historical defects locally. The next model experiment must state a separate
call budget and should be reported separately from the earlier synthetic run.
