# Ports and the recall coordinator

This page describes `memfw.ports` (four port protocols, value types, staging, adapter
registry) and `memfw.recall` (the recall coordinator and its built-in providers). The code is
the contract; this page explains it.

## 1. Purpose

memfw is an authoritative kernel plus a coordination layer. The kernel owns what a memory is
(vault and event log), whose it is (id, tenant, source), whether it may be given out
(privacy, validity, supersession, epistemic/causal gates) and where it came from
(provenance). External systems plug in through four ports: `RecallProvider`, `Projector`,
`Ingestor` and `Deriver`. They find things fast, find them
well, or present them in another form. Their code never enters memfw: an adapter uses the
external project's public interface, and the kernel uses the adapter only through a port.

**Status in this release.** The coordinator is wired into MCP `memory_search` as the opt-in
`mode=two_stage` (§4.1). It is not used by HTTP or the CLI. The default mode (`hybrid`) and
`fts`, `vector` and `multihop` are unchanged: their output is byte-identical to before.

### 1.1 Packaging boundaries

Every component is a separately packageable, replaceable unit:

- **`memfw.ports` is the socket.** It imports only the standard library, so it can ship as a
  standalone `memfw-ports` distribution that adapters depend on without depending on memfw.
  The shared dataclasses live there. `EPISTEMIC_VALUES` and `CAUSAL_VALUES` are mirrored from
  `memfw.core.epistemic`, and a test keeps the two equal.
- **`memfw.recall` is replaceable.** Its modules import only `memfw.ports` and each other.
  One exception is `memfw.recall.builtin`, the only module that reaches memfw internals
  (vault, repository, retrievers). It holds the vault resolver and the built-in providers.
  `import memfw.recall` does not load it.
- **Enforced by tests.** `tests/test_ports_isolation.py` checks the imports of each source
  file (AST). It also runs `import memfw.ports` in a subprocess and asserts that no other
  memfw module was loaded. The same check runs for `import memfw.recall`.
  `tests/test_architecture_layers.py` extends the AST check to every memfw module (see
  [Architecture guards](architecture.md)).

`memfw/__init__.py` imports `memfw.related` when the package loads. The subprocess check
therefore compares against a baseline taken after `import memfw`. If `memfw-ports` is ever
extracted, it ships as its own distribution, so this does not affect it.

## 2. The four port contracts

Protocols live in `memfw.ports.protocols` and are `runtime_checkable`; `isinstance` only
checks that the attributes exist. Value types live in `memfw.ports.types`: frozen dataclasses
that validate in `__post_init__`, so a bad value fails inside the adapter's own call. Every
adapter has a `name` matching `^[a-z][a-z0-9_-]{0,63}$` (`check_name`) and an `available()`
returning `Availability.ok()` or `Availability.missing(reason)`. The reason is a short code,
never user data.

### 2.1 RecallProvider

```python
class RecallProvider(Protocol):
    name: str
    def available(self) -> Availability: ...
    def search(
        self,
        query: str,
        *,
        limit: int,
        budget: RecallBudget,
        tenant: str,
        as_of: datetime | None,
    ) -> Sequence[Candidate]: ...
```

`search` returns at most `limit` candidates with `provider == self.name` and ranks 1..n.

| `Candidate` field | Rule |
|---|---|
| `ref` | `memory_id`: the vault id rule `MEMORY_ID_RE`; `path`: `PATH_REF_RE`, at most 512 chars, no whitespace, control characters, quotes, backslashes, backticks or angle brackets (non-ASCII letters are fine) |
| `ref_kind` | `"memory_id"` or `"path"` (`REF_KINDS`) |
| `score` | the provider's own score; a finite `int` or `float`, not bool |
| `provider` | the provider's name |
| `rank` | 1-based position in the provider's list |
| `extra` | allowlisted keys only, short scalar values, 256 bytes in total |

**Never body text.** The kernel reads the body from the vault after resolve and filter.
`extra` takes only keys in `ALLOWED_EXTRA_KEYS` (`why`, `score_raw`, `rank_raw`, `chunk`,
`page`, `t`, `lang`, `kind`). This is an allowlist, not a denylist, so no key name can carry
text. Values must be plain `str | int | float | bool | None`, finite, and strings at most 64
chars; the whole mapping is capped at `MAX_EXTRA_BYTES = 256`. `validate_candidate(c)` runs
every rule. It runs at construction and again in the coordinator and the contract kit, so a
`Candidate` subclass or an `object.__setattr__` after construction cannot get a value past.

