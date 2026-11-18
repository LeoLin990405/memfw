"""``Runtime`` and ``build_runtime``: recall, context (C1-C3, U6), tool and usage (C5) wiring."""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from memfw.context import (
    CORE_SLOT,
    CURRENT_SLOT,
    DEFAULT_SLOTS,
    HANDOFF_SLOT,
    SLOT_NAMES,
    TOPICS_SLOT,
    CoreSlotProvider,
    CurrentSlotProvider,
    HandoffSlotProvider,
    Keep,
    SlotInflight,
    TopicsSlotProvider,
    assemble_pack,
    context_request,
    core_context_v1,
    if_version_request,
    slots_request,
)
from memfw.core.errors import MemfwError, NotFoundError
from memfw.core.models import EventKind, MemoryDocument, Tier
from memfw.core.repository import Repository
from memfw.index.readonly import vault_legacy_privacy
from memfw.ports.context import ContextPack, ContextSlotProvider, SlotRequest
from memfw.ports.privacy import admits, effective_level, privacy_value
from memfw.ports.usage import UsageSignal, UsageSink
from memfw.recall.coordinator import InflightRegistry

from .context_cache import ContextCache, cached_core_candidates
from .context_sources import concept_catalogue, share_feed
from .index import ensure_search_index
from .recall_adapters import LoadedAdapter, RecallAdapterError, load_adapters, parse_entries
from .usage import (
    RuntimeOptions,
    checked_used_request,
    read_options,
    readable_ids,
    signal_app,
)

if TYPE_CHECKING:
    from memfw.conversations import ConversationShare
    from memfw.history import TranscriptIndex
    from memfw.recall import RecallResult

#: Default per-provider wait in ``mode=two_stage`` (the coordinator's total
#: deadline still applies). Above the 1.5 s port default so a cold SQLite page
#: does not count as a timeout.
TWO_STAGE_PROVIDER_TIMEOUT = 3.0
#: Default total wait for all ``mode=two_stage`` providers and resolvers together.
TWO_STAGE_TOTAL_TIMEOUT = 4.0

#: The ``policy_name`` values ``Runtime.recall`` accepts.
RECALL_POLICIES = ("compat_fts", "strict")

#: Per-slot deadline of ``memory_context`` (seconds from the start of the call).
#: A slot that misses it is returned as ``degraded: timeout``.
CONTEXT_SLOT_TIMEOUTS: dict[str, float] = {
    CORE_SLOT: 10.0,
    HANDOFF_SLOT: 3.0,
    TOPICS_SLOT: 3.0,
    CURRENT_SLOT: 10.0,
}

#: What one provider's ``observe`` hook recorded: its raw index rows keyed by
#: memory id, and its raw page payloads.
Observed = tuple[dict[str, dict[str, Any]], list[Any]]


@dataclass(frozen=True, slots=True)
class RecallOutcome:
    """One ``Runtime.recall`` call.

    ``observed`` holds what each provider that answered (per the result's
    labels) recorded for this call, taken when the coordinator returned; a
    provider that timed out is never in it, even if its worker writes later.
    """

    result: RecallResult
    observed: dict[str, Observed] = field(default_factory=dict)
    #: Fixed codes for post-filter side effects that failed (``ACCESS_ERROR``);
    #: the hits are unaffected.
    errors: tuple[str, ...] = ()


#: The ``RecallOutcome.errors`` code when warming or logging a returned hit
#: failed (read-only log, unreadable archive): the hits are still returned.
ACCESS_ERROR = "access_not_recorded"


def _touch_returned(
    repository: Repository,
    memory_ids: Sequence[str],
    keep: Callable[[MemoryDocument], bool] | None = None,
) -> tuple[str, ...]:
    """Warm and access-log the hits ``recall`` returns, best effort.

    Per returned id, in hit order, as the direct hybrid path does per hit: a
    cold or stub memory is warmed (``autowarm``), then one ``memory_access``
    event (``operation: two_stage_retrieve``) is appended. ``keep`` (the
    interface's privacy clearance, when one is set) skips a hit the caller
    will never be shown: it is neither warmed nor logged. A failure (for
    example a read-only ``events.jsonl``) never drops a hit; it is reported
    as ``ACCESS_ERROR`` once.
    """

    failed = False
    for memory_id in dict.fromkeys(memory_ids):
        try:
            document = repository.get(memory_id)
            if keep is not None and not keep(document):
                continue
            if document.is_stub or document.tier == Tier.COLD:
                repository.autowarm(memory_id, actor="mechanism")
            repository.capture_event(
                EventKind.MEMORY_ACCESS,
                actor="mechanism",
                details={"memory_id": memory_id, "operation": "two_stage_retrieve"},
            )
        except (MemfwError, OSError, ValueError):
            failed = True
    return (ACCESS_ERROR,) if failed else ()


