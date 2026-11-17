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

- **`memfw.ports` is the socket.** It imports only the standard library and ships as the
  standalone `memfw-ports` wheel (below) that adapters depend on without depending on memfw.
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
therefore compares against a baseline taken after `import memfw`. The standalone wheels below
do not contain `memfw/__init__.py`, so this does not affect them.

**Standalone wheels (A5b).** Two uv workspace members build wheels from these same sources:

| Distribution | Import name | Contents | Depends on |
|---|---|---|---|
| `memfw-ports` (`packages/memfw-ports`) | `memfw_ports` | all of `src/memfw/ports`, `reference` included | nothing |
| `memfw-recall` (`packages/memfw-recall`) | `memfw_recall` | `src/memfw/recall` without `builtin.py` | `memfw-ports` (same version) |

- **One copy of the source.** Each wheel maps memfw's own files to a new top-level name with
  hatch `force-include`; nothing is copied into `packages/`. Every import inside
  `memfw.ports` is relative, so the files work as `memfw.ports.*` and as `memfw_ports.*`.
  An adapter written against the wheel changes only the first name:
  `from memfw_ports.testing import check_recall_provider`.
- **The recall seam.** `memfw.recall` takes every port name through `memfw.recall._ports`.
  When recall sits inside a parent package (`memfw.recall`, or a vendored
  `<vendor>.memfw.recall`), it imports the sibling `..ports` relatively; inside memfw that is
  `memfw.ports`, the same objects as before (a test checks identity). When recall is a
  top-level package (`memfw_recall`), it imports `memfw_ports`. The choice follows the
  module's own name, not what is installed. Type checkers always see the relative branch, so
  in a venv without memfw the seam's names are untyped (`Any`). No other shipped module may
  climb above its wheel's top-level package (`from ..x` in a recall module, `from ...x` in
  `ports/reference`); a test checks every file.
- **One process, one copy.** Inside a memfw process, use `memfw.ports`. `memfw_ports` is a
  second copy of the same modules: its classes are distinct, so `isinstance` and `is` never
  match across the two, and an adapter built on `memfw_ports` types is for memfw-less use
  (contract tests, a service that does not load memfw). memfw itself never imports
  `memfw_ports`.
- **Not shipped.** `memfw.recall.builtin` (vault resolver, FTS/multihop/hybrid providers) is
  memfw-internal. The recall wheel lists its files one by one, so a new recall module stays
  out until it is added; `tests/test_standalone_ports_wheel.py` fails until it is.
- **memfw does not depend on the wheels.** It still ships `src/memfw/ports` and
  `src/memfw/recall` itself; `import memfw.ports` and every memfw output are unchanged. Making
  memfw depend on `memfw-ports` (and re-export it) is a later step (architecture section 6).
- **Versions.** Both wheels carry `memfw.ports.PORTS_API_VERSION` (semver, `0.3.0` since K3 added `ProjectionStore`), and
  `memfw-recall` pins `memfw-ports` to it; a test keeps the three equal. Bump it with every
  change to the ports API (major for a breaking one). The members pin their build backend
  (`hatchling==1.33.0`) and ship memfw's `LICENSE` and `NOTICE` (symlinks in each package
  directory) under `.dist-info/licenses/`.
- **Compatibility table.** Each package version and the contract version (`PORT_VERSION`,
  `memfw.ports.PORT_VERSIONS`) of every port it ships or builds on. An adapter checked
  against a port's version stays compatible while that number is unchanged.
  `tests/test_port_versions.py` checks this table against `PORT_VERSIONS` and the package
  metadata; update both together.

<!-- port-versions:start -->
| Package | Version | Port | PORT_VERSION |
|---|---|---|---|
| `memfw-ports` | 0.3.0 | `RecallProvider` | 1 |
| `memfw-ports` | 0.3.0 | `Projector` | 1 |
| `memfw-ports` | 0.3.0 | `Ingestor` | 1 |
| `memfw-ports` | 0.3.0 | `Deriver` | 1 |
| `memfw-ports` | 0.3.0 | `Clock` | 1 |
| `memfw-ports` | 0.3.0 | `IdGen` | 1 |
| `memfw-ports` | 0.3.0 | `ClaimVerifier` | 1 |
| `memfw-ports` | 0.3.0 | `ClaimStager` | 1 |
| `memfw-ports` | 0.3.0 | `ContextSlotProvider` | 1 |
| `memfw-ports` | 0.3.0 | `UsageSink` | 1 |
| `memfw-ports` | 0.3.0 | `ModelBackend` | 1 |
| `memfw-ports` | 0.3.0 | `ProjectionStore` | 1 |
| `memfw-recall` | 0.3.0 | `RecallProvider` | 1 |
| `memfw-adapter-deepseek` | 0.1.0 | `ModelBackend` | 1 |
<!-- port-versions:end -->

