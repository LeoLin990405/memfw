# Day 7 LoCoMo evaluation

## Result

The official local run completed in **249.418 s (4 min 9 s)** of measured pipeline time, below the 600 s target. It processed all 10 conversations, 5,882 turns, and 1,986 questions, then ran 1,000 paired-bootstrap repeats per condition/budget scope and a 200-record capture gate. Four memory-retention scopes each had 1,982 valid-evidence questions and 2,820 deduplicated evidence IDs.

No retention policy showed an overall Holm-adjusted significant advantage over another BM25 policy at either budget or access condition. Salience had the highest point recall at 25% with shared access (14.539% vs FIFO 13.014%), but the paired comparison was not significant. One exploratory category contrast passed its within-category Holm family: at shared-access/10%, category 1 decay minus salience was −2.381 percentage points (raw p=.003996; Holm p=.03996). Category tests were not corrected across all categories and scopes, so treat that as exploratory.

The oracle’s overall recall was 95.035%, limited by the 128-token retrieval context even though it sees the gold evidence. It is a query-conditioned cheating bound, not a deployable retention set. The capture gate found **zero H6 passes** among the four strategies. Even the oracle gate’s packed-stage fraction was 22.185% logical and 33.333% allocated while achieving 100% important recall.

## Requirements status

| # | Requirement | Result |
|---:|---|---|
| 1 | BM25 default retrieval | Complete. `MemoryIndex.ranked_search` and `VaultRetriever` default to BM25; legacy FTS count remains an explicit arm. See `tests/test_day7_eval.py`, `tests/test_index.py`. |
| 2 | BM25 parity and former count comparison | Complete. `fifo_bm25` and standalone `bm25_fifo` had 0 hit-count mismatches across 7,928 valid question/scope comparisons and identical retained IDs. FTS-count results are separately reported. |
| 3 | Sample-local, past-only keyness | Complete. Production `candidates_for` uses rolling prior-20 reference per sample; no future sample leakage. See `tests/test_day7_eval.py`. |
| 4 | Shared query-independent access flow | Complete. Condition B reuses a fixed-seed per-sample access stream across every method; condition A has no accesses. Digest is in the output JSON. |
| 5 | Bootstrap CIs and paired tests | Complete. 1,000 paired within-conversation question resamples; all 13 methods get CIs. The primary family is 10 pairwise contrasts among five BM25 policies per scope, with Holm correction. |
| 6 | Cheating evidence upper bound | Complete. Query-conditioned evidence knapsack uses the same byte and 128-token limits and sees gold evidence. It is non-deployable; retrieval context caps observed recall at 95.035%. |
| 7 | Fact-byte budgets | Complete. Full-body and mock CRUD facts are measured against each sample's 10%/25% canonical-body budget. Mock CRUD retains 1,172 facts / 72,177 B at 10%, and 2,955 / 181,331 B at 25% (sums of separate sample budgets). |
| 8 | Single-run official evaluator | Complete. `eval_locomo.py --day7` writes the evaluation, bootstrap, and gate result directly; no post-run corrector. |
| 9 | Cold-source reads and source-protection guard | Complete. Cold indexed bodies retrieve verified archive content; missing cold content fails closed. Payload-bound source deletion proposals survive cooling and reject edited content. Markerless legacy Day6 raw-bound proposals remain valid but block cooling until resolved; semantic-v1 proposals survive cooling. See the three cold/source tests in `tests/test_day7_core.py`. |
| 10 | Label-blind gate judge | Complete. The deterministic judge receives text and prior text, not truth labels; only oracle/metric code sees labels. See `tests/test_day7_gating.py`. |
| 11 | Peak and all-in storage accounting | Complete. Gate output records stage, all-in, acquisition/pack and post-confirmation peaks, including operation-sampled atomic temp-file usage. Logical and allocated bytes are separate. |
| 12 | Remove the `_em` estimator label | Complete. `src/memfw/stats/tail_fit.py` now reports the actual estimator name `golden_section_discrete_zeta_mle`; `tests/test_day7_core.py::test_tail_fit_label_names_actual_estimator` verifies it. |
| 13 | Append and verification performance | Complete. Matched 100k event stream from frozen Day6 and current source: 56.407 s vs 46.009 s append, one pair only. |
| 14 | End-to-end quick run ≤600 s | Complete. All 10 samples/all questions, 1,000 bootstrap repeats, and N=200 gate completed in 249.418 s. The v1 failure and its 1-byte normalization cause are preserved. |

## Official data and evaluation boundary

Input: `<MEMFW_DATA_DIR>/locomo10.json`, SHA-256 `79fa87e90f04081343b8c8debecb80a9a6842b76a7aa537dc9fdf651ea698ff4`. The input stayed outside the repository. The result JSON contains identifiers, categories, evidence counts, scores, hashes, and metrics, but no question or conversation bodies.

The adapter read 5,882 `session_N` turns. It normalized evidence IDs such as `D30:05` to integer `D30:5`, salvaged valid references, and excluded malformed/unknown references. The run had 2 unknown references, 1 malformed evidence fragment, and 4 questions with no valid evidence (all category 3). The remaining 1,982 QA entries contributed 2,820 per-question-deduplicated evidence IDs. The category denominators were:

