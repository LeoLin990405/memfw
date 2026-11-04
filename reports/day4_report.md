# Day 4 data, capture, and evaluation report

The bounded Day 4 quick run completed in **35.241927 seconds** for 200 records, six capture thresholds, two old/new storage profiles, and three local LoCoMo-shaped fixtures. It used seed `20261018`, no network or model calls, and an interval-derived capture ring. At threshold `.85`, physically allocated vault bytes were **13.85%** of always-on bytes, while the system retained only **11 of 101** assigned-important records (**10.89% recall**). This meets H6 for this one setup, but loses 89.11% of assigned-important records; it is not a useful general policy claim.

All importance and topic labels are synthetic evaluation assignments, not human judgments. Labels live in `day4_labels.jsonl`; capture and scoring receive only content and observable signals. The 1 MB/10 MB storage matrix below enforces a **logical body-byte budget**, not a physical-vault quota. The separate 1,000-record total-mode acceptance regression is reported independently.

## Capture sweep

The run covered 30 days. The record interval was 12,960 seconds (3.6 hours), and the ring retained two intervals (25,920 seconds, 7.2 hours). Standby was 1,800 seconds; dormant transition was 172,800 seconds. Each row below was captured into a real repository and measured with allocated physical bytes. Denominators: 101 assigned-important records, 29 assigned-noise records, 2,592,000 elapsed seconds, and the listed wake-event count. “False wakes” counts `CAPTURE_WAKE` events occurring at a record timestamp assigned the noise label. Backfills are matched `CAPTURE_RECORD` events whose logged event time is later than the source candidate timestamp and within the ring window.

| High threshold | Candidates | Important captured / 101 (recall) | Noise captured / 29 (retention) | False wakes / wake events | Ring backfills | Full-duty fraction | Physical bytes / always-on bytes | H6 <= 20% |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | :---: |
| .35 | 199 | 100 (99.01%) | 29 (100.00%) | 9 / 51 | 31 | 62.47% | 1,007,616 / 946,176 (106.49%) | No |
| .45 | 163 | 84 (83.17%) | 22 (75.86%) | 5 / 38 | 43 | 44.07% | 823,296 / 946,176 (87.01%) | No |
| .55 | 126 | 65 (64.36%) | 14 (48.28%) | 4 / 28 | 41 | 30.88% | 634,880 / 946,176 (67.10%) | No |
| .65 | 95 | 49 (48.51%) | 10 (34.48%) | 2 / 20 | 33 | 22.82% | 483,328 / 946,176 (51.08%) | No |
| .75 | 57 | 31 (30.69%) | 5 (17.24%) | 2 / 13 | 14 | 16.33% | 299,008 / 946,176 (31.60%) | No |
| .85 | 23 | 11 (10.89%) | 3 (10.34%) | 1 / 7 | 4 | 6.92% | 131,072 / 946,176 (13.85%) | Yes, in this setup |

The generated set contains seven interleaved themes and is bilingual. Its labels are assigned from the generator’s synthetic process; 46 of 101 assigned-important items (45.54%) have intensity below `.35`. That is a useful low-intensity stress case, not evidence of human importance.

## Keyness, judge, policy, and tail diagnostics

The experiment computes rolling keyness against the prior 20 documents. The top decile contained 13 assigned-important items out of 20 (65%), compared with an overall assigned-important rate of 101/200 (50.5%). The deterministic hash judge’s top decile contained 9/20 (45%); overlap between its and keyness’ top deciles was 2/20 (10%). Mean keyness was 0.2232; assigned-important mean was 0.2273, and other records averaged 0.2191. The judge is a hash-based placeholder, not a semantic model. These synthetic results do not establish ranker superiority, and keyness distinctiveness is not the same as human importance.

