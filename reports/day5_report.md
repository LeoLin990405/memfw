# Day 5 experiment report

## Outcome

The corrected 1,000-record storage matrix completed all five policies at both actual total-vault budgets. Under 1 MB, the policies retained 119–121 records in 987,136–999,424 allocated bytes. The target of retaining about 200 records was not reached. Under 10 MB, capacity was inactive: every policy retained all 1,000 records and pairwise survivor Jaccard was 1.0. This is not evidence that the policies are equivalent under pressure.

The 16-cell capture scan completed in 83.773951 seconds. No cell met H6, which requires both important-item recall of at least 70% and allocated bytes at or below 20% of always-on. The best recall-gated cell retained 87.13% of important records but used 96.88% of always-on allocated bytes.

All generated labels were evaluation-only. The judge, capture policy, and budget policy received content and observable timestamps/access metadata, not importance/noise/theme labels. Results are deterministic synthetic mechanism checks, not evidence of natural-user effectiveness.

## Storage matrix

The fixture used seed `20261018`, 1,000 records, 2,890 logged access events, 1,100–1,117 UTF-8 body bytes per record, and 512 important / 331 normal / 157 noise labels. Labels are stored separately. Access events occur at or after record creation. Bodies are bilingual, with deterministic per-record alphabetic capacity tokens. Only the ten declared exact duplicate pairs share identical complete bodies. A full-fixture `DeterministicJudge.find_near_duplicates` preflight returned exactly those 10 candidates.

| Total allocated-byte budget | Policy | Retained | Important retained / 512 | Important recall | Noise retained / 157 | Allocated bytes after | Budget satisfied |
|---:|---|---:|---:|---:|---:|---:|---|
| 1 MB | FIFO | 121 | 59 | 11.52% | 18 / 157 (11.46%) | 991,232 | yes |
| 1 MB | LRU | 120 | 64 | 12.50% | 20 / 157 (12.74%) | 995,328 | yes |
| 1 MB | Decay | 119 | 63 | 12.30% | 20 / 157 (12.74%) | 991,232 | yes |
| 1 MB | Salience | 119 | 59 | 11.52% | 21 / 157 (13.38%) | 987,136 | yes |
| 1 MB | Consolidate then forget | 121 | 59 | 11.52% | 18 / 157 (11.46%) | 999,424 | yes |
| 10 MB | FIFO | 1,000 | 512 | 100% | 157 / 157 (100%) | 4,501,504 | yes |
| 10 MB | LRU | 1,000 | 512 | 100% | 157 / 157 (100%) | 4,501,504 | yes |
| 10 MB | Decay | 1,000 | 512 | 100% | 157 / 157 (100%) | 4,501,504 | yes |
| 10 MB | Salience | 1,000 | 512 | 100% | 157 / 157 (100%) | 4,501,504 | yes |
| 10 MB | Consolidate then forget | 1,000 | 512 | 100% | 157 / 157 (100%) | 4,501,504 | yes |

Noise retention is noise-labeled survivors divided by the 157 noise records. The fixture has no anchors, so `anchors=0` in this matrix is not evidence of anchor protection. Anchor protection is covered separately by the storage regression and policy tests.

At 1 MB, pairwise survivor Jaccard was:

| 1 MB | FIFO | LRU | Decay | Salience | Consolidate |
|---|---:|---:|---:|---:|---:|
| FIFO | 1.0000 | 0.0524 | 0.0526 | 0.0573 | 1.0000 |
| LRU | 0.0524 | 1.0000 | 0.2713 | 0.1950 | 0.0524 |
| Decay | 0.0526 | 0.2713 | 1.0000 | 0.6301 | 0.0526 |
| Salience | 0.0573 | 0.1950 | 0.6301 | 1.0000 | 0.0573 |
| Consolidate | 1.0000 | 0.0524 | 0.0526 | 0.0573 | 1.0000 |

At 10 MB, the 5×5 survivor Jaccard matrix is:

| 10 MB | FIFO | LRU | Decay | Salience | Consolidate |
|---|---:|---:|---:|---:|---:|
| FIFO | 1.0000 | 1.0000 | 1.0000 | 1.0000 | 1.0000 |
| LRU | 1.0000 | 1.0000 | 1.0000 | 1.0000 | 1.0000 |
| Decay | 1.0000 | 1.0000 | 1.0000 | 1.0000 | 1.0000 |
| Salience | 1.0000 | 1.0000 | 1.0000 | 1.0000 | 1.0000 |
| Consolidate | 1.0000 | 1.0000 | 1.0000 | 1.0000 | 1.0000 |