- **Wheels only.** `uv build --wheel --package memfw-ports` (and `memfw-recall`). An sdist is
  not supported because the sources live outside the package directory. Editable installs of
  the members are not supported either; `uv sync` installs memfw alone, as before.
- **Proof.** `tests/standalone/wheel_smoke.py` runs, in a venv without memfw, every reference
  implementation through its `memfw_ports.testing` check (recall provider, both projectors,
  both ingestors, both derivers, clocks, id generators, claim verifier and stager) and a
  `memfw_recall.Coordinator` search with tenant and privacy filtering. The CI job
  `standalone-wheels` builds both wheels, installs them with `--no-index` into a fresh venv and
  runs it. Locally: `MEMFW_STANDALONE_WHEELS=1 pytest tests/test_standalone_ports_wheel.py`
  (needs `uv`); the static checks in that file always run.

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
| `SystemClock(source=...)` | `datetime.now(UTC)` (as `memfw.core.models.utc_now`), made monotonic: if the wall clock steps back it repeats the last value it returned (time stalls) until the wall clock catches up |
| `FixedClock(at)` | returns `at` (timezone-aware) until `advance(delta)` or `set(at)`; never moves backwards |
| `UuidIdGen()` | `str(uuid.uuid4())`, the default `Repository` `id_factory` |
| `SeededIdGen(seed, *, start=0)` | `sha256(f"{seed}:{n}")[:32]` for n = start+1, start+2, ...: the `memfw --id-seed` scheme; to continue an existing vault's sequence pass `start = 2 * (non-empty lines in events.jsonl)`, as the CLI does |

`clock.now` and `id_gen.new_id` are the zero-argument callables `Repository(clock=...,
id_factory=...)` already accepts. Core does not use the ports yet; A3 (composition root) wires
them in. `check_clock(clock, *, samples=32)` and `check_id_gen(id_gen, *, samples=1000)` in
`memfw.ports.testing` are the contract: aware and monotonic times; distinct `str` ids matching
`^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$`.

### 2.6 UsageSink (C5)

`memfw.ports.usage` is the first event kind of the architecture's `EventSink` port: the
`memory_used` signal (§4.5). A `UsageSignal(memory_ids, session_ref, app, at)` holds 1 to
`MAX_USAGE_IDS` (100) distinct memory ids, an opaque session token (`SESSION_REF_RULE`:
letters, digits and `_ . : @ + = -`, at most 200 characters, no spaces), the calling app
(`""` or a lower-case app name) and a timezone-aware time; anything else is refused at
construction. There is no field for query or body text.

```python
class UsageSink(Protocol):
    name: str
    def record(self, signal: UsageSignal) -> None: ...   # TypeError for a non-signal
```

