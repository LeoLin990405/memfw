# Retrieval architecture and staged plan

## 0. Current implementation status

The repository now contains more than the Stage A interface layer:

- notes hybrid retrieval: FTS + optional sqlite-vec + RRF, filters, opt-in abstention;
- memory_search: FTS by default, optional NotesRetriever hybrid, mode/degraded reporting;
- temporal memory fields (`valid_from`, `valid_to`, `superseded_by`) and human-only supersede;
- opt-in entity third route for memory retrieval;
- feedback, evaluation, shadow/manifest, AI cross-label diagnostics, and code retrieval;
- rerank and query rewrite remain default-off because measured results were negative;
- training/distillation, production cutover, and permission ACLs are not implemented.

The stage sections below are historical design notes; use this section, the code, and
README as the current behavior contract.

Status: Stage A implemented; Stages B-E are planned, not implemented.
This document is based on the repository at commit `fc112bc` plus the local
Stage A changes.

## 1. Scope and non-goals

The objective is a configurable retrieval system:

```text
query
  -> optional query rewrite
  -> FTS5 recall + vector recall
  -> RRF fusion / dedupe
  -> optional rerank
  -> results with provenance and mode
```

Markdown remains authoritative. The index is rebuildable. Writes and memory
changes keep using the existing confirmation/audit path.

Non-goals in this stage:

- no training;
- no model downloads;
- no paid API calls;
- no production switch;
- no graph or multimodal embeddings;
- no large language model loaded by default.

## 2. Current-code audit

Observed in the repository:

- `memfw.index.MemoryIndex`: SQLite FTS5 projection over memory documents;
  trigram tokenizer, jieba/unicode61 fallback, atomic rebuild.
- `memfw.retrieval.VaultRetriever`: lexical retrieval plus cold restore and
  access metadata.
- `memfw.notes.index.NotesIndex`: section-level Markdown index with FTS5,
  optional sqlite-vec, `HttpEmbeddingProvider`, hash-incremental indexing,
  checkpoint commits.
- `memfw.notes.mcp`: read-only notes MCP facade.
- `memfw.agents`: deterministic proposal/judge schemas and injected transports,
  not a general text-generation provider.
- `eval/`: historical evaluation scripts; the new notes benchmark has its own
  golden-query harness outside the public repo.

Gaps relevant to the objective:

1. No reranker interface or implementation.
2. No general LanguageModelProvider for rewrite/extraction/summaries.
3. No sparse-retriever interface.
4. Candidate count/RRF parameters were hard-coded in `NotesIndex.search`.
5. No retrieval telemetry or client-feedback interface.
6. No training/export/evaluation pipeline for distillation.
7. No model/index versioning beyond a basic embedding dimension marker.
8. An actual similarity/embeddings model was not part of the public repo.

## 3. Interfaces (Stage A)

### EmbeddingProvider

```python
class EmbeddingProvider(Protocol):
    dimension: int
    def embed(texts, *, role: str = "document") -> list[list[float]]: ...
```

`role` is `query` or `document`. `HttpEmbeddingProvider` supports separate
query/document instruction prefixes, batching, timeout, retries, and a
fingerprint over model/dimension/instructions. Older providers that only
implement `embed(texts)` remain callable through an inspect-based adapter.

### RerankerProvider

```python
class RerankerProvider(Protocol):
    name: str
    def rerank(query, candidates, *, top_k) -> list[NoteHit]: ...
```

The Stage A pipeline accepts an injected reranker. No concrete model is
bundled. Failures/empty results degrade to the fused candidate list.

### LanguageModelProvider

```python
class LanguageModelProvider(Protocol):
    name: str
    def rewrite_query(query, *, limit=3) -> QueryRewrite: ...
```

`QueryRewrite` preserves the original query and records the model/evidence.
The retriever searches the original query first and adds bounded rewrites.
No model is loaded in Stage A; `NullLanguageModel` is the default.

### SparseRetriever

Reserved protocol only. It is deliberately not wired until an experiment
shows it beats the existing FTS5 baseline on the same evaluation set.