All policies retained the same 1,000 IDs while the budget was inactive. The deterministic consolidation harness proposed, approved, and applied 10 merges, then used FIFO fallback because the vault remained over budget. Its final 1 MB survivor set exactly matched FIFO; it did not improve retention. Simulated approvals were local harness actions, not human or model review.

At 10 MB, all Jaccards were 1.0 because the 5,832,704-byte initial Day 5 vault was already below the budget. Forgetting was inactive. The runner still invoked total-mode enforcement and compaction for comparable before/after accounting.

### Day 4 versus Day 5 storage format

The same fixture, IDs, timestamps, labels, budget mode, and retention settings were used. Day 4 source was imported from `reports/day5_baseline_snapshot.zip`; Day 5 used the current source. Both base vaults began with 1,000 memories and 2,890 access events. The Day 4 base measured 9,928,704 allocated bytes; the Day 5 base measured 5,832,704 bytes.

Logical values are file `st_size` sums. Allocated values are filesystem block allocation (`st_blocks × 512` on this host). These units are separate and are not interchangeable.

| Source / state | Retained | Logical events | Logical history | Logical memories | Logical other | Logical total | Allocated events | Allocated history | Allocated memories | Allocated other | Allocated total |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Day 4 base | 1,000 | 1,725,520 | 1,521,357 | 1,521,357 | 379 | 4,768,613 | 1,732,608 | 4,096,000 | 4,096,000 | 4,096 | 9,928,704 |
| Day 4 after 1 MB FIFO | 16 | 801,253 | 24,335 | 24,335 | 427 | 850,350 | 860,160 | 65,536 | 65,536 | 4,096 | 995,328 |
| Day 4 after 10 MB FIFO | 1,000 | 237,765 | 1,521,357 | 1,521,357 | 428 | 3,280,907 | 249,856 | 4,096,000 | 4,096,000 | 4,096 | 8,445,952 |
| Day 5 base | 1,000 | 1,725,520 | 0 | 1,521,357 | 428 | 3,247,305 | 1,732,608 | 0 | 4,096,000 | 4,096 | 5,832,704 |
| Day 5 after 1 MB FIFO | 121 | 481,350 | 0 | 184,070 | 476 | 665,896 | 491,520 | 0 | 495,616 | 4,096 | 991,232 |
| Day 5 after 10 MB FIFO | 1,000 | 387,083 | 0 | 1,521,357 | 477 | 1,908,917 | 401,408 | 0 | 4,096,000 | 4,096 | 4,501,504 |

Allocated and logical tables include the `other` category; zero-valued anchor/archive/index categories are omitted for readability. Allocated category sums exactly match each total. The 10 MB event component is larger in Day 5 (401,408 vs 249,856 allocated bytes); the overall reduction comes from removing separate history copies, not from every component shrinking. Both baselines have `index=0`; the measurements do not include a persistent rebuilt retrieval index, which would add allocated bytes. This is a local format comparison, not a claim about every workload or filesystem.

## Capture scan

The scan used 200 records, seed `20261018`, fixed content/IDs/labels/timestamps/access history across the four requested rho values, 60-second record spacing, and a 1,800-second ring. The labels were 101 important, 70 normal, and 29 noise. Always-on captured all 200 and occupied 262,144 allocated bytes. Requested and empirical Pearson correlations matched to less than `1e-12`.

`false_wake_rate` means noise-labeled wake events divided by all wake events; normal-labeled wakes are not counted as errors. `backfill` counts candidates whose later `CAPTURE_RECORD` event timestamp is after the original candidate timestamp and within the 1,800-second ring.