A sink writes only its own store, never a memory, `events.jsonl` or another vault file.
Implementations: `memfw.usage.SqliteUsageSink` (persistent), `NullUsageSink` (drops; the
wiring when the signal is off) and `MemoryUsageSink` (in memory) in `memfw.ports.reference`.
`check_usage_sink(sink, *, signals, vault=None, vault_exclude=(), stored=None)` is the
contract: `record` returns `None` (twice per signal), refuses non-signals with `TypeError`,
leaves `vault` unchanged, and with `stored` (the sink's rows) keeps every id and no string
that is not a signal's id, session ref, app or ISO time. `USAGE_PORT_VERSION = 1`; the ports
API moves to `0.2.0`.

### 2.7 ModelBackend (conversation worker)

`memfw.ports.models` holds the contract between the conversation worker and a model:
`ModelBackend` (`name`, `local`, `run(task, data, *, max_tokens) -> ModelReply`, plus the
optional `prompt_version` and `tasks`), `ModelReply`, the errors a backend raises to stop a
run (`BackendUnavailable(code)`, `BackendBudgetExhausted(code)`) and the `conv-v2` tag schema
(`render_tag_prompt`, `parse_tagged`, `normalize_tags`). The worker redacts before the call
and validates plus redacts again after it; a backend only moves text. Backends outside memfw
(`packages/adapters/memfw-adapter-deepseek`) import this module and nothing else of memfw.
`MODELS_PORT_VERSION = 1` (in `PORT_VERSIONS`; it ships in the same `0.2.0` ports API as
`UsageSink`). `check_model_backend(backend)` in `memfw.ports.testing` is the contract: every accepted task
answers one JSON object (a `conv-v2` tag task matches the tag schema), token counts are
non-negative integers, the input is not changed, and a task outside `tasks` raises. See
`docs/conversations-share.md` for the DeepSeek adapter.

### 2.8 ProjectionStore (K3) and path patterns (K4)

`memfw.ports.projection_store` holds the contract for a rebuildable derived store
(architecture section 4: `rebuild()`, `version()`, `query()`). Version 1 carries the K3
typed-edge projection: rows are `Edge` values (relation, both ends with their kind, tenant and
privacy level, the vault field, validity and the relation's `epistemic_max`). `rebuild(edges)`
replaces the whole content (duplicate keys or non-`Edge` values raise and keep the old content),
`version()` is `None` before the first rebuild and then `edge_set_version(edges)` (the same in
every implementation), and `query(scope, *, node, direction, relations, limit)` returns the edges
at `node` that `edge_visible` admits for an `EdgeScope(tenant, clearance)`, sorted by key. The
clearance is required: a store can never be asked for everything, a private or `unknown`
endpoint hides the edge, and another tenant's endpoint never shows.

Implementations: `memfw.edges.SqliteProjectionStore` (`~/.local/share/memfw/edges/<key>.sqlite3`, outside the vault, mode 0600) and
`MemoryProjectionStore` in `memfw.ports.reference`. `check_projection_store(store, *, vault=None,
vault_exclude=(), edges=SAMPLE_EDGES)` is the contract (answers equal the reference `select` for
every node, direction and `SAMPLE_SCOPES` scope; deletion on rebuild; refusals).
`PROJECTION_STORE_PORT_VERSION = 1`; the ports API moves to `0.3.0`.

`memfw.ports.patterns` (K4) parses a bounded path (`about>broader*1..2><about`) and matches it
over any `ProjectionStore` with Cypher-like rules and hard caps (`MAX_DEPTH` 4 hops,
`MAX_FANOUT`, `MAX_LIMIT`, `MAX_EXPANSIONS`), so every store gives the same paths for the same
edges. See `docs/ontology.md` (K3, K4).

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

**Read-only.** No built-in provider appends an event or warms a memory. `HybridProvider`
builds its retriever with `record_access=False` and `autowarm=False`, so neither its
hybrid/vector hits nor the retriever's internal FTS fallback write `memory_access`,
`tier_change` or `memory_restore` events. On first use it may build or refresh the disposable
`.memory-hybrid.sqlite` index (and, with entities on, `.memory-entities.sqlite`): an index
projection, not an event. Access is recorded by the caller for what it returns (§4.1).

**`prepare`.** `FtsProvider` and `MultihopProvider` accept `prepare: Callable[[Repository],
None]`, run before each search, so the wiring PR can pass the MCP server's index staleness
check. Without it, a missing `index.sqlite3` makes the provider unavailable.

All three built-in providers pass `memfw.ports.testing.check_recall_provider` (see §7.1).
`memfw.ports.reference.InMemoryRecallProvider` (stdlib BM25-lite over `{memory_id: text}`, or
`from_vault(root)`) passes the same check and can stand in for any of them; see the swap
drills in `docs/architecture.md`.
`tests/test_recall_builtin.py` holds the equivalence guard for the wiring PR. A coordinator
with only `FtsProvider` and `FilterPolicy.compat_fts(...)` returns the same ids, in the same
order, as `Gateway.search(mode="fts")`. This is checked across tenants, limits, `source`,
`include_history`, `as_of` and `allowed_sources`.

### 4.1 MCP `memory_search` `mode=two_stage`

`mode=two_stage` runs the query through a `Coordinator` and builds the rows from the fused
hits. It is opt-in; nothing else changes. The wiring (providers, resolvers, policy choice,
`InflightRegistry`, the coordinator call) lives in the composition root,
`memfw.runtime.Runtime.recall` (A3a); the MCP server validates the input, refreshes the
index and builds the rows.

- **Providers.** `FtsProvider` always; `HybridProvider` (`mode=hybrid`) only when the server
  has an embedder (`MEMFW_EMBEDDING_URL`). Both get the server's `allowed_sources`, the
  call's `source`, `include_history` and (FTS only) `scope`. Each provider may take 3 s;
  all providers and resolvers together get **4 s** (the total deadline). The index staleness
  check (`Runtime.ensure_index`) runs before the deadline starts. Resolvers are `default_resolvers(root)`.
- **Warming and access events.** After the filter and fusion, `Runtime.recall` warms each
  returned hit that is cold or a stub and then appends one `memory_access` event
  (`operation: two_stage_retrieve`), per returned hit, in hit order. A candidate the policy
  drops (privacy, tenant, source, validity, lifecycle) is never warmed or access-logged, so
  it feeds no strength, reinforcement or tier signal. Best effort: a failure keeps the hits,
  sets `degraded` and adds the error `access_not_recorded`. Known race: rows are built after
  `recall` returns and each hit is re-read from the vault; a hit removed or changed in
  between is logged but not shown. Logging follows the coordinator's output.
  A hit warmed by this call is marked `stale` on that call: its row comes from the
  provider's pre-warm index row, and warming changed the file (tier, version). The next
  call's index refresh reports it fresh. Direct `mode=hybrid` marks a warmed hit `stale`
  the same way.
- **One thread per hung provider.** Every call on one `Runtime` (one Runtime per Gateway)
  shares its `InflightRegistry`, so `max_inflight` (1) holds across calls: a provider that hangs holds
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
  labels, then the open-question labels below). `score` is the fused RRF score, so it is not
  comparable with `mode=fts` scores.
- **Open-question labels (U2).** `open_question:<id>` for each unresolved open question
  whose `about` shares a concept with the hit's `about` (sorted by id; a question never
  labels itself), then `conflicted` when such a question lists the hit in its `conflicts`.
  A question counts only when it has no `resolved_by`, no `valid_to`, is not superseded,
  and the call's `FilterPolicy` keeps it (tenant, source, `as_of`; `strict` adds privacy,
  status, lifecycle and archive), so a question in another tenant or one the policy drops
  never shows up in a label. A question with `valid_to` is never shown, even for an earlier
  `as_of` (conservative). `scope` changes no label (as in `mode=fts` it only selects scoped
  alias labels; labels match concept ids). The pure function is
  `memfw.recall.open_question_labels` over an `OpenQuestionIndex`;
  `memfw.recall.builtin.open_question_index` builds it per call from the caller's
  `Repository`: candidate ids come from the search index (`memory_access`, type
  `open-question`, the policy's tenant; a scan of `memories/` only when the index cannot be
  read), and each candidate is parsed from the vault and judged by the policy, never by index
  columns. `Runtime.recall` appends the labels to the coordinator's hits, judged at the
  coordinator's `now`; an empty page reads nothing. The other modes get the same
  labels, with the `compat_fts` policy they filter by, only with `labels: true`
  (`Runtime.open_question_labels`): every row then has `recall_labels`. Without the flag
  their output is byte-identical; without a visible open question `two_stage` is too. An open
  question is itself recalled like any memory, with `type: open-question`. The tool
  description tells agents that either signal means "unknown: say so, don't guess".
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

