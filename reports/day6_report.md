# Day 6 experiment report

## Current result

The final tree passed 312 tests in 184.20 s after the total-budget approval repair. Ruff check passed, Ruff format check passed for 93 files, and strict mypy passed for 87 source files. The measured-source capsule predates that one isolated guard repair; the source-version boundary and no-reference control are documented below. The restored three-fixture, five-policy byte-budget regression and simulated merge-actor regression are included in the final suite.

Append performance is measured and verified. The frozen Day 5 source took 4.311661 s to append 1,000 events (231.93 events/s) and 0.006484 s to verify them. Day 6 took 0.561528 s (1,780.86 events/s) and 0.006992 s to verify the same 1,000-event stream. Both produced the same event head and JSONL SHA-256 (`f894a81b…cb6ec24`, `89888a3f…77720a`). Day 6 also appended and fully verified 100,000 events: 57.315234 s append (1,744.74 events/s), 0.816143 s verify, 42,600,000 JSONL bytes, SHA-256 `a261363e…0209125`. The scheduler observed load1 peak 2.0581 under its sampled limit of 7; the sample interval was 1 s. The first detached scheduler attempt was reaped by the host during the 100,000-event job; its interrupted receipt is retained, and the 100,000-event run was repeated in a waited shell with a fresh vault. No result from the interrupted attempt is used.

This benchmark measures public `capture_event(MEMORY_ACCESS)` calls with metadata only, on the same warm repository. It includes locking, WAL, fsync, and cursor updates; it excludes body retrieval, ordinary Markdown mutations, and the full-verification pass from append time. The 100,000-event verification is timed separately. If the cursor is absent, verification uses the full-scan fallback. File locking is implemented, but no multi-process contention stress test was run, so these numbers do not establish concurrent throughput or a repository-wide O(1) guarantee.

The official LoCoMo10 evaluation, both N=1,000 primary matrix runs, the N=200 access-rho diagnostics, the 16-cell capture scan, and fixed/automatic tail fits are complete. The primary matrix runs took 3,656.922414 s and 3,534.679161 s. Their semantic result hashes match, and every file in all ten corresponding policy/budget vault pairs matches byte for byte. The official source remains at `<MEMFW_DATA_DIR>/locomo10.json`; the dataset is not copied into this repository.

## Day 6 requirement status

| # | Original requirement | Status and evidence |
|---:|---|---|
| 1 | Append-cache benchmark at 1k/100k with full verification | Complete for required Day6 1k/100k runs. Day5 source and Day6 current each passed 1k append plus full verify; Day6 current passed 100k. A Day5 100k run was not requested and was not measured. Source imports were verified against the frozen ZIP for Day5 jobs and the workspace source for Day6. |
| 2 | Configurable probe timeout and two independent primary runs | Complete. Both primary matrices used `--probe-timeout-seconds 0`, separate output/work directories and fresh vaults; full survivor/file-byte comparison is reported below. |
| 3 | Use official data at its local path | Complete. Input path is `<MEMFW_DATA_DIR>/locomo10.json`; no dataset copy was made in the repository. |
| 4 | Replace the old finite event-ID range | Complete. IDs come from an unbounded deterministic counter. A regression generates 1,802 unique IDs, beyond the former 36-turn bound of 1,801; targeted test passed. |
| 5 | Keep evaluation retrieval read-only | Complete. Read-only index search is used; fixture tests assert zero `MEMORY_ACCESS` events and no question-informed recency updates. |
| 6 | Isolate each sample in its own vault | Complete. Official evaluation isolated all ten samples; each used its own vault and timestamp anchored to that sample’s conversation dates. |
| 7 | Compare 10%, 25%, 50% original-body byte budgets | Complete in implementation and fixture tests. Capacity is whole-turn UTF-8 body bytes; tests assert measured retained bytes do not exceed each byte budget. |
| 8 | Official 10-sample evaluation of five MemFW policies, BM25, and offline Mem0-style baseline | Complete. Ten isolated samples ran across five MemFW strategies, BM25, and offline first-sentence mock CRUD. The offline baseline is not official Mem0; corrected baseline output is `reports/day6_locomo_official_corrected.json`. |
| 9 | Long-query/event-ID regression | Complete. A 35-turn/100-question workload completes without query-triggered access-event writes. A separate factory regression emits 1,802 unique IDs, beyond the former 1,801-ID bound. |
| 10 | Access-rho diagnostics and random-retention comparison | Complete for the secondary N=200 tier. Requested rho 0/.3/.6 realized −0.003997/0.298632/0.595551 with 20 important records and 447 accesses; all five policies ran at 300 KB. Exact random-retention intervals and per-policy counts are reported below. The separate primary N=1,000 fixture has 2,890 accesses and empirical rho 0.299699 for requested 0.3. |
| 11 | Exercise the 10 MB capacity tier | Complete. Both runs exercised all five policies at 1 MB and 10 MB; all ten primary policy/budget outputs met their requested total allocated-byte budgets. |
| 12 | H6 capture scan | Complete, no pass. Across 16 cells, 15 met the 70% important-recall floor and 0 of those 15 met the 20% packed-staging ratio; the complete grid is below. |
| 13 | Require real human approval before any cited-source deletion | Complete in the final source guard and regressions. Direct/manual deletion and multi-target forget fail safely for cited sources; body- and total-budget paths create or reuse source-protection proposals. The measured matrices used a pre-repair capsule; all fixtures had zero active knowledge refs, and a before/after no-reference control produced the same vault bytes. |
| 14 | Use explicit simulated approval actor for merge proposals | Complete in storage API and tests, including a positive ordinary-merge audit-actor regression. The official LoCoMo run recorded 15 simulated consolidation reviews across 30 consolidation profiles. The simulated actor never emits a human decision event; protected/anchor sources are rejected. |
| 15 | Compare fixed `xmin=1` and automatically selected xmin tail fits | Complete. Fixed `xmin=1` and auto-selected `xmin=3` were both fit on the main Day6 access marginal; provenance and both estimates are in `reports/day6_runs/access_tail_fixed1_auto.json`. |