## 4. Retrieval policy

`RetrievalConfig`:

```python
mode: "fts" | "vector" | "hybrid"
candidates: int = 50
final_k: int = 10
rrf_k: int = 60
rerank_candidates: int = 0
rewrite: bool = False
max_rewrites: int = 3
auto_degrade: bool = True
```

`NotesRetriever` applies rewrites, recalls candidates, fuses multiple query
lists with RRF, applies path/tag/course filters before exposing candidates to
a reranker, then optionally reranks. It reports:

- actual `mode_used`;
- `degraded`;
- rewrite list;
- errors;
- candidate count.

The original query is never replaced by a rewrite. A model or reranker
failure is reported and does not suppress lexical/vector hits.

## 5. Evaluation

`memfw.notes.evaluation` provides:

- `EvalCase`;
- `evaluate_retriever`;
- `evaluate_ablation` (FTS / vector / hybrid);
- Recall@k, MRR, nDCG@k, p50/p95 latency, per-kind breakdown.

Stage A measured actual ablation on a 1,000-query deterministic sample of the
existing 5,000-query set over the 4,979-note index:

| Arm | Recall@10 | MRR | nDCG@10 | p50 latency |
|---|---:|---:|---:|---:|
| FTS only | 0.676 | 0.606 | 0.623 | 5.34 ms |
| Vector only | 0.733 | 0.670 | 0.686 | 106.7 ms |
| Hybrid (RRF) | 0.756 | 0.691 | 0.707 | 113.5 ms |

The earlier 5,000-query run produced hybrid Recall@10 0.7866 and p50 129 ms; after the FTS query-builder fix the 1,000-sample hybrid results are as shown above.
The queries were generated from titles/headings/content snippets; this is a
retrieval-consistency test, not human relevance judgement. There is no
no-answer set yet, and no reranker/rewrite arm because no model was available
or approved.

## 6. Stage B-E plan

### Stage B: hybrid hardening and reranking

Deliverables:

- reranker adapter with explicit candidate cap, timeout, batch/truncation, and
  fallback policy;
- candidate/rerank metrics: latency, memory, score stability;
- query rewrite integration behind a feature flag;
- filter-before-model tests for time/source/tag/path;
- sparse-retriever experiment only after baseline is frozen;
- ablation: FTS, vector, hybrid, hybrid+rerank, +rewrite.

Exit gate:

- reranker improves nDCG/MRR on held-out queries without unacceptable latency;
- fallback produces the same candidates when reranker fails;
- no model/index compatibility regressions.

### Stage C: optional language model and feedback

Deliverables:

- `LanguageModelProvider` adapters (local first; caller-provided model allowed);
- structured outputs for query rewrite, memory-candidate extraction,
  duplicate/update/conflict suggestions, summaries, optional entity relations;
- schema validation, evidence pointers, human-confirmation boundary;
- retrieval telemetry and feedback API:
  - query, rewrite chain, candidate IDs, content versions, sources, ranks,
    scores, model versions, returned content;
  - cited/corrected/task-outcome events, task correlation ID;
  - unknown labels when no feedback arrives;
  - minimal body retention by default, redaction, tenant/source policy.
- retrieval telemetry separate from the memory change log.

All model output is untrusted data. Instructions embedded in retrieved notes
must not control labels, training, or tool execution.

### Stage D: offline distillation

Deliverables:

- training-candidate export with source, confidence, dedupe and provenance;
- weak-positive/citation, unknown, correction, hard-negative and teacher-label
  schemas kept distinct;
- leakage isolation by document/task/time;
- reranker-first experiment, embedding-second experiment;
- base-model retention and replay mix;
- offline training entry points and budget/resource limits.

No training starts without explicit approval. The first experiment must compare
reranker fine-tuning against embedding fine-tuning on identical data and
metrics.

### Stage E: shadow evaluation, versioning and rollback

Deliverables:

- shadow evaluation before production;
- index/model version association: model, chunk policy, document hash, index
  generation;
- re-embed in a separate index; validate; switch only after approval;
- rollback of both model and index;
- no deletion of old model/index without approval.