### 4.2 MCP `memory_context` `v=2` (ContextPack)

`memory_context` is the session-start (CAG) context: anchors first, then procedural
memories, by id, cut to a character budget (`limit_tokens * 4`, at least 100 characters).
The slot logic lives in the capability package `memfw.context` (L3), which imports only
`memfw.core` and `memfw.ports`: who may see a memory is injected as a `keep` predicate,
because an L3 capability may not import another (`memfw.recall`). The composition root
`memfw.runtime` builds `keep` (`Runtime.context_keep`) and the pack (`Runtime.context_v1`,
`Runtime.context_pack`, which take the caller's per-call `Repository`); the MCP tool only
parses arguments, asks the Runtime and serialises. Without new arguments (or with `v=1`) the payload is byte-identical
to earlier versions: `limit_tokens`, `used_chars`, `entries`.

`v=2` returns a `ContextPack` (`memfw.ports.context`, stdlib only) as JSON:

```json
{"v": 2, "limit_tokens": 2000, "pack_version": "<64 hex>", "policy": "compat_fts",
 "generated_at": "2026-11-16T09:00:00+00:00",
 "slots": [{"name": "core", "entries": [{"id": "...", "title": "...", "type": "procedural",
   "layer": "current", "anchor": null, "epistemic": null, "causal": "none", "text": "...",
   "version": 1, "content_hash": "sha256:..."}],
   "used_tokens_est": 12, "overflow": 0, "truncated_ids": [], "skipped": 0}]}
```

- **One slot, `core`,** with the same selection, order and text as v1 under `compat_fts`.
  `content_hash` is the sha256 of the memory file, the value `memory_search` reports.
  `used_tokens_est` is the characters carried divided by four, rounded up. The budget never
  drops below 100 characters, so with a very small `limit_tokens` (under 25) `used_tokens_est`
  can exceed `limit_tokens`.
- **One read per entry.** Each selected memory's file is read once more and its entry (body,
  `version`, `content_hash`) is built from those bytes, then selected and ordered again, so a
  concurrent write never pairs one version's text with another's hash. `skipped` counts
  selected memories the pack cannot represent: an id outside the memory id pattern (a
  hand-placed `memories/a.dot.md`, which v1 still returns), an unreadable or unparsable file,
  or one that no longer qualifies when re-read. They are not overflow. `skipped` is the
  difference between the two selections, so it also counts a memory that changed between the
  two reads (retagged, or moved out of the slot).