Observable strength used 300 logged synthetic `MEMORY_ACCESS` events: 150 documents were accessed and 50 were not. It combined access count, last-access recency, and rolling keyness. The SaliencePolicy received these inputs without evaluation labels and retained 160/200 records; 84/101 assigned-important records and 20/29 assigned-noise records remained. Both the judge and policy payloads were checked for the literal evaluation markers and theme names.

Tail fitting ran on access counts, stored reference counts, and evaluation-only theme frequencies. Access counts had 150 positive samples, 50 excluded zeros, `xmin=1`, approximate alpha 2.674, descriptive KS distance 0.347, and log-likelihood ratio -6.980. Stored reference counts were all zero (200 excluded); topic frequencies had seven samples. Both returned `insufficient_sample`. The helper uses continuous-tail approximations for discrete observations; KS is descriptive, and the run did not compute a bootstrap goodness-of-fit p-value or make a Clauset statistical-support claim.

## Local LoCoMo-shaped retrieval after forgetting

The adapter accepts either one `conversation.session_N` object or a top-level list of samples, including turn arrays or `{ "turns": [...] }`; it ignores non-session conversation metadata and validates evidence IDs. It ran three self-made fixtures, 13 turns and six evidence questions. Each fixture used a 128-token retrieval context and a hard FIFO retention cap of half its initial turns (2 retained per fixture). Both MemFW and the local offline CRUD/retrieval reference had 6/6 evidence hits before forgetting. After forgetting, both retained 4/6 evidence items and hit 4/6 questions (66.67% evidence recall and question-hit fraction). The reference supports add/update/delete and lexical overlap retrieval; it is **not official Mem0**. No answers or LLM calls were used. These fixtures are not the official LoCoMo dataset.

## Storage comparison and reproducibility

Both profiles used the same 200 clean Day4 records, seed, timestamps, FIFO hard-forget body cap, no anchors, and one metadata summary update. Before forgetting, bodies totalled 1,500,000 bytes for the 1 MB profile and 15,000,000 bytes for the 10 MB profile. Both old and new implementations retained the same 133 survivors and ended below their logical body caps. Physical allocated bytes exceeded the body-cap values; these profiles do not assert a total-vault physical quota.

| Body cap | Runtime | Physical before forget | Physical after forget, before compaction | Physical after final step | Final allocated breakdown: events / history / memories / anchors / archives / index / other |
| --- | --- | ---: | ---: | ---: | --- |
| 1 MB | Frozen baseline, no compaction | 3,391,488 | 2,355,200 | 2,355,200 | 163,840 / 1,097,728 / 1,089,536 / 0 / 0 / 0 / 4,096 |
| 1 MB | Current runtime, then compaction | 3,387,392 | 2,326,528 | 2,265,088 | 77,824 / 1,093,632 / 1,089,536 / 0 / 0 / 0 / 4,096 |
| 10 MB | Frozen baseline, no compaction | 31,313,920 | 20,946,944 | 20,946,944 | 163,840 / 10,428,416 / 10,350,592 / 0 / 0 / 0 / 4,096 |
| 10 MB | Current runtime, then compaction | 31,240,192 | 20,848,640 | 20,787,200 | 77,824 / 10,354,688 / 10,350,592 / 0 / 0 / 0 / 4,096 |

Compaction archived 169 events and retained the newest 100. It reduced allocated bytes by 90,112 (3.83%) for the 1 MB body-cap case and 159,744 (0.76%) for the 10 MB body-cap case relative to the corresponding baseline’s un-compacted post-forget size. The separate total-mode 1,000-record acceptance test is separate from the fixed-survivor body-budget comparison above. FIFO and salience each retained 32 records, including 22 non-anchors, and allocated 991,232 bytes after compaction. Their survivor sets differ by 34 IDs. Both had 1,969 logical events and exact replay agreement with extant Markdown and deletion tombstones. Their measured breakdowns are identical:

| Total-mode 1 MB test, FIFO and salience | Allocated bytes per strategy |
| --- | ---: |
| Events | 724,992 |
| History | 131,072 |
| Memories | 90,112 |
| Anchors | 40,960 |
| Archives | 0 |
| Index | 0 |
| Other | 4,096 |
| **Total** | **991,232** |