## Dataset and measurement boundaries

The official local file preflight reported 10 samples, 5,882 turns, and 1,986 QA questions. Categories contain 282, 321, 96, 841, and 446 questions for categories 1–5. All 288 session date strings parsed; no timestamp fallback was needed. The adapter has 1,982 questions with valid evidence, 2,820 deduplicated valid evidence references, two unknown references, one malformed evidence fragment, and four questions without valid evidence (all four in category 3). Category results use micro recall: total retrieved evidence IDs divided by total valid evidence IDs, not mean per-sample recall. The local source SHA-256 is `79fa87e90f04081343b8c8debecb80a9a6842b76a7aa537dc9fdf651ea698ff4`.

The evaluator reads `session_N` turn text and the shared speaker metadata only. It does not fetch images, infer image content, create captions, or produce answers. Category 5 still has evidence IDs; its score remains evidence-ID recall, not adversarial QA accuracy. Question retrieval does not generate access events. This official evaluator uses the default `keyness_enabled=false`; access, reference, and manual scores are zero. Salience therefore breaks ties by timestamp, decay scores by age/order, and LRU uses its created-order fallback. This run provides no evidence of a salience/keyness benefit; bounded query retrieval alone does not explain the equal retained sets. The raw result remains preserved at `reports/day6_locomo_official.json` (SHA-256 `b2c5b24c3333e087d2ed81ef98a4065966f653a0a70f12cb786e4fc7f5d1fc2a`). Its corrected counterpart uses oldest-first whole-turn eviction for BM25 and mock-CRUD at the same UTF-8 body-byte budgets (SHA-256 `c965bcfc149be444315c7464959f78c8061fd4342dd97b4837358dd8eea08dfd`). Correction metadata records the raw and dataset hashes, corrector SHA-256 `afa1c86c39e5bfe36b730c447947ed8cfe243d05a1e2ef0f84468b5cf1f1c9a3`, and confirms the MemFW projection hash is unchanged. The offline Mem0-style baseline repeats the same deterministic first-sentence fact during its update step; this exercises idempotent CRUD plumbing, not entity resolution or semantic updates, and it does not represent the full Mem0 system.

### Official LoCoMo evidence recall

Across 1,982 questions with valid evidence, the evaluator scored 2,820 deduplicated evidence IDs. The remaining four questions had no valid evidence and are excluded from recall. The adapter counted two unknown references and one malformed fragment. Valid evidence-reference denominators by category are 882, 375, 208, 895, and 460 for categories 1–5. These metrics are evidence-ID retrieval only; they do not measure whether the system answered the question correctly.

At the 128-token query-context budget, corrected micro evidence hits were:

| System | Before retention | 10% body budget | 25% body budget | 50% body budget |
|---|---:|---:|---:|---:|
| MemFW FIFO/LRU/decay/salience | 737 / 2,820 | 158 / 2,820 | 315 / 2,820 | 465 / 2,820 |
| MemFW consolidation | 737 / 2,820 | 157 / 2,820 | 315 / 2,820 | 465 / 2,820 |
| Full-text BM25 with oldest-first eviction | 1,031 / 2,820 | 169 / 2,820 | 367 / 2,820 | 628 / 2,820 |
| First-sentence mock CRUD with oldest-first eviction | 555 / 2,820 | 156 / 2,820 | 329 / 2,820 | 537 / 2,820 |

The category table below reports micro evidence-ID recall across all ten samples. Category 5 remains evidence-ID retrieval only.