- **`policy`** (v=2 only). `compat_fts` (default) applies v1's filters, tenant and source
  (`FilterPolicy.compat_fts` with `include_history`, since the slot keeps v1's own rule that a
  memory with `valid_to` is never resident). `strict` is the default `FilterPolicy`: privacy
  clearance `none` (`local-private` and unrecognised privacy are dropped), status, lifecycle
  (`lifecycle:retired` and other non-active tags), supersession, validity and archive (cold
  tier or stub), evaluated with `memfw.recall.policy.evaluate` on `meta_from_document`, so it
  uses recall's fail-closed tag reading (any case, strictest tag wins: `Sensitivity:Private`
  and `sensitivity:secret` are dropped too). `compat_fts` is v1's own visibility check. Absent,
  `null` and `""` mean `compat_fts`; a non-string or unknown value is an error, and so is a
  `policy` other than `compat_fts` with `v=1`. `v` must be the integer 1 or 2. These are new
  errors for v1 callers: a v1 call that passes `policy` (other than `compat_fts`), or a `v`
  that is not the integer 1 or 2, used to have the argument ignored and now fails.
- **Overflow.** `overflow` is how many selected memories the slot did not carry in full and
  `truncated_ids` names them in order: the memory cut part-way (its entry is the last one
  carried, with a prefix of the body) first, then every memory left out once the budget ran
  out. Fetch them with `memory_get` or `memory_search(mode=two_stage)`. Memories the policy
  dropped are not overflow, and a memory with an empty body is never counted.
- **`pack_version`** is a sha256 over a canonical JSON encoding (`memfw-context-pack/1`) of the
  policy name and, per slot, its name, each entry's `[id, version, content_hash, chars]`
  (`chars` = characters carried, so two cuts of the same memory differ) and `truncated_ids`.
  No body text, title or query goes in (`pack_version_payload` returns the exact bytes), and
  `generated_at` is not part of it. The key changes when a carried memory changes (body, version
  or hash), when the selection or cut changes, or with the policy; it does not change when an
  unrelated memory does. Because `content_hash` covers the whole file, a frontmatter-only
  change (`updated_at`, `strength`, `reference_count`, ...) also changes the key, even when the
  carried text is the same: expect that churn when caching (C3). `ContextPack` rejects a
  `pack_version` that does not match its slots.
  The design also named a projection version; C3 checked that none is needed: every slot is
  built from what it reads at call time (memory files, concept files, the conversation share)
  and each entry's hash covers that content, so a projection rebuild that changes nothing a
  slot carries leaves the key as it is, and one that does changes it (§4.4).
- **Contract kit.** `check_context_pack(pack, *, slots=None, max_tokens=None, forbidden=())`
  (in `memfw.ports.testing`) checks the type, the recomputed version, `overflow ==
  len(truncated_ids)`, at most one partial entry, which must be the last carried entry and be
  listed in `truncated_ids` (their order is the producer's; `core` uses selection order), the
  token cap, that no string in the version payload contains an entry's text or title (8+
  characters, other than one equal to an id, slot name or the policy) or any `forbidden`
  string, and that `as_dict()` is JSON serialisable (else `ContractError`). A `forbidden` string
  under 8 characters is a usage error (`ValueError`).

### 4.3 Context slots and resources (C2)

`memory_context(v=2, slots=[...])` builds the named slots and returns them in that order;
`slots` defaults to `["core"]` (output byte-identical to C1), applies to `v=2` only, and must
be a non-empty list of known names without repeats (`unknown slot (known: core, handoff,
topics)` otherwise; the input is not echoed).

| Slot | Entries | Source |
|---|---|---|
| `core` | `ContextEntry` (memories, §4.2) | the vault |
| `handoff` | `ContextItem` `kind: session`, `id: conversation:<app>/<session_id>`, `label: <app> <updated_at>`, `text`: redacted goal (≤ 280 chars), `count`: turns, `refs`: latest turn refs | `ConversationShare` as the calling app (the 3 latest visible sessions) |
| `topics` | `ContextItem` `kind: topic`, `id`: concept oid, `label`: `pref_label`, `count`: memories, no text | active, unmerged concepts; memories the policy keeps with no `valid_to` that name the concept (or one merged into it) in `about`/`applies_to`; only concepts with at least one |

- **Share rules.** `handoff` reads only through `ConversationShare.list_sessions` and
  `handoff`, so `conversation_share` applies: hidden and opted-out apps never appear,
  `can_read` narrows. No app, the reserved `operator`, sharing off or a caller that may not
  read give an empty slot (not degraded); a missing store gives `degraded: unavailable`.
- **Budget.** Each slot gets the `limit_tokens` budget on its own. Items are never cut
  part-way: an item that does not fit, and every one after it, is overflow (`truncated_ids`
  holds item ids).