| Requested rho | Threshold | Captured / 200 | Important recall | Noise retained / 29 | Wakes; noise wakes | False-wake rate | Backfilled | Allocated bytes | / always-on | H6 |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| 0.0 | .35 | 200 | 100.00% | 29 | 1; 0 | 0.00% | 0 | 262,144 | 100.00% | no |
| 0.0 | .50 | 200 | 100.00% | 29 | 2; 0 | 0.00% | 1 | 262,144 | 100.00% | no |
| 0.0 | .65 | 195 | 97.03% | 28 | 7; 2 | 28.57% | 14 | 266,240 | 101.56% | no |
| 0.0 | .80 | 114 | 56.44% | 14 | 5; 0 | 0.00% | 73 | 151,552 | 57.81% | no |
| 0.3 | .35 | 200 | 100.00% | 29 | 1; 0 | 0.00% | 0 | 262,144 | 100.00% | no |
| 0.3 | .50 | 200 | 100.00% | 29 | 3; 0 | 0.00% | 2 | 262,144 | 100.00% | no |
| 0.3 | .65 | 190 | 96.04% | 26 | 7; 1 | 14.29% | 36 | 262,144 | 100.00% | no |
| 0.3 | .80 | 107 | 53.47% | 13 | 5; 0 | 0.00% | 73 | 143,360 | 54.69% | no |
| 0.6 | .35 | 200 | 100.00% | 29 | 2; 0 | 0.00% | 0 | 262,144 | 100.00% | no |
| 0.6 | .50 | 200 | 100.00% | 29 | 4; 0 | 0.00% | 8 | 266,240 | 101.56% | no |
| 0.6 | .65 | 178 | 90.10% | 23 | 13; 0 | 0.00% | 41 | 266,240 | 101.56% | no |
| 0.6 | .80 | 107 | 53.47% | 13 | 5; 0 | 0.00% | 76 | 143,360 | 54.69% | no |
| 0.9 | .35 | 200 | 100.00% | 29 | 2; 0 | 0.00% | 2 | 262,144 | 100.00% | no |
| 0.9 | .50 | 200 | 100.00% | 29 | 11; 0 | 0.00% | 9 | 274,432 | 104.69% | no |
| 0.9 | .65 | 195 | 99.01% | 28 | 13; 0 | 0.00% | 18 | 274,432 | 104.69% | no |
| 0.9 | .80 | 166 | 87.13% | 19 | 10; 0 | 0.00% | 93 | 253,952 | 96.88% | no |

H6 passed 0/16 because every cell exceeded the 20% byte condition. The recall-gated subset is especially restrictive: the smallest capture footprint among cells with at least 70% important recall was 253,952 bytes (96.88% of always-on), at rho 0.9 / threshold 0.8. The minimum footprint overall was 143,360 bytes (54.69%) but retained only 53.47% of important inputs. These results apply to this synthetic prevalence and timing setup. With 50.5% important labels, preserving 70% recall inherently requires retaining many records. No labels or prevalence were changed to seek an H6 pass.

Allocated capture bytes include event logs and staged candidates. They represent the measured local staging vault, not the eventual human-approved memory vault. Filesystem allocation granularity affects these totals.

## Access, keyness, and LoCoMo-shaped retrieval

The 1,000-record access-bearing fixture has 2,890 accesses, a mean of 2.89 accesses per record, median 1, maximum 240, and 1,000 distinct last-access timestamps. Accesses are sampled only after each memory’s creation. The production policy receives access count, last-access time, creation time, and rolling keyness; labels remain external to policy inputs.

The 200-record policy diagnostic retained 160 records under an equal `max_count=160` budget. Important recall was FIFO 76.24% (77/101), LRU 83.17% (84/101), decay 82.18% (83/101), and salience 77.23% (78/101). The keyness top decile contained 10 important records out of 20 (50%). These small, synthetic results do not establish policy superiority. Keyness measures term distinctiveness against a rolling 20-document reference; it is not a direct measure of human importance.

Three local LoCoMo-shaped fixtures exercised 12 turns and 6 questions across categories 1–5 and all five retention policies. Five questions had valid evidence references; one category-5 question had empty evidence and was excluded from the valid-evidence denominator. Each policy kept 2 of 4 turns under `max_count=2`; retrieval used the same normalized query tokenization and 128-token context for MemFW, deterministic BM25, and a local mock-extraction CRUD baseline. This is not a byte-budget comparison. Each table cell below is evidence hits / valid evidence items after the count budget; `—` means no valid evidence in that category. Before retention, each retrieval method recalled all 5/5 valid evidence items.