| Category | Valid-evidence questions | Evidence IDs |
|---:|---:|---:|
| 1 | 282 | 882 |
| 2 | 321 | 375 |
| 3 | 92 | 208 |
| 4 | 841 | 895 |
| 5 | 446 | 460 |

The score is evidence-ID recall: `retrieved gold evidence IDs / valid gold evidence IDs`. There is no answer generation or answer correctness measure. The run uses only turn text; it does not use image understanding or generated captions. Category 5 is included because it has gold evidence, even though its questions can be adversarial.

## Systems and budgets

The standalone FIFO BM25 baseline and the FIFO policy BM25 arm produced 0 hit-count mismatches in 7,928 valid question/scope comparisons (1,982 questions × four scopes); retained IDs were identical. The shared index-ranking implementation also has focused parity coverage.

The five policy arms are FIFO, LRU, decay, salience, and consolidate-then-forget. Each is scored with two retrieval orders: the prior per-term FTS hit-count order and BM25. The legacy FTS order counts each matched query term’s FTS hit and sorts by descending count then ID. BM25 uses the production searchable fields (speaker title, body, summary, tags), shared tokenizer, and IDF/length statistics recomputed on each retained ID set. The standalone FIFO BM25 baseline uses the same BM25 ranker and index. FTS and BM25 rows for a given policy share the same retained IDs.

The offline mock CRUD arm adds and updates a deterministic first-sentence fact per turn, then deletes oldest facts until they fit. It is a local CRUD/retrieval exercise, not official Mem0. Its evidence score still credits the original turn ID; it does not verify that a retained first sentence contains the answer-supporting span, so its evidence recall can overstate semantic preservation.

Mock CRUD totals sum the ten per-sample stores; each sample had its own capacity, so these are not pooled budgets.

| Body budget | Retained fact count | Retained fact UTF-8 bytes | Sum of sample capacities |
|---:|---:|---:|---:|
| 10% | 1,172 | 72,177 | 72,687 |
| 25% | 2,955 | 181,331 | 181,724 |

Each sample’s capacity is `floor(ratio × sum(canonical stored body UTF-8 bytes))`, at 10% and 25%. A repository Markdown round trip strips trailing newlines. Therefore the canonical stored total (726,912 B) is the denominator; raw input turn-text totals 726,954 B are reported separately. All policy and full-body baseline retention checks were within budget. Mock CRUD measures fact bytes against the same per-sample capacity. Retrieval uses full-turn speaker-aware contexts, admits whole turns only, and skips a turn if it cannot fit the 128-token query context; there is no chunking.

Condition A (`keyness_only`) enables rolling prior-only G² keyness with a 20-document window and has no access events. Condition B (`keyness_plus_shared_access`) adds a fixed-seed, question-independent access stream once per sample and shares it across all methods. The BM25 and mock retrieval algorithms do not use access counts; the access condition changes only policy inputs that consume them. Results are logical retention projections, not physical vault measurements. Consolidation uses deterministic non-overlapping mock-judge proposals, simulated approval semantics, production delete disposition, and FIFO fallback when projected merges do not meet the byte budget; the helper has a persistent API parity test.

## Evidence recall with paired 95% intervals

Each cell is micro evidence recall with a 95% percentile interval from 1,000 paired bootstrap replicates. Resampling occurs within each of the 10 conversations. The pairwise family is exactly the five BM25 policy arms (10 contrasts) in each overall or category scope; two-sided sign-tail bootstrap p-values use plus-one correction and Holm adjustment within each scope. Intervals describe question resampling conditional on these 10 conversations, not generalization to new users.

### keyness only/10%