- **Default request.** `slots` absent or exactly `["core"]` builds core inline in the
  caller's thread with C1's semantics: a vault error (a pending transaction, say) is the
  tool error it always was, byte for byte, never a degraded slot.
- **Degrade.** For any other request, providers run in parallel threads, each with a deadline
  (`Runtime.context_slot_timeouts`: `core` 10 s, `handoff` and `topics` 3 s, from the start
  of the call). A provider that raises (`error`; the message is never returned), a missing
  store (`unavailable`), a missed deadline (`timeout`), another slot returned (`invalid`)
  two builds of that slot still running from earlier calls (`busy`), or a worker thread that
  cannot start (`error`, its in-flight count released) give an empty slot with
  `degraded: <reason>`. Vault errors get their own codes: a pending transaction
  (`pending_recovery`, run `memfw recover`), `IntegrityError` (`integrity`),
  `NotFoundError` (`not_found`), any other `MemfwError` (`vault_error`). Each degrade is
  logged at WARNING on `memfw.context.slots` with the slot, the reason and the exception's
  class name only (never its message); the other slots are returned as usual. `degraded` appears in
  `as_dict()` and in the `pack_version` payload only when set, so a healthy slot encodes
  exactly as in C1.
- **`pack_version`** covers every slot in order. An item contributes `[id, kind,
  content_hash, chars]`, where `content_hash` is `item_content_hash` (sha256 over kind, id,
  label, text, count and refs); the payload itself still holds no label or text.
- **Ports.** `ContextSlotProvider` (`name`, `build(SlotRequest) -> ContextSlot`; must not
  write) and `SlotRequest(limit_tokens, policy, now)` are in `memfw.ports.context`.
  `memfw.context` has the providers and `assemble_pack`; the `handoff` and `topics`
  providers take a `SessionFeed` / `TopicCatalogue` that the Runtime builds
  (`memfw.runtime.context_sources`), so `memfw.context` imports no other capability.
- **MCP resources.** `resources/list` lists `memfw://context/core`, `/handoff` and `/topics`
  (`application/json`). `resources/read {"uri": ...}` returns one `contents` item whose
  `text` is that slot's v=2 payload (`pack_version` included), for the same caller (app,
  tenant, sources) and the default budget. The policy is `?policy=compat_fts|strict` when
  given (`memfw://context/core?policy=strict`); without it, `topics` uses `strict` (so its
  counts never include local-private memories) and `core` and `handoff` use `compat_fts`.
  Any other query (another key, an unknown or empty value, a repeat, a fragment) is
  `-32602` (fail closed). An unknown URI is error `-32002`, a missing or non-string `uri`
  `-32602`, a failure `-32603` with the client-safe message.
  `initialize` advertises `capabilities.resources = {"listChanged": false}`.

### 4.3a The `current` slot (U6, off by default)

`current` is short-lived resident context, kept apart from `core` (identity and standing
rules): the active ticket, today's meeting, this week's task. It is quickly replaced, so every
entry has a lifetime.

- **Switch.** `vault.yaml` `runtime: {context: {current: true}}` (no environment variable;
  `build_runtime(context_current=...)` overrides it). Read once with the other `runtime:`
  switches (§4.5); a value that is not a boolean is off. Off (the default): `current` is an
  unknown slot exactly as before (`unknown slot (known: core, handoff, topics)`),
  `tools/list` is `TOOLS` byte for byte, `resources/list` is the three C2 resources and
  `memfw://context/current` is `-32002`. On: the `memory_context` `slots` enum gains
  `current`, `resources/list` gains `memfw://context/current` (last), and the slot can be
  requested. It is never in the default slot list, so `memory_context` without `slots` (v1 or
  v2) is byte-identical either way.
- **Which memories.** Tagged `current` (any case; a tag, so no memory file or schema change;
  the `layer` field cannot be used because `current` is already every memory's default layer),
  not superseded, and valid at the call's `now`: `valid_from` absent or `<= now`; with
  `valid_to`, `now < valid_to`; without it, `now < created_at + current_ttl_days`. The TTL is
  `runtime.context.current_ttl_days` (an integer from 0 to 365, default 7); any other value
  fails closed to 0, so only memories with an explicit `valid_to` count.
- **TTL anchor.** `created_at`, which no write changes after `add`. `updated_at` is not used:
  mechanical writes (tier changes, autowarm, strength, privacy re-classing) refresh it and
  would bring an expired memory back. To keep a memory current longer, set its `valid_to`.
  A `created_at` more than 5 minutes after `now` (a skewed clock, a hand-edited date) would
  never expire, so such a memory is not current.
- **Timestamps.** `valid_from` and `valid_to` must include a timezone
  (`valid_to: 2026-11-20T00:00:00Z`). A naive value makes the memory file unparseable, and the
  slot skips it like any unreadable memory.
- **Visibility.** The call's `keep`, exactly as for `core`: tenant and sources (`compat_fts`),
  the recall strict policy (`strict`), and the MM0 clearance on both. The resource defaults to
  `compat_fts`, like `core`; `?policy=strict` narrows it.
- **Order and budget.** Newest first (`created_at` descending, then id). `ContextEntry` values
  with `core`'s character budget and overflow rules (at most one memory cut part-way, the rest
  in `truncated_ids`); each entry comes from one fresh read of its file and is checked again at
  the same `now`. Deadline 10 s, like `core`.