| System | Body budget | Cat 1 hits / refs | Cat 2 hits / refs | Cat 3 hits / refs | Cat 4 hits / refs | Cat 5 hits / refs | Total hits / refs |
|---|---:|---:|---:|---:|---:|---:|---:|
| MemFW FIFO | 10% | 24/882 (2.7%) | 23/375 (6.1%) | 7/208 (3.4%) | 65/895 (7.3%) | 39/460 (8.5%) | 158/2820 (5.60%) |
| MemFW LRU | 10% | 24/882 (2.7%) | 23/375 (6.1%) | 7/208 (3.4%) | 65/895 (7.3%) | 39/460 (8.5%) | 158/2820 (5.60%) |
| MemFW decay | 10% | 24/882 (2.7%) | 23/375 (6.1%) | 7/208 (3.4%) | 65/895 (7.3%) | 39/460 (8.5%) | 158/2820 (5.60%) |
| MemFW salience | 10% | 24/882 (2.7%) | 23/375 (6.1%) | 7/208 (3.4%) | 65/895 (7.3%) | 39/460 (8.5%) | 158/2820 (5.60%) |
| MemFW consolidate | 10% | 24/882 (2.7%) | 23/375 (6.1%) | 7/208 (3.4%) | 65/895 (7.3%) | 38/460 (8.3%) | 157/2820 (5.57%) |
| BM25 | 10% | 24/882 (2.7%) | 23/375 (6.1%) | 10/208 (4.8%) | 71/895 (7.9%) | 41/460 (8.9%) | 169/2820 (5.99%) |
| Mock CRUD | 10% | 21/882 (2.4%) | 32/375 (8.5%) | 7/208 (3.4%) | 69/895 (7.7%) | 27/460 (5.9%) | 156/2820 (5.53%) |
| MemFW FIFO | 25% | 45/882 (5.1%) | 45/375 (12.0%) | 8/208 (3.8%) | 146/895 (16.3%) | 71/460 (15.4%) | 315/2820 (11.17%) |
| MemFW LRU | 25% | 45/882 (5.1%) | 45/375 (12.0%) | 8/208 (3.8%) | 146/895 (16.3%) | 71/460 (15.4%) | 315/2820 (11.17%) |
| MemFW decay | 25% | 45/882 (5.1%) | 45/375 (12.0%) | 8/208 (3.8%) | 146/895 (16.3%) | 71/460 (15.4%) | 315/2820 (11.17%) |
| MemFW salience | 25% | 45/882 (5.1%) | 45/375 (12.0%) | 8/208 (3.8%) | 146/895 (16.3%) | 71/460 (15.4%) | 315/2820 (11.17%) |
| MemFW consolidate | 25% | 45/882 (5.1%) | 45/375 (12.0%) | 8/208 (3.8%) | 146/895 (16.3%) | 71/460 (15.4%) | 315/2820 (11.17%) |
| BM25 | 25% | 39/882 (4.4%) | 51/375 (13.6%) | 11/208 (5.3%) | 173/895 (19.3%) | 93/460 (20.2%) | 367/2820 (13.01%) |
| Mock CRUD | 25% | 37/882 (4.2%) | 53/375 (14.1%) | 10/208 (4.8%) | 161/895 (18.0%) | 68/460 (14.8%) | 329/2820 (11.67%) |
| MemFW FIFO | 50% | 51/882 (5.8%) | 80/375 (21.3%) | 6/208 (2.9%) | 220/895 (24.6%) | 108/460 (23.5%) | 465/2820 (16.49%) |
| MemFW LRU | 50% | 51/882 (5.8%) | 80/375 (21.3%) | 6/208 (2.9%) | 220/895 (24.6%) | 108/460 (23.5%) | 465/2820 (16.49%) |
| MemFW decay | 50% | 51/882 (5.8%) | 80/375 (21.3%) | 6/208 (2.9%) | 220/895 (24.6%) | 108/460 (23.5%) | 465/2820 (16.49%) |
| MemFW salience | 50% | 51/882 (5.8%) | 80/375 (21.3%) | 6/208 (2.9%) | 220/895 (24.6%) | 108/460 (23.5%) | 465/2820 (16.49%) |
| MemFW consolidate | 50% | 51/882 (5.8%) | 80/375 (21.3%) | 6/208 (2.9%) | 220/895 (24.6%) | 108/460 (23.5%) | 465/2820 (16.49%) |
| BM25 | 50% | 60/882 (6.8%) | 96/375 (25.6%) | 18/208 (8.7%) | 295/895 (33.0%) | 159/460 (34.6%) | 628/2820 (22.27%) |
| Mock CRUD | 50% | 83/882 (9.4%) | 96/375 (25.6%) | 16/208 (7.7%) | 246/895 (27.5%) | 96/460 (20.9%) | 537/2820 (19.04%) |

FIFO, LRU, decay, and salience tie at the three byte budgets because this evaluation has no external access stream and has keyness disabled. Consolidation differs by one evidence hit at 10% and ties the other policies at 25% and 50%. The results verify the adapter, retention, and evidence-ID accounting pipeline; they do not show a strategy quality advantage. Baseline before-retention hits differ because BM25 indexes full text while the mock stores first-sentence facts. The budgeted table uses a fixed 128-token retrieval context and counts actual returned text tokens. Baseline retention is oldest-first whole-turn eviction, which matches chronological order for these ten monotonic-session-date inputs.

### First primary storage matrix