class Runtime:
    """Capability objects for one vault: one Runtime per Gateway.

    ``root`` is the configured vault path (``expanduser`` only, never
    resolved): a symlinked or relative root follows its target per call, so
    every call takes the caller's open ``Repository`` (resolved per call, as
    the interface opens it) and uses ``repository.root``. Holds the
    settings the interface resolved (tenant, allowed sources, embedder, entity
    flag), the two_stage timeouts, and one ``InflightRegistry`` per Runtime.

    In-flight slots are counted per provider name and shared by every caller
    of this Runtime: a hung provider holds at most ``max_inflight`` worker
    threads per Runtime, not one per call, and while it hangs every later call
    on this Runtime (from any tool) reports that provider as ``busy``.

    ``context_cache`` (C3): ``True`` (default) gives this Runtime its own
    ``ContextCache``; ``False`` or ``None`` disables caching; a
    ``ContextCache`` instance is used as given (it may be shared). The cache
    never changes ``memory_context`` output (see ``context_cache``).
    """

    def __init__(
        self,
        root: Path,
        *,
        tenant: str,
        allowed_sources: tuple[str, ...],
        embedder: Any,
        entity_enabled: bool,
        provider_timeout: float = TWO_STAGE_PROVIDER_TIMEOUT,
        total_timeout: float = TWO_STAGE_TOTAL_TIMEOUT,
        context_cache: ContextCache | bool | None = True,
        usage_signal: bool | None = None,
        record_access: bool | None = None,
        usage_sink: UsageSink | None = None,
        clearance: frozenset[str] | None = None,
        context_current: bool | None = None,
        embedding_allow_remote: bool | None = None,
    ) -> None:
        self.root = root
        #: ``MEMFW_EMBEDDING_ALLOW_REMOTE``; ``None``: ``runtime.embedding.allow_remote``.
        self._embedding_allow_remote = embedding_allow_remote
        self.tenant = tenant
        #: Privacy levels this interface may return (MM0). ``None`` filters
        #: nothing, the behaviour before MM0.
        self.clearance = clearance
        self._legacy_privacy_cache: tuple[tuple[int, int] | None, str] | None = None
        self.allowed_sources = allowed_sources
        self.embedder = embedder
        self.entity_enabled = entity_enabled
        self.provider_timeout = provider_timeout
        self.total_timeout = total_timeout
        self.recall_inflight = InflightRegistry()
        #: Slot builds still running after their deadline, per slot name (C2).
        self.context_inflight = SlotInflight()
        self.context_slot_timeouts = dict(CONTEXT_SLOT_TIMEOUTS)
        #: The ``core`` slot's id cache (C3), or None when disabled.
        self.context_cache: ContextCache | None = _context_cache(context_cache)
        # C5/C6 switches: an explicit value (MEMFW_*) wins; otherwise vault.yaml
        # ``runtime:`` decides, read once on first use (``options``).
        self._usage_signal = usage_signal
        self._record_access = record_access
        self._context_current = context_current
        self._usage_sink = usage_sink
        self._options: RuntimeOptions | None = None
        self._options_lock = threading.Lock()
        #: ``runtime.recall.adapters`` (built once, on the first ``recall``).
        self._recall_adapters: tuple[LoadedAdapter, ...] | None = None
        self._recall_adapters_lock = threading.Lock()
        #: The time a usage signal is stamped with (a seam for tests).
        self.usage_clock: Callable[[], datetime] = lambda: datetime.now(UTC)

    def ensure_index(self, repository: Repository | None = None) -> None:
        """Rebuild the memory index when it is stale (see ``ensure_search_index``).

        ``repository`` is the caller's open vault for this root. Without one
        the Runtime opens ``Repository(self.root)``, but only an existing
        vault: a missing root (or one without ``vault.yaml``) raises
        ``ValueError("vault root does not exist")``, like the MCP server's
        vault opener; it never creates one.
        """

        if repository is None:
            repository = self.open_repository()
        ensure_search_index(repository)

    def open_repository(self) -> Repository:
        """Open the existing vault at ``root``; never create one.

        A missing root (or one without ``vault.yaml``) raises
        ``ValueError("vault root does not exist")``. Opened per call from the
        configured (unresolved) root, so a symlinked or relative root follows
        its current target on every call; the Runtime never keeps one open.
        """

        root = self.root
        if not root.is_dir() or not (root / "vault.yaml").is_file():
            raise ValueError("vault root does not exist")
        return Repository(root)

    def visible(self, document: MemoryDocument) -> bool:
        """In the bound tenant and, when allowed sources are set, an allowed source.

        With a ``clearance``, also a privacy level it admits.
        """

        return self._visible(document)

    def require_visible(self, document: MemoryDocument) -> None:
        """Raise unless the caller may see ``document``.

        A memory the privacy clearance withholds raises the same
        ``NotFoundError`` as a missing one, so its existence is not revealed;
        any other rule keeps the tenant message.
        """

        if not self.privacy_admits(document):
            raise NotFoundError(f"Memory not found: {document.id}")
        if not self._visible(document):
            raise ValueError("memory is not visible in this tenant")

    def legacy_privacy(self) -> str:
        """The vault's ``legacy_privacy_default``, re-read when ``vault.yaml`` changes."""

        path = self.root / "vault.yaml"
        try:
            stat = path.stat()
            key: tuple[int, int] | None = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            key = None
        cached = self._legacy_privacy_cache
        if cached is not None and cached[0] == key:
            return cached[1]
        value = vault_legacy_privacy(self.root)
        self._legacy_privacy_cache = (key, value)
        return value

    def _question_keep(self) -> Callable[[MemoryDocument], bool] | None:
        """Which open questions may label a hit: ``None`` (all) without a clearance."""

        return None if self.clearance is None else self.privacy_admits

    def privacy_admits(self, document: MemoryDocument) -> bool:
        """Whether the clearance admits ``document`` (always, without a clearance)."""

        if self.clearance is None:
            return True
        level = effective_level(
            document.privacy,
            document.tags,
            legacy=self.legacy_privacy(),
            sensitivity=document.top_level_sensitivity,
        )
        return admits(self.clearance, level)

    # --- runtime options and memory_used (C5, C6) ------------------------------

    def options(self) -> RuntimeOptions:
        """The usage, record-access and ``current`` slot switches (``memfw.runtime.usage``).

        Explicit values (``build_runtime`` arguments, i.e. ``MEMFW_USAGE_SIGNAL``
        and ``MEMFW_RECORD_ACCESS``, and ``context_current``) win; for one left
        ``None`` the vault's ``runtime:`` section decides (``current_ttl_days``
        always comes from it). A successful read is kept for the Runtime's
        life; ``vault.yaml`` is not read when the first two are explicit and
        ``context_current`` is ``False``. No
        vault, or an unreadable ``vault.yaml``, fails closed (usage off, access
        not recorded) and is not cached: the next use reads again.
        """

        with self._options_lock:
            if self._options is not None:
                return self._options
            from_vault, cacheable = RuntimeOptions(), True
            # ``current_ttl_days`` has no override: an enabled slot needs the vault's.
            if (
                self._usage_signal is None
                or self._record_access is None
                or self._context_current is not False
            ):
                try:
                    repository: Repository | None = self.open_repository()
                except (MemfwError, OSError, ValueError):
                    repository = None
                from_vault, cacheable = read_options(repository)
            options = RuntimeOptions(
                usage_enabled=(
                    from_vault.usage_enabled if self._usage_signal is None else self._usage_signal
                ),
                record_access=(
                    from_vault.record_access if self._record_access is None else self._record_access
                ),
                context_current=(
                    from_vault.context_current
                    if self._context_current is None
                    else self._context_current
                ),
                current_ttl_days=from_vault.current_ttl_days,
            )
            if cacheable:
                self._options = options
            return options

    def embedding_allow_remote(self, repository: Repository | None = None) -> bool:
        """Whether an off-host embedder may receive ``local-private``/``unknown`` memories.

        The explicit setting (``MEMFW_EMBEDDING_ALLOW_REMOTE``) wins; otherwise
        ``vault.yaml`` ``runtime.embedding.allow_remote`` (a boolean, default
        ``false``; anything else, a missing vault or an unreadable file is
        ``false``). Read only when the embedder is off-host
        (``memfw.notes.http.embedder_sends_off_host``), so a loopback or
        in-process embedder never reads it. Off, only ``public``/``shared``
        memories are embedded (``memfw.memory_hybrid``).
        """

        if self._embedding_allow_remote is not None:
            return self._embedding_allow_remote
        if self.embedder is None:
            return False
        from memfw.notes.http import embedder_sends_off_host

        if not embedder_sends_off_host(self.embedder):
            return False
        try:
            config = (repository or self.open_repository()).read_config()
        except (MemfwError, OSError, ValueError):
            return False
        section = config.get("runtime")
        if not isinstance(section, dict):
            return False
        embedding = section.get("embedding")
        if not isinstance(embedding, dict):
            return False
        return embedding.get("allow_remote") is True

    def usage_enabled(self) -> bool:
        """Whether ``memory_used`` is registered (default off)."""

        return self.options().usage_enabled

    def record_access(self) -> bool:
        """Whether search modes append ``memory_access`` events and warm hits (default on)."""

        return self.options().record_access

    def usage_sink(self, repository: Repository) -> UsageSink:
        """The sink for this vault: the injected one, else SQLite when enabled, else null."""

        if self._usage_sink is not None:
            return self._usage_sink
        if not self.usage_enabled():
            from memfw.ports.reference.usage import NullUsageSink

            return NullUsageSink()
        from memfw.usage import SqliteUsageSink, projection_path

        return SqliteUsageSink(projection_path(repository.root))

    def memory_used(self, ids: object, session_ref: object, *, app: str) -> dict[str, Any]:
        """``memory_used``: record one usage signal; never touch a memory or the vault.

        ``ids`` (1 to ``MAX_USAGE_IDS`` strings) and ``session_ref`` (an opaque
        token) are checked by shape first (``ValueError``). Then the vault is
        opened and each well-formed id read: an id that does not exist, is
        unreadable, or is outside the caller's tenant or allowed sources (the
        ``memory_get`` rule) is dropped. The rest, if any, become one
        ``UsageSignal`` (ids, ``session_ref``, the calling app, the time) for
        ``usage_sink``. A sink failure is swallowed: the signal is best effort.
        The answer's content is the same whatever was dropped or recorded. That
        is not a hiding guarantee: timing differs (a kept id costs a write), and
        ``memory_get`` already tells a visible id from an unknown one.
        """

        wanted, ref = checked_used_request(ids, session_ref)
        repository = self.open_repository()
        kept = readable_ids(repository, wanted, self._visible)
        if kept:
            signal = UsageSignal(
                memory_ids=kept, session_ref=ref, app=signal_app(app), at=self.usage_clock()
            )
            try:
                self.usage_sink(repository).record(signal)
            except (OSError, ValueError):
                pass  # best effort; the answer must not depend on it
        return {"ok": True}

    # --- memory_search (A3b) ----------------------------------------------------

    def search(
        self,
        query: str,
        limit: int = 10,
        *,
        mode: str = "hybrid",
        source: str = "",
        include_history: bool = False,
        as_of: str | None = None,
        rounds: int | None = None,
        scope: object = "",
        policy: str | None = "compat_fts",
        labels: bool = False,
        client_message: Callable[[str, Path], str] | None = None,
        app: str | None = None,
    ) -> dict[str, Any]:
        """``memory_search``: check the arguments, open the vault, run ``mode``.

        Checks, in this order and before the vault is opened (each raises
        ``ValueError``): a non-blank ``query`` (``query required``); ``policy``
        (``None``/``""`` mean ``compat_fts``) one of ``RECALL_POLICIES``
        (``unknown policy``) and other than ``compat_fts`` only with
        ``mode=two_stage`` (``policy only applies to mode=two_stage``);
        ``scope`` ``None`` or a string of at most 512 characters (``scope must
        be a string``, ``scope too long``; it is stripped); an explicit
        ``source`` in the allowed sources when they are set (``source is not
        allowed for this account``). Then ``open_repository`` (per call) and
        the mode: fts (the shared read-only reader), hybrid and vector
        (``MemoryHybridRetriever``), multihop, two_stage (``recall``), plus
        ontology alias expansion and the U2 labels; rows are built here.
        ``labels=true`` also gives every row, in every mode, the U5
        ``corroboration`` field as ``app`` (the calling app) may see it
        (``memfw.runtime.corroboration``); ``app`` is read for nothing else.

        ``client_message(text, root)`` cleans error texts that may name vault
        paths (the MCP server passes its own); the default returns the text.
        """

        from .search import checked_scope, memory_search

        if not query.strip():
            raise ValueError("query required")
        if policy is None or policy == "":
            policy = "compat_fts"  # absent, as before this argument existed
        if policy not in RECALL_POLICIES:
            raise ValueError("unknown policy")
        if mode != "two_stage" and policy != "compat_fts":
            raise ValueError("policy only applies to mode=two_stage")
        cleaned_scope = checked_scope(scope)
        if source and self.allowed_sources and source not in self.allowed_sources:
            raise ValueError("source is not allowed for this account")
        repository = self.open_repository()
        return memory_search(
            self,
            repository,
            query,
            limit,
            mode=mode,
            source=source,
            include_history=include_history,
            as_of=as_of,
            rounds=rounds,
            scope=cleaned_scope,
            policy=policy,
            labels=labels,
            client_message=client_message or _plain_message,
            app=app,
        )

    # --- memory_remember, memory_related, memory_status (A3b) -------------------

    def remember(
        self,
        content: str,
        title: str,
        *,
        tags: list[str] | None,
        related: list[str] | None,
        privacy: str | None = None,
    ) -> dict[str, Any]:
        """Stage ``content`` as a candidate under ``_candidates/remember/`` (no authority).

        ``privacy`` (MM0) must be one of the three levels when given.
        """

        from .memories import REMEMBER_MAX_BYTES, stage_remember

        checked_privacy = privacy_value(privacy)

        if len(content.encode("utf-8")) > REMEMBER_MAX_BYTES:
            raise ValueError("content exceeds 64 KiB")
        text = content.strip()
        if not text:
            raise ValueError("content required")
        repository = self.open_repository()
        return stage_remember(
            self.root,
            repository,
            text,
            title,
            tenant=self.tenant,
            embedder=self.embedder,
            tags=tags,
            related=related,
            privacy=checked_privacy,
            embedding_allow_remote=self.embedding_allow_remote(repository),
            allowed_sources=tuple(self.allowed_sources),
            # Tenant, allowed sources and the privacy clearance: a suggestion
            # never names a memory this caller could not read.
            visible=self._visible,
        )

    def related(self, repository: Repository, memory_id: str, limit: int) -> dict[str, Any]:
        """``memory_related`` for a memory visible to this Runtime's caller."""

        from .memories import related_memories

        return related_memories(
            self.root,
            repository,
            memory_id,
            limit,
            tenant=self.tenant,
            visible=self._visible,
            require=self.require_visible,
        )

    def status(self, repository: Repository) -> dict[str, Any]:
        """``memory_status``: visible memories per layer and tier, and the log head."""

        from .memories import vault_status

        return vault_status(repository, visible=self._visible)

    # --- conversations and history (A3b) ----------------------------------------

    def conversation_share(self, app: str) -> ConversationShare:
        """The shared conversation store as ``app`` sees it.

        ``app`` is required and may not be the reserved operator name; the
        vault must exist.
        """

        from .sharing import check_conversation_app, open_share

        check_conversation_app(app)
        self.open_repository()
        # The embedder serves only ``conversations_search(mode=two_stage)``.
        return open_share(self.root, app, embedder=self.embedder)

    def history_scoped(
        self, repository: Repository, app: str, *, refresh_seconds: float
    ) -> tuple[TranscriptIndex, frozenset[str] | None]:
        """The transcript index and the source names ``app`` may read (``None``: all).

        The same visibility as the conversation share: a source named after
        an opted-out app or with ``readable: false`` is never returned, and
        the caller's own rule applies (opted out or ``can_read: false`` is
        refused; a ``can_read`` list narrows the sources).
        """

        from .sharing import history_scoped

        return history_scoped(repository, app, refresh_seconds=refresh_seconds)

    @staticmethod
    def history_ref_source(ref: str) -> str:
        """The source name of a history ``ref`` (``SOURCE:FILE@OFFSET``)."""

        from .sharing import ref_source

        return ref_source(ref)

    # --- dreams (A3b) -------------------------------------------------------------

    def dream_run(
        self,
        repository: Repository,
        *,
        now: datetime,
        dry_run: bool,
        focus: str,
        all_memories: bool,
        limit: int,
        max_sessions: int,
        source: str,
        since: str,
        until: str,
        stale_days: int,
        min_events: int,
    ) -> dict[str, Any]:
        """One dream for this tenant, without a judge or a transcript index refresh."""

        from .dreams import run_interface_dream

        return run_interface_dream(
            repository,
            now=now,
            dry_run=dry_run,
            tenant=self.tenant,
            focus=focus,
            all_memories=all_memories,
            limit=limit,
            max_sessions=max_sessions,
            source=source,
            since=since,
            until=until,
            stale_days=stale_days,
            min_events=min_events,
        )

    def staged_dreams(self, repository: Repository) -> tuple[list[dict[str, Any]], list[str]]:
        """This tenant's staged dreams, newest first, and the entries skipped."""

        from .dreams import staged_dreams

        return staged_dreams(repository, tenant=self.tenant)

    def _two_stage_providers(
        self,
        root: Path,
        *,
        source: str,
        include_history: bool,
        scope: str,
        observe: Callable[[str], Any],
    ) -> tuple[list[Any], dict[str, Any]]:
        """The ``two_stage`` providers and their configs for one call.

        ``observe(name)`` returns the hook that records that provider's raw
        rows for this call only. A seam for tests, which add stub providers.
        """

        from memfw.ports import RecallBudget
        from memfw.recall import ProviderConfig
        from memfw.recall.builtin import FtsProvider, HybridProvider

        allowed = tuple(self.allowed_sources)
        budget = RecallBudget(timeout_seconds=self.provider_timeout)
        providers: list[Any] = [
            FtsProvider(
                root,
                source=source,
                include_history=include_history,
                scope=scope,
                allowed_sources=allowed,
                observe=observe("fts"),
            )
        ]
        configs: dict[str, Any] = {"fts": ProviderConfig(budget=budget)}
        if self.embedder:
            providers.append(
                HybridProvider(
                    root,
                    embedder=self.embedder,
                    source=source,
                    include_history=include_history,
                    allowed_sources=allowed,
                    entity_enabled=self.entity_enabled,
                    observe=observe("hybrid"),
                    embedding_allow_remote=self.embedding_allow_remote(),
                )
            )
            configs["hybrid"] = ProviderConfig(budget=budget)
        return providers, configs

    def recall_adapters(
        self, repository: Repository, *, taken: Sequence[str] = ()
    ) -> tuple[LoadedAdapter, ...]:
        """The operator's ``runtime.recall.adapters`` (``memfw.runtime.recall_adapters``).

        Read and built on first use, then kept for the Runtime's life (restart
        to pick up a change), so an adapter keeps its state (an index, a
        connection) between calls. No key: none, and nothing is imported. An
        unreadable ``vault.yaml`` adds none and is read again next time (fewer
        providers is the closed side). A malformed entry or an adapter that
        cannot be built raises ``RecallAdapterError`` every time (fail closed).

        Factories run outside the lock, so a slow one never blocks another
        caller; concurrent first calls may each build the adapters, and the
        first set stored is the one every caller uses (the others are dropped).
        Factories should therefore be cheap: no network or heavy I/O, which
        belongs in ``available()``/``search()``.
        """

        with self._recall_adapters_lock:
            if self._recall_adapters is not None:
                return self._recall_adapters
        try:
            config = repository.read_config()
        except (MemfwError, OSError, ValueError):
            return ()
        entries = parse_entries(config)
        loaded = (
            load_adapters(entries, root=repository.root, embedder=self.embedder, taken=taken)
            if entries
            else ()
        )
        with self._recall_adapters_lock:
            if self._recall_adapters is None:
                self._recall_adapters = loaded
            return self._recall_adapters

    def recall(
        self,
        repository: Repository,
        query: str,
        *,
        limit: int,
        policy_name: str,
        source: str,
        include_history: bool,
        as_of: datetime | None,
        scope: str,
    ) -> RecallOutcome:
        """Run the ``two_stage`` providers through the coordinator.

        ``repository`` is the caller's open vault (its resolved ``root`` is
        used, so recall reads the same target as ``ensure_index`` and the
        other modes of that call). ``limit`` is the page size, already
        clamped by the caller; ``as_of`` is already parsed. ``policy_name``
        is ``"compat_fts"`` or ``"strict"``; any other value raises
        ``ValueError`` (fail closed) before a provider runs. Providers: FTS
        always, plus hybrid when an embedder is configured, plus the
        operator's ``runtime.recall.adapters`` (``recall_adapters``; none by
        default, a bad entry raises ``RecallAdapterError``). Call
        ``ensure_index`` first: the coordinator's deadline starts here.

        Providers are read-only: none appends an event or warms a memory.
        After the filter and fusion, unless ``record_access()`` is off (the
        session-safe setting, C6), each returned hit that is cold or a stub
        is warmed and gets one ``memory_access`` event (``operation:
        two_stage_retrieve``, the same shape the direct hybrid path writes),
        so a hit the policy dropped (privacy, tenant, source, validity) is
        never warmed or access-logged. This is best effort: a failure is
        reported in ``RecallOutcome.errors`` and never drops a hit.

        Warming happens here, before the caller builds rows, but the rows
        are built from what the providers saw: for a hit this call warmed,
        the provider's index row describes the pre-warm version, so its row
        is marked ``stale`` on this call (the vault file changed: tier and
        version). The next call's index refresh makes it fresh. Direct
        ``mode=hybrid`` marks a warmed hit ``stale`` the same way (and until
        its vector index is rebuilt).

        Known race: the caller builds rows after this returns and re-checks
        each hit against the vault; a hit removed or changed in between is
        logged here but not shown. Logging follows the coordinator's output,
        not the final rows.
        """

        if policy_name not in RECALL_POLICIES:
            raise ValueError("unknown policy")
        root = repository.root

        # Imported here so callers that never recall never load them.
        from memfw.recall import Coordinator
        from memfw.recall.builtin import default_resolvers

        moment = datetime.now(UTC)
        captured: dict[str, Observed] = {}

        def observe(name: str) -> Callable[[Any, Any], None]:
            def record(items: Any, payloads: Any) -> None:
                # One assignment, so a late (timed-out) worker never leaves
                # a half-written entry.
                captured[name] = (
                    {str(item.get("id") or ""): dict(item) for item in items},
                    [dict(payload) for payload in payloads],
                )

            return record

        providers, configs = self._two_stage_providers(
            root,
            source=source,
            include_history=include_history,
            scope=scope,
            observe=observe,
        )
        adapters = self.recall_adapters(
            repository, taken=[str(getattr(p, "name", "")) for p in providers]
        )
        if adapters:
            from memfw.ports import RecallBudget
            from memfw.recall import ProviderConfig

            for adapter in adapters:
                name = adapter.provider.name
                if name in configs or any(getattr(p, "name", None) == name for p in providers):
                    raise RecallAdapterError("duplicate_name")
                providers.append(adapter.provider)
                timeout = adapter.timeout_seconds or self.provider_timeout
                configs[name] = ProviderConfig(
                    weight=adapter.weight, budget=RecallBudget(timeout_seconds=timeout)
                )
        filter_policy = self._filter_policy(
            policy_name, source=source, include_history=include_history, as_of=as_of
        )
        coordinator = Coordinator(
            providers,
            resolvers=default_resolvers(root),
            configs=configs,
            inflight=self.recall_inflight,
            total_timeout=self.total_timeout,
            # One instant for the whole call: the coordinator's filter and the
            # open-question labels judge validity at the same ``now``.
            clock=lambda: moment,
        )
        result = coordinator.search(query, limit=limit, policy=filter_policy)
        result = _with_open_question_labels(
            repository, result, filter_policy, now=moment, keep=self._question_keep()
        )
        errors: tuple[str, ...] = ()
        if self.record_access():
            # The rows are cleared later (``_cleared_rows``); a hit the
            # clearance withholds must not feed access or tier signals either.
            keep = None if self.clearance is None else self.privacy_admits
            errors = _touch_returned(repository, [hit.memory_id for hit in result.hits], keep)
        answered = set(result.labels.providers)
        seen = {name: captured[name] for name in list(captured) if name in answered}
        return RecallOutcome(result=result, observed=seen, errors=errors)

    def _filter_policy(
        self,
        policy_name: str,
        *,
        source: str,
        include_history: bool,
        as_of: datetime | None,
    ) -> Any:
        """The ``FilterPolicy`` for ``policy_name`` (already checked by the caller)."""

        from memfw.recall import FilterPolicy

        allowed = tuple(self.allowed_sources)
        if policy_name == "strict":
            return FilterPolicy(
                tenant=self.tenant,
                allowed_sources=allowed,
                source=source,
                as_of=as_of,
                include_history=include_history,
            )
        return FilterPolicy.compat_fts(
            tenant=self.tenant,
            allowed_sources=allowed,
            source=source,
            as_of=as_of,
            include_history=include_history,
        )

    def open_question_labels(
        self,
        repository: Repository,
        memory_ids: Sequence[str],
        *,
        policy_name: str,
        source: str,
        include_history: bool,
        as_of: datetime | None,
        scope: str = "",
        now: datetime | None = None,
    ) -> dict[str, tuple[str, ...]]:
        """U2 labels for hits of the other ``memory_search`` modes (``labels: true``).

        The same labels and the same ``FilterPolicy`` as ``recall`` adds to
        ``two_stage`` hits: ``open_question:<id>`` for each unresolved open
        question the policy keeps whose ``about`` shares a concept with the
        hit's, and ``conflicted`` when such a question lists the hit in its
        ``conflicts``. ``repository`` is the caller's open vault. Ids without
        labels are absent. Any other ``policy_name`` raises ``ValueError``.

        ``scope`` is taken as ``mode=fts`` takes it: it only selects scoped
        alias labels and filters no hit, and labels match ``about`` concept
        ids, never aliases, so it changes no label. ``now`` defaults to the
        current time. No ids means nothing is read; otherwise the index
        staleness check runs first (the hybrid and vector paths do not run it).
        """

        if policy_name not in RECALL_POLICIES:
            raise ValueError("unknown policy")
        del scope  # see the docstring: no label depends on it
        if not memory_ids:
            return {}
        from memfw.recall.builtin import open_question_labels_for

        policy = self._filter_policy(
            policy_name, source=source, include_history=include_history, as_of=as_of
        )
        self.ensure_index(repository)
        moment = now if now is not None else datetime.now(UTC)
        return open_question_labels_for(
            repository, memory_ids, policy, now=moment, keep=self._question_keep()
        )

    def open_question_queue(
        self,
        repository: Repository,
        *,
        app: str | None,
        policy_name: str = "strict",
        allow_operator: bool = False,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """The open-question review queue, corroborated conflicts first (U5 reverse signal).

        Fails closed: ``policy_name`` defaults to ``strict`` (privacy clearance
        ``none``, status, lifecycle, archive), and both the questions and the
        conflicts that may add support must pass it, so a private, rejected or
        archived question is never listed and a private conflict never moves
        one. The questions are the unresolved, current ones the policy keeps
        for this Runtime's tenant and sources. Order: ``memfw.recall.priority``,
        from corroboration trimmed to what ``app`` may read; ``allow_operator``
        is for the memfw CLI only. Never writes the vault or the corroboration
        projection.
        """

        if policy_name not in RECALL_POLICIES:
            raise ValueError("unknown policy")
        from memfw.recall import evaluate
        from memfw.recall.builtin import meta_from_document, open_questions

        from .corroboration import open_question_queue

        policy = self._filter_policy(policy_name, source="", include_history=False, as_of=None)
        self.ensure_index(repository)
        moment = now if now is not None else datetime.now(UTC)

        def keep(document: MemoryDocument) -> bool:
            if not self._visible(document):
                return False
            return evaluate(meta_from_document(document), policy, now=moment).kept

        queue = open_question_queue(
            repository,
            [item for item in open_questions(repository, policy, now=moment) if keep(item)],
            app=app,
            visible=keep,
            allow_operator=allow_operator,
        )
        if self.clearance is not None:
            # MM0: a listed question may still name a memory the clearance
            # withholds; such an id is not shown.
            for row in queue["questions"]:
                row["conflicts"] = [
                    memory_id
                    for memory_id in row["conflicts"]
                    if self._conflict_shown(repository, memory_id)
                ]
        return queue

    def _conflict_shown(self, repository: Repository, memory_id: str) -> bool:
        try:
            document = repository.get(memory_id)
        except (MemfwError, OSError, UnicodeDecodeError, ValueError):
            return False
        return self.privacy_admits(document)

    # --- memory_context (C1) ---------------------------------------------------

    @staticmethod
    def context_request(v: object, policy: object) -> tuple[int, str]:
        """Validate ``memory_context`` ``v`` and ``policy`` (see ``memfw.context``)."""

        return context_request(v, policy)

    def _visible(self, document: MemoryDocument) -> bool:
        """v1's rule: the bound tenant and, when set, an allowed source."""

        if document.tenant != self.tenant:
            return False
        if self.allowed_sources and document.source not in self.allowed_sources:
            return False
        return self.privacy_admits(document)

    def context_keep(self, policy_name: str, *, now: datetime) -> Keep:
        """Who may see a memory in a context pack under ``policy_name``.

        ``compat_fts`` is v1's tenant/source rule (what
        ``FilterPolicy.compat_fts`` checks when the slot does its own temporal
        filtering). ``strict`` is the recall strict ``FilterPolicy`` evaluated on
        ``meta_from_document``: privacy clearance ``none`` (fail-closed tag
        reading), status, lifecycle, supersession, validity and archive. Any
        other name raises ``ValueError`` (fail closed).
        """

        if policy_name not in RECALL_POLICIES:
            raise ValueError("unknown policy")
        if policy_name == "compat_fts":
            return self._visible
        # Imported here so callers that never use strict never load them.
        from memfw.recall import FilterPolicy, evaluate
        from memfw.recall.builtin import meta_from_document

        rules = FilterPolicy(tenant=self.tenant, allowed_sources=tuple(self.allowed_sources))

        def keep(document: MemoryDocument) -> bool:
            if not self.privacy_admits(document):
                return False
            return evaluate(meta_from_document(document), rules, now=now).kept

        return keep

    def context_v1(self, repository: Repository, *, limit_tokens: Any) -> dict[str, Any]:
        """The original ``memory_context`` payload for the caller's open vault."""

        return core_context_v1(repository, keep=self._visible, limit_tokens=limit_tokens)

    def context_current_enabled(self) -> bool:
        """Whether the ``current`` slot exists (U6, ``runtime.context.current``; default off)."""

        return self.options().context_current

    def context_slot_names(self) -> tuple[str, ...]:
        """The slots ``memory_context`` knows: ``SLOT_NAMES``, plus ``current`` when enabled."""

        if self.context_current_enabled():
            return (*SLOT_NAMES, CURRENT_SLOT)
        return SLOT_NAMES

    def context_slots_request(self, v: int, slots: object) -> tuple[str, ...]:
        """Validate ``memory_context`` ``slots`` for version ``v`` (see ``memfw.context``).

        ``current`` is a known name only while it is enabled; off, it is
        refused with the same message as before U6.
        """

        # The options are read only for a request that names a slot outside
        # ``SLOT_NAMES``: every request valid before U6 takes the same path.
        if not isinstance(slots, list) or all(name in SLOT_NAMES for name in slots):
            return slots_request(v, slots)
        return slots_request(v, slots, self.context_slot_names())

    @staticmethod
    def context_if_version_request(v: int, if_version: object) -> str | None:
        """Validate ``memory_context`` ``if_version`` for version ``v`` (see ``memfw.context``)."""

        return if_version_request(v, if_version)

    def context_slot_provider(
        self, name: str, repository: Repository, *, keep: Keep, app: str, policy_name: str = ""
    ) -> ContextSlotProvider:
        """The provider of slot ``name`` for one call (a seam for tests).

        ``core`` reads the caller's ``repository``, its selection served from
        ``context_cache`` when there is one and ``policy_name`` is given (C3);
        ``handoff`` reads the conversation share as ``app``
        (``conversation_share`` applies); ``topics`` reads the concept
        catalogue and counts the memories ``keep`` keeps, on its own
        ``Repository`` of the same root (the slots run in parallel);
        ``current`` (U6) reads its own ``Repository`` too, with the vault's
        ``current_ttl_days``. Only ``core`` is ever cached: ``current`` depends
        on the clock, so a cached selection could serve an expired memory.
        """

        root = repository.root
        if name == CORE_SLOT:
            if self.context_cache is None or not policy_name:
                return CoreSlotProvider(repository, keep)
            candidates = cached_core_candidates(
                self.context_cache,
                tenant=self.tenant,
                policy=policy_name,
                allowed_sources=tuple(self.allowed_sources),
                visible=self._visible,
            )
            return CoreSlotProvider(repository, keep, candidates=candidates)
        if name == HANDOFF_SLOT:
            return HandoffSlotProvider(share_feed(root, app))
        if name == TOPICS_SLOT:
            return TopicsSlotProvider(Repository(root), keep, lambda: concept_catalogue(root))
        if name == CURRENT_SLOT:
            return CurrentSlotProvider(
                Repository(root), keep, ttl_days=self.options().current_ttl_days
            )
        raise ValueError("unknown slot")

    def context_pack(
        self,
        repository: Repository,
        *,
        policy_name: str,
        limit_tokens: int,
        now: datetime | None = None,
        slots: Sequence[str] = DEFAULT_SLOTS,
        app: str = "",
    ) -> ContextPack:
        """The ``memory_context`` v2 pack for the caller's open vault.

        ``repository`` is the caller's per-call ``Repository`` (never one opened
        from ``self.root``). ``policy_name`` is ``compat_fts`` or ``strict``;
        anything else raises ``ValueError`` before the vault is read, and so
        does an unknown slot name. ``slots`` (default ``("core",)``) are built
        in parallel and returned in this order; each has its deadline in
        ``context_slot_timeouts``, and one that raises, misses it or is still
        busy from an earlier call is returned ``degraded`` (the rest are not
        affected). The default ``("core",)`` alone runs inline with C1's error
        behaviour: a vault error raises. ``app`` is the calling app, for ``handoff``.
        ``current`` is known only while ``context_current_enabled()``.
        """

        moment = now if now is not None else datetime.now(UTC)
        keep = self.context_keep(policy_name, now=moment)
        names = tuple(slots)
        known = SLOT_NAMES if set(names) <= set(SLOT_NAMES) else self.context_slot_names()
        if not names or any(name not in known for name in names):
            raise ValueError("unknown slot")
        providers = [
            self.context_slot_provider(
                name, repository, keep=keep, app=app, policy_name=policy_name
            )
            for name in names
        ]
        request = SlotRequest(limit_tokens=int(limit_tokens), policy=policy_name, now=moment)
        if names == DEFAULT_SLOTS:
            # The default request keeps C1's semantics exactly: core runs in the
            # caller's thread and a vault error (a pending transaction, say)
            # propagates as the tool error it always was, never a degraded slot.
            core = providers[0]
            slot = core.build_raw(request) if isinstance(core, CoreSlotProvider) else None
            if slot is not None:
                return ContextPack.build((slot,), policy=policy_name, generated_at=moment)
        return assemble_pack(
            providers,
            request,
            timeouts=self.context_slot_timeouts,
            default_timeout=max(self.context_slot_timeouts.values(), default=3.0),
            inflight=self.context_inflight,
        )


def _context_cache(value: ContextCache | bool | None) -> ContextCache | None:
    if value is True:
        return ContextCache()
    if value is False or value is None:
        return None
    if not isinstance(value, ContextCache):
        raise TypeError("context_cache must be a ContextCache, True, False or None")
    return value


def _plain_message(message: str, root: Path) -> str:
    """The default ``client_message``: the text unchanged."""

    del root
    return message


def _with_open_question_labels(
    repository: Repository,
    result: RecallResult,
    policy: Any,
    *,
    now: datetime,
    keep: Callable[[MemoryDocument], bool] | None = None,
) -> RecallResult:
    """``result`` with each hit's open-question labels appended to its ``labels``."""

    if not result.hits:
        return result
    from dataclasses import replace

    from memfw.recall.builtin import open_question_labels_for

    extra = open_question_labels_for(
        repository, [hit.memory_id for hit in result.hits], policy, now=now, keep=keep
    )
    if not extra:
        return result
    hits = tuple(
        replace(hit, labels=(*hit.labels, *extra[hit.memory_id])) if hit.memory_id in extra else hit
        for hit in result.hits
    )
    return replace(result, hits=hits)


def build_runtime(
    root: Path | str,
    *,
    tenant: str = "",
    allowed_sources: tuple[str, ...] = (),
    embedder: Any = None,
    entity_enabled: bool = False,
    provider_timeout: float = TWO_STAGE_PROVIDER_TIMEOUT,
    total_timeout: float = TWO_STAGE_TOTAL_TIMEOUT,
    context_cache: ContextCache | bool | None = True,
    usage_signal: bool | None = None,
    record_access: bool | None = None,
    usage_sink: UsageSink | None = None,
    clearance: frozenset[str] | None = None,
    context_current: bool | None = None,
    embedding_allow_remote: bool | None = None,
) -> Runtime:
    """The default assembly: today's behaviour unless a switch is set.

    ``root`` is kept as configured (``expanduser`` only; see ``Runtime``).
    Other values are taken as given: ``resolve_settings`` turns explicit
    values or ``MEMFW_*`` into them (the MCP server passes its result).
    ``usage_signal`` and ``record_access`` left ``None`` are read from the
    vault's ``runtime:`` section (``memfw.runtime.usage``); ``usage_sink``
    replaces the sink ``memory_used`` writes to (a seam for tests and adapters).
    ``context_current`` left ``None`` is ``runtime.context.current`` (U6).
    """

    return Runtime(
        Path(root).expanduser(),
        tenant=tenant,
        allowed_sources=tuple(allowed_sources),
        embedder=embedder,
        entity_enabled=entity_enabled,
        provider_timeout=provider_timeout,
        total_timeout=total_timeout,
        context_cache=context_cache,
        usage_signal=usage_signal,
        record_access=record_access,
        usage_sink=usage_sink,
        clearance=clearance,
        context_current=context_current,
        embedding_allow_remote=embedding_allow_remote,
    )


__all__ = [
    "CONTEXT_SLOT_TIMEOUTS",
    "RECALL_POLICIES",
    "TWO_STAGE_PROVIDER_TIMEOUT",
    "TWO_STAGE_TOTAL_TIMEOUT",
    "Observed",
    "RecallOutcome",
    "Runtime",
    "build_runtime",
]
