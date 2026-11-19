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
- `memfw.retrieval.health` imports nothing else from memfw: `import
  memfw.retrieval.health` loads only `memfw`, `memfw.retrieval` and the
  module itself (the package root and `memfw.retrieval` re-export their
  names lazily), so no vault, index or their dependencies are loaded.

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
| `score_kind` | `rank`, `bm25`, `cosine`, `mixed`, `keyword`, `none` | what a hit's `score` means (see below) |
| `note` | one short sentence (Chinese) | human summary of `mode` (or of the `empty` code) |

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
(`no`).

## Rules

First match wins:

1. **No results** → `empty` / `low`. Exactly one `empty` code is added (any
   other signal stays listed), the first that applies, in the order of
   `_EMPTY_ORDER` (exported as `empty_order`):
   1. `all_sources_failed`: every source failed;
   2. `page_empty`: this page is empty but the search has candidates (the
      offset is past them, or nothing fit the cap; `conversations_search`
      `two_stage`);
   3. `empty_degraded`: any `yes` or `fallback` reason. 没有结果不代表不存在:
      no result is then not proof of absence;
   4. `withheld_only`: every match was withheld by policy;
   5. `no_match`: nothing matched, and only when every path was healthy.
2. **A `fallback` reason** (the primary path failed and another answered)
   → `fallback` / `low`.
3. **A `yes` reason** → `degraded` / `medium`. `degraded` is always
   `medium`.
4. Otherwise (only `no` reasons, or none) → `full` / `high`.

Informational (`no`) reasons such as `keyword_only`, `adapters_shadow`,
`withheld:<category>:<n>` and `access_not_recorded` are listed but never
lower mode or confidence. Every code is checked against `REASON_CODES`; an
unknown code raises `ValueError`.

`fts_fallback` and `grep_fallback` describe **single-source** answers only.
In a multi-source answer (recall, context) one source answering from its own
fallback is `source_fallback:<name>` (`yes`): the other sources still ran,
so the answer is degraded, not a fallback.

## One canonical code table

memfw holds the **one** canonical code table, `REASON_CODES`.
`export_codes()` returns it as JSON-serialisable data (schema
`retrieval-health-codes/1`: modes, confidences, score kinds, `affects_mode`
values, withheld categories, `name_pattern`, `empty_order` and every code
with its class and meaning), and `export_codes_json()` returns the committed
text. [`retrieval-health-codes.json`](retrieval-health-codes.json) is that
data; a test checks the file matches. Regenerate it after any change:

```sh
python -m memfw.retrieval.health > docs/retrieval-health-codes.json
```

Extend `REASON_CODES`, the JSON and both docs tables together.

fleet-memory carries its own copy of the table until it upgrades to
memfw >= 0.5.0, then switches to these functions; until then a cross-repo
test on the fleet-memory side compares its copy against the JSON.

## Reason codes

`<name>`, `<n>` and `<category>` are parameters. A `<name>` (provider,
source, slot, resolver) follows `NAME_PATTERN` `[a-z][a-z0-9_.-]{0,63}`: it
is lowercased, and a value that still does not match is reported as
`unknown`. `<n>` is a count; `<category>` is a withheld category (below).

"clients (fleet-memory)" marks codes memfw itself never emits; they exist
for clients that wrap memfw output with their own sources.