## 7. Metrics and release gates

Primary: Recall@k, nDCG@k, MRR, answerable/no-answer behavior, p50/p95 latency,
memory. Secondary: generic capability regression, score stability, failure
rate, fallback rate.

A release requires more than a Recall increase:

- no statistically meaningful regression on held-out and time-separated data;
- no unacceptable latency/memory increase;
- models, licenses, and runtime/device compatibility verified;
- rollback rehearsal completed;
- human approval for paid services, model downloads, training, and production
  deployment.

## 8. Model candidates and approvals

Candidate metadata checked against model cards (2026-11-05):

| Candidate | License (verified from model card) | Runtime artifact status | M5 measurement |
|---|---|---|---|
| BAAI/bge-m3 | MIT | GGUF mirror `gpustack/bge-m3-GGUF` is MIT; llama.cpp server supports embeddings | not measured here |
| Qwen/Qwen3-Embedding-0.6B | Apache-2.0 | official GGUF repo is Apache-2.0; llama.cpp server supports embeddings | used as an existing local endpoint in Stage A; no new download |
| Qwen/Qwen3-Reranker-0.6B | Apache-2.0 | official model card is Apache-2.0; no official GGUF repo found; community GGUF (e.g. `mradermacher/Qwen3-Reranker-0.6B-GGUF`) is Apache-2.0; llama.cpp server has a reranking endpoint (`--rerank`) | not measured here |

Sources:
- https://huggingface.co/BAAI/bge-m3
- https://huggingface.co/Qwen/Qwen3-Embedding-0.6B
- https://huggingface.co/Qwen/Qwen3-Reranker-0.6B
- https://huggingface.co/gpustack/bge-m3-GGUF
- https://huggingface.co/Qwen/Qwen3-Embedding-0.6B-GGUF
- https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md

This table verifies model-card licenses and documented runtime interfaces; it
does **not** claim M5 throughput, memory, or reranker quality. Community GGUF
artifacts still need their own checksum/license/runtime verification before
download or use. No model was downloaded for Stage A.

Approval-required actions:

1. any model download or paid API;
2. any training run or long-running benchmark that competes for M5 resources;
3. production service/index switch;
4. deletion or retention changes;
5. push/merge of these changes.

## 9. Stage A files

- `src/memfw/notes/index.py` — search modes/candidates/RRF, section IDs,
  embedding roles/instructions/fingerprint.
- `src/memfw/notes/retrieval.py` — config, provider protocols, retriever.
- `src/memfw/notes/evaluation.py` — metrics and ablation.
- `src/memfw/notes/mcp.py` — configurable search envelope.
- `src/memfw/cli.py` — `--mode`, `--candidates`, `--rrf-k`.
- `tests/test_notes_retrieval.py`, `tests/test_notes_embedding.py` — new tests.
- `docs/retrieval-architecture.md` — this plan.


## Stage B experiment (actual)

A local BAAI/bge-reranker-v2-m3 Q8_0 GGUF (Apache-2.0, llama.cpp `/v1/rerank`)
was tested on a 500-query deterministic sample with a 50-candidate pool:

| Arm | Recall@10 | MRR | nDCG@10 | p50 |
|---|---:|---:|---:|---:|
| Hybrid | 0.762 | 0.693 | 0.710 | 164.4 ms |
| Hybrid + BGE rerank (20 candidates) | 0.728 | 0.575 | 0.612 | 559.5 ms |

Reranking degraded all ranking metrics and increased p50 latency on this
synthetic query set, so it is **not enabled by default**. The optional provider
and fallback remain available for a later, better-labelled evaluation. The
Qwen3-Reranker GGUF was downloaded for a runtime check but was not used after a
local llama.cpp ordering check did not establish correct behavior; this is a
runtime observation, not a general claim about the model.


## Stage B result (actual)

`HttpRerankerProvider` is implemented with candidate caps, document
truncation, timeout/retry and fallback. A local BAAI/bge-reranker-v2-m3 Q8_0
GGUF (Apache-2.0; SHA-256 recorded in the evidence directory) was benchmarked
on 500 deterministic queries:

| Arm | Recall@10 | MRR | nDCG@10 | p50 |
|---|---:|---:|---:|---:|
| Hybrid | 0.762 | 0.693 | 0.710 | 164.4 ms |
| Hybrid + rerank (20 candidates) | 0.728 | 0.575 | 0.612 | 559.5 ms |

This reranker is **not enabled by default**. The optional interface remains for
a better-labelled evaluation or a different model. Time/metadata filters are
applied before the reranker sees candidates; tests cover them.


## Stage C implementation (interfaces only)

`memfw.notes.language_model` now contains:

- `NullLanguageModel` as the default no-model implementation;
- `HttpLanguageModelProvider` for an optional OpenAI-compatible chat endpoint;
- structured `MemoryCandidate`, `RelationshipSuggestion`, `MemorySummary`,
  `EntityMention`, and `ProposalBundle` records;
- query rewrite preserves the original query; memory extraction and
  relationship suggestions return candidates only.

`memfw.notes.feedback` contains a separate SQLite `FeedbackStore`:

- trace fields: task ID, query, mode, rewrites, candidate IDs/paths/hashes/
  ranks/scores/sources, model versions, tenant/source;
- feedback types: citation, correction, task outcome, permission;
- relevance, correctness and permission are separate labels;
- document bodies are omitted by default and only stored if explicitly enabled;
- unknown trace IDs are rejected, and no label is fabricated when feedback is
  absent.

No real language model was downloaded or run, and no real user feedback was
collected. Training/distillation and shadow switching remain Stage D/E.


## Stage D/E implementation (interfaces only)

`memfw.notes.distillation` exports provenance-bearing training pairs from
explicit feedback:

- citations are weak positives only when marked relevant/weak-positive;
- uncited results stay unknown and are never auto-negatives;
- explicit incorrect/correction events may provide negatives;
- pairs carry task/trace/source/confidence and a deterministic task-level
  train/validation/test split;
- `check_pairs` rejects missing fields, positive-in-negative leakage and
  task-level split leakage;
- no training is started; export is a candidate file only.

`memfw.notes.versioning` records and verifies:

- index ID and manifest version;
- chunker version;
- embedding fingerprint and model marker;
- dimension, note/section/vector counts and creation time;
- compatibility failures when fingerprint, dimension or chunker changes.

This supports a future separate re-encoding index and rollback, but no index
switch, production deployment or deletion is implemented/performed here.


## Local language-model experiment (actual)

Qwen2.5-1.5B-Instruct Q4_K_M (Apache-2.0; checksum recorded in evidence) was
run locally through llama.cpp. On 200 deterministic queries:

| Arm | Recall@10 | MRR | nDCG@10 | p50 |
|---|---:|---:|---:|---:|
| Hybrid | 0.755 | 0.702 | 0.715 | 118.7 ms |
| Hybrid + local rewrite | 0.700 | 0.542 | 0.580 | 535.2 ms |

Rewrite is therefore disabled by default; the optional provider remains for
better-labelled query distributions. Structured extraction was smoke-tested
but no candidate was applied to the vault. No user feedback or training data
exists yet.


## Phase 1 trace integration (implemented)

`NotesRetriever.search(..., task_id=..., tenant=..., source=...)` now records a
minimal retrieval trace into the optional `FeedbackStore`:

- task/trace IDs;
- query, actual mode, rewrite chain;
- candidate path, rank, score, section ID, source;
- embedding/reranker/LM version markers;
- tenant/source;
- no document body by default.

MCP additions:

- `notes_search` accepts `task_id`, `tenant`, `source`, and returns
  `trace_id`/`task_id`;
- `retrieval_feedback` records citation/correction/task_outcome/permission
  with separate relevance/correctness/permission fields;
- `retrieval_trace` reads the stored trace;
- `NotesGateway`/CLI accept an optional feedback database.

Tests cover trace recording, content minimization, MCP feedback round-trip,
unknown trace rejection and task correlation.


## CLI and shadow operations

Added CLI entry points:

- `memfw notes eval` for JSON/JSONL/YAML golden files;
- `memfw notes feedback-export` for explicit feedback pairs with quality checks;
- `memfw notes manifest-write` / `manifest-verify` for generation identity;
- `memfw notes shadow` for read-only baseline/candidate comparison.

The shadow command evaluates both indexes on identical cases and reports
metric deltas and per-query rank changes. It does not switch or delete either
index.


Trace candidates include the section content hash and the full candidate set
(before the final top-k truncation), so feedback can be tied to an exact
content version without storing the body by default.


## Abstention experiment (actual, opt-in)

Added `RetrievalConfig.min_query_coverage` and `RetrievalResult.abstained`.
The signal is lexical query-term coverage in the top hit; it is opt-in and not
enabled by default.

Calibrated on the dev split (28 queries: 19 answerable, 9 no-answer):

| Setting | Answerable recall@5 | No-answer with results |
|---|---:|---:|
| Hybrid, no abstention | 1.000 | 9/9 |
| Hybrid + coverage < 0.25 | 0.947 | 0/9 |

Frozen test split (12 queries: 6 answerable, 6 no-answer):

| Setting | Answerable recall@5 | No-answer with results |
|---|---:|---:|
| Hybrid, no abstention | 1.000 | 6/6 |
| Hybrid + coverage < 0.25 | 1.000 | 1/6 |

The threshold is a small-sample experiment, not a production default. The dev
set has one false abstention (a human-labelled answerable query with low
lexical overlap); more no-answer data is required before enabling by default.


## Cross-LLM judging (diagnostic only)

Two local judges were compared against the human batch on 216 candidate
decisions:

| Judge | F1 | Answerable accuracy | Kappa vs human |
|---|---:|---:|---:|
| Qwen2.5-1.5B | 0.061 | 0.107 | 0.052 |
| Phi-3.5-mini | 0.561 | 0.893 | 0.464 |

Cross-judge kappa: 0.030. Neither model is adopted as an annotator; LLM labels
are diagnostics only. Human labels remain the ground truth.


## MiniMax external judge (diagnostic)

Using the user's existing MiniMax subscription (MiniMax-M3.1-Flash), 216
candidate decisions were judged:

| Metric | Value |
|---|---:|
| Accuracy | 0.940 |
| Precision | 0.852 |
| Recall | 0.719 |
| F1 | 0.780 |
| Kappa vs human | 0.745 |
| Answerable accuracy | 0.964 |

Still not adopted as automatic labels. StepFun could not be used because the
local config has only a web Oasis token, not an OpenAI-compatible API key.


## External judge consensus (diagnostic)

MiniMax-M3.1-Flash, Doubao-Seed-2.1-lite and DeepSeek-latest judged the same
216 candidate decisions and produced identical predictions:

| Metric | Value |
|---|---:|
| Accuracy | 0.940 |
| Precision | 0.852 |
| Recall | 0.719 |
| F1 | 0.780 |
| Kappa vs human | 0.745 |
| Answerable accuracy | 0.964 |

Pairwise kappa between external judges: 1.000. This is evidence of stability,
not correctness; human labels remain ground truth. Local Phi-3.5 scored F1
0.561; local Qwen1.5B scored 0.061.


## StepFun judge (diagnostic)

StepFun Step Plan (`step-5-preview`) judged the same 216 candidate decisions:

| Metric | Value |
|---|---:|
| Accuracy | 0.935 |
| Precision | 0.821 |
| Recall | 0.719 |
| F1 | 0.767 |
| Kappa vs human | 0.729 |
| Answerable accuracy | 0.929 |

Pairwise kappa with MiniMax/Doubao/DeepSeek was 0.979. The external judges are
stable with each other but still differ from human labels; no automatic
annotation was adopted.


## Memory vault integration (implemented)

- `memory_search` uses `MemoryHybridRetriever`, which reuses the notes
  `NotesRetriever`/RRF/index manifest instead of a parallel retriever.
  FTS is the default; a local `MEMFW_EMBEDDING_URL` enables hybrid; failures
  degrade to FTS with mode/degraded/errors reported.
