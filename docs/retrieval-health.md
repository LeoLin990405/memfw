# Retrieval health (`retrieval_health`)

中文版：[retrieval-health.zh-CN.md](retrieval-health.zh-CN.md)

Every retrieval answer now carries one extra top-level key,
`retrieval_health`, that says in a fixed vocabulary how much to trust it:
whether every path ran, which ones degraded, and what the scores mean.
It is computed by `memfw.retrieval.health` (schema `retrieval-health/1`).

## Guarantee

- It is **derived only from fields the output already carries**
  (`degraded`, `errors`, `recall`, `recall_metrics`, `fallback_reason`, a
  slot's `degraded` code, ...). Nothing is re-queried and nothing new is
  measured.
- Results, their order and their scores are **unchanged**. With no
  degradation, the only difference from the previous output is the new key.
- It is output metadata, not a port contract: `PORT_VERSIONS` is unchanged,
  and the recall coordinator (`memfw.recall`) is untouched. `from_recall`
  reads the plain dicts of `RecallLabels.as_dict()` and
  `RecallMetrics.as_dict()`.

Where it appears:

| Output | Carries `retrieval_health` |
|---|---|
| `memory_search` (every mode; MCP and `Runtime.search` payload) | yes |
| `memory_context` v=2 packs | yes |
| `memory_context` v1, and the `unchanged: true` short reply | no (payload unchanged by design) |
| `conversations_search` (default and `two_stage`) | yes |

## Fields

| Field | Values | Meaning |
|---|---|---|
| `schema` | `retrieval-health/1` | version of this object |
| `mode` | `full`, `degraded`, `fallback`, `empty` | how the answer was produced |
| `confidence` | `high`, `medium`, `low` | how much to trust it |
| `reasons` | sorted list of codes from the table below | why |
| `score_kind` | `rank`, `bm25`, `cosine`, `mixed`, `none` | what a hit's `score` means (see below) |
| `note` | one short sentence (Chinese) | human summary of `mode` |

Every reason code has an `affects_mode` class (`REASON_CODES` maps each
code to `Reason(affects_mode, meaning)`; `affects_mode(code)` returns the
class of one concrete code, `AFFECTS_MODE` lists the four values):

- `yes`: the answer is `degraded` / `medium`;
- `no`: informational, listed but never lowers mode or confidence;
- `fallback`: the primary path failed and another answered (`fallback` / `low`);
- `empty`: only explains an empty answer.

The degradation is carried by the **cause** code, not by a description of
the answer: with no embedder the cause is `semantic_unavailable` (`yes`),
while `keyword_only` only says that literal matching produced the answer
(`no`). This semantics is aligned with fleet-memory, which derives its
object with the same functions.

Rules, first match wins:

1. **No results** → `empty` / `low`. The reasons add exactly one of
   `no_match`, `all_sources_failed` (every source failed) or `withheld_only`
   (every match withheld), plus any other signal.
2. **A `fallback` reason** (the primary path failed and another answered)
   → `fallback` / `low`.
3. **A `yes` reason** → `degraded` / `medium`. `degraded` is always
   `medium`.
4. Otherwise (only `no` reasons, or none) → `full` / `high`.

Informational (`no`) reasons such as `keyword_only`, `adapters_shadow`,
`withheld:<category>:<n>` and `access_not_recorded` are listed but never
lower mode or confidence. Every code is checked against `REASON_CODES`; an
unknown code raises `ValueError`.

## Reason codes

`<name>`, `<n>` and `<category>` are parameters. A `<name>` that is not a
plain identifier is reported as `unknown`.

Columns: **MS** `memory_search` (non-recall modes), **R** `two_stage` recall
(`memory_search mode=two_stage`), **MC** `memory_context` v=2,
**CS** `conversations_search`.

| Code | `affects_mode` | Meaning | MS | R | MC | CS |
|---|:-:|---|:-:|:-:|:-:|:-:|
| `semantic_unavailable` | `yes` | no embedder is configured: the semantic side did not run | x | x | | x |
| `semantic_off` | `yes` | a locality setting kept the query or summaries from the embedder | x | x | | x |
| `semantic_partial` | `yes` | some memories have no vector (kept from an off-host embedder) | x | x | | |
| `summaries_unavailable` | `yes` | session summaries/tags could not be read | | | | x |
| `semantic_empty` | `yes` | the semantic side ran but recalled nothing | x | | | |
| `fts_fallback` | `fallback` | the semantic path failed; FTS answered instead | x | | | |
| `keyword_only` | `no` | only literal (keyword) matching produced the answer | x | x | | x |
| `expansion_empty` | `yes` | multihop expansion added no hit; first-hop results only | x | | | |
| `route_fallback` | `fallback` | the routed providers failed; the remaining ones answered | | x | | |
| `qmd_live_read_errors:<n>` | `yes` | a QMD live index had `<n>` read errors | reserved | | | |
| `source_timeout:<name>` | `yes` | source `<name>` timed out | | x | x | |
| `source_busy:<name>` | `yes` | source `<name>` was busy (in-flight cap) and was skipped | | x | x | |
| `source_error:<name>` | `yes` | source `<name>` failed or returned invalid output | x | x | x | x |
| `source_unavailable:<name>` | `yes` | source `<name>` reported itself unavailable | | x | x | |
| `resolver_error:<name>` | `yes` | the `<name>` resolver failed; its candidates were dropped | | x | | |
| `adapters_shadow` | `no` | recall adapters run in shadow mode (not in the answer) | reserved | | | |
| `adapters_timeout` | `yes` | at least one recall adapter timed out | | x | | |
| `adapters_busy` | `yes` | at least one recall adapter was busy | | x | | |
| `withheld:<category>:<n>` | `no` | `<n>` matches withheld by policy (privacy, tenant, retired, other) | | x | | |
| `access_not_recorded` | `no` | a returned hit's access event or warm-up failed; hits unaffected | x | x | | |
| `no_match` | `empty` | nothing matched | x | x | x | x |
| `all_sources_failed` | `empty` | every source failed: an empty answer proves nothing | | x | x | |
| `withheld_only` | `empty` | every match was withheld by policy | | x | | |

### Withheld matches

`<category>` is one of `privacy`, `tenant`, `retired`, `other`. Recall
policy drop reasons map through `withheld_category()`: `privacy` and
`tenant` keep their name; `archived`, `superseded`, `expired` and `status`
become `retired`; anything else becomes `other`.

Withheld counts are reported only where the output **already exposes counts**:
the `two_stage` recall's `recall_metrics.dropped`. `conversations_search`
default mode silently skips withheld turns and reports no count, on purpose:
a count would reveal that hidden material matched.

## `score_kind`

| Value | Meaning | Used by |
|---|---|---|
| `rank` | fused reciprocal-rank score, `weight/(60+rank)`, normalised. It **orders** hits; it is **not** a relevance measure, so do not threshold on it | `memory_search` hybrid and `two_stage`; `conversations_search two_stage` |
| `bm25` | full-text (BM25) score | `memory_search` fts and multihop, and hybrid/vector that fell back to fts |
| `cosine` | embedding cosine similarity | `memory_search` vector |
| `none` | hits carry no score | `conversations_search` default mode (newest-first keyword turns), `memory_context` |
| `mixed` | reserved for outputs that mix score kinds | nothing today |

## How today's signals map to codes

`memory_search` (`from_memory_search`), from `errors`:

| Signal | Codes |
|---|---|
| `semantic_partial` | `yes` | `semantic_partial` |
| `semantic_query_withheld`, `rerank_withheld`, `rewrite_withheld` | `semantic_off` |
| `access_not_recorded` | `no` | `access_not_recorded` |
| `hybrid unavailable: ...` | `source_error:hybrid` |
| `fts index unavailable...`, `rerank:...`, `rewrite:...`, `trace:...` | `source_error:fts` / `:rerank` / `:rewrite` / `:trace` |
| any other error text | `source_error:unknown` |
| hybrid/vector asked, fts answered, the semantic path raised (`source_error:hybrid`) | `source_error:hybrid` + `fts_fallback` + `keyword_only` → `fallback` / `low` |
| hybrid/vector asked, fts answered, a locality setting withheld the query (`semantic_off`) | `semantic_off` + `keyword_only` → `degraded` / `medium` |
| hybrid/vector asked, fts answered, no embedder | `semantic_unavailable` + `keyword_only` → `degraded` / `medium` |
| hybrid/vector asked, fts answered, embedder present but the semantic side recalled nothing | `semantic_empty` + `keyword_only` → `degraded` / `medium` |
| hybrid/vector answered with `degraded` and no error (the hits are the FTS side's) | `semantic_empty` + `keyword_only` → `degraded` / `medium` |
| multihop with `degraded` | `expansion_empty` |

Recall coordinator (`from_recall`, used for `two_stage`), from
`recall.degraded` per provider status:

| Signal | Codes |
|---|---|
| `timeout` | `source_timeout:<name>` (+ `adapters_timeout` if `<name>` is not a builtin) |
| `busy` | `source_busy:<name>` (+ `adapters_busy` if not a builtin) |
| `unavailable` | `source_unavailable:<name>` |
| `error`, `invalid`, anything else | `source_error:<name>` |
| `resolvers_degraded` entry | `resolver_error:<name>` |
| `fallback_reason: routed_providers_failed` | `route_fallback`, mode `fallback` |
| `fallback_reason: all_providers_failed` or `fail_closed` | `all_sources_failed` (when empty), else mode `fallback` |
| no embedder configured | `semantic_unavailable` → `degraded` / `medium` |
| every answering provider is `fts` and the semantic side was missing (no embedder, or a `:hybrid` source code) | + `keyword_only` (informational) |
| `recall_metrics.dropped` | `withheld:<category>:<n>` |

Builtin providers are `fts` and `hybrid`; any other provider name is an
adapter. The `errors` table above also applies to `two_stage` payloads.

`conversations_search two_stage` (`from_conversation_search`), from
`degraded`:

| Signal | Codes |
|---|---|
| `no_embedder` | `semantic_unavailable` → `degraded` / `medium` |
| `semantic_off` | `yes` | `semantic_off` |
| `embedder_error` | `source_error:embedder` |
| `no_summaries` | `summaries_unavailable` |
| any of the first three and no tag candidates | + `keyword_only` (informational) |

`memory_context` v=2 (`from_context_pack`): each slot's `degraded` code maps
like a recall provider status (`not_found` counts as `unavailable`), named by
the slot. When every slot degraded, the answer is `all_sources_failed`.

## Codes reserved for clients

memfw itself never emits `adapters_shadow` or `qmd_live_read_errors:<n>`.
They exist for clients that wrap memfw output with their own sources (the
fleet-memory MCP server, for example). Such a client should derive its
object with the same function so the vocabulary stays shared:

```python
from memfw.retrieval.health import derive

health = derive(result_count=len(hits), reasons=["adapters_shadow"], score_kind="rank")
```

The module is pure (plain mappings in, plain dict out, no I/O, no other memfw
imports), so a client holding only the JSON gets the same codes.

## Known limits

- For `two_stage`, `keyword_only` is only added when every answering
  provider is `fts`; a lexical adapter such as bm25s is not counted as
  lexical. `keyword_only` is informational either way, so this changes only
  the listed reasons, never mode or confidence (the cause code, such as
  `semantic_unavailable` or `source_error:hybrid`, sets those).
- Free-text errors that match no known code or prefix become
  `source_error:unknown`: the answer is still marked degraded, but the
  source is not named.
- `retrieval_health` reflects the signals the output already carries. A
  failure the output does not report cannot appear here.
- `conversations_search` `two_stage` keeps its whole-response `max_bytes`
  cap: the larger of the key's two possible forms (with and without
  sessions) is reserved before sessions are packed. Near the cap one fewer
  session or turn window may fit than before; `truncated` and
  `next_offset` then say so as usual.
- For `memory_search` `two_stage`, a client that has only the JSON infers
  whether an embedder is configured from the `hybrid` provider appearing in
  `recall.providers` or `recall.degraded` (`from_memory_search`); memfw's
  Runtime passes the real setting.
- Not covered yet: the read-only HTTP service's `POST /context`
  (`memfw-http/3`, unchanged) and the `memfw://context/<slot>` MCP
  resources.