- **Not cached.** Validity depends on the clock, so a cached selection could serve a memory
  that expired after it was built. The slot is rebuilt on every call and never touches
  `ContextCache`. `pack_version` covers it like any slot, and an expiry changes the selection,
  so `if_version` then reports `unchanged: false`.

### 4.4 Context cache and `if_version` (C3)

- **`if_version`.** `memory_context(v=2, if_version="<pack_version>")` (and
  `memfw://context/<slot>?if_version=<pack_version>`, alone or with `policy=` in either
  order, joined by `&`) still builds the pack from the vault. When its `pack_version` is the
  one given, the reply is `{"v": 2, "limit_tokens", "pack_version", "policy", "generated_at",
  "unchanged": true}` with no `slots`; otherwise the full pack plus `"unchanged": false`.
  `if_version` must be 64 lowercase hex characters and applies to `v=2` only (checked before
  the vault is opened); `""` or absent means no check, and then the reply has no `unchanged`
  key, byte-identical to C2. In a resource query an empty, invalid or repeated `if_version`
  is `-32602`.
- **Cache.** `memfw.runtime.context_cache.ContextCache` (one per `Runtime`; inject one with
  `build_runtime(context_cache=...)`, disable with `context_cache=False`) holds, per `(tenant,
  policy, slot, scope)` (scope: vault root and allowed sources), only the ids and frontmatter
  versions of the `core` slot's pool (core-shaped memories, `is_core_shaped`, visible to the
  tenant and sources) and a vault fingerprint: no body, title or query. It is bounded (LRU,
  256 entries by default), per process and thread-safe; `stats()` reports hits, misses and size.
- **Invalidation.** The fingerprint is a sha256 over the stat (inode, size, mtime and ctime in
  nanoseconds) of every `memories/*.md`, `events.jsonl`, `vault.yaml` and the
  pending-transaction marker. Any vault write (it appends an event), a hand-placed, removed or
  edited memory file (ctime moves even when mtime is set back) or a config change
  (`conversation_share` included) makes the next call a miss, which reads the whole vault as
  before. A projection rebuild touches none of these and cannot change the `core` slot.
- **Hit.** The pooled memories are re-read from the vault and filtered again with the call's
  own policy at the call's `now` (a `strict` validity window that opens later is honoured, a
  memory made private is dropped); a pooled file that is missing, unreadable or at another
  version falls back to a full build. The slot is then built exactly as without the cache, so
  a hit returns the same pack and `pack_version` as a fresh build.
- **Not cached.** `handoff` and `topics` are rebuilt on every call: re-reading and re-filtering
  them is their whole build (the conversation share applies `conversation_share` per call;
  topic counts need every visible memory). `v=1` never uses the cache.
### 4.5 `memory_used` (C5) and no vault writes mid-session (C6)

**`memory_used(ids, session_ref)`** leaves a signal that a session used some memories (from
`memory_context` or `memory_search`). It is off by default and only an auxiliary signal:
whether a caller sends it depends on its prompt, so the C4 eval never relies on it alone.

- **Switch.** `vault.yaml` `runtime: {usage: {enabled: true}}`, or `MEMFW_USAGE_SIGNAL`
  (`1`/`true`/`yes`/`on`, `0`/`false`/`no`/`off`; a set variable wins; any other value refuses
  to start). The section is read on first use and kept once read; while the vault is missing
  or `vault.yaml` is unreadable it is read again on every use and both switches are closed
  (usage off, access not recorded), never cached. Off, or a value that is not a
  boolean: the tool is not in `tools/list` (exactly `TOOLS`, byte for byte), calling it is
  `-32601 unknown tool: memory_used`, and the Runtime's sink is `NullUsageSink`.
- **Arguments.** `ids`: a list of 1 to 100 strings; `session_ref`: a token as above. Wrong
  shapes are tool errors that depend only on the arguments.
- **Filtering.** Each well-formed id is read from the vault (`Repository.get`, which warms
  nothing). Ids that do not exist, cannot be read, are malformed, or fall outside the
  caller's tenant or allowed sources (the `memory_get` rule) are dropped. The answer is
  always `{"ok": true}`, also when the store cannot be written (best effort): its content
  does not say which ids were kept. This is not a hiding guarantee: response timing differs
  (a kept id costs a write), and `memory_get` already distinguishes a visible id from an
  unknown one.