| Category | FIFO MemFW | FIFO BM25 | FIFO CRUD | LRU MemFW | LRU BM25 | LRU CRUD | Decay MemFW | Decay BM25 | Decay CRUD | Salience MemFW | Salience BM25 | Salience CRUD | Consolidate MemFW | Consolidate BM25 | Consolidate CRUD |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | 0/1 | 0/1 | 0/1 | 0/1 | 0/1 | 0/1 | 1/1 | 1/1 | 1/1 | 1/1 | 1/1 | 1/1 | 0/1 | 1/1 | 1/1 |
| 2 | 1/1 | 1/1 | 1/1 | 1/1 | 0/1 | 0/1 | 0/1 | 0/1 | 0/1 | 0/1 | 0/1 | 0/1 | 1/1 | 0/1 | 0/1 |
| 3 | 0/1 | 0/1 | 0/1 | 0/1 | 0/1 | 0/1 | 1/1 | 0/1 | 0/1 | 1/1 | 0/1 | 0/1 | 0/1 | 0/1 | 0/1 |
| 4 | 0/1 | 0/1 | 0/1 | 0/1 | 0/1 | 0/1 | 1/1 | 1/1 | 1/1 | 1/1 | 1/1 | 1/1 | 0/1 | 1/1 | 1/1 |
| 5 | 1/1 | 1/1 | 1/1 | 1/1 | 1/1 | 1/1 | 1/1 | 1/1 | 1/1 | 1/1 | 1/1 | 1/1 | 1/1 | 1/1 | 1/1 |

Overall MemFW evidence recall was FIFO 2/5, LRU 2/5, decay 4/5, salience 4/5, and consolidation 2/5. Per-category table values are tiny fixture counts, not stable policy rankings. Evidence denominator excludes the one empty-evidence category-5 question; answers were never generated.

The offline baseline is a local deterministic first-sentence extractor with add/update/delete and lexical-overlap retrieval. It is Mem0-style only; it is not the official Mem0 implementation. The tiny fixtures are for adapter and retrieval plumbing checks, not comparative quality claims.

The official 10-sample LoCoMo file was not evaluated. Network access was denied, and no further retrieval was attempted. The local adapter and malformed-reference tests do not substitute for an official-corpus result.

| Official evaluation scope | Result |
|---|---|
| Official samples | 0/10 evaluated; local official file unavailable |
| Categories 1–5 × five policies | Not run; no official results are claimed |
| Reason | Network retrieval was denied; no further retrieval attempt was made |

## Access-tail fit

The 1,000-record access counts were fit with `fit_discrete_tail` using seed `20261018`, minimum tail sample 20, and 200 bootstrap replicates. It selected `xmin=3`, with 238 positive-tail observations, discrete zeta-MLE `alpha=2.3761`, KS distance `0.03572`, and bootstrap GOF `p=0.07960` (200/200 successful). The discrete lognormal comparison was inconclusive (Vuong `z=0.4702`, `p=0.6382`). These data do not distinguish a power law from a lognormal tail; a GOF p-value above 0.05 does not establish a power law. This is a descriptive synthetic workload result.

Known-law tests also passed. For an 800-value synthetic discrete power-law sample, the fitted alpha was 2.3414, KS 0.02492, bootstrap GOF p 0.2439 (40 replicates), and comparison inconclusive. For 1,200 discretized lognormal draws, 885 values were in the fitted tail; GOF p was 0.04762 (20 replicates) and the comparison favored lognormal (Vuong z -7.2402, p 4.48e-13). The estimator uses a discrete zeta likelihood and KS xmin selection, then semiparametric bootstrap with refitting and a discrete-bin lognormal comparison. P-values and Vuong comparison are approximate, and bootstrap resolution depends on replicate count.

Re-run the known-law tests with `.venv/bin/pytest -q tests/test_tail_fit.py`.

Reproduce the 1,000-record access fit with:

```sh
.venv/bin/python - <<'PY'
import json, sys
from dataclasses import asdict
sys.path.insert(0, "scripts")
from memfw.stats.tail_fit import fit_discrete_tail
from gen_day5 import SEED, generate_dataset
records, labels, accesses = generate_dataset(count=1000, seed=SEED, rho=0.6)
fit = fit_discrete_tail(
    [record.access_count for record in records],
    minimum_sample=20,
    bootstrap_samples=200,
    seed=SEED,
)
result = {
    "seed": SEED,
    "records": len(records),
    "access_events": len(accesses),
    "access_counts": [record.access_count for record in records],
    "fit": asdict(fit),
}
print(json.dumps(result, sort_keys=True))
PY
```

