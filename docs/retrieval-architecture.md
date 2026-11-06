# Retrieval architecture and staged plan

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