- **Store.** Outside the vault, in memfw's local data directory and keyed by vault:
  `~/.local/share/memfw/usage/<key>.sqlite3` (`<key>`: the first 24 hex digits of sha256 of
  the vault's resolved path; `memfw.usage.projection_path`). So it never counts toward the
  physical-size budget that budget enforcement acts on, nor the transaction fingerprint,
  whatever the signal volume. The directory is 0700; the file is created 0600 with
  `O_EXCL`/`O_NOFOLLOW` (a planted symlink or non-database is refused, and the signal
  dropped) and kept 0600. One row per id: `memory_id`, `session_ref`, `app` (from
  `--app`/`MEMFW_APP`, lower case; `""` when unnamed), `at`. No query text, no body text.
- **Retention.** Each write prunes rows older than 90 days (`DEFAULT_MAX_AGE_DAYS`, from the
  new signal's time) and then the oldest rows beyond 100000 (`DEFAULT_MAX_ROWS`);
  `SqliteUsageSink(path, max_rows=, max_age_days=)` takes other limits. With the 100-id cap
  per call the file stays at a few MB.
  It is a projection in the sense that it is disposable and never authoritative: deleting it
  (`SqliteUsageSink.reset()`) loses only signals, and memfw rebuilds it empty on the next one.
  Nothing ranks or filters by it; `SqliteUsageSink.counts()` and `rows()` are for the C4
  coverage/waste eval. Usage-weighted ranking needs its own eval and stays off.
- **Wiring.** `memfw.runtime` (`Runtime.memory_used`, `Runtime.usage_sink`;
  `build_runtime(usage_signal=..., usage_sink=...)` injects either); the MCP layer only
  lists and routes the tool (`memfw.mcp.tools_usage`).

**No vault writes mid-session (C6).** During a session the vault is read, never written.
Session-end writes stay where they are: the M2 conversation worker runs afterwards and only
stages candidates under `_candidates/`, and `memory_remember` only stages a candidate;
neither appends to the event chain. Reads keep it as follows:

| Call | Writes the vault? |
|---|---|
| `memory_context` (v1, v2, every slot, `current` included) and `memfw://context/*` | never |
| `memory_search` `fts`, `multihop` | never |
| `memory_used` | never (writes only its store outside the vault) |
| `memory_search` `hybrid`, `vector` | by default yes: one `memory_access` event per returned hit, and a cold or stub hit is warmed (autowarm: `tier_change`/`memory_restore` events, the memory file, history and archive change). Off with `record_access: false` |
| `memory_search` `two_stage` | by default yes: the same, for each hit the coordinator returns after the filter (never for a hit the policy dropped; #55). Off with `record_access: false` |

The exception is access recording, which feeds strength and tier signals and has always been
part of those modes. The **session-safe setting** turns it off: `vault.yaml` `runtime:
{search: {record_access: false}}` or `MEMFW_RECORD_ACCESS=0`. Then no `memory_search` mode
appends an event or warms a memory (cold and stub hits are returned as they are), so every
call above leaves the memory files, `events.jsonl`, `events.state.json`, `vault.yaml` and
`_candidates/` byte-identical. A non-boolean value fails closed to `false`. Disposable
projections may still be built or refreshed during a session: `index.sqlite3`,
`.memory-hybrid.sqlite` (and its manifest), their SQLite side files and `.memfw.lock`. `tests/test_session_no_vault_writes.py` hashes the vault before and after a
session of every call above (both settings) and checks that the default exception is only
access and warming events.

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
| `check_context_pack(pack, *, slots=None, max_tokens=None, forbidden=())` | exactly a `ContextPack`; `pack_version` recomputes; overflow matches `truncated_ids`; one partial entry at most, carried last and listed in `truncated_ids`; no content (memory text/title, item label/text) in the version payload; `as_dict()` serialisable (§4.2) |
| `check_context_slot_provider(provider, *, request, max_tokens=None, forbidden=(), vault=None, vault_exclude=(), allow_degraded=False)` | `ContextSlotProvider`; `build` returns exactly a `ContextSlot` with the provider's name that passes `check_context_pack`; not degraded unless allowed; two builds give the same version key; vault unchanged when given (§4.3) |

`tree_digest(root, *, exclude=())` is the helper they use to compare file trees.

`memfw.ports.reference` holds a stdlib-only implementation of every port that passes these
checks (projectors, ingestors, derivers, a recall provider, a claim verifier and stager), a
starting point for an adapter and a second implementation for the swap drills
(`tests/test_swap_drills.py`).

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