Events and history account for 856,064 bytes (86.36%) of that observed total.

The frozen baseline is archived at `day4_baseline_snapshot.zip` (GitHub Release [data-v1](https://github.com/LeoLin990405/memfw/releases/download/data-v1/day4_baseline_snapshot.zip); see [LARGE_FILES.md](LARGE_FILES.md)), SHA-256 `44f47f7859658defc241f52339fae11b7df75cb7531f5c23d7b89c10c34441d7`. Its 51-file inventory matches the archive contents; the archive contains the inventory manifest as the extra member. Extraction validates member paths. Day 3 reports and source artifacts were not regenerated.

## 18-claim status and evidence

| # | Claim | Status | Evidence and command | Limitation |
| ---: | --- | --- | --- | --- |
| 1 | Compaction verifies archived/current event chains, survives interruption, and records hard-delete tombstones | Pass | `tests/test_day4_storage.py::test_log_compaction_preserves_cross_segment_chain_and_future_appends`, `test_log_compaction_failure_is_readable_and_explicit_recovery_trims_tail`, `test_delete_tombstone_is_durable_before_content_erasure`; replay hashes and DELETE event asserted | Hash chain has no external trust anchor; a full-log rewrite can recompute it. Compaction bounds the retained tail but does not stop new metadata events from accumulating |
| 2 | Metadata-only history deltas preserve rollback with the default two-version retention | Pass | `tests/test_day4_storage.py::test_metadata_only_history_delta_reconstructs_and_rolls_back` and `test_metadata_delta_body_base_is_not_extra_rollback_retention` | Older versions outside configured rollback retention are unavailable by design |
| 3 | Physical breakdown and unsatisfied floors are explicit | Pass | `tests/test_day4_storage.py::test_physical_breakdown_is_nonoverlapping_and_sums_to_total`; `tests/test_day3_budget.py::test_anchor_metadata_floor_records_one_deduplicated_unsatisfied_event` | Floor is an observed state, not a theoretical minimum; rebuildable index bytes may be reclaimed |
| 4 | A 1,000-record total-mode run meets 1 MB while retaining non-anchors under different policies | Pass | `tests/test_day4_storage.py::test_1000_record_one_mb_budget_compacts_and_preserves_nonanchors_by_strategy`; FIFO and salience each retained 32/22 non-anchors, 991,232 bytes, 1,969 events; survivor ID symmetric difference 34 | Synthetic fixture, one budget profile |
| 5 | Day4 labels are isolated from generated content | Pass | `scripts/gen_day4.py`; `tests/test_day4_experiment_generator.py`; separate `reports/day4_labels.jsonl` | Synthetic assigned labels, not human judgments |
| 6 | Observable strength uses access count, last-access recency, and keyness | Pass | 300 logged `MEMORY_ACCESS` events; 150 accessed docs; mean strength 0.5678; `scripts/day4_experiment.py` | Synthetic access schedule |
| 7 | Seven themes are interleaved | Pass | Seven label themes; more than three occur in the first 14 records; generator test | Themes are synthetic |
| 8 | Judge and policy inputs are checked for label leakage | Pass | `test_judge_and_policy_use_observables_without_evaluation_labels`; runtime scan covers importance/noise markers and theme names | Checks the generated vocabulary and actual payloads |
| 9 | Keyness uses G², `%DIFF`, local tokenization, and a rolling reference | Pass | `tests/test_keyness.py`; 200-record report uses prior-document window 20 | `%DIFF` is undefined (`null`) where reference frequency is zero; score is distinctiveness, not importance |
| 10 | Salience and decay use keyness; observed keyness is compared with the mock judge | Pass, descriptive | `tests/test_keyness.py::test_measured_keyness_changes_both_policies_and_observable_strength`; 13/20 assigned-important for keyness, 9/20 for judge, 2/20 top-decile overlap | Judge is a deterministic hash baseline; no semantic or inferential superiority claim |
| 11 | MLX live adapter accepts injected transport without a provider key | Pass | `tests/test_judges.py` MLX live test removes `MLX_API_KEY` and injects transport | Adapter does not launch a local model or create a transport |
| 12 | Capture intensity varies continuously and includes quiet important cases | Pass | 46/101 assigned-important records have intensity `<.35`; seed `20261018` report | Labels are synthetic assignments |
| 13 | Interval-aware ring backfills recent candidates on wake | Pass | 12,960-second interval; 25,920-second window; matched backfills 31, 43, 41, 33, 14, 4; standby-ring test | Two-interval window is this experiment’s configuration |
| 14 | Threshold scan measures H6 against always-on allocated bytes | Pass, conditional | Six thresholds; only `.85` is <=20% (131,072/946,176=13.85%) | At `.85`, 90/101 assigned-important records are missed |
| 15 | Complete quick experiment finishes in under five minutes | Pass | `reports/day4_quick_runtime_receipt.json`: 35.241927 seconds, 200 records, 3 fixtures; command shown above | Bounded run, not the optional full 1,000-capture run |
| 16 | Local LoCoMo-shaped evaluation compares post-forget evidence with offline CRUD baseline | Pass on self-made fixtures | `tests/test_day4_experiment_locomo.py`; three fixtures, six questions; both methods 4/6 evidence hits after FIFO forget | Not official LoCoMo or official Mem0; no generated answers |
| 17 | Knowledge cards persist as Markdown and mark missing source refs without deleting statements | Pass | `tests/test_knowledge_store.py::test_persist_and_reload_all_knowledge_card_kinds`, `test_card_rejects_nonexistent_sources_without_writing`, and `test_source_deletion_marks_card_missing_and_logs_event`; all three card kinds round-trip; nonexistent source is rejected; deletion marks missing while retaining statement | Card is marked `missing_sources`; provenance is incomplete until repaired |
| 18 | Tail analysis reports access, reference, and topic-frequency fits | Partial by design | `reports/day4_experiment.json`; access fit descriptive; reference and topic series insufficient | Continuous approximation; no bootstrap p-value or Clauset support claim |

Quick-run receipt: `reports/day4_quick_runtime_receipt.json` (35.241927 seconds, below 300 seconds; storage matrix 23.578548 seconds). The complete post-freeze test suite passed: `./.venv/bin/pytest -q -s --basetemp=/private/tmp/memfw-day4-final-tests` reported **234 passed in 165.67 seconds**. The root run included all preserved Day 3 tests and the 1,000-record total-mode compaction test. Post-freeze `./.venv/bin/ruff check .` passed, `./.venv/bin/ruff format --check src tests scripts` found all 65 files formatted, and `./.venv/bin/mypy --strict src tests scripts` passed for 65 files.

Reproduction and verification commands:

```sh
./.venv/bin/python scripts/day4_quick.py --output-dir reports --count 200 --seed 20261018 --ring-intervals 2.0
./.venv/bin/python scripts/eval_locomo.py tests/fixtures/day4_locomo/fixture_1.json tests/fixtures/day4_locomo/fixture_2.json tests/fixtures/day4_locomo/fixture_3.json --budget-tokens 128
./.venv/bin/python scripts/day4_experiment.py --count 1000 --output-dir reports/day4_capture_1000
./.venv/bin/pytest -q -s
./.venv/bin/ruff check .
./.venv/bin/ruff format --check src tests scripts
./.venv/bin/mypy --strict src tests scripts
```

The 1,000-capture command is optional and distinct from the 1,000-record total-mode storage acceptance test. The baseline archive uses a frozen Day 3 source snapshot. Compaction verifies internal hashes and replay; the chain has no external trust anchor. The system still generates metadata events between compactions, so compaction limits retained tail size but does not eliminate ongoing metadata growth.
