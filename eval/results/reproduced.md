## LCA dynamic replay (run 36124065326)

| Difficulty | USABLE | REFERENCE_FAILED | BASELINE_NOT_REPRODUCED | SETUP_FAILED | UNSUPPORTED | ERROR | Total |
|---|---:|---:|---:|---:|---:|---:|---:|
| 0 | 25 | 4 | 1 | 2 | 2 | 2 | 36 |
| 1 | 4 | 2 | 0 | 0 | 1 | 0 | 7 |
| 2 | 9 | 5 | 1 | 2 | 2 | 0 | 19 |
| 3 | 1 | 3 | 0 | 0 | 2 | 0 | 6 |
| All | 39 | 14 | 2 | 4 | 7 | 2 | 68 |

- Usable jobs: 49; replaying an error line from their CI log: 37

## Round 1

| | CI Repair | Pi |
|---|---:|---:|
| Trials passed | 107/117 (91.5%) | 113/117 (96.6%) |
| Difficulty 0 / 1 / 2 / 3 | 75/75, 10/12, 22/27, 0/3 | 75/75, 12/12, 26/27, 0/3 |
| Model calls, median (mean) | 5 (9.2) | 8 (11.0) |
| Estimated cost, mean per trial | $0.0069 | $0.0078 |
| Agent time, median | 60 s | 25 s |

- Tasks that differ: 45 (2/3 against 3/3), 53 (1/3 against 3/3), 57 (0/3 against 3/3)
- Mean per-task difference: -5.1 points; 95% bootstrap interval clustered by repository: [-12.9, 0.0]
- CI Repair's own verdict matched the grader in 117 of 117 trials

## Round 2

| Task | CI Repair round 1 | CI Repair round 2 | Pi round 1 |
|---|---:|---:|---:|
| 53 | 1/3 | 2/3 | 3/3 |
| 57 | 0/3 | 3/3 | 3/3 |
| 26, 29, 60, 96, 142 | 15/15 | 15/15 | 15/15 |
| **Total** | 16/21 | 20/21 | 21/21 |
| Model calls, median (mean) | 12 (15.9) | 11 (14.0) | 14 (18.6) |
| Estimated cost, mean per trial | $0.0132 | $0.0098 | $0.0149 |

## Round 3

| 117 trials | CI Repair round 1 (`8cd5bce`) | CI Repair round 3 | Pi round 1 |
|---|---:|---:|---:|
| Passed | 107 | 111 | 113 |
| Time per trial, median / mean / p90 | 60 / 227 / 761 s | 40 / 107 / 274 s | 25 / 66 / 171 s |
| Total time | 26,617 s | 12,522 s | 7,707 s |
| Model calls, median (mean) | 5 (9.2) | 5 (8.6) | 8 (11.0) |
| Estimated cost, mean per trial | $0.0069 | $0.0058 | $0.0078 |

- Multi-job trials: mean time 385 s -> 176 s; passed 16 -> 21 of 21

## Round 4

| 39 trials | CI Repair round 3, repetition 1 | CI Repair round 4 | Pi round 1, repetition 1 |
|---|---:|---:|---:|
| Passed | 36 | 37 | 38 |
| Time per trial, median / mean / p90 | 46 / 101 / 273 s | 42 / 100 / 241 s | 25 / 66 / 206 s |
| Model calls, mean | 8.3 | 10.3 | 10.2 |

- Pipeline baselines: 43; baseline 30.1 s per trial, waited 19.3 s, overlapped 10.8 s (36%)
- Run-level baselines: 3.2 s per trial
- Total time: round 3 repetitions 3,930, 4,522, 4,070 s; round 4 3,886 s

## Audit against upstream fixes

| Passing patches | CI Repair round 1 | CI Repair round 3 | CI Repair round 4 | Pi round 1 |
|---|---:|---:|---:|---:|
| Passed | 107 | 111 | 37 | 113 |
| Same files as upstream | 81 | 85 | 28 | 85 |
| Proper subset of upstream's files | 26 | 26 | 9 | 27 |
| Any file upstream did not change | 0 | 0 | 0 | 1 |
| Edits tests | 32 | 34 | 12 | 35 |
| Edits a test file upstream left alone | 0 | 0 | 0 | 0 |
| More suppressions than upstream | 0 | 0 | 0 | 1 |
| Removes more assertions than it adds | 0 | 0 | 0 | 0 |

- pi task 53 repetition 2: ['yt_dlp/extractor/elevensports.py', 'yt_dlp/extractor/ellentube.py']
- ci-repair round 3: passed 111, own verdict PASS 111, ALLOW 74, REVIEW 37 (34 edit tests), ALLOW in a SUPPORTED environment 44
- pi round 1: passed 113, own verdict PASS -, ALLOW 75, REVIEW 38 (35 edit tests), ALLOW in a SUPPORTED environment 45

## Job sessions (rounds 1-4)

| Session outcome | Sessions | Mean calls | Searched history | Commands per session | Searched for copies | Commands per session |
|---|---:|---:|---:|---:|---:|---:|
| `FAIL` | 1 | 30.0 | 1 | 5.0 | 1 | 2.0 |
| `NO_PATCH` | 26 | 30.0 | 25 | 4.5 | 23 | 5.7 |
| `PASS`, 15 or more calls | 38 | 20.7 | 27 | 1.7 | 31 | 2.3 |
| `PASS`, under 15 calls | 268 | 5.3 | 20 | 0.1 | 52 | 0.2 |

NO_PATCH sessions by task: 26: 1, 45: 3, 53: 12, 158: 3, 160: 7

## The intervention

| Task | Round 3 | With the intervention |
|---|---:|---:|
| 45 | 2/3 | 2/3 |
| 53 | 3/3 | 3/3 |
| 158 | 1/3 | 3/3 |
| 160 | 0/3 | 0/3 |
| Total | 6/12 | 8/12 |
| `NO_PATCH` job sessions | 6 | 5 |
| Mean model calls per trial | 26.5 | 25.2 |
| Mean seconds per trial | 366 | 357 |
| Estimated cost, 12 trials | $0.317 | $0.319 |