`RecallBudget(timeout_seconds=1.5, max_candidates=50)` is the per-call limit. The provider
honours the tenant when it can (exact match; the empty tenant matches only itself); the
coordinator filters again either way. Raise `ProviderUnavailable` when the provider cannot
serve right now; any other exception counts as an error. Messages are never recorded.

### 2.2 Projector

```python
class Projector(Protocol):
    name: str
    def available(self) -> Availability: ...
    def export(self, vault: Path, out: Path, policy: ProjectionPolicy) -> Manifest: ...
```

Read-only on the vault; the output is rebuildable. `ProjectionPolicy(version, options)`
requires a version. `Manifest` fields: `projector`, `policy_version`, `projected` (not
negative), `skipped` (reason → count), `content_sha256` (64 lowercase hex over the exported
files in a stable order, so two exports of the same vault and policy compare equal),
`generated_at` (timezone-aware) and `files` (relative paths inside `out`, no `..`).

### 2.3 Ingestor

```python
class Ingestor(Protocol):
    name: str
    def available(self) -> Availability: ...
    def stage(
        self,
        vault: Path,
        items: Sequence[IngestItem],
        *,
        proposal_id: str,
        source: str,
    ) -> StageResult: ...
```

An ingestor writes only under `<vault>/_candidates/<ingestor>/`. `proposal_id` and `source`
are required. Staging is idempotent. `IngestItem(title, body, external_ref="", metadata={})`
is one record; `StageResult` holds `staged` and `duplicates` ids.

`stage_candidates(vault, ingestor, items, *, proposal_id, source)` in `memfw.ports.staging`
is the reference implementation:

- the vault root must hold `vault.yaml`; `_candidates/` and its ingestor directory must not be
  symlinks and must resolve inside the vault;
- a malformed or empty `proposal_id` or `source` raises `StagingError`; nothing is written;
- every item is validated and serialised (JSON, no NaN) before anything is written; one bad
  item leaves the directory untouched;
- `candidate_id(ingestor, source, item)` is `cand-` plus 24 hex chars of SHA-256 over
  ingestor, source, `external_ref` and `content_sha256(item)` (title, body, metadata). The
  proposal id is not part of it. The same `external_ref` with changed content is therefore
  a new candidate, never silently dropped;
- each item is written to a temporary file (`O_EXCL | O_NOFOLLOW`, mode `0600`, fsynced) and
  linked to `<candidate_id>.json`, which fails if the name exists (atomic). An existing file
  counts as a duplicate only if its recorded `content_sha256` matches; otherwise staging
  raises `StagingError`.

### 2.4 Deriver

```python
class Deriver(Protocol):
    name: str
    def available(self) -> Availability: ...
    def derive(self, vault: Path, request: DeriveRequest) -> DerivedView: ...
```

`DeriveRequest(kind, memory_ids, options={})` selects the input. `DerivedView(deriver, kind,
content, citations, epistemic_cap="inference", causal_cap="association", read_only=True)`:

- `read_only` must be true. A view never writes back; anything worth keeping goes through an
  `Ingestor` as a candidate.
- `citations` must be non-empty; each is `Citation(memory_id, version=None, note="")`.
- `epistemic_cap` is the strongest type any statement may claim: `experience`, `fact`,
  `judgment` or `inference`.
- `causal_cap` is `none`, `association` or `hypothesis`; never `"verified"`. Verification
  needs a reviewed record, not a derivation.

`check_view_citations(view, exists)` raises `ValueError` if the view cites an id for which
`exists(memory_id)` is false.

### 2.5 Clock and IdGen

`memfw.ports.clock` names the two values the kernel must never invent on its own: the current
time and new ids. Both are small `runtime_checkable` protocols:

```python
class Clock(Protocol):
    def now(self) -> datetime: ...   # timezone-aware; never goes backwards

class IdGen(Protocol):
    def new_id(self) -> str: ...     # fresh, valid as a memory id, never repeated
```

| Implementation | Behaviour |
|---|---|
| `SystemClock()` | `datetime.now(UTC)`, the same as `memfw.core.models.utc_now` |
| `FixedClock(at)` | returns `at` (timezone-aware) until `advance(delta)` or `set(at)`; never moves backwards |
| `UuidIdGen()` | `str(uuid.uuid4())`, the default `Repository` `id_factory` |
| `SeededIdGen(seed, *, start=0)` | `sha256(f"{seed}:{n}")[:32]` for n = start+1, start+2, ...: the `memfw --id-seed` scheme |