Run1 completed all ten actual-total-byte profiles in 3,656.922414 seconds (about 60 minutes 57 seconds); run2 completed in 3,534.679161 seconds (about 58 minutes 55 seconds). Both independent runs produced the same semantic result hash, and every physical file in each corresponding profile vault matched. The following policy metrics therefore apply to both runs. Byte measurements are allocated vault bytes; the persistent index is not created in this fixture (`index=0`). Every measured profile satisfied its corresponding budget. The important-label denominator is 100 of 1,000 records. At 1 MB, salience retained 29 important records: 100% important precision but only 29% important recall. At 10 MB, important precision was 64/582 for FIFO, 70/582 for LRU, 61/582 for decay, and 58/582 for salience, approximately the 10% random-retention baseline. Do not read the 1 MB precision as high recall.

| Policy | 1 MB retained / allocated | 10 MB retained / allocated |
|---|---:|---:|
| FIFO | 29 / 978,944 B | 582 / 9,986,048 B |
| LRU | 28 / 966,656 B | 582 / 9,986,048 B |
| Decay | 28 / 966,656 B | 582 / 9,986,048 B |
| Salience | 29 / 983,040 B | 582 / 9,986,048 B |
| Consolidate then forget | 29 / 987,136 B | 581 / 9,973,760 B |

Pairwise survivor-set Jaccard values are identical in both runs:

| 1 MB | FIFO | LRU | Decay | Salience | Consolidate |
|---|---:|---:|---:|---:|---:|
| FIFO | 1.0000 | 0.0000 | 0.0000 | 0.0000 | 1.0000 |
| LRU | 0.0000 | 1.0000 | 0.0980 | 0.0962 | 0.0000 |
| Decay | 0.0000 | 0.0980 | 1.0000 | 0.5833 | 0.0000 |
| Salience | 0.0000 | 0.0962 | 0.5833 | 1.0000 | 0.0000 |
| Consolidate | 1.0000 | 0.0000 | 0.0000 | 0.0000 | 1.0000 |

| 10 MB | FIFO | LRU | Decay | Salience | Consolidate |
|---|---:|---:|---:|---:|---:|
| FIFO | 1.0000 | 0.5137 | 0.6033 | 0.5837 | 0.9983 |
| LRU | 0.5137 | 1.0000 | 0.7935 | 0.5356 | 0.5143 |
| Decay | 0.6033 | 0.7935 | 1.0000 | 0.6417 | 0.6041 |
| Salience | 0.5837 | 0.5356 | 0.6417 | 1.0000 | 0.5845 |
| Consolidate | 0.9983 | 0.5143 | 0.6041 | 0.5845 | 1.0000 |

The 1 MB FIFO and consolidate survivor sets are exactly equal; consolidation did not improve retention there. At 10 MB their Jaccard is 0.9983, also near-identical. Do not describe these results as every strategy producing a distinct survivor set.

The following rows show the complete logical and allocated category breakdowns for the Day5 frozen source versus Day6 current source, before and after FIFO enforcement. Zero categories are retained in the table so totals remain auditable. The 1 MB and 10 MB post-forget FIFO results are identical between sources in this fixture; base event allocation is not.

Logical file-size bytes:

| Source | State | Budget | Events | History | Memories | Anchors | Archives | Index | Other | Logical total |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Day5 frozen | Base | — | 1,725,520 | 0 | 15,419,265 | 0 | 0 | 0 | 428 | 17,145,213 |
| Day6 current | Base | — | 1,725,520 | 0 | 15,419,265 | 0 | 0 | 0 | 646 | 17,145,431 |
| Day5 frozen | After FIFO | 1 MB | 489,467 | 0 | 447,138 | 0 | 0 | 0 | 476 | 937,081 |
| Day6 current | After FIFO | 1 MB | 489,467 | 0 | 447,138 | 0 | 0 | 0 | 476 | 937,081 |
| Day5 frozen | After FIFO | 10 MB | 433,900 | 0 | 8,974,061 | 0 | 0 | 0 | 477 | 9,408,438 |
| Day6 current | After FIFO | 10 MB | 433,900 | 0 | 8,974,061 | 0 | 0 | 0 | 477 | 9,408,438 |

Allocated filesystem bytes:

| Source | State | Budget | Events | History | Memories | Anchors | Archives | Index | Other | Allocated total |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Day5 frozen | Base | — | 1,732,608 | 0 | 16,384,000 | 0 | 0 | 0 | 4,096 | 18,120,704 |
| Day6 current | Base | — | 2,109,440 | 0 | 16,384,000 | 0 | 0 | 0 | 8,192 | 18,501,632 |
| Day5 frozen | After FIFO | 1 MB | 499,712 | 0 | 475,136 | 0 | 0 | 0 | 4,096 | 978,944 |
| Day6 current | After FIFO | 1 MB | 499,712 | 0 | 475,136 | 0 | 0 | 0 | 4,096 | 978,944 |
| Day5 frozen | After FIFO | 10 MB | 446,464 | 0 | 9,535,488 | 0 | 0 | 0 | 4,096 | 9,986,048 |
| Day6 current | After FIFO | 10 MB | 446,464 | 0 | 9,535,488 | 0 | 0 | 0 | 4,096 | 9,986,048 |

The 10 MB tier retains a majority of the 1,000 records; reported similarity in that tier must be interpreted with its survivor-set Jaccard values, not as evidence of no strategy differences. The 1 MB and 10 MB budgets apply to one fixed 1,000-record fixture. They do not establish that a vault can remain under 1 MB over an unlimited lifetime: logs, event summaries, and tombstones have a nonzero growth floor.

