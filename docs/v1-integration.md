# MemFW V1 integration status

V1 is a **read-only federation candidate**, not a production cutover and not a
complete replacement of Conductor's proposal/approval memory.

## Stable V1 surface

- Markdown is authoritative; hash-chained JSONL events remain the audit log.
- `memory_search` defaults to FTS5 and optionally delegates to the shared
  NotesRetriever hybrid when a local OpenAI-compatible embedding endpoint is
  configured; unavailable embeddings degrade to FTS.
- `memory_get`, `memory_remember` (staging only), `memory_context`, and
  `memory_status` are available over local MCP.
- Temporal fields `valid_from`, `valid_to`, `superseded_by` live in Markdown and
  are projected separately; Conductor's existing `memory_rows` schema stays
  byte-compatible.
- A human `supersede` keeps the old body, marks its validity end, and appends a
  `supersede` event.
- Optional tenant/source allowlists are local-bound, not a centralized account
  service.

## Conductor adapter

The Conductor V1 adapter is prepared in the isolated worktree
`/Users/leo/conductor-wt/memfw-v1`, branch `feat/memfw-v1-federation`:

- uses the read-only HTTP service, not a vault copy or mount;
- accepts MemFW `supersede` events;
- excludes documents with `valid_to`/`superseded_by` from current recall;
- preserves temporal/tenant/related metadata in hits;
- binds `local-dev-user` to an absolute local MemFW path;
- supports `MEMFW_PRIMARY=1` for MemFW-primary recall while Conductor PG remains
  the proposal destination.

Focused Conductor federation tests: 91 passed.

## Remaining replacement gap

A full replacement needs:

1. an authoritative MemFW vault path reachable by Conductor (a local snapshot is
   not enough);
2. a write bridge from approved Conductor proposals to MemFW
   `_candidates/` / human confirmation / Markdown + event;
3. a restart/cutover window and rollback;
4. real-user evaluation of the bound vault.

Until then, V1 is read-only and does not bypass MemFW's human-confirmation
principle.


## Read-only remote service

`scripts/memfw_readonly_service.py` is a stdlib-only HTTP facade over an
existing MemFW vault. It exposes `/health` and authenticated `/search`; it has no
write endpoint. It reads the disposable SQLite index and Markdown only.

On HA500, the V1 service is rooted at `/vol1/fleet-memory/memfw-vault`, bound to
the tailnet on port 18080, and protected by `X-Memfw-Token`. Conductor binds
`memfw_url` and reads `MEMFW_READONLY_TOKEN` from its environment. No vault sync
is performed.

The current service is started by `setsid/nohup`; durable systemd management,
token rotation and HA remain follow-ups.


## Relatedness

`memfw.related` is a rebuildable edge projection over current memories and notes.
V1 implements explicit links/`related`, shared tags/courses, shared entities, and
lexical overlap. `memory_related` is exposed over MCP; `notes_related` is available from the
notes MCP when configured. Semantic edges are opt-in through `build_semantic`
using sqlite-vec KNN and one shared embedding fingerprint. QMD notes remain
outside semantic edges until a local vector index is available.


## Section-level edges and relatedness annotation

- `RelatedIndex.rebuild_sections` builds bounded section-level edges
  (`source=notes_sections`); the QMD 2,000-section subset produced 39,980 edges.
- `RelatedIndex` items/edges carry tenant/scope columns; `related()` accepts
  tenant/scope filters. Memory build uses each document's tenant/source.
- `related_annotation.generate_pairs` creates a 100-pair positive/negative batch;
  `calibrate` computes precision/recall by threshold after human labels.
  The current QMD batch is at
  `~/Documents/Codex/memfw-related-annotation-20261106/related-pairs.csv`.
- This is still a generated candidate set; human labels are required before
  changing a production threshold.