`clock.now` and `id_gen.new_id` are the zero-argument callables `Repository(clock=...,
id_factory=...)` already accepts. Core does not use the ports yet; A3 (composition root) wires
them in. `check_clock(clock, *, samples=32)` and `check_id_gen(id_gen, *, samples=1000)` in
`memfw.ports.testing` are the contract: aware and monotonic times; distinct `str` ids matching
`^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$`.

## 3. The coordinator pipeline

`Coordinator(providers, *, resolvers, configs=None, router=None, total_timeout=4.0,
rrf_k=60, clock=..., timer=..., metrics_sink=None, inflight=None)` takes a sequence of `RecallProvider`s and
a `Mapping[RefKind, Resolver]`. Provider names must be unique; `configs` may only name known
providers. `ProviderConfig(weight=1.0, budget=RecallBudget(), lexical=None, overfetch=1,
max_inflight=1)` (weight must be finite and `>= 0`). `inflight` is an `InflightRegistry`
shared between coordinators that run the same providers, so `max_inflight` holds across them;
`lexical=None` means "use the provider's `lexical` attribute". `search(query, *, limit=10, policy)` caps
`limit` at 200 and runs seven steps. No body text passes through.

### 3.1 Route

`DefaultRouter` calls `classify_query`, a pure function that never logs the query. A query is
`exact` if, in order:

1. it has a quoted span (`"…"`, `“…”`, `「…」`, `『…』`, `'…'`, `` `…` ``);
2. it has a URL scheme (`x://`), a hex id of 12–64 chars, or 6 or more consecutive digits;
3. it has a version (`1.2`, `v0.2.0`), an uppercase acronym of 4 or more chars, or something
   path-like (`a/b`, `./x`, `*.md`, `.py`, `.json`, `.yaml`, `.yml`, `.toml`);
4. digits are at least 30% of the non-space characters;
5. it is 1–3 tokens that all look like proper names (`Leo`, `O'Brien`);
6. it has at most 8 non-space characters and at most 2 tokens.

Otherwise it is `semantic`. Exact queries go to the lexical providers when at least one
exists; everything else goes to all providers in order. `AllProvidersRouter` sends every
query everywhere. A custom router implements `route(query, providers, *, lexical)` returning
a `RouteDecision(kind, providers)`.

### 3.2 Recall

For each routed provider a daemon worker runs `available()` and then `search` with
`limit = min(limit * overfetch, budget.max_candidates)` and the policy's tenant and `as_of`.
The coordinator waits until the earlier of the provider's own deadline (start +
`budget.timeout_seconds`) and the total deadline. A late provider is abandoned; the others
carry on.

A worker cannot be killed. Each provider therefore has an in-flight limit
(`max_inflight`, default 1). While an earlier call is still running, later searches skip
that provider as `busy` instead of starting another thread. A hung adapter holds at most
`max_inflight` threads, and `Coordinator.inflight("provider:<name>")` reports the count.

| Status | Cause | Degraded code |
|---|---|---|
| `unavailable` | `available()` false or raised, or `ProviderUnavailable` | `unavailable[:<reason>]` |
| `timeout` | no answer in time (including a hanging `available()`) | `timeout` |
| `busy` | an earlier call is still running (`max_inflight` reached) | `busy` |
| `error` | any other exception, `BaseException` included | `error:exception` |
| `invalid` | not a sequence, iteration raised, or no valid item | `invalid:not_a_sequence`, `invalid:bad_output`, `invalid:no_valid_candidates` |

Items that fail `validate_candidate` or carry another provider's name are dropped and counted
as `invalid`. Validating a return value never raises. Labels carry only fixed codes. An
availability reason is kept only if it already is a code (`[A-Za-z0-9_-]{1,64}`), otherwise
it becomes `unavailable`. A `projection_version` is kept only if it matches
`[A-Za-z0-9._:+-]{1,64}`. Exception messages and type names from adapter code are never
recorded.

**Fallback wave.** When every routed provider failed and the route left some out, those run
as a second wave if time remains (`fallback_reason="routed_providers_failed"`). An exact
query still gets semantic results when FTS is down.

### 3.3 Resolve

Refs are grouped by `ref_kind` and sent to that kind's resolver in one batch call,
`Resolver.resolve(self, refs: Sequence[str]) -> Mapping[str, Resolution]`. The call runs in a
bounded worker under the same total deadline, with an in-flight limit of 1 per kind.
`Resolution(memory_id, meta)` names a memory; `meta=None` means it could not be read. These
all leave the ref `unresolved`, and it is dropped:

- a ref missing from the result;
- a kind without a resolver;
- a resolver that raises, times out, is still busy or returns something that is not a
  mapping. This is reported in `labels.resolvers_degraded` (`error`, `timeout`, `busy`,
  `invalid`) and counted in `resolver_errors`;
- an entry that is not a `Resolution`, or has an invalid memory id.

A `Resolution` whose `meta.memory_id` differs from its `memory_id` is dropped as
`id_mismatch`.
`memfw.recall.builtin.default_resolvers(root)` returns:

- `VaultIdResolver(root)` for `memory_id`: reads `memories/<id>.md` (no symlinks). An
  unreadable file gives `meta=None`, which fails closed.
- `MemoryPathResolver(ids)` for `path` refs of the form `memories/<id>.md` (optionally under
  `./` or `fleet-memory/`); other path forms stay unresolved.

The full path resolver (`files/<rel>` → `stable_id` → meta-index) is R6 PR-1's `join_meta`.
It plugs in through the same protocol (`PathResolver` marks the seam). Until then
`meta_from_document` reads privacy from tags: `privacy:<value>` is the declared value,
`sensitivity:private` marks the memory private, `lifecycle:<value>` is the lifecycle.

### 3.4 Filter

`evaluate(meta, policy, *, now)` applies R6 §5.4 to one memory and returns a `Verdict`.
Missing metadata fails closed. Reasons are the fixed codes in `DROP_REASONS`:

| Reason | Drops when | Reads |
|---|---|---|
| `unresolved` | no resolver returned the ref (counted per ref) | resolver result |
| `missing_meta` | `meta is None` | resolver result |
| `tenant` | `meta.tenant != policy.tenant` | `tenant` |
| `privacy` | class not in `privacy_clearance` (if `check_privacy`) | `privacy`, `sensitivity_private` |
| `source` | differs from `policy.source`, or not in `allowed_sources` (when set) | `source` |
| `status` | missing, not `confirmed`/`unconfirmed`/`disputed`, or in `excluded_statuses` | `status` |
| `expiry_unparsable` | a temporal field is set but not ISO-8601 | `valid_from`, `created_at`, `valid_to`, `valid_until` |
| `retired` | `lifecycle == "retired"` (strict mode; case and spaces ignored) | `lifecycle` |
| `superseded` | `superseded_by` set, or `lifecycle == "superseded"`, and no `as_of` | `superseded_by`, `lifecycle` |
| `lifecycle` | any other lifecycle than empty/`active` (e.g. `archived`, `deleted`) | `lifecycle` |
| `expired` | strict: `min(valid_to, valid_until) <= now` (or `<= as_of`); index: `valid_to` set (or `<= as_of`) | `valid_to`, `valid_until` |
| `not_yet_valid` | `valid_from` after now (strict); with `as_of`, `valid_from` (else `created_at`) after it | `valid_from`, `created_at` |
| `id_mismatch` | resolver returned metadata for another id (coordinator) | resolver result |
| `archived` | `drop_archived` and (`is_stub` or `tier == "cold"`) | `is_stub`, `tier` |

With `as_of`, a superseded memory is judged by its validity window at that instant and kept
with a `superseded` label when it was valid then. `include_retired` keeps retired,
superseded and expired memories with a label;
`include_history` skips temporal checks and keeps retired and superseded with a label.
Neither widens tenant, privacy or source. Kept memories may get the labels
`privacy:<class>` (when not `none`), `disputed` (weight times `disputed_weight`), `retired`,
`superseded`, `expired` and `anchor` (`layer == "anchor"`); `status_weights` can lower the
weight further.

**Privacy.** `classify_privacy` maps a raw value to `none` (unset, empty, `none`, `null`,
`public`, `fleet`, `shared`), `local-private`, or `unknown` (anything else). A memory's class
is the stricter of the declared value and the tag; disagreement is counted in
`privacy_source_mismatch`. **`unknown` is always dropped** (design decision): `evaluate`
drops it even under a hand-built clearance that lists it. `parse_clearance(value)` reads a
comma-separated string or an iterable. Only `none` and `local-private` are grantable
(`GRANTABLE_PRIVACY`). Unset, empty or malformed gives `DEFAULT_CLEARANCE = {"none"}`. Any
other token, `unknown` included, makes the whole value malformed, so a typo can never widen
access.

| Setting | `FilterPolicy()` (strict) | `FilterPolicy.compat_fts(...)` |
|---|---|---|
| tenant, source | checked | checked |
| privacy | checked, clearance `{"none"}` | not checked |
| status | checked | not checked |
| archived/stub | dropped | kept |
| temporal | `strict` (lifecycle, `valid_until`, `now`) | `index` |
| `disputed_weight` | 0.5 | 1.0 |