| System | Overall | Cat 1 | Cat 2 | Cat 3 | Cat 4 | Cat 5 |
|---|---:|---:|---:|---:|---:|---:|
| FIFO / FTS-count | 5.603% [4.782%, 6.482%] | 2.721% [1.571%, 3.863%] | 6.133% [3.988%, 8.517%] | 3.365% [0.994%, 6.488%] | 7.263% [5.599%, 9.071%] | 8.478% [6.223%, 11.064%] |
| LRU / FTS-count | 5.603% [4.782%, 6.482%] | 2.721% [1.571%, 3.863%] | 6.133% [3.988%, 8.517%] | 3.365% [0.994%, 6.488%] | 7.263% [5.599%, 9.071%] | 8.478% [6.223%, 11.064%] |
| Decay / FTS-count | 5.567% [4.692%, 6.482%] | 2.834% [1.730%, 4.039%] | 5.867% [3.773%, 8.158%] | 3.846% [1.578%, 6.780%] | 7.151% [5.494%, 8.850%] | 8.261% [5.702%, 10.630%] |
| Salience / FTS-count | 6.206% [5.322%, 7.151%] | 3.288% [2.127%, 4.488%] | 7.200% [4.773%, 10.029%] | 4.808% [2.193%, 7.772%] | 8.045% [6.208%, 9.978%] | 8.043% [5.727%, 10.691%] |
| Consolidate then forget / FTS-count | 5.567% [4.748%, 6.461%] | 2.721% [1.571%, 3.863%] | 6.133% [3.988%, 8.517%] | 3.365% [0.994%, 6.488%] | 7.263% [5.599%, 9.071%] | 8.261% [5.947%, 10.753%] |
| FIFO / BM25 | 5.993% [5.135%, 6.957%] | 2.721% [1.618%, 3.956%] | 6.133% [4.021%, 8.494%] | 4.808% [1.739%, 8.644%] | 7.933% [6.271%, 9.652%] | 8.913% [6.451%, 11.649%] |
| LRU / BM25 | 5.993% [5.135%, 6.957%] | 2.721% [1.618%, 3.956%] | 6.133% [4.021%, 8.494%] | 4.808% [1.739%, 8.644%] | 7.933% [6.271%, 9.652%] | 8.913% [6.451%, 11.649%] |
| Decay / BM25 | 6.135% [5.286%, 7.121%] | 2.834% [1.736%, 3.996%] | 5.600% [3.514%, 7.904%] | 4.808% [1.702%, 8.467%] | 8.268% [6.570%, 10.068%] | 9.348% [6.753%, 11.791%] |
| Salience / BM25 | 7.163% [6.210%, 8.240%] | 3.968% [2.775%, 5.385%] | 7.467% [5.106%, 10.317%] | 6.250% [3.375%, 9.650%] | 9.050% [7.190%, 11.111%] | 9.783% [7.125%, 12.877%] |
| Consolidate then forget / BM25 | 5.993% [5.135%, 6.957%] | 2.721% [1.618%, 3.956%] | 6.133% [4.021%, 8.494%] | 4.808% [1.739%, 8.644%] | 7.933% [6.271%, 9.652%] | 8.913% [6.451%, 11.649%] |
| BM25, FIFO retention | 5.993% [5.135%, 6.957%] | 2.721% [1.618%, 3.956%] | 6.133% [4.021%, 8.494%] | 4.808% [1.739%, 8.644%] | 7.933% [6.271%, 9.652%] | 8.913% [6.451%, 11.649%] |
| Offline mock CRUD | 5.532% [4.700%, 6.415%] | 2.381% [1.333%, 3.564%] | 8.533% [5.994%, 11.141%] | 3.365% [0.926%, 6.468%] | 7.709% [5.942%, 9.646%] | 5.870% [3.871%, 8.133%] |
| Query-conditioned evidence oracle | 95.035% [93.488%, 96.429%] | 87.528% [83.940%, 90.462%] | 100.000% [100.000%, 100.000%] | 85.577% [77.550%, 94.304%] | 100.000% [100.000%, 100.000%] | 100.000% [100.000%, 100.000%] |

Overall denominator: 2820 evidence IDs across 1982 valid-evidence questions. Category denominators are listed above. Intervals resample questions within each of the 10 conversations.
### keyness only/25%

| System | Overall | Cat 1 | Cat 2 | Cat 3 | Cat 4 | Cat 5 |
|---|---:|---:|---:|---:|---:|---:|
| FIFO / FTS-count | 11.170% [10.025%, 12.407%] | 5.102% [3.559%, 6.748%] | 12.000% [8.943%, 15.152%] | 3.846% [1.515%, 6.771%] | 16.313% [13.950%, 18.565%] | 15.435% [12.253%, 18.913%] |
| LRU / FTS-count | 11.170% [10.025%, 12.407%] | 5.102% [3.559%, 6.748%] | 12.000% [8.943%, 15.152%] | 3.846% [1.515%, 6.771%] | 16.313% [13.950%, 18.565%] | 15.435% [12.253%, 18.913%] |
| Decay / FTS-count | 10.071% [8.974%, 11.245%] | 4.308% [2.928%, 5.763%] | 11.467% [8.377%, 14.514%] | 3.846% [1.333%, 7.304%] | 14.413% [12.179%, 16.649%] | 14.348% [11.253%, 17.505%] |
| Salience / FTS-count | 10.957% [9.742%, 12.171%] | 5.782% [4.402%, 7.267%] | 14.933% [11.824%, 18.431%] | 4.327% [1.905%, 7.029%] | 14.413% [12.056%, 16.856%] | 13.913% [10.988%, 17.031%] |
| Consolidate then forget / FTS-count | 11.170% [10.025%, 12.407%] | 5.102% [3.559%, 6.748%] | 12.000% [8.943%, 15.152%] | 3.846% [1.515%, 6.771%] | 16.313% [13.950%, 18.565%] | 15.435% [12.253%, 18.913%] |
| FIFO / BM25 | 13.014% [11.784%, 14.354%] | 4.422% [3.106%, 5.910%] | 13.600% [10.389%, 16.758%] | 5.288% [2.242%, 9.446%] | 19.330% [16.741%, 21.949%] | 20.217% [16.595%, 24.017%] |
| LRU / BM25 | 13.014% [11.784%, 14.354%] | 4.422% [3.106%, 5.910%] | 13.600% [10.389%, 16.758%] | 5.288% [2.242%, 9.446%] | 19.330% [16.741%, 21.949%] | 20.217% [16.595%, 24.017%] |
| Decay / BM25 | 12.908% [11.624%, 14.249%] | 4.762% [3.418%, 6.332%] | 13.600% [10.432%, 16.843%] | 4.808% [1.945%, 9.001%] | 18.547% [16.040%, 21.114%] | 20.652% [16.702%, 24.461%] |
| Salience / BM25 | 13.546% [12.305%, 14.880%] | 6.349% [4.878%, 7.977%] | 15.200% [11.936%, 18.700%] | 9.135% [5.741%, 13.434%] | 17.654% [15.050%, 20.447%] | 20.000% [16.486%, 23.759%] |
| Consolidate then forget / BM25 | 13.014% [11.784%, 14.354%] | 4.422% [3.106%, 5.910%] | 13.600% [10.389%, 16.758%] | 5.288% [2.242%, 9.446%] | 19.330% [16.741%, 21.949%] | 20.217% [16.595%, 24.017%] |
| BM25, FIFO retention | 13.014% [11.784%, 14.354%] | 4.422% [3.106%, 5.910%] | 13.600% [10.389%, 16.758%] | 5.288% [2.242%, 9.446%] | 19.330% [16.741%, 21.949%] | 20.217% [16.595%, 24.017%] |
| Offline mock CRUD | 11.667% [10.507%, 12.858%] | 4.195% [2.999%, 5.491%] | 14.133% [10.751%, 17.475%] | 4.808% [1.941%, 8.002%] | 17.989% [15.471%, 20.804%] | 14.783% [11.637%, 18.142%] |
| Query-conditioned evidence oracle | 95.035% [93.505%, 96.378%] | 87.528% [84.205%, 90.467%] | 100.000% [100.000%, 100.000%] | 85.577% [76.917%, 94.150%] | 100.000% [100.000%, 100.000%] | 100.000% [100.000%, 100.000%] |

