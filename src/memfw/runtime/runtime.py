"""``Runtime`` and ``build_runtime``: recall (A3a) and context (C1) wiring for the interfaces."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from memfw.context import Keep, build_core_pack, context_request, core_context_v1
from memfw.core.models import MemoryDocument
from memfw.core.repository import Repository
from memfw.ports.context import ContextPack
from memfw.recall.coordinator import InflightRegistry

from .index import ensure_search_index

if TYPE_CHECKING:
    from memfw.recall import RecallResult

#: Default per-provider wait in ``mode=two_stage`` (the coordinator's total
#: deadline still applies). Above the 1.5 s port default so a cold SQLite page
#: does not count as a timeout.
TWO_STAGE_PROVIDER_TIMEOUT = 3.0
#: Default total wait for all ``mode=two_stage`` providers and resolvers together.
TWO_STAGE_TOTAL_TIMEOUT = 4.0

#: The ``policy_name`` values ``Runtime.recall`` accepts.
RECALL_POLICIES = ("compat_fts", "strict")

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
    ) -> None:
        self.root = root
        self.tenant = tenant
        self.allowed_sources = allowed_sources
        self.embedder = embedder
        self.entity_enabled = entity_enabled
        self.provider_timeout = provider_timeout
        self.total_timeout = total_timeout
        self.recall_inflight = InflightRegistry()

    def ensure_index(self, repository: Repository | None = None) -> None:
        """Rebuild the memory index when it is stale (see ``ensure_search_index``).

        ``repository`` is the caller's open vault for this root. Without one
        the Runtime opens ``Repository(self.root)``, but only an existing
        vault: a missing root (or one without ``vault.yaml``) raises
        ``ValueError("vault root does not exist")``, like the MCP server's
        vault opener; it never creates one.
        """

        if repository is None:
            root = self.root
            if not root.is_dir() or not (root / "vault.yaml").is_file():
                raise ValueError("vault root does not exist")
            repository = Repository(root)
        ensure_search_index(repository)

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
                )
            )
            configs["hybrid"] = ProviderConfig(budget=budget)
        return providers, configs

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
        always, plus hybrid when an embedder is configured. Call
        ``ensure_index`` first: the coordinator's deadline starts here.
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
        result = _with_open_question_labels(repository, result, filter_policy, now=moment)
        answered = set(result.labels.providers)
        seen = {name: captured[name] for name in list(captured) if name in answered}
        return RecallOutcome(result=result, observed=seen)

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
        return open_question_labels_for(repository, memory_ids, policy, now=moment)

    # --- memory_context (C1) ---------------------------------------------------

    @staticmethod
    def context_request(v: object, policy: object) -> tuple[int, str]:
        """Validate ``memory_context`` ``v`` and ``policy`` (see ``memfw.context``)."""

        return context_request(v, policy)

    def _visible(self, document: MemoryDocument) -> bool:
        """v1's rule: the bound tenant and, when set, an allowed source."""

        if document.tenant != self.tenant:
            return False
        return not self.allowed_sources or document.source in self.allowed_sources

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
            return evaluate(meta_from_document(document), rules, now=now).kept

        return keep

    def context_v1(self, repository: Repository, *, limit_tokens: Any) -> dict[str, Any]:
        """The original ``memory_context`` payload for the caller's open vault."""

        return core_context_v1(repository, keep=self._visible, limit_tokens=limit_tokens)

    def context_pack(
        self,
        repository: Repository,
        *,
        policy_name: str,
        limit_tokens: int,
        now: datetime | None = None,
    ) -> ContextPack:
        """The ``memory_context`` v2 pack (one ``core`` slot) for the caller's open vault.

        ``repository`` is the caller's per-call ``Repository`` (never one opened
        from ``self.root``). ``policy_name`` is ``compat_fts`` or ``strict``;
        anything else raises ``ValueError`` before the vault is read.
        """

        moment = now if now is not None else datetime.now(UTC)
        keep = self.context_keep(policy_name, now=moment)
        return build_core_pack(
            repository, keep=keep, policy=policy_name, limit_tokens=limit_tokens, now=moment
        )


def _with_open_question_labels(
    repository: Repository, result: RecallResult, policy: Any, *, now: datetime
) -> RecallResult:
    """``result`` with each hit's open-question labels appended to its ``labels``."""

    if not result.hits:
        return result
    from dataclasses import replace

    from memfw.recall.builtin import open_question_labels_for

    extra = open_question_labels_for(
        repository, [hit.memory_id for hit in result.hits], policy, now=now
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
) -> Runtime:
    """The default assembly: today's behaviour, no ``runtime:`` config yet.

    ``root`` is kept as configured (``expanduser`` only; see ``Runtime``).
    Other values are taken as given; resolving them from the
    environment stays with the interface for now (A3 follow-up).
    """

    return Runtime(
        Path(root).expanduser(),
        tenant=tenant,
        allowed_sources=tuple(allowed_sources),
        embedder=embedder,
        entity_enabled=entity_enabled,
        provider_timeout=provider_timeout,
        total_timeout=total_timeout,
    )


__all__ = [
    "RECALL_POLICIES",
    "TWO_STAGE_PROVIDER_TIMEOUT",
    "TWO_STAGE_TOTAL_TIMEOUT",
    "Observed",
    "RecallOutcome",
    "Runtime",
    "build_runtime",
]
