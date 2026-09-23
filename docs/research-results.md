# Prior evaluation results

The standalone benchmark runners, fixture corpora and per-trial files were
removed from the active tree when the project refocused on automatic CI repair.
This page keeps the measured conclusions and their limits. The complete
[evaluation protocol](https://github.com/0xJieREN/ci-repair/blob/a0a3377c69e04c21fb933d398546a9a5e482bb9d/docs/evaluation.md),
[experiment records](https://github.com/0xJieREN/ci-repair/tree/a0a3377c69e04c21fb933d398546a9a5e482bb9d/docs/experiments)
and [benchmark inputs](https://github.com/0xJieREN/ci-repair/tree/a0a3377c69e04c21fb933d398546a9a5e482bb9d/benchmarks)
remain available at the pre-cleanup commit.
Private trajectories and source snapshots remain local under ignored `runs/`;
this cleanup does not move or delete them.

| Experiment | CI Repair | Upstream mini-SWE-agent | What it shows |
|---|---:|---:|---|
| Six synthetic fixes, three paired repetitions each (2026-09-19) | 18/18 verified; 73 calls | 18/18 verified; 107 calls | No repair-success difference on easy synthetic cases. |
| Two pinned more-itertools historical defects (2026-09-19) | 2/2 verified; 17 calls | 2/2 verified; 20 calls | No demonstrated success advantage; three of four agents hit the call limit before submitting. |
| Two selected LCA CI failures, IDs 24 and 107 (2026-09-22) | 2/2 selected checks; 13 calls | 2/2 selected checks; 28 calls | Local check replay only, not the official full-workflow dataset score. |

All model trials requested `deepseek/deepseek-flash`; the returned model name
was an alias, not an immutable backend version. Calls and estimated costs came
from single small experiments, so they do not establish a general efficiency
advantage. The LCA replay had no hidden oracle or complete workflow execution;
its false-PASS rate was unavailable, not zero. The historical cases used public
fixes and hindsight-informed checks. These cohorts must not be pooled into a
single repair-success rate.

Current verification is the project test gate described in
[verification](verification.md). Any future claim about repair performance needs
a new, frozen and representative evaluation protocol; the old pilots are retained
only as historical evidence.