Tail diagnostics use the main Day6 fixture’s 2,890 access events over 1,000 memory IDs. Its sorted access-count marginal exactly matches the previously frozen tail input (`reports/day5_access_tail_1000.json`), verified against the actual `day5_storage_accesses.jsonl`; the fit is not extrapolated to a different workload. With 200 bootstrap samples and seed `20261018`, fixed `xmin=1` gives n=1,000, α=2.0684, KS=0.03785, GOF p=0.00498, and an inconclusive discrete-lognormal comparison (Vuong p=0.1013). Automatic KS selection chooses `xmin=3`, tail n=238, α=2.3761, KS=0.03572, GOF p=0.07960, and an inconclusive comparison (Vuong p=0.6382). The fixed-cutoff result fails its bootstrap fit check; the automatic result does not establish a power law merely because its p-value exceeds 0.05. Full estimates and provenance are in `reports/day6_runs/access_tail_fixed1_auto.json`.

### Access-correlation diagnostic

The small diagnostic uses 200 records, 20 evaluation-labeled important records, 447 access events, 1,100-byte target bodies, and an actual 300,000-byte total-vault budget. It runs each policy against the same generated data for each rho. Requested/realized Pearson rho values are 0/−0.003997, 0.3/0.298632, and 0.6/0.595551; attainable bounds for this fixed access-count marginal were −0.141037 to 0.766850. Each cell retained 40 records and met the byte budget. The random-retention 95% prediction interval is 1–8 important records of 40 (2.5%–20% precision); it is a finite-population subset interval, not a deployment confidence interval.

| Requested rho | Policy | Important retained / 20 (recall) | Important / 40 retained (precision) | Allocated bytes |
|---:|---|---:|---:|---:|
| 0.0 (−0.003997 realized) | FIFO | 5/20 (25%) | 5/40 (12.5%) | 278,528 |
|  | LRU | 4/20 (20%) | 4/40 (10.0%) | 278,528 |
|  | Decay | 2/20 (10%) | 2/40 (5.0%) | 278,528 |
|  | Salience | 2/20 (10%) | 2/40 (5.0%) | 278,528 |
|  | Consolidate | 5/20 (25%) | 5/40 (12.5%) | 286,720 |
| 0.3 (0.298632 realized) | FIFO | 5/20 (25%) | 5/40 (12.5%) | 278,528 |
|  | LRU | 5/20 (25%) | 5/40 (12.5%) | 278,528 |
|  | Decay | 5/20 (25%) | 5/40 (12.5%) | 278,528 |
|  | Salience | 5/20 (25%) | 5/40 (12.5%) | 278,528 |
|  | Consolidate | 5/20 (25%) | 5/40 (12.5%) | 286,720 |
| 0.6 (0.595551 realized) | FIFO | 5/20 (25%) | 5/40 (12.5%) | 278,528 |
|  | LRU | 8/20 (40%) | 8/40 (20.0%) | 278,528 |
|  | Decay | 12/20 (60%) | 12/40 (30.0%) | 278,528 |
|  | Salience | 13/20 (65%) | 13/40 (32.5%) | 278,528 |
|  | Consolidate | 5/20 (25%) | 5/40 (12.5%) | 286,720 |

At rho 0.6, decay and salience exceed the random-subset interval in this one fixed synthetic sample (randomization upper-tail probabilities 3.47e−5 and 3.80e−6); the N=200 sample is small and only one seed was run, so this is diagnostic evidence, not a general policy ranking. At rho 0 and .3, outcomes are within the 1–8 random prediction interval. The complete 15-cell records, survivor IDs, fixture hashes, empirical-rho metadata, and exact randomization summaries are in `reports/day6_runs/rho_diagnostic_run2/day6_rho_diagnostic.json` (SHA-256 `772e4263e0709836810fef0fdd666d783a966a4188db3ac3ea8fcd82f824f5e3`). The runner is `scripts/day6_rho_diagnostic.py` (SHA-256 `a1da0e2e5de65110306f9cc9985212a34451305d8f32b1d54cd83d61e889f2c3`). An initial preserved attempt stopped after four rho-0 policy cells because an invalid alias named the consolidation policy; no result from that attempt is used.

### Capture intensity and H6 scan

The 16-cell capture scan used 1,000 records, seed `20261018`, 100 important / 900 normal labels, 60-second inter-record spacing, a 1,800-second ring, and a 60,000-second simulated stream. The requested capture-intensity rho values were 0, .3, .6, and .9; empirical correlations were within 1e−12 of those requests. The always-on staging pack used 110,592 allocated bytes; the always-on all-in vault used 716,800 allocated bytes. H6 requires both at least 70% important recall and a packed-staging ratio no more than 20% of the always-on staging allocation. Fifteen cells met the recall floor; none passed the byte-ratio gate (0/15 eligible, 0/16 overall). The best eligible cell retained 76/100 important records at rho=.3, threshold=.8, with 73,728 packed-stage bytes (ratio .6667), still above the 20% limit. At rho=.6, threshold=.8, the stage ratio was lower (.4815) but recall was only 51%, so it was ineligible. The JSON artifact contains no elapsed-time receipt, so no capture runtime is claimed here.