Overall denominator: 2820 evidence IDs across 1982 valid-evidence questions. Category denominators are listed above. Intervals resample questions within each of the 10 conversations.
### keyness plus shared access/10%

| System | Overall | Cat 1 | Cat 2 | Cat 3 | Cat 4 | Cat 5 |
|---|---:|---:|---:|---:|---:|---:|
| FIFO / FTS-count | 5.603% [4.745%, 6.503%] | 2.721% [1.622%, 3.821%] | 6.133% [3.753%, 8.696%] | 3.365% [0.971%, 6.404%] | 7.263% [5.587%, 9.009%] | 8.478% [6.073%, 11.064%] |
| LRU / FTS-count | 4.752% [3.964%, 5.583%] | 2.608% [1.569%, 3.753%] | 5.867% [3.713%, 8.201%] | 2.404% [0.549%, 4.546%] | 6.369% [4.788%, 8.018%] | 5.870% [3.921%, 8.079%] |
| Decay / FTS-count | 4.645% [3.911%, 5.399%] | 2.381% [1.499%, 3.316%] | 5.067% [3.021%, 7.388%] | 3.365% [0.975%, 6.452%] | 6.257% [4.750%, 7.778%] | 6.087% [4.057%, 8.390%] |
| Salience / FTS-count | 6.560% [5.655%, 7.477%] | 4.535% [3.236%, 5.808%] | 8.000% [5.405%, 11.111%] | 3.846% [1.604%, 6.667%] | 8.380% [6.492%, 10.078%] | 6.957% [4.772%, 9.409%] |
| Consolidate then forget / FTS-count | 5.567% [4.699%, 6.466%] | 2.721% [1.622%, 3.821%] | 6.133% [3.753%, 8.696%] | 3.365% [0.971%, 6.404%] | 7.263% [5.587%, 9.009%] | 8.261% [5.844%, 10.824%] |
| FIFO / BM25 | 5.993% [5.071%, 6.907%] | 2.721% [1.660%, 3.850%] | 6.133% [3.753%, 8.564%] | 4.808% [1.801%, 8.840%] | 7.933% [6.143%, 9.765%] | 8.913% [6.373%, 11.564%] |
| LRU / BM25 | 5.603% [4.723%, 6.475%] | 2.608% [1.549%, 3.738%] | 6.133% [3.886%, 8.488%] | 3.365% [1.042%, 6.522%] | 7.821% [6.053%, 9.663%] | 7.609% [5.470%, 10.219%] |
| Decay / BM25 | 5.674% [4.831%, 6.555%] | 2.268% [1.378%, 3.275%] | 5.600% [3.522%, 8.022%] | 3.365% [0.975%, 6.452%] | 8.156% [6.430%, 9.944%] | 8.478% [6.022%, 11.016%] |
| Salience / BM25 | 7.092% [6.148%, 8.014%] | 4.649% [3.293%, 5.930%] | 7.733% [5.163%, 10.569%] | 5.769% [2.834%, 9.279%] | 8.827% [6.961%, 10.574%] | 8.478% [6.060%, 11.088%] |
| Consolidate then forget / BM25 | 5.993% [5.071%, 6.907%] | 2.721% [1.660%, 3.850%] | 6.133% [3.753%, 8.564%] | 4.808% [1.801%, 8.840%] | 7.933% [6.143%, 9.765%] | 8.913% [6.373%, 11.564%] |
| BM25, FIFO retention | 5.993% [5.071%, 6.907%] | 2.721% [1.660%, 3.850%] | 6.133% [3.753%, 8.564%] | 4.808% [1.801%, 8.840%] | 7.933% [6.143%, 9.765%] | 8.913% [6.373%, 11.564%] |
| Offline mock CRUD | 5.532% [4.670%, 6.342%] | 2.381% [1.311%, 3.423%] | 8.533% [5.974%, 11.261%] | 3.365% [0.930%, 6.418%] | 7.709% [6.024%, 9.466%] | 5.870% [3.930%, 8.026%] |
| Query-conditioned evidence oracle | 95.035% [93.512%, 96.376%] | 87.528% [84.374%, 90.510%] | 100.000% [100.000%, 100.000%] | 85.577% [76.617%, 93.750%] | 100.000% [100.000%, 100.000%] | 100.000% [100.000%, 100.000%] |