## Requirement status (original 1–19)

| # | Requirement | Status and evidence |
|---:|---|---|
| 1 | Single-body metadata history below 10% | **Complete.** The 1,000-record storage regression asserts both logical and allocated history are below 10% of active memory bytes before budget enforcement. Test: `tests/test_day4_storage.py::test_1000_record_one_mb_budget_compacts_and_preserves_nonanchors_by_strategy`. |
| 2 | Hash-summary snapshot log and verification | **Complete.** Cross-segment chain, append-after-compaction, snapshots, tamper detection, migration from legacy payload segments, and summary verification are tested in `tests/test_day4_storage.py`; the final Day 5 base and policy probes also replay successfully. This local hash chain is not an external signature or trusted timestamp. |
| 3 | Append-only JSONL capture staging and byte accounting | **Complete.** JSONL hash-chain staging, intent recovery, partial append recovery, and rejection of unlogged records are covered by `tests/test_capture.py`; physical and logical category sums are checked by the matrix runner. |
| 4 | Unsatisfied physical budget component overage | **Complete.** Anchor-floor overage and the explicit unsatisfied event are covered by `tests/test_repairs.py::test_total_budget_anchor_floor_is_reported_for_every_policy` and total-budget tests. |
| 5 | About 200 of 1,000 ~1 KB records under 1 MB | **Partial; target not reached.** The separate anchor regression retains 181 records (10 anchors + 171 non-anchors) at 983,040 allocated bytes; ID sets differ. The formal 2,890-access fixture retains 119–121 records at 987,136–999,424 bytes and has no anchors. Both are below the requested approximate 200-record target. |
| 6 | Heavy-tailed accesses, distinct recency, observable salience | **Complete.** Formal 1,000-record workload: 2,890 access events, maximum count 240, and 1,000 distinct last-access timestamps. Access/recency/keyness values flow into policy candidates; labels do not. |
| 7 | Deterministic simulated approval and merge | **Complete with explicit simulation.** The 1 MB consolidation run proposed/approved/applied 10 merges, then FIFO fallback; final survivors equaled FIFO. No human or live model reviewed the simulated approvals. |
| 8 | Five policies at 1/10 MB with Jaccard | **Complete.** All 10 real total-budget profiles completed. 1 MB pairwise Jaccards are listed above. Every 10 MB Jaccard is 1.0 because the budget was inactive; this is not policy-equivalence evidence. |
| 9 | LoCoMo reference normalization and malformed counts | **Complete for adapter tests.** Tolerant local adapter tests cover semicolon-separated IDs, spaces, `D:11:26` normalization, unknown references, malformed fragments, list-shaped top-level inputs, and deduplication. |
| 10 | Official 10 samples × categories × five policies | **Not run for official data.** The official corpus was unavailable after network denial. The self-made fixtures exercised 3 fixtures, categories 1–5, and all five policies; this does not satisfy the official-data run. |
| 11 | BM25 and offline mock-extraction CRUD baseline | **Complete as local scaffolding.** BM25 and a distinct local add/update/delete mock-extraction baseline were evaluated with the same query tokens. It is not official Mem0. |
| 12 | Evidence retrieval only, no answer generation | **Complete.** The evaluator reports evidence recall and question-hit fraction; `answer_generation` is explicitly `none; evidence retrieval only`. |
| 13 | Adjustable intensity/importance rho | **Complete.** Rho values 0, 0.3, 0.6, 0.9 preserved all fixture content and labels; empirical correlations matched requested rho to <1e-12. |
| 14 | Ring duration independent from record interval | **Complete.** Capture scan uses a 1,800-second ring independently of 60-second record intervals. Positive interval-aware backfill was recorded, up to 93 candidates. |
| 15 | 16-cell rho × threshold actual capture scan | **Complete.** All 16 cells recorded allocated-byte totals, recall, noise retention, false wakes, duty, and backfill. Quick scan runtime was 83.773951 seconds. |
| 16 | H6 with at least 70% important recall and at most 20% bytes | **Not achieved in this scan.** 0/16 cells met both conditions. The smallest footprint among recall-qualified cells was 253,952/262,144 = 96.88%; no label prevalence was tuned to force a pass. |
| 17 | Knowledge source validation, protection, and relinking | **Complete by tests.** Tests cover all three card kinds, nonexistent source rejection without write, missing-source marking, source protection/repoint before merge/delete, and approval requirements (`tests/test_knowledge_store.py`). Deleting a source marks it missing; it does not erase the knowledge statement, which must not be treated as currently supported. |
| 18 | Discrete tail fit, bootstrap/Vuong, known-law checks | **Complete with approximation limits.** 1,000-record access fit and discrete power-law/lognormal synthetic known-law tests passed. Estimator, xmin, bootstrap and comparison details are reported above; no claim of exact asymptotic inference is made. |
| 19 | Keyness positive terms, stopwords, and configurable top-k | **Complete by tests.** G² and %DIFF known-token checks, absent-reference handling, repeated non-stopwords, custom stopwords, rolling reference, and configurable top-k are tested in `tests/test_keyness.py`. Keyness distinctiveness is not human importance. |