| Capture rho | Threshold | Candidates | Important retained / 100 | Normal retained / 900 | Wakes / normal wakes | Packed-stage alloc / ratio | All-in alloc / ratio | Duty | H6 eligible / pass |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| 0.0 | 0.35 | 1000 | 100 (100.0%) | 900 (100.0%) | 1 / 1 | 110,592 B / 1.0000 | 716,800 B / 1.0000 | 100.0% | yes / no |
| 0.0 | 0.50 | 1000 | 100 (100.0%) | 900 (100.0%) | 2 / 2 | 110,592 B / 1.0000 | 716,800 B / 1.0000 | 99.9% | yes / no |
| 0.0 | 0.65 | 996 | 100 (100.0%) | 896 (99.6%) | 18 / 15 | 110,592 B / 1.0000 | 782,336 B / 1.0914 | 95.3% | yes / no |
| 0.0 | 0.80 | 855 | 89 (89.0%) | 766 (85.1%) | 37 / 31 | 94,208 B / 0.8519 | 700,416 B / 0.9771 | 31.4% | yes / no |
| 0.3 | 0.35 | 1000 | 100 (100.0%) | 900 (100.0%) | 1 / 1 | 110,592 B / 1.0000 | 716,800 B / 1.0000 | 100.0% | yes / no |
| 0.3 | 0.50 | 1000 | 100 (100.0%) | 900 (100.0%) | 2 / 2 | 110,592 B / 1.0000 | 716,800 B / 1.0000 | 99.9% | yes / no |
| 0.3 | 0.65 | 996 | 100 (100.0%) | 896 (99.6%) | 23 / 18 | 110,592 B / 1.0000 | 782,336 B / 1.0914 | 95.2% | yes / no |
| 0.3 | 0.80 | 679 | 76 (76.0%) | 603 (67.0%) | 28 / 16 | 73,728 B / 0.6667 | 548,864 B / 0.7657 | 23.0% | yes / no |
| 0.6 | 0.35 | 1000 | 100 (100.0%) | 900 (100.0%) | 1 / 1 | 110,592 B / 1.0000 | 716,800 B / 1.0000 | 100.0% | yes / no |
| 0.6 | 0.50 | 996 | 100 (100.0%) | 896 (99.6%) | 5 / 4 | 110,592 B / 1.0000 | 716,800 B / 1.0000 | 98.7% | yes / no |
| 0.6 | 0.65 | 983 | 100 (100.0%) | 883 (98.1%) | 62 / 25 | 106,496 B / 0.9630 | 778,240 B / 1.0857 | 63.2% | yes / no |
| 0.6 | 0.80 | 433 | 51 (51.0%) | 382 (42.4%) | 19 / 0 | 53,248 B / 0.4815 | 397,312 B / 0.5543 | 9.8% | no / no |
| 0.9 | 0.35 | 996 | 100 (100.0%) | 896 (99.6%) | 4 / 3 | 110,592 B / 1.0000 | 716,800 B / 1.0000 | 98.7% | yes / no |
| 0.9 | 0.50 | 967 | 100 (100.0%) | 867 (96.3%) | 66 / 3 | 106,496 B / 0.9630 | 778,240 B / 1.0857 | 46.0% | yes / no |
| 0.9 | 0.65 | 962 | 100 (100.0%) | 862 (95.8%) | 73 / 0 | 106,496 B / 0.9630 | 778,240 B / 1.0857 | 34.4% | yes / no |
| 0.9 | 0.80 | 776 | 89 (89.0%) | 687 (76.3%) | 48 / 0 | 86,016 B / 0.7778 | 626,688 B / 0.8743 | 21.8% | yes / no |

“Normal wakes” are reported separately; false-wake and noise-retention metrics are unavailable because this generator has only important/normal labels and does not label any record as noise. The 60-second event interval is independent of the 1,800-second ring retention. Allocated bytes depend on filesystem block size. This end-of-session packed-stage measurement does not represent peak in-session or lifetime storage. The scan JSON is `reports/day6_runs/capture_grid/day6_capture_scan.json` (SHA-256 `d40745474d6b01eb1b50f820c4e832e17121a3b37573aae636be5d7d7e86b76e`).


The Day 6 1,000-record storage fixture uses seed `20261018`, 15,000-byte target bodies, 1,000 records, and 2,890 access events. Creation timestamps are 60 seconds apart, spanning 16 h 39 min; access events span 30 days and occur after creation. The body length includes deterministic per-record capacity tokens. This is a capacity stress fixture, not natural-language evidence for keyness or salience quality. Its importance labels are evaluation-only. The baseline archive is the frozen Day 5 source snapshot (`reports/day6_day5_source_baseline.zip`, SHA-256 `1d70d9b1…f668686`). The runner’s internal `day4_baseline` role refers to this frozen Day 5 source; `day5_current` refers to the Day 6 workspace source. The matrix’s physical byte totals describe the tested vault; if its index component is zero, they do not include a resident FTS index. The LoCoMo body-byte budgets are a separate metric and cannot be compared directly with total allocated vault bytes.