Overall denominator: 2820 evidence IDs across 1982 valid-evidence questions. Category denominators are listed above. Intervals resample questions within each of the 10 conversations.
### keyness plus shared access/25%

| System | Overall | Cat 1 | Cat 2 | Cat 3 | Cat 4 | Cat 5 |
|---|---:|---:|---:|---:|---:|---:|
| FIFO / FTS-count | 11.170% [9.938%, 12.483%] | 5.102% [3.605%, 6.635%] | 12.000% [9.042%, 15.343%] | 3.846% [1.613%, 6.471%] | 16.313% [14.015%, 18.785%] | 15.435% [12.121%, 18.751%] |
| LRU / FTS-count | 10.284% [9.176%, 11.484%] | 3.855% [2.677%, 5.234%] | 13.067% [9.999%, 16.350%] | 2.885% [0.930%, 5.203%] | 14.637% [12.471%, 16.987%] | 15.217% [12.035%, 18.083%] |
| Decay / FTS-count | 10.035% [8.921%, 11.124%] | 4.308% [3.178%, 5.581%] | 11.467% [8.357%, 14.516%] | 2.885% [0.897%, 5.509%] | 14.637% [12.415%, 17.095%] | 14.130% [11.015%, 17.068%] |
| Salience / FTS-count | 11.206% [9.996%, 12.509%] | 4.989% [3.605%, 6.381%] | 15.467% [11.842%, 19.061%] | 4.808% [2.301%, 8.001%] | 15.419% [13.155%, 17.878%] | 14.348% [11.087%, 17.648%] |
| Consolidate then forget / FTS-count | 11.170% [9.938%, 12.483%] | 5.102% [3.605%, 6.635%] | 12.000% [9.042%, 15.343%] | 3.846% [1.613%, 6.471%] | 16.313% [14.015%, 18.785%] | 15.435% [12.121%, 18.751%] |
| FIFO / BM25 | 13.014% [11.748%, 14.384%] | 4.422% [3.196%, 5.903%] | 13.600% [10.410%, 16.986%] | 5.288% [2.234%, 9.044%] | 19.330% [16.947%, 21.896%] | 20.217% [16.450%, 24.177%] |
| LRU / BM25 | 12.837% [11.545%, 14.141%] | 4.195% [2.940%, 5.583%] | 14.133% [10.760%, 17.730%] | 3.846% [1.070%, 7.212%] | 19.106% [16.722%, 21.854%] | 20.217% [16.703%, 23.778%] |
| Decay / BM25 | 12.872% [11.605%, 14.116%] | 4.422% [3.158%, 5.902%] | 13.067% [9.972%, 16.407%] | 4.327% [1.477%, 7.692%] | 19.777% [17.265%, 22.285%] | 19.348% [15.778%, 22.944%] |
| Salience / BM25 | 14.539% [13.175%, 15.984%] | 6.463% [4.853%, 8.124%] | 17.333% [13.456%, 21.206%] | 8.173% [4.973%, 12.438%] | 19.777% [17.182%, 22.613%] | 20.435% [16.702%, 24.290%] |
| Consolidate then forget / BM25 | 13.014% [11.748%, 14.384%] | 4.422% [3.196%, 5.903%] | 13.600% [10.410%, 16.986%] | 5.288% [2.234%, 9.044%] | 19.330% [16.947%, 21.896%] | 20.217% [16.450%, 24.177%] |
| BM25, FIFO retention | 13.014% [11.748%, 14.384%] | 4.422% [3.196%, 5.903%] | 13.600% [10.410%, 16.986%] | 5.288% [2.234%, 9.044%] | 19.330% [16.947%, 21.896%] | 20.217% [16.450%, 24.177%] |
| Offline mock CRUD | 11.667% [10.421%, 12.872%] | 4.195% [2.907%, 5.569%] | 14.133% [10.782%, 17.554%] | 4.808% [2.298%, 8.173%] | 17.989% [15.488%, 20.637%] | 14.783% [11.842%, 18.261%] |
| Query-conditioned evidence oracle | 95.035% [93.413%, 96.359%] | 87.528% [84.294%, 90.640%] | 100.000% [100.000%, 100.000%] | 85.577% [76.543%, 94.353%] | 100.000% [100.000%, 100.000%] | 100.000% [100.000%, 100.000%] |

Overall denominator: 2820 evidence IDs across 1982 valid-evidence questions. Category denominators are listed above. Intervals resample questions within each of the 10 conversations.

## Overall paired policy contrasts

### keyness only/10%