`compat_fts` exists so that `mode=fts` can later run through the coordinator with
byte-identical output. It applies only what `search_vault` already applies: tenant, source
and the index's temporal clause (the `as_of` window; otherwise drop `superseded_by` and any
non-empty `valid_to`). Like the index, it never drops on an unparsable `valid_from` or
`created_at`, and it ignores `valid_until`.

### 3.5 Fuse

```text
score(id) = Σ_p weight_p · w_id / (k + rank_p(id))      k = 60
```

`rank_p` is the 1-based position in provider `p`'s filtered, deduplicated list, not the
provider's own rank. `weight_p` is `ProviderConfig.weight`; `w_id` is the filter weight. A
weight of 0 removes the contribution but keeps the provider in the hit's labels. Ties break
by best rank, then by the first provider in route order that returned the id, then by id.
The order never depends on dict or thread order.

### 3.6 Label

Each `RecallHit` has `memory_id`, `score`, `providers`, `ranks`, `provider_scores` (raw
scores), filter `labels`, and the `extra` of the first kept candidate. `RecallLabels` has
`route`, `providers` (those that answered), `degraded` (name → code),
`projection_versions` (each provider's `projection_version`, when set), `fail_closed` and
`fallback_reason`. When no provider answered, `fail_closed` is true, `hits` is empty and
`fallback_reason` is `all_providers_failed` (or `routed_providers_failed` after a failed
fallback wave). A provider that answered with nothing is not a failure.

### 3.7 Metrics

`RecallMetrics`: `total_ms`, `route`, `providers` (one `ProviderReport` each: `name`,
`status`, `reason`, `latency_ms`, `candidates`, `invalid`, `kept`), `resolved` (distinct ids),
`returned`, `dropped` (reason → count), `privacy_source_mismatch`, `resolver_errors`. It is
kept in `last_metrics` and passed to `metrics_sink`; a failing sink is logged and ignored.
Metrics never contain the query text. Exception messages and type names from adapter code are
not recorded, only fixed codes (R6 §5.9).

## 4. Built-in providers

`memfw.recall.builtin` wraps today's search paths unchanged. Each returns `memory_id`
candidates with `extra={"why": ...}`, capped at 50 per page like MCP `memory_search`.

| Provider | Wraps | Lexical | Unavailable reasons |
|---|---|---|---|
| `FtsProvider(root, *, name="fts", source="", include_history=False, scope="", allowed_sources=(), prepare=None)` | `search_vault` (`mode=fts`, HTTP `/search`) | yes | `no_vault`, `no_index`, `readonly_service_missing` |
| `HybridProvider(root, *, embedder, name="hybrid", mode="hybrid", source="", include_history=False, allowed_sources=(), entity_enabled=False)` | `MemoryHybridRetriever`, `hybrid` or `vector` | no | `no_vault`, `no_embedder`, `sqlite_vec_missing` |
| `MultihopProvider(root, *, name="multihop", source="", include_history=False, allowed_sources=(), rounds=None, prepare=None)` | `search_multihop` | no | `no_vault`, `no_index` |

When the hybrid retriever falls back to FTS internally, `HybridProvider` raises
`ProviderUnavailable("fell_back_to_fts")`: the FTS provider answers instead. Its
`projection_version` is the embedder's `fingerprint`.

**`prepare`.** `FtsProvider` and `MultihopProvider` accept `prepare: Callable[[Repository],
None]`, run before each search, so the wiring PR can pass the MCP server's index staleness
check. Without it, a missing `index.sqlite3` makes the provider unavailable.

All three built-in providers pass `memfw.ports.testing.check_recall_provider` (see §7.1).
`tests/test_recall_builtin.py` holds the equivalence guard for the wiring PR. A coordinator
with only `FtsProvider` and `FilterPolicy.compat_fts(...)` returns the same ids, in the same
order, as `Gateway.search(mode="fts")`. This is checked across tenants, limits, `source`,
`include_history`, `as_of` and `allowed_sources`.

### 4.1 MCP `memory_search` `mode=two_stage`

`mode=two_stage` runs the query through a `Coordinator` and builds the rows from the fused
hits. It is opt-in; nothing else changes.

- **Providers.** `FtsProvider` always; `HybridProvider` (`mode=hybrid`) only when the server
  has an embedder (`MEMFW_EMBEDDING_URL`). Both get the server's `allowed_sources`, the
  call's `source`, `include_history` and (FTS only) `scope`. Each provider may take 3 s;
  all providers and resolvers together get **4 s** (the total deadline). The index staleness
  check runs before the deadline starts. Resolvers are `default_resolvers(root)`.
- **One thread per hung provider.** Every call on one `Gateway` shares an
  `InflightRegistry`, so `max_inflight` (1) holds across calls: a provider that hangs holds
  one worker thread, and later calls report it as `busy` instead of starting another.
  Concurrent `two_stage` calls on one gateway can therefore see a provider as `busy`.
- **`policy`.** `compat_fts` (default) is `FilterPolicy.compat_fts(...)`: tenant, source and
  the index's temporal clause, exactly what `mode=fts` applies. `strict` is the default
  `FilterPolicy(...)` with the same tenant, sources, `as_of` and `include_history`: it adds
  the privacy (clearance `none`), status, lifecycle and archive checks and the strict temporal
  rule (`valid_from` in the future is dropped). Absent, `null` and `""` mean `compat_fts`. An unknown
  value, or a non-string in the tool call, is an error. **Any other mode rejects a `policy` other than `compat_fts`** (an error
  where it was ignored before).
- **Input rules match `mode=fts`.** The query must be 1..512 characters without NUL or lone
  surrogates, and fails with the same error as `fts`, before any provider runs. `as_of` is
  normalised the same way: `Z` and a timestamp without a timezone are read as UTC. `limit` is
  capped to 1..50.
- **Rows.** Each hit is built by the same code as the other modes, from the provider's own
  index row when it returned one (passed in-process through the providers' `observe` hook,
  never through `Candidate.extra`). So `stale`, `index_version`, `sot_version`,
  `content_hash`, `epistemic`, `causal` and `why` match `mode=fts` for FTS hits. A hit with
  no index row (a hybrid row without one, or an adapter) is built from the vault document.
  Every row adds `providers` (which providers returned it) and `recall_labels` (the filter
  labels). `score` is the fused RRF score, so it is not comparable with `mode=fts` scores.
- **Payload.** `mode: "two_stage"`, `policy`, `errors` and `ambiguity` (from the answering
  providers' pages, as `fts` reports them), `recall` (`RecallLabels.as_dict()`) and
  `recall_metrics` (`RecallMetrics.as_dict()`). `recall` and `recall_metrics` never contain
  the query or body text, and provider exception messages never appear, only fixed codes.
- **`degraded`.** Clients read the top-level `degraded`. It is true when any provider or
  resolver degraded (`recall.degraded`, `recall.resolvers_degraded`), the call failed closed
  (`recall.fail_closed`), or a provider's page said it was degraded. `recall` says which.
- **No ontology alias expansion** in this mode yet: `concepts/` aliases add no rows. (FTS
  still reports `ambiguity` for the query itself.)
- **Parity.** With no embedder and `policy=compat_fts`, `two_stage` returns the same rows as
  `mode=fts` (same ids, order and every field except `score`, plus `providers` and
  `recall_labels`), and the same `errors` and `ambiguity`, across `source`,
  `allowed_sources`, `include_history` and `as_of` (`tests/test_mcp_two_stage.py`). With an
  embedder, the hybrid provider is fused in and the order differs by design.

## 5. Adapter rules

From the compat framework spec, §5:

1. **No vendoring.** The external project runs as a separate process or service (MCP, HTTP,
   CLI), or is an optional extra (`memfw[name]`). Its code never goes into `src/memfw/`.
2. **One module per adapter** at `src/memfw/adapters/<name>/`, depending only on the port
   protocols and the external project's public interface. If the project is missing, the
   adapter reports itself unavailable; the kernel is unaffected.
3. **Contract tests.** Recorded request/response samples with a pinned external version, for
   example `tests/fixtures/<name>_contract/`. Run them before upgrading the external project.
4. **Default off.** With the adapter off, existing output is byte-identical. Enabling it needs
   an evaluation, a shadow period and maintainer approval.
5. **License and source.** Only source-available projects under compatible licences (MIT,
   Apache-2.0, BSD and similar), recorded in `NOTICE` or THIRD_PARTY. Never a package without
   visible source.
6. **Security.** Endpoints come only from config or environment, never from a request body
   (SSRF). External responses are data. An external system's own public entry points must
   not bypass memfw filtering.

## 6. Registry

```python
AdapterSpec(
    name: str,
    kind: AdapterKind,                 # "recall" | "projector" | "ingestor" | "deriver"
    factory: Callable[[Mapping[str, Any]], object],
    requires: tuple[str, ...] = (),    # importable modules of the optional extra
    version: str = "",                 # pinned external version
    source: str = "",                  # where the external project lives
    license: str = "",
)
```

Specs come from `register(spec, *, replace=False)` on `default_registry()`, or from
`AdapterRegistry.discover()`, which reads the entry-point group `memfw.adapters`
(`ENTRY_POINT_GROUP`). **Discovery imports nothing**: it records entry points by name, and
`listing()` returns `AdapterInfo(name, origin, value, distribution, kind)` from metadata
only. An entry point is imported the first time `load` asks for it while it is enabled, so a
disabled adapter's code never runs. Loading, building and `available()` are guarded: any
failure, even `SystemExit`, becomes a status reason; only `KeyboardInterrupt` propagates. The
entry point resolves to an `AdapterSpec` or a zero-argument callable returning one, and its
name must equal the spec name.

```toml
[project.entry-points."memfw.adapters"]
example-http = "memfw_example_http:SPEC"
```

Every adapter is off until `AdaptersConfig.from_mapping({"enabled": [...], "<name>": {...}})`
enables it; each `"<name>"` mapping is that adapter's settings. Settings reach a factory only
from this mapping, never from a request body. A malformed `enabled` turns everything off. `registry.load(name, config)` returns
`AdapterStatus(name, kind, enabled, available, reason, adapter)`, with `adapter` set only when
usable; `load_enabled(config, kind=None)` loads every enabled name.

| Reason | Meaning |
|---|---|
| `unknown_adapter` | no spec or entry point by that name (`bad_name`, `duplicate` from discovery) |
| `load_error` | importing the entry point raised (anything but `ImportError`) |
| `not_a_spec` | the entry point did not produce an `AdapterSpec` |
| `disabled` | not in `enabled` |
| `missing_dependency` | a module in `requires` is missing, or import/factory raised `ImportError` |
| `factory_error` | the factory raised anything else |
| `protocol_mismatch` | the object does not satisfy the protocol for `kind` |
| `name_mismatch` | `adapter.name` (or the loaded spec's name) differs from the adapter name |
| `availability_error` | `available()` raised |
| adapter's own code | `available()` returned false (`unavailable` if no reason) |

## 7. How to write an adapter

A recall adapter for an external HTTP search service that returns paths and scores. An MCP
JSON-RPC client has the same shape.

```python
# src/memfw/adapters/example_http/__init__.py
from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from memfw.ports import AdapterSpec, Availability, Candidate, ProviderUnavailable, RecallBudget

NAME = "example-http"


class ExampleHttpProvider:
    lexical = False

    def __init__(self, endpoint: str, *, collection: str) -> None:
        self.name = NAME
        self.endpoint = endpoint
        self.collection = collection
        self.projection_version = ""

    def available(self) -> Availability:
        if not self.endpoint.startswith(("http://", "https://")):  # cheap, no network
            return Availability.missing("no_endpoint")
        return Availability.ok()

    def search(
        self, query: str, *, limit: int, budget: RecallBudget, tenant: str,
        as_of: datetime | None,
    ) -> Sequence[Candidate]:
        data = json.dumps({"query": query, "limit": limit, "collection": self.collection})
        request = urllib.request.Request(
            self.endpoint, data=data.encode(), headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(request, timeout=budget.timeout_seconds) as response:
                payload = json.load(response)
        except (urllib.error.URLError, TimeoutError) as exc:
            raise ProviderUnavailable("unreachable") from exc
        rows = payload.get("results") if isinstance(payload, dict) else None
        out: list[Candidate] = []
        for row in rows if isinstance(rows, list) else []:
            try:  # the response is data: skip rows that do not validate
                out.append(Candidate(
                    ref=str(row["path"]), ref_kind="path", score=float(row["score"]),
                    provider=self.name, rank=len(out) + 1,
                    extra={"kind": "qmd"},
                ))
            except (KeyError, TypeError, ValueError):
                continue
            if len(out) >= limit:
                break
        return out


def _factory(settings: Mapping[str, Any]) -> ExampleHttpProvider:
    endpoint = str(settings.get("endpoint", ""))  # from config only, never a request
    return ExampleHttpProvider(endpoint, collection=str(settings.get("collection", "memory")))


SPEC = AdapterSpec(
    name=NAME, kind="recall", factory=_factory,
    version="1.4.2", source="https://example.org/example-search", license="Apache-2.0",
)
```

The service cannot filter by tenant or `as_of`, so the adapter ignores both and the
coordinator filters. Nothing from the response body goes into `extra`. Path refs resolve only
when they look like `memories/<id>.md` until the R6 PR-1 path resolver is plugged in.

Register it in-process with `register(SPEC)`, or ship it as a distribution with the entry
point from §6 and call `default_registry().discover()`.

**Contract test sketch.** Record one real exchange against the pinned version and replay it:

```python
# tests/test_example_http_contract.py  (imports: io, json, Path, example_http, memfw.ports, memfw.ports.testing)
FIXTURE = Path(__file__).parent / "fixtures" / "example_http_contract" / "search.json"

def test_search_matches_recorded_contract(monkeypatch):
    recorded = json.loads(FIXTURE.read_text())
    assert recorded["external_version"] == example_http.SPEC.version
    sent = {}
    def fake_urlopen(request, timeout):
        sent["body"] = json.loads(request.data)
        return io.BytesIO(json.dumps(recorded["response"]).encode())

    monkeypatch.setattr(example_http.urllib.request, "urlopen", fake_urlopen)
    provider = example_http.SPEC.factory({"endpoint": "http://127.0.0.1:1/search"})
    assert isinstance(provider, RecallProvider)
    found = check_recall_provider(provider, queries=["q"], limit=5)["q"]
    assert sent["body"].keys() == recorded["request"].keys()
    assert [c.ref for c in found] == recorded["expected_refs"]
```

### 7.1 The contract kit

`memfw.ports.testing` is stdlib-only and works with any test runner. Each check raises
`ContractError` (an `AssertionError`) on the first rule it finds broken:

| Check | Asserts |
|---|---|
| `check_recall_provider(provider, *, queries, limit=5, tenant="", as_of=None, budget=None, require_available=True, vault=None, vault_exclude=(), tenant_of=None, error_queries=(), budget_allowance=2.0)` | protocol and name; plain `Candidate` values passing `validate_candidate` (no subclass, no smuggled `extra`/`ref`); this provider's name; ranks 1..n; no ref twice; at most `limit`; two calls return the same refs; each call within `timeout_seconds * budget_allowance + 0.5 s`; only `Exception` subclasses raised, and only for `error_queries`; with `tenant_of`, every ref belongs to `tenant`; vault byte-identical when `vault` is given |
| `check_projector(projector, *, vault, out, policy, vault_exclude=())` | vault unchanged; `Manifest` names the projector and policy version; listed files exist; two exports have the same `content_sha256` and count |
| `check_ingestor(ingestor, *, vault, items, proposal_id=..., source=...)` | refuses an empty proposal id or source without writing; writes only under `_candidates/`; a second run stages nothing new and returns the same ids |
| `check_deriver(deriver, *, vault, request, vault_exclude=())` | vault unchanged; `DerivedView` is read-only, names the deriver and kind, and cites only requested memories |
| `check_clock(clock, *, samples=32)` | `Clock` protocol; every `now()` is a timezone-aware `datetime`; no sample earlier than the previous one |
| `check_id_gen(id_gen, *, samples=1000)` | `IdGen` protocol; every `new_id()` is a `str` valid as a memory id; no id repeated |

`tree_digest(root, *, exclude=())` is the helper they use to compare file trees.

**Building a coordinator with it.**

```python
from memfw.adapters import example_http
from memfw.ports import AdaptersConfig, RecallBudget, default_registry, register
from memfw.recall import (
    Coordinator, FilterPolicy, ProviderConfig, parse_clearance,
)
from memfw.recall.builtin import FtsProvider, default_resolvers

register(example_http.SPEC)
config = AdaptersConfig.from_mapping(settings_from_config_file["adapters"])
status = default_registry().load("example-http", config)

providers = [FtsProvider(root)]
configs = {"fts": ProviderConfig(weight=1.0, budget=RecallBudget(timeout_seconds=1.0))}
if status.available:
    providers.append(status.adapter)
    configs["example-http"] = ProviderConfig(
        weight=0.8, budget=RecallBudget(timeout_seconds=1.5, max_candidates=50), overfetch=2
    )

coordinator = Coordinator(providers=providers, resolvers=default_resolvers(root), configs=configs)
policy = FilterPolicy(tenant="", privacy_clearance=parse_clearance(clearance_setting))
result = coordinator.search("nas1 disk migration order", limit=10, policy=policy)
result.ids                # fused, filtered memory ids
result.labels.as_dict()   # route, degraded providers, fail_closed, ...
result.metrics.as_dict()  # counts and timings, no query text
```

Add a `ProviderConfig` only for providers you pass in; a config for an unknown provider
raises. If the adapter is disabled or unavailable, the coordinator runs with FTS alone and
the result is the same as without it.