## Reproduction commands

Append runs were scheduled sequentially with `scripts/run_day6_jobs.py`, one worker and one process slot, load1 limit 7, and 1-second sampling. The first scheduler receipt is `reports/day6_runs/perf/scheduler_receipt.json`; the completed 100k rerun receipt is `reports/day6_runs/perf-100k-rerun/scheduler_receipt.json`. The official evaluator command is recorded in `reports/day6_locomo_job.json`. Main matrix arguments and hashes are recorded in each run directory’s `day6_policy_matrix_checkpoint.json`. The full-vault comparison receipt is `reports/day6_runs/main_matrix_vault_byte_audit.json` (3,112 files in each run; all ten profile pairs identical, no exclusions). Day5-source 100k performance has not been measured, so the current 100k number is not a before/after comparison.

Run these commands from the repository root. They use new temporary roots, so they do not reuse saved checkpoints. They extract the exact measured Day6 source capsule before invoking its scripts. The full matrix and official LoCoMo runs are long. Run the primary profiles sequentially for comparable load measurements and keep their separate work directories and vaults for byte audits. The final pytest/Ruff/mypy gate was run against the final workspace tree after the cited-source guard repair; matrix reproduction uses the pinned pre-repair capsule.

```sh
RUN_ROOT=$(mktemp -d /private/tmp/memfw-day6-repro.XXXXXX)
unzip -q reports/day6_measured_source.zip -d "$RUN_ROOT/measured"
unzip -q reports/day6_day5_source_baseline.zip -d "$RUN_ROOT/day5-source"
PYTHONPATH="$RUN_ROOT/day5-source/src" .venv/bin/python "$RUN_ROOT/measured/scripts/benchmark_day6_append.py" --root "$RUN_ROOT/day5-1000" --count 1000 --label day5-source-1000
PYTHONPATH="$RUN_ROOT/measured/src" .venv/bin/python "$RUN_ROOT/measured/scripts/benchmark_day6_append.py" --root "$RUN_ROOT/day6-1000" --count 1000 --label day6-current-1000
PYTHONPATH="$RUN_ROOT/measured/src" .venv/bin/python "$RUN_ROOT/measured/scripts/benchmark_day6_append.py" --root "$RUN_ROOT/day6-100000" --count 100000 --label day6-current-100000
PYTHONPATH="$RUN_ROOT/measured/src" .venv/bin/python "$RUN_ROOT/measured/scripts/day5_storage_matrix.py" --output-dir "$RUN_ROOT/main1" --work-dir "$RUN_ROOT/main1-work" --baseline-archive "$PWD/reports/day6_day5_source_baseline.zip" --count 1000 --seed 20261018 --target-body-bytes 15000 --rho-access 0.3 --important-prevalence 0.10 --budget 1MB:1000000 --budget 10MB:10000000 --probe-timeout-seconds 0
PYTHONPATH="$RUN_ROOT/measured/src" .venv/bin/python "$RUN_ROOT/measured/scripts/day5_storage_matrix.py" --output-dir "$RUN_ROOT/main2" --work-dir "$RUN_ROOT/main2-work" --baseline-archive "$PWD/reports/day6_day5_source_baseline.zip" --count 1000 --seed 20261018 --target-body-bytes 15000 --rho-access 0.3 --important-prevalence 0.10 --budget 1MB:1000000 --budget 10MB:10000000 --probe-timeout-seconds 0
PYTHONPATH="$RUN_ROOT/measured/src" .venv/bin/python "$RUN_ROOT/measured/scripts/compare_day6_matrices.py" "$RUN_ROOT/main1/day6_policy_matrix.json" "$RUN_ROOT/main2/day6_policy_matrix.json"
PYTHONPATH="$RUN_ROOT/measured/src" .venv/bin/python "$RUN_ROOT/measured/scripts/eval_locomo.py" --data <MEMFW_DATA_DIR>/locomo10.json --budget-tokens 128 --policies fifo lru decay salience consolidate_then_forget --output "$RUN_ROOT/locomo-raw.json" --checkpoint "$RUN_ROOT/locomo-checkpoint.json"
PYTHONPATH="$RUN_ROOT/measured/src" .venv/bin/python "$RUN_ROOT/measured/scripts/correct_locomo_baselines.py" --data <MEMFW_DATA_DIR>/locomo10.json --raw-evaluation "$RUN_ROOT/locomo-raw.json" --output "$RUN_ROOT/locomo-corrected.json" --query-budget 128
PYTHONPATH="$RUN_ROOT/measured/src" .venv/bin/python "$RUN_ROOT/measured/scripts/day6_capture_scan.py" --output-dir "$RUN_ROOT/capture" --count 1000 --seed 20261018 --important-prevalence 0.10
PYTHONPATH="$RUN_ROOT/measured/src" .venv/bin/python "$RUN_ROOT/measured/scripts/day6_rho_diagnostic.py" --output-dir "$RUN_ROOT/rho" --work-dir "$RUN_ROOT/rho-work" --count 200 --seed 20261018
PYTHONPATH="$RUN_ROOT/measured/src" .venv/bin/python "$RUN_ROOT/measured/scripts/day6_tail_compare.py" --input "$PWD/reports/day5_access_tail_1000.json" --output "$RUN_ROOT/tail.json" --bootstrap-samples 200 --seed 20261018 --minimum-sample 20
.venv/bin/pytest -q
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy --strict src tests scripts
```