## Failed exploratory run and correction

An earlier prepack matrix was interrupted and excluded. Shared repeated padding created thousands of unintended near-duplicate proposals: the 1 MB consolidation profile emitted 1,378 proposal events. Its reported 4,570,999 bytes were a live sum of logical file sizes (`st_size`), measured while the process was changing files; they were racy and were not allocated bytes. Four completed non-consolidation profiles from that attempt also failed the 1 MB target with zero survivors and 1,069,056–1,093,632 allocated bytes. See `reports/day5_policy_matrix_prepack_failure.json` for the preserved receipt. The final fixture uses deterministic unique capacity tokens and a preflight that returns only the ten declared duplicate pairs; the prepack results are not used in the tables above.

## Reproduction and validation

Final policy matrix command:

```sh
.venv/bin/python scripts/day5_storage_matrix.py --count 1000 --seed 20261018 --output-dir reports --baseline-archive reports/day5_baseline_snapshot.zip --work-dir /private/tmp/memfw-day5-final-work --checkpoint reports/day5_policy_matrix_checkpoint.json
```

Final 16-cell capture command:

```sh
.venv/bin/python scripts/day5_experiment.py --count 200 --seed 20261018 --output-dir reports --output reports/day5_capture_scan.json
```

Re-run the local three-fixture adapter/evidence evaluation with:

```sh
.venv/bin/python scripts/eval_locomo.py tests/fixtures/day5_locomo/fixture_1.json tests/fixtures/day5_locomo/fixture_2.json tests/fixtures/day5_locomo/fixture_3.json --budget-tokens 128 --policies fifo lru decay salience consolidate_then_forget --output reports/day5_locomo_selfmade_eval.json
```

The matrix ran in 1,210.241517 seconds. The capture scan ran in 83.773951 seconds, below 300 seconds. Runtime receipts are stored separately in `reports/day5_storage_runtime_receipt.json` and `reports/day5_runtime_receipt.json`.

Validation completed on Python 3.11.16, SQLite 3.53.1, zstandard 0.25.0, and jieba 0.42.1 on macOS 26.7.1 arm64. Allocated-byte measurements use filesystem block accounting (`st_blocks × 512` on this host); those figures can vary across filesystems and block sizes. The stable JSON inputs and semantic outputs are hash-identified in `reports/day5_policy_matrix.json`.

Final checks:

```sh
.venv/bin/pytest -q --basetemp=/private/tmp/memfw-day5-final-tests
.venv/bin/ruff check .
.venv/bin/ruff format --check src tests scripts
.venv/bin/mypy --strict src tests scripts
```

The full test suite passed: 269 passed in 149.90 seconds. Ruff, format check (73 files), and strict mypy (73 files) passed. The official LoCoMo corpus remains unverified, and no remote download was attempted after network access was denied.

The matrix records these input hashes: documents `05f52fb5e84e29bdf972d21fb6cbb2fad6c12df02fafa8c588fb2ea5c09e427f`, accesses `1ee45af26d494502e421b9c2bdc0f88eb09ee2ed14373cdb0fe55d388e502b49`, and evaluation-only labels `5a8408611b9d456d19ce1ec90c6cf6a39a88fb3226bf61849e9978148e7c7f35`. The Day 4 baseline ZIP hash is `a8308bb25ad91e5f0d80dfb5ba09c0db8f3a9cc432adf6a786f3135ebcf98858`. The matrix JSON also stores the baseline/current source-tree hashes and runner hashes. Runtime receipts are separate because wall time is not deterministic.