- One MCP server is bound to one vault; `source` narrows further. There is no
  account/tenant ACL system.
- Memory documents support optional `valid_from`, `valid_to`, `superseded_by`;
  `memfw supersede` creates a replacement and marks the old document historical
  without deleting its body. `include_history` and `as_of` are supported by
  memory search.
- Optional entity projection (`.memory-entities.sqlite`) is a rebuildable third
  RRF route; default off pending a real-golden evaluation.
- Candidate `memory_remember` writes duplicate/extends/conflicts suggestions to
  candidate metadata only; it never writes the vault.
- `memfw consolidate` writes a review-only candidate/forget-plan packet; dry-run
  is the default.
- `notes.ai_annotation` requires unanimous cross-model labels and a human-kappa
  gate above 0.745; it produces an AI candidate set and a human queue, never
  human golden or training data.
- `memfw code` provides lexical + optional semantic + structure code retrieval,
  with FTS+structure fallback and a repo map.

Still not implemented: automatic candidate tag persistence after human confirm,
a review UI for suggestions, the full 5-class LongMemEval/LoCoMo suite, training,
permission ACLs, and production cutover.


## Tenant, related links, and review UI

- Memory documents now carry optional `tenant` and `related` fields alongside
  `valid_from`/`valid_to`/`superseded_by`.
- A `Gateway` is bound to one vault and one tenant; cross-tenant `get`/search
  are rejected. This is a single-vault gate, not a full account ACL.
- `memfw candidates` shows staged candidates, tags, related ids, and
  duplicate/conflict suggestions; `memfw candidate-apply` is dry-run by default
  and requires an explicit human `--apply`.
- Memories and staged candidates carry `recorded_at` (server write time,
  bitemporal with `valid_from`/`valid_to`); MCP search results report it. Staged
  candidates expire 30 days after `recorded_at`: `memfw candidates` hides them
  and `memfw candidates-expire` marks them. Nothing is deleted.
- Human apply persists `tags`, `related`, and `tenant` into the new memory.
- `memory_eval.py` provides a generated five-class local evaluation scaffold
  (single-hop, multi-hop, temporal, update, refusal); it is not a substitute for
  the human-labelled LongMemEval/LoCoMo suite.


## Memory golden derived benchmark and entity decision

A 100-question benchmark was generated from 40 real pilot-vault documents
(20 single-hop, 20 multi-hop, 20 temporal, 20 update, 20 refusal). Twenty
documents were superseded in a temporary copy to create update/temporal cases.

Entity route on/off (Qwen3-Embedding hybrid, top-10):

| Entity | Recall@10 | MRR | nDCG@10 | Stale fact | As-of | Refusal |
|---|---:|---:|---:|---:|---:|---:|
| off | 1.000 | 0.9792 | 0.9845 | 0.000 | 1.000 | 0.000 |
| on | 1.000 | 1.0000 | 1.0000 | 0.000 | 1.000 | 0.000 |

Default remains **off**: labels are generated from source Markdown, not human;
the sample is small and easy, and refusal accuracy is zero. Enable with
`MEMFW_ENTITY_ENABLED=true` only after a human memory golden confirms the gain.


## Actual AI-labeled batch

Using MiniMax-M3.1-Flash and Doubao-Seed-2.1-Lite on the existing 28-query /
216-candidate set:

- human kappa: 0.745098 (just above the >0.745 gate);
- accepted AI labels: 27;
- human queue: 1 (judge parse error on a no-answer query);
- disagreements: 0;
- confidence threshold: 0.80.

The accepted set overlaps the human evaluation set, so it is a pipeline
demonstration, not independent validation and not training data. No human
golden was overwritten and no model was trained.


## Account-bound source ACL

In addition to the exact tenant gate, a Gateway can be bound to an allowed-source
set via `MEMFW_ALLOWED_SOURCES` (comma-separated) or the constructor. Search
rejects a requested source outside the allowlist, and post-filters documents
whose source is not allowed. This is still a local single-vault policy, not a
central account database.