The primary measured-source capsule is `reports/day6_measured_source.zip` (SHA-256 `dd0841bc4e30e4eef516599b87dc955ece273ae16794ad0c0abc27016ab37846`); its file manifest is `reports/day6_measured_source_manifest.json`. The Day6 matrix identity recomputed from that capsule is `01b1402b01429eb98aae437d9d325124d0c6bafd7aec25ff03c71a40bc93a407`; a companion manifest uses a different documented algorithm and therefore has a different digest. The Day5 baseline archive is separately pinned above. These roles matter: the matrix field called `day4_baseline` is the frozen Day5 source, while `day5_current` is the Day6 workspace source.

After all measurements, the final total-budget guard repaired cited-source handling in `src/memfw/curation/engine.py`. The measured engine SHA-256 was `3410ad217a33182f485e07b5556dd320bea375ac6c02d02728ca46a9d1370ba7`; the final engine SHA-256 is `4ffee69979655f07ccfa1d33238a73cba6e5c96a7c1dca4fe9f913fb57f5a722`. The measured-source capsule and final tree match across all 61 packaged members except that engine file. The repair’s regression tests passed; an expected-red pre-fix run and the green post-fix run are preserved in `reports/day6_total_source_red.log` and `reports/day6_total_source_green.log`. A fixed-clock 20-record zero-knowledge control ran the same total-budget operation before and after the repair; all 16 relative paths and file SHA-256 values matched (`reports/day6_total_guard_control.json`, SHA-256 `4676fff0653c997256e79492095e153716ffa36de484d2b419e1391aa855b594`). Therefore the matrix results are measured on the pinned pre-repair source; the tested repair is isolated to cited-source workflow behavior absent from these fixtures.

The two independent primary matrices completed in 3,656.922414 s and 3,534.679161 s. Their semantic comparison excludes only run metadata (runtime, timestamps, checkpoint path, and work-directory persistence) and matches at SHA-256 `21b7865ac845fed5510dc888e60d958194b7d0b2806e4a0607a087550530690a`; the result is saved in `reports/day6_runs/main_matrix_semantic_comparison.json`. A separate recursive audit compared every relative path and file SHA-256 in all ten matching policy/budget vault pairs. It found 3,112 files per run and no differences or exclusions (`reports/day6_runs/main_matrix_vault_byte_audit.json`, SHA-256 `25b00750fab425322502a277a8e7a83c6add00694328ba0e509f8f11a88ec615`).

The final verification gate ran `.venv/bin/pytest -q` (312 passed in 184.20 s), `.venv/bin/ruff check .`, `.venv/bin/ruff format --check .` (93 files), and `.venv/bin/mypy --strict src tests scripts` (87 source files). The test output is preserved in `reports/day6_final_pytest.log` (SHA-256 `e373e4520a8748042c35a3a5e862ff29e4921db7a824f6ee6ed28716e8738d56`). Runtime environment: Python 3.11.16, SQLite 3.53.1, zstandard 0.25.0, macOS arm64. Allocated-byte totals use filesystem block allocation and can vary on another filesystem.

The long whole-vault runs show that faster append operations do not make the full retention workflow fast. Repository transactions, markdown deletion, compaction, and repeated retrieval dominate end-to-end wall time in these measurements. The full workload is not a sub-five-minute experiment.

The scheduler load monitor is sampled, not a hard system-wide ceiling. The tracked workload was kept within four experiment process slots; direct matrix and capture runs did not continuously record a global process/load maximum, so this is not a full-system concurrency audit. Filesystem allocation size may vary by platform. A summary hash chain detects local corruption but is not an external trusted signature or timestamp. Simulated consolidation approval is an experiment action by `simulated_approver`, not a human or model approval. Capture H6 uses post-session packed staging allocation; it does not establish peak in-session allocation or an all-in ratio including every log component.

The initial 60-line and 88-line SHA-256 values (`9de15d0bb1976a45e502351d31f4804954db2d042c622542a2e658b6f1272137` and `47963bf6b1e13dfdd0e91c43f86508e233cf8627df5188e22e79022698577d3a`) identify the preserved change-memory log prefixes. Separately, the network-blocking pytest `conftest.py` was preserved unchanged; its whole-file SHA-256 is `b6f0c20ef048d533cd3f78d5dc779af43caee49db36df1529ec4b052b31ac81e`.

The retention experiments use one fixed synthetic seed. The exact hypergeometric random-retention comparison is a prediction for random subsets of this fixture, not a confidence interval for deployment accuracy. H6 likewise applies only to the selected synthetic capture stream and post-session packed size; it says nothing about peak or lifetime storage.