| Paired policy contrast (BM25) | Δ micro recall | 95% paired CI | Raw p | Holm p |
|---|---:|---:|---:|---:|
| decay_bm25 − consolidate_then_forget_bm25 | 0.142% | [-0.360%, 0.651%] | 0.6494 | 1.0000 |
| decay_bm25 − salience_bm25 | -1.028% | [-2.269%, 0.254%] | 0.1279 | 0.8951 |
| fifo_bm25 − consolidate_then_forget_bm25 | 0.000% | [0.000%, 0.000%] | 1.0000 | 1.0000 |
| fifo_bm25 − decay_bm25 | -0.142% | [-0.651%, 0.360%] | 0.6494 | 1.0000 |
| fifo_bm25 − lru_bm25 | 0.000% | [0.000%, 0.000%] | 1.0000 | 1.0000 |
| fifo_bm25 − salience_bm25 | -1.170% | [-2.472%, 0.180%] | 0.0819 | 0.8192 |
| lru_bm25 − consolidate_then_forget_bm25 | 0.000% | [0.000%, 0.000%] | 1.0000 | 1.0000 |
| lru_bm25 − decay_bm25 | -0.142% | [-0.651%, 0.360%] | 0.6494 | 1.0000 |
| lru_bm25 − salience_bm25 | -1.170% | [-2.472%, 0.180%] | 0.0819 | 0.8192 |
| salience_bm25 − consolidate_then_forget_bm25 | 1.170% | [-0.180%, 2.472%] | 0.0819 | 0.8192 |
### keyness only/25%

| Paired policy contrast (BM25) | Δ micro recall | 95% paired CI | Raw p | Holm p |
|---|---:|---:|---:|---:|
| decay_bm25 − consolidate_then_forget_bm25 | -0.106% | [-0.743%, 0.492%] | 0.7672 | 1.0000 |
| decay_bm25 − salience_bm25 | -0.638% | [-2.296%, 0.930%] | 0.4116 | 1.0000 |
| fifo_bm25 − consolidate_then_forget_bm25 | 0.000% | [0.000%, 0.000%] | 1.0000 | 1.0000 |
| fifo_bm25 − decay_bm25 | 0.106% | [-0.492%, 0.743%] | 0.7672 | 1.0000 |
| fifo_bm25 − lru_bm25 | 0.000% | [0.000%, 0.000%] | 1.0000 | 1.0000 |
| fifo_bm25 − salience_bm25 | -0.532% | [-2.275%, 1.197%] | 0.5295 | 1.0000 |
| lru_bm25 − consolidate_then_forget_bm25 | 0.000% | [0.000%, 0.000%] | 1.0000 | 1.0000 |
| lru_bm25 − decay_bm25 | 0.106% | [-0.492%, 0.743%] | 0.7672 | 1.0000 |
| lru_bm25 − salience_bm25 | -0.532% | [-2.275%, 1.197%] | 0.5295 | 1.0000 |
| salience_bm25 − consolidate_then_forget_bm25 | 0.532% | [-1.197%, 2.275%] | 0.5295 | 1.0000 |
### keyness plus shared access/10%

| Paired policy contrast (BM25) | Δ micro recall | 95% paired CI | Raw p | Holm p |
|---|---:|---:|---:|---:|
| decay_bm25 − consolidate_then_forget_bm25 | -0.319% | [-1.262%, 0.704%] | 0.5674 | 1.0000 |
| decay_bm25 − salience_bm25 | -1.418% | [-2.506%, -0.352%] | 0.0200 | 0.1998 |
| fifo_bm25 − consolidate_then_forget_bm25 | 0.000% | [0.000%, 0.000%] | 1.0000 | 1.0000 |
| fifo_bm25 − decay_bm25 | 0.319% | [-0.704%, 1.262%] | 0.5674 | 1.0000 |
| fifo_bm25 − lru_bm25 | 0.390% | [-0.434%, 1.230%] | 0.3337 | 1.0000 |
| fifo_bm25 − salience_bm25 | -1.099% | [-2.390%, 0.146%] | 0.0999 | 0.7992 |
| lru_bm25 − consolidate_then_forget_bm25 | -0.390% | [-1.230%, 0.434%] | 0.3337 | 1.0000 |
| lru_bm25 − decay_bm25 | -0.071% | [-0.911%, 0.749%] | 0.8691 | 1.0000 |
| lru_bm25 − salience_bm25 | -1.489% | [-2.786%, -0.289%] | 0.0200 | 0.1998 |
| salience_bm25 − consolidate_then_forget_bm25 | 1.099% | [-0.146%, 2.390%] | 0.0999 | 0.7992 |
### keyness plus shared access/25%

| Paired policy contrast (BM25) | Δ micro recall | 95% paired CI | Raw p | Holm p |
|---|---:|---:|---:|---:|
| decay_bm25 − consolidate_then_forget_bm25 | -0.142% | [-1.307%, 0.936%] | 0.7912 | 1.0000 |
| decay_bm25 − salience_bm25 | -1.667% | [-3.100%, -0.217%] | 0.0280 | 0.2797 |
| fifo_bm25 − consolidate_then_forget_bm25 | 0.000% | [0.000%, 0.000%] | 1.0000 | 1.0000 |
| fifo_bm25 − decay_bm25 | 0.142% | [-0.936%, 1.307%] | 0.7912 | 1.0000 |
| fifo_bm25 − lru_bm25 | 0.177% | [-0.649%, 1.102%] | 0.7612 | 1.0000 |
| fifo_bm25 − salience_bm25 | -1.525% | [-3.290%, 0.245%] | 0.0779 | 0.6234 |
| lru_bm25 − consolidate_then_forget_bm25 | -0.177% | [-1.102%, 0.649%] | 0.7612 | 1.0000 |
| lru_bm25 − decay_bm25 | -0.035% | [-0.745%, 0.699%] | 0.9590 | 1.0000 |
| lru_bm25 − salience_bm25 | -1.702% | [-3.249%, -0.142%] | 0.0420 | 0.3776 |
| salience_bm25 − consolidate_then_forget_bm25 | 1.525% | [-0.245%, 3.290%] | 0.0779 | 0.6234 |