| Code | `affects_mode` | Meaning | Emitted by |
|---|:-:|---|---|
| `semantic_unavailable` | `yes` | no embedder is configured: the semantic side did not run | `memory_search` (hybrid/vector answered by fts), `two_stage` recall, `conversations_search two_stage` |
| `semantic_off` | `yes` | a locality setting kept the query or summaries from the embedder | `memory_search` (`errors`), `conversations_search two_stage` |
| `semantic_partial` | `yes` | some providers returned partial semantic results | clients (fleet-memory) |
| `vectors_partial` | `yes` | some memories have no vector (kept from an off-host embedder) | `memory_search` (its `semantic_partial` error) |
| `semantic_empty` | `yes` | the semantic side ran but recalled nothing | `memory_search` hybrid/vector |
| `summaries_unavailable` | `yes` | session summaries/tags could not be read | `conversations_search two_stage` |
| `expansion_empty` | `yes` | multihop expansion added no hit; first-hop results only | `memory_search` multihop |
| `meta_index_stale` | `yes` | a metadata index the answer relies on is stale | clients (fleet-memory) |
| `qmd_live_read_errors:<n>` | `yes` | a QMD live index had `<n>` read errors | clients (fleet-memory) |
| `source_timeout:<name>` | `yes` | source `<name>` timed out | `two_stage` recall, `memory_context` v=2 |
| `source_busy:<name>` | `yes` | source `<name>` was busy (in-flight cap) and was skipped | `two_stage` recall, `memory_context` v=2 |
| `source_error:<name>` | `yes` | source `<name>` failed or returned invalid output | `memory_search`, `two_stage` recall, `memory_context` v=2, `conversations_search two_stage` |
| `source_unavailable:<name>` | `yes` | source `<name>` reported itself unavailable | `memory_search` (fts `index_stale`), `two_stage` recall, `memory_context` v=2 |
| `source_fallback:<name>` | `yes` | in a multi-source answer, source `<name>` answered from its own fallback | `memory_search two_stage` (from `degraded_sources`); clients (fleet-memory) for recall and context |
| `resolver_error:<name>` | `yes` | the `<name>` resolver failed; its candidates were dropped | `two_stage` recall |
| `adapters_timeout` | `yes` | at least one recall adapter timed out | `two_stage` recall |
| `adapters_busy` | `yes` | at least one recall adapter was busy | `two_stage` recall |
| `fts_fallback` | `fallback` | a single-source answer came from the FTS fallback (primary path failed) | `memory_search` hybrid/vector |
| `grep_fallback` | `fallback` | a single-source answer came from a plain-text grep fallback | clients (fleet-memory) |
| `route_fallback` | `fallback` | the routed providers failed; the remaining ones answered | `two_stage` recall |
| `keyword_only` | `no` | only literal (keyword) matching produced the answer | `memory_search`, `two_stage` recall, `conversations_search two_stage` |
| `adapters_shadow` | `no` | recall adapters run in shadow mode (not in the answer) | clients (fleet-memory) |
| `withheld:<category>:<n>` | `no` | `<n>` matches withheld by policy (privacy, tenant, retired, other) | `two_stage` recall |
| `access_not_recorded` | `no` | a returned hit's access event or warm-up failed (hits unaffected) | `memory_search` (`errors`) |
| `all_sources_failed` | `empty` | every source failed: an empty answer proves nothing | `memory_search` (every source the mode reads errored), `two_stage` recall, `memory_context` v=2 |
| `page_empty` | `empty` | this page is empty but the search has candidates (offset past them, or cap) | `conversations_search two_stage` |
| `empty_degraded` | `empty` | nothing came back while a path was degraded or fell back: not proof of absence | every output (`derive`) |
| `withheld_only` | `empty` | every match was withheld by policy | `two_stage` recall |
| `no_match` | `empty` | nothing matched, with every path healthy | every output |

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
| `keyword` | literal-match order (grep, newest first) with no ranking score | clients (fleet-memory), e.g. a grep fallback |
| `none` | hits carry no score | `conversations_search` default mode (newest-first keyword turns), `memory_context` |
| `mixed` | reserved for outputs that mix score kinds | nothing today |

## How today's signals map to codes

`memory_search` (`from_memory_search`), from `errors` (exact code or text
prefix):

| Signal | Codes |
|---|---|
| `semantic_partial` (some memories have no vector) | `vectors_partial` |
| `semantic_query_withheld`, `rerank_withheld`, `rewrite_withheld` | `semantic_off` |
| `access_not_recorded` | `access_not_recorded` (informational) |
| `index_stale` (the FTS index needs a rebuild and returned nothing) | `source_unavailable:fts` |
| `hybrid unavailable...` | `source_error:hybrid` |
| `fts index unavailable...` | `source_error:fts` |
| `rerank:...`, `rewrite:...`, `trace:...` | `source_error:rerank` / `:rewrite` / `:trace` |
| `unicode61 query terms unavailable...` | `source_error:query_terms` |
| `alias expansion unavailable...` | `source_error:aliases` |
| any other error text | `source_error:unknown` |

`memory_search`, from the mode asked and the mode that answered:

| Signal | Codes → mode |
|---|---|
| hybrid/vector asked, fts answered, the semantic path raised (`source_error:hybrid`) | + `fts_fallback` + `keyword_only` → `fallback` / `low` |
| hybrid/vector asked, fts answered, a locality setting withheld the query (`semantic_off`) | + `keyword_only` → `degraded` / `medium` |
| hybrid/vector asked, fts answered, no embedder | `semantic_unavailable` + `keyword_only` → `degraded` / `medium` |
| hybrid/vector asked, fts answered, embedder present but the semantic side recalled nothing | `semantic_empty` + `keyword_only` → `degraded` / `medium` |
| hybrid/vector answered with `degraded` and no error (the hits are the FTS side's) | `semantic_empty` + `keyword_only` → `degraded` / `medium` |
| multihop with `degraded` | `expansion_empty` |
| no results, and every source the mode reads errored or is unavailable (hybrid/vector: `hybrid` and `fts`; fts/multihop: `fts`) | `all_sources_failed` → `empty` / `low` |

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
| `degraded_sources: [<name>, ...]` (providers whose page was degraded; present only when non-empty) | `source_fallback:<name>` each |
| payload `degraded` with no `degraded_sources`, no recall/resolver/`fail_closed` signal and no error at all (a reply from before `degraded_sources`) | `source_fallback:unknown` |
| payload `degraded` only because an access event failed (`errors: [access_not_recorded]`) | `access_not_recorded` (informational: `full` / `high`) |

Builtin providers are `fts` and `hybrid`; any other provider name is an
adapter. The `errors` table above also applies to `two_stage` payloads.

`conversations_search two_stage` (`from_conversation_search`), from
`degraded`:

| Signal | Codes |
|---|---|
| `no_embedder` | `semantic_unavailable` → `degraded` / `medium` |
| `semantic_off` | `semantic_off` → `degraded` / `medium` |
| `embedder_error` | `source_error:embedder` |
| `no_summaries` | `summaries_unavailable` |
| any other entry | `source_error:unknown` |
| any of the first three and no tag candidates | + `keyword_only` (informational) |
| no session on this page but `candidates.total` > 0 | `page_empty` |

`conversations_search` default mode reports `full` (or `no_match`) with
`score_kind` `none`.

`memory_context` v=2 (`from_context_pack`): each slot's `degraded` code maps
like a recall provider status (`not_found` counts as `unavailable`), named by
the slot. When every slot degraded, the answer is `all_sources_failed`.

## Codes reserved for clients

memfw itself never emits `semantic_partial`, `meta_index_stale`,
`qmd_live_read_errors:<n>`, `grep_fallback` or `adapters_shadow`, nor
`source_fallback:<name>` with a real name. They exist for clients that wrap
memfw output with their own sources (the fleet-memory MCP server, for
example). Such a client derives its object with the same function so the
vocabulary stays shared:

```python
from memfw.retrieval.health import derive

health = derive(result_count=len(hits), reasons=["adapters_shadow"], score_kind="rank")
```

The module is pure (plain mappings in, plain dict out, no I/O, no other memfw
imports), so a client holding only the JSON gets the same codes; a client
that cannot import memfw yet compares its copy against
`retrieval-health-codes.json`.

## Known limits

- For `two_stage`, `keyword_only` is only added when every answering
  provider is `fts`; a lexical adapter such as bm25s is not counted as
  lexical. `keyword_only` is informational either way, so this changes only
  the listed reasons, never mode or confidence (the cause code, such as
  `semantic_unavailable` or `source_error:hybrid`, sets those).
- Free-text errors that match no known code or prefix become
  `source_error:unknown`: the answer is still marked degraded, but the
  source is not named. Likewise `source_fallback:unknown` (only for a reply
  without `degraded_sources`, i.e. from before it existed) cannot name the
  provider that fell back.
- `retrieval_health` reflects the signals the output already carries. A
  failure the output does not report cannot appear here.
- `conversations_search` `two_stage` keeps its whole-response `max_bytes`
  cap: `retrieval_health` is reserved inside the cap in its "with sessions"
  form only, before sessions are packed. A reply that ends with no session
  is small (nothing was appended), so its empty form always fits. Near the
  cap one fewer session or turn window may fit than before; `truncated` and
  `next_offset` then say so as usual.
- For `memory_search` `two_stage`, a client that has only the JSON infers
  whether an embedder is configured from the `hybrid` provider appearing in
  `recall.providers` or `recall.degraded` (`from_memory_search`); memfw's
  Runtime passes the real setting.
- Not covered yet: the read-only HTTP service's `POST /context`
  (`memfw-http/3`, unchanged) and the `memfw://context/<slot>` MCP
  resources.
