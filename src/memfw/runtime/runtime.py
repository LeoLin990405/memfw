"""``Runtime`` and ``build_runtime``: recall wiring for the interfaces (A3a)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from memfw.recall.coordinator import InflightRegistry

if TYPE_CHECKING:
    from memfw.recall import RecallResult

#: Per-provider wait in ``mode=two_stage`` (the coordinator's total deadline
#: still applies). Above the 1.5 s port default so a cold SQLite page does not
#: count as a timeout.
TWO_STAGE_PROVIDER_TIMEOUT = 3.0
#: Total wait for all ``mode=two_stage`` providers and resolvers together.
TWO_STAGE_TOTAL_TIMEOUT = 4.0

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
    """Capability objects for one vault, built once per interface instance.

    Holds the settings the interface resolved (tenant, allowed sources,
    embedder, entity flag) and the ``InflightRegistry`` shared by every recall
    call, so a hung provider holds at most ``max_inflight`` worker threads per
    runtime, not one per call.
    """

    def __init__(
        self,
        root: Path,
        *,
        tenant: str,
        allowed_sources: tuple[str, ...],
        embedder: Any,
        entity_enabled: bool,
    ) -> None:
        self.root = root
        self.tenant = tenant
        self.allowed_sources = allowed_sources
        self.embedder = embedder
        self.entity_enabled = entity_enabled
        self.recall_inflight = InflightRegistry()

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
        budget = RecallBudget(timeout_seconds=TWO_STAGE_PROVIDER_TIMEOUT)
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
        root: Path,
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

        ``root`` is the opened vault's (resolved) root. ``limit`` is the page
        size, already clamped by the caller; ``as_of`` is already parsed.
        ``policy_name`` ``"strict"`` selects the strict filter; anything else
        is ``compat_fts`` (the interface validates the name). Providers: FTS
        always, plus hybrid when an embedder is configured.
        """

        # Imported here so callers that never recall never load them.
        from memfw.recall import Coordinator, FilterPolicy
        from memfw.recall.builtin import default_resolvers

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
        allowed = tuple(self.allowed_sources)
        if policy_name == "strict":
            filter_policy = FilterPolicy(
                tenant=self.tenant,
                allowed_sources=allowed,
                source=source,
                as_of=as_of,
                include_history=include_history,
            )
        else:
            filter_policy = FilterPolicy.compat_fts(
                tenant=self.tenant,
                allowed_sources=allowed,
                source=source,
                as_of=as_of,
                include_history=include_history,
            )
        coordinator = Coordinator(
            providers,
            resolvers=default_resolvers(root),
            configs=configs,
            inflight=self.recall_inflight,
            total_timeout=TWO_STAGE_TOTAL_TIMEOUT,
        )
        result = coordinator.search(query, limit=limit, policy=filter_policy)
        answered = set(result.labels.providers)
        seen = {name: captured[name] for name in list(captured) if name in answered}
        return RecallOutcome(result=result, observed=seen)


def build_runtime(
    root: Path | str,
    *,
    tenant: str = "",
    allowed_sources: tuple[str, ...] = (),
    embedder: Any = None,
    entity_enabled: bool = False,
) -> Runtime:
    """The default assembly: today's behaviour, no ``runtime:`` config yet.

    Values are taken as given; resolving them from the environment stays with
    the interface for now (A3 follow-up).
    """

    return Runtime(
        Path(root).expanduser(),
        tenant=tenant,
        allowed_sources=tuple(allowed_sources),
        embedder=embedder,
        entity_enabled=entity_enabled,
    )


__all__ = [
    "TWO_STAGE_PROVIDER_TIMEOUT",
    "TWO_STAGE_TOTAL_TIMEOUT",
    "Observed",
    "RecallOutcome",
    "Runtime",
    "build_runtime",
]