Δ and confidence intervals are absolute percentage-point differences in micro recall. Across the four overall scopes, none of the 40 adjusted contrasts was below .05. Across category scopes, the single adjusted contrast reported above occurred at shared-access/10%, category 1. The JSON contains the full category pairwise families and all method intervals.

## Capture gate and H6

The capture gate is a separate synthetic 200-record measurement with requested prevalence 10%, capture-intensity correlation 0.6, access correlation 0, seed 20261018, and four policies: always-on, existing intensity, deterministic keyness/text, and label oracle. Only the oracle and the final metric code receive labels. The feature judge sees text and prior text only. Simulated keep confirmation is applied to staged candidates. H6 uses packed staged-candidate bytes divided by the always-on packed-stage bytes; it is not an all-in cap and does not represent continuous acquisition peak.

| Capture strategy | Candidates / 200 | Important recall | Packed stage logical / allocated B | Acquisition+pack peak allocated B | Post-confirm peak allocated B | Final all-in logical / allocated B | H6 (logical / allocated) |
|---|---:|---:|---:|---:|---:|---:|---|
| always_on | 200 (100.0%) | 20/20 (100.0%) | 24,278 / 24,576 | 303,104 | 1,175,552 | 486,628 / 1,159,168 | fail / fail |
| existing_intensity | 101 (50.5%) | 16/20 (80.0%) | 12,737 / 16,384 | 155,648 | 638,976 | 253,619 / 622,592 | fail / fail |
| deterministic_keyness_text | 199 (99.5%) | 20/20 (100.0%) | 24,252 / 24,576 | 303,104 | 1,171,456 | 490,902 / 1,155,072 | fail / fail |
| oracle_upper_bound | 38 (19.0%) | 20/20 (100.0%) | 5,386 / 8,192 | 102,400 | 323,584 | 128,912 / 307,200 | fail / fail |

The gate’s label prevalence was 20/200. Text/keyness staged 199/200 candidates and found all 20 important examples. The realized oracle staged 38 candidates (20 important, 18 overcapture) and found all 20. The always-on candidate count was 200. All four strategies were recall-eligible, but none met the 20% packed-stage threshold. Measurements sample file usage at operation boundaries, including atomic temp-file fsync before replacement; allocated bytes use filesystem blocks. The reported acquisition/pack and post-confirmation peaks are not continuous tracing.

## Gate storage ratios

The fixed empty-vault event-log allocated baseline is 4,096 B. The table gives stage and final all-in fractions relative to always-on, plus event-log growth; H6 is the stage fraction only.

| Strategy | Stage logical / allocated ratio | Final all-in logical / allocated ratio | Event baseline allocated | Event growth logical / allocated |
|---|---:|---:|---:|---:|
| always_on | 1.000 / 1.000 | 1.000 / 1.000 | 4,096 B | 321,885 / 331,776 B |
| existing_intensity | 0.525 / 0.667 | 0.521 / 0.537 | 4,096 B | 169,303 / 200,704 B |
| deterministic_keyness_text | 0.999 / 1.000 | 1.009 / 0.996 | 4,096 B | 323,693 / 331,776 B |
| oracle_upper_bound | 0.222 / 0.333 | 0.265 / 0.265 | 4,096 B | 96,706 / 143,360 B |

## Append and verification benchmark

Both benchmark runs appended and verified 100,000 identical metadata access events, with identical 42,600,000-byte event-stream SHA-256 `a261363e891fa7d4f4e9b6b651f964c8555683db6b8d2310985e9f6e80209125` and terminal hash `414f78e0a1bbc036fc6181df6fae5f28d166a723b1bf6c4208ec02d7ef7a2281`.

| Source | Append seconds | Events/s | Full verification seconds | State/cursor bytes |
|---|---:|---:|---:|---:|
| Day6 frozen capsule | 56.406778 | 1,772.84 | 0.802876 | cursor 221 |
| Day7 current | 46.008704 | 2,173.50 | 0.815064 | state 221 |

This single matched pair was 18.43% faster on append (22.60% higher throughput), with nearly equal verification time. It measures public metadata `capture_event(MEMORY_ACCESS)` appends with lock, WAL, fsync, cursor/state maintenance, and full verification. It does not include Markdown body mutations or read/get costs; it does not measure concurrent writers or establish a repeated-run performance guarantee.

Reproduction from the repository root:

```bash
# Day6 source capsule; extract only to an external temporary directory.
unzip -q reports/day7_day6_source.zip -d /private/tmp/memfw-day7-day6-capsule-v2
PYTHONPATH=/private/tmp/memfw-day7-day6-capsule-v2/src \
  .venv/bin/python /private/tmp/memfw-day7-day6-capsule-v2/scripts/benchmark_day6_append.py \
  --count 100000 --root /private/tmp/memfw-day7-day6-baseline-repro --label day6-capsule-100k

# Current source; use a different empty root.
.venv/bin/python scripts/benchmark_day6_append.py --count 100000 \
  --root /private/tmp/memfw-day7-current-repro --label day7-current-100k
```

## Commands and source receipts

Official command:

```bash
.venv/bin/python scripts/eval_locomo.py --day7 \
  --data <MEMFW_DATA_DIR>/locomo10.json \
  --output reports/day7_official.json --quick
```

The run log records completion for each sample at `reports/day7_official_quick_v2.log`. Pipeline timings in the JSON are 157.852 s official evaluation, 8.230 s bootstrap, 83.337 s synthetic capture gate, total 249.418 s. The report output was serialized after this timer; the command completed successfully under the 600 s limit. The earlier v1 failed after six samples on a 1-byte trailing-newline budget discrepancy and wrote no result; it is preserved at `reports/day7_official_quick.log`. The v2 fix used canonical stored bodies consistently and added a trailing-newline regression test.

Frozen source capsule: `reports/day7_day6_source.zip`, SHA-256 `e6e30107cabd73c9930a8a9babc48bfc71cd9365ea97349e8d2d4ab836aac8d4`; manifest SHA-256 `ceb449fe4172552aec8c1205c707fc0fb05c7d6d4ddad0e162022a43a593f502`. Day7 v2 measured source capsule: `reports/day7_measured_source_v2.zip`, SHA-256 `1732ee398239cb4f652a486e181a94fcc8438abb84f78732f977a400de3a44fd`; source tree hash `e1ff0cb4f6a8316cf0c8f97ea0ff365a6512d4b61f135d8bacf087d79f05401b`.

Main JSON: `reports/day7_official.json`, SHA-256 `08aa8d47356c5941edb6156b1caeedcfdda7facba42077f6d7f397273a66743e`. Benchmark receipts: `reports/day7_day6_baseline_100k_v2.json` SHA-256 `8c08c3305f399937daff4d127c68f867a29fb3ab822dd7f5928fb7db35bdf44d`; `reports/day7_current_100k_v2.json` SHA-256 `12e1c4beef468f0dc16493b065953eea5f292420f80407ddc7f72848fbbf1157`.

Environment: Python 3.11.16, SQLite 3.53.1, zstandard 0.25.0. Allocated-byte results depend on filesystem block size. Final current-tree validation: `.venv/bin/pytest -q` passed 336 tests in 180.85 s; `.venv/bin/ruff check .`, `.venv/bin/ruff format --check .` (103 files), and `.venv/bin/mypy --strict src tests scripts` (97 source files) passed. Pytest log: `reports/day7_final_pytest.log`, SHA-256 `b7056f4ef0160618bb6ac72622132c544a046ca4f439d86abd463dbef333921b`.

## Reproducible validation commands

Run from the repository root. The official corpus remains at its local path and is not copied into the repository.

```bash
.venv/bin/python scripts/eval_locomo.py --day7 --data <MEMFW_DATA_DIR>/locomo10.json --output reports/day7_official.json --quick
.venv/bin/pytest -q tests/test_day7_core.py tests/test_day7_eval.py tests/test_day7_gating.py tests/test_day7_retention.py tests/test_day7_bootstrap.py
.venv/bin/pytest -q
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy --strict src tests scripts
```

Cold-source retrieval and source protection: `tests/test_day7_core.py::test_cold_stub_index_searches_verified_archived_body`, `::test_cold_stub_index_fails_closed_on_missing_archive`, and `::test_source_deletion_proposal_survives_cooling_but_rejects_content_edit`. Estimator naming: `tests/test_day7_core.py::test_tail_fit_label_names_actual_estimator`. Label isolation: `tests/test_day7_eval.py` and `tests/test_day7_gating.py`.

## Limitations

- The 95% intervals are conditional on the 10 fixed conversations. They do not establish new-user generalization. The per-question bootstrap uses 1,000 paired, conversation-stratified resamples.
- The evaluation measures evidence-ID recall, not answer accuracy. The first-sentence CRUD arm can receive evidence credit even when its retained fact omits the support span.
- The 128-token context keeps whole turns and skips turns that do not fit; it does not chunk long turns. This constrains both production arms and the oracle.
- The query-conditioned oracle receives gold evidence by design and cannot be deployed. Its result is not directly comparable as a policy ranking.
- Synthetic access behavior is one fixed-seed stream; BM25 and mock CRUD do not consume access events.
- The capture test is a synthetic N=200 session, and filesystem peaks are sampled at write/pack/confirmation operation boundaries. It does not establish continuous peak use or production-vault capacity.
- The optional full-mode N=1,000 capture gate was not run; reported capture results are N=200 with seed 20261018 and should not be generalized to full scale.
- The append benchmark is one sequential matched pair. It does not test concurrent writers or ordinary Markdown mutation cost.
- The official corpus was read from a local file; no remote fetch, LLM answer generation, or image/caption processing was used.
