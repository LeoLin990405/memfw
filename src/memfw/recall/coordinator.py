"""The recall coordinator: route → recall → resolve → filter → fuse → label → metrics.

One ``Coordinator.search`` call:

1. **Route** the query to one or more providers (``DefaultRouter``).
2. **Recall** from each routed provider in a bounded worker thread. The
   worker runs ``available()`` and then ``search()``; both are under the
   provider's budget and the call's total deadline. A provider that is
   unavailable, raises, times out, is still busy with an earlier call, or
   returns malformed output is marked degraded; the others carry on. When
   every routed provider failed and some were left out by the route, those
   run as a fallback wave.
3. **Resolve** refs to memory ids through the resolver for each ref kind,
   also in a bounded worker under the same deadline. A ref nobody resolves
   in time is dropped (``unresolved``).
4. **Filter** with the kernel rules (``memfw.recall.policy``). Missing
   metadata fails closed.
5. **Fuse** the filtered lists with weighted RRF and a deterministic order.
6. **Label** each hit with its providers, and the result with degraded
   providers/resolvers, projection versions and ``fail_closed``.
7. **Metrics**: latency, counts and drop reasons as fixed codes. The query
   text and exception messages or type names from adapter code are never
   recorded.

A worker that overruns cannot be killed. Each provider (and each resolver)
has an in-flight limit (default 1): while an earlier call is still running,
new calls skip it as ``busy``, so a hung adapter holds at most that many
threads instead of one per search.

Body text never passes through here; callers read it from the vault by id.
"""

from __future__ import annotations

import logging
import math
import re
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from memfw.ports.protocols import RecallProvider
from memfw.ports.types import (
    MEMORY_ID_RE,
    Availability,
    Candidate,
    ExtraValue,
    ProviderUnavailable,
    RecallBudget,
    RefKind,
    check_name,
    validate_candidate,
)

from .fusion import DEFAULT_RRF_K, rrf_fuse
from .policy import FilterPolicy, MemoryMeta, Verdict, evaluate
from .resolve import Resolution, Resolver
from .routing import DefaultRouter, RouteDecision

_log = logging.getLogger(__name__)

ProviderStatus = Literal["ok", "unavailable", "timeout", "error", "invalid", "busy"]

DEFAULT_TOTAL_TIMEOUT = 4.0
MAX_LIMIT = 200

_CODE_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_VERSION_RE = re.compile(r"^[A-Za-z0-9._:+-]{1,64}$")


class Router(Protocol):
    def route(
        self, query: str, providers: Sequence[str], *, lexical: frozenset[str]
    ) -> RouteDecision: ...


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    """Per-provider settings.

    ``overfetch`` multiplies the requested limit; ``max_inflight`` caps the
    calls to this provider that may run at once across searches.
    """

    weight: float = 1.0
    budget: RecallBudget = field(default_factory=RecallBudget)
    lexical: bool | None = None
    overfetch: int = 1
    max_inflight: int = 1

    def __post_init__(self) -> None:
        if (
            isinstance(self.weight, bool)
            or not isinstance(self.weight, int | float)
            or not math.isfinite(self.weight)
            or self.weight < 0
        ):
            raise ValueError("weight must be a finite number >= 0")
        if self.overfetch < 1:
            raise ValueError("overfetch must be at least 1")
        if self.max_inflight < 1:
            raise ValueError("max_inflight must be at least 1")


@dataclass(frozen=True, slots=True)
class ProviderReport:
    """What one provider did in one call. ``reason`` is a fixed code."""

    name: str
    status: ProviderStatus
    reason: str = ""
    latency_ms: float = 0.0
    candidates: int = 0
    invalid: int = 0
    kept: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "reason": self.reason,
            "latency_ms": round(self.latency_ms, 3),
            "candidates": self.candidates,
            "invalid": self.invalid,
            "kept": self.kept,
        }


@dataclass(frozen=True, slots=True)
class RecallHit:
    """One fused result. Ids, scores and labels only; no body text."""

    memory_id: str
    score: float
    providers: tuple[str, ...]
    ranks: Mapping[str, int]
    provider_scores: Mapping[str, float]
    labels: tuple[str, ...] = ()
    extra: Mapping[str, ExtraValue] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RecallLabels:
    route: str
    providers: tuple[str, ...]
    degraded: Mapping[str, str]
    projection_versions: Mapping[str, str]
    fail_closed: bool
    fallback_reason: str = ""
    resolvers_degraded: Mapping[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "route": self.route,
            "providers": list(self.providers),
            "degraded": dict(self.degraded),
            "projection_versions": dict(self.projection_versions),
            "fail_closed": self.fail_closed,
            "fallback_reason": self.fallback_reason,
            "resolvers_degraded": dict(self.resolvers_degraded),
        }


@dataclass(frozen=True, slots=True)
class RecallMetrics:
    """Counts and timings for one call. Never contains the query."""

    total_ms: float
    route: str
    providers: tuple[ProviderReport, ...]
    resolved: int
    returned: int
    dropped: Mapping[str, int]
    privacy_source_mismatch: int = 0
    resolver_errors: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "total_ms": round(self.total_ms, 3),
            "route": self.route,
            "providers": [report.as_dict() for report in self.providers],
            "resolved": self.resolved,
            "returned": self.returned,
            "dropped": dict(sorted(self.dropped.items())),
            "privacy_source_mismatch": self.privacy_source_mismatch,
            "resolver_errors": self.resolver_errors,
        }


@dataclass(frozen=True, slots=True)
class RecallResult:
    hits: tuple[RecallHit, ...]
    labels: RecallLabels
    metrics: RecallMetrics

    @property
    def ids(self) -> list[str]:
        return [hit.memory_id for hit in self.hits]


class _Slot:
    """Result holder for one worker thread."""

    __slots__ = ("done", "error", "finished_at", "value")

    def __init__(self) -> None:
        self.done = threading.Event()
        self.value: object = None
        self.error: BaseException | None = None
        self.finished_at = 0.0


@dataclass(frozen=True, slots=True)
class _Unavailable:
    reason: str


@dataclass(slots=True)
class _Outcome:
    report: ProviderReport
    candidates: list[Candidate]
    projection_version: str = ""


def _code(reason: object, default: str) -> str:
    """A provider-supplied reason, kept only if it already is a short code."""

    return reason if isinstance(reason, str) and _CODE_RE.fullmatch(reason) else default


def _version(value: object) -> str:
    return value if isinstance(value, str) and _VERSION_RE.fullmatch(value) else ""


class Coordinator:
    """Runs recall providers and turns their refs into filtered, fused memory ids."""

    def __init__(
        self,
        providers: Sequence[RecallProvider],
        *,
        resolvers: Mapping[RefKind, Resolver],
        configs: Mapping[str, ProviderConfig] | None = None,
        router: Router | None = None,
        total_timeout: float = DEFAULT_TOTAL_TIMEOUT,
        rrf_k: int = DEFAULT_RRF_K,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        timer: Callable[[], float] = time.monotonic,
        metrics_sink: Callable[[RecallMetrics], None] | None = None,
    ) -> None:
        names: list[str] = []
        for provider in providers:
            check_name(provider.name, what="provider")
            if provider.name in names:
                raise ValueError(f"duplicate provider {provider.name!r}")
            names.append(provider.name)
        if not (isinstance(total_timeout, int | float) and math.isfinite(total_timeout)):
            raise ValueError("total_timeout must be a finite number")
        if not total_timeout > 0:
            raise ValueError("total_timeout must be positive")
        self.providers: dict[str, RecallProvider] = {p.name: p for p in providers}
        self.order: tuple[str, ...] = tuple(names)
        self.resolvers = dict(resolvers)
        self.configs = dict(configs or {})
        unknown = set(self.configs) - set(names)
        if unknown:
            raise ValueError("config for unknown provider(s)")
        self.router: Router = router or DefaultRouter()
        self.total_timeout = float(total_timeout)
        self.rrf_k = rrf_k
        self.clock = clock
        self.timer = timer
        self.metrics_sink = metrics_sink
        self.last_metrics: RecallMetrics | None = None
        self._lock = threading.Lock()
        self._inflight: dict[str, int] = {}

    # -- configuration -------------------------------------------------

    def config(self, name: str) -> ProviderConfig:
        return self.configs.get(name) or ProviderConfig()

    def lexical(self) -> frozenset[str]:
        out = set()
        for name, provider in self.providers.items():
            flag = self.config(name).lexical
            if flag is None:
                flag = bool(getattr(provider, "lexical", False))
            if flag:
                out.add(name)
        return frozenset(out)

    def inflight(self, key: str) -> int:
        """Running workers for ``provider:<name>`` or ``resolver:<kind>``."""

        with self._lock:
            return self._inflight.get(key, 0)

    # -- workers -------------------------------------------------------

    def _spawn(self, key: str, limit: int, call: Callable[[], object]) -> _Slot | None:
        """Start ``call`` in a daemon thread unless ``key`` is at its in-flight limit."""

        with self._lock:
            if self._inflight.get(key, 0) >= limit:
                return None
            self._inflight[key] = self._inflight.get(key, 0) + 1
        slot = _Slot()

        def run() -> None:
            try:
                slot.value = call()
            except BaseException as exc:  # noqa: BLE001 - reported as degraded
                slot.error = exc
            finally:
                slot.finished_at = self.timer()
                with self._lock:
                    self._inflight[key] -= 1
                slot.done.set()

        thread = threading.Thread(target=run, name=f"memfw-recall-{key}", daemon=True)
        try:
            thread.start()
        except BaseException:
            with self._lock:
                self._inflight[key] -= 1
            raise
        return slot

    # -- search --------------------------------------------------------

    def search(self, query: str, *, limit: int = 10, policy: FilterPolicy) -> RecallResult:
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query required")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive integer")
        limit = min(limit, MAX_LIMIT)
        started = self.timer()
        deadline = started + self.total_timeout
        decision = self.router.route(query, self.order, lexical=self.lexical())
        routed = tuple(name for name in decision.providers if name in self.providers)
        outcomes = self._recall(query, routed, limit=limit, policy=policy, deadline=deadline)
        fallback_reason = ""
        if routed and not any(item.report.status == "ok" for item in outcomes.values()):
            rest = tuple(name for name in self.order if name not in routed)
            if rest and self.timer() < deadline:
                fallback_reason = "routed_providers_failed"
                outcomes.update(
                    self._recall(query, rest, limit=limit, policy=policy, deadline=deadline)
                )
        tried = tuple(name for name in self.order if name in outcomes)
        ok = [name for name in tried if outcomes[name].report.status == "ok"]
        resolutions, resolver_degraded = self._resolve(
            [candidate for name in ok for candidate in outcomes[name].candidates],
            deadline=deadline,
        )
        now = self.clock()
        verdicts: dict[str, Verdict] = {}
        unresolved_refs: set[tuple[str, str]] = set()
        lists: list[tuple[str, list[str]]] = []
        first_candidate: dict[str, Candidate] = {}
        for name in ok:
            kept: list[str] = []
            for candidate in outcomes[name].candidates:
                resolution = resolutions.get((candidate.ref_kind, candidate.ref))
                if resolution is None:
                    unresolved_refs.add((candidate.ref_kind, candidate.ref))
                    continue
                memory_id = resolution.memory_id
                verdict = verdicts.get(memory_id)
                if verdict is None:
                    verdict = self._verdict(resolution, policy, now)
                    verdicts[memory_id] = verdict
                if not verdict.kept:
                    continue
                if memory_id not in kept:
                    kept.append(memory_id)
                    first_candidate.setdefault(memory_id, candidate)
            outcomes[name].report = _with(outcomes[name].report, kept=len(kept))
            lists.append((name, kept))
        dropped: dict[str, int] = {}
        if unresolved_refs:
            dropped["unresolved"] = len(unresolved_refs)
        mismatch = 0
        for verdict in verdicts.values():
            if verdict.reason is not None:
                dropped[verdict.reason] = dropped.get(verdict.reason, 0) + 1
            if verdict.privacy_mismatch:
                mismatch += 1
        weights = {name: self.config(name).weight for name in ok}
        item_weights = {mid: verdict.weight for mid, verdict in verdicts.items() if verdict.kept}
        fused = rrf_fuse(
            lists, weights=weights, item_weights=item_weights, k=self.rrf_k, limit=limit
        )
        scores_by_id: dict[str, dict[str, float]] = {}
        for name in ok:
            for candidate in outcomes[name].candidates:
                resolution = resolutions.get((candidate.ref_kind, candidate.ref))
                if resolution is not None:
                    per = scores_by_id.setdefault(resolution.memory_id, {})
                    per.setdefault(name, float(candidate.score))
        fail_closed = not ok
        hits = (
            ()
            if fail_closed
            else tuple(
                RecallHit(
                    memory_id=item.memory_id,
                    score=item.score,
                    providers=item.providers,
                    ranks=item.ranks,
                    provider_scores=dict(scores_by_id.get(item.memory_id, {})),
                    labels=verdicts[item.memory_id].labels,
                    extra=dict(first_candidate[item.memory_id].extra),
                )
                for item in fused
            )
        )
        degraded = {
            name: _degraded_code(outcomes[name].report)
            for name in tried
            if outcomes[name].report.status != "ok"
        }
        versions = {
            name: outcomes[name].projection_version
            for name in ok
            if outcomes[name].projection_version
        }
        labels = RecallLabels(
            route=decision.kind,
            providers=tuple(ok),
            degraded=degraded,
            projection_versions=versions,
            fail_closed=fail_closed,
            fallback_reason=fallback_reason or ("all_providers_failed" if fail_closed else ""),
            resolvers_degraded=resolver_degraded,
        )
        metrics = RecallMetrics(
            total_ms=max(0.0, self.timer() - started) * 1000.0,
            route=decision.kind,
            providers=tuple(outcomes[name].report for name in tried),
            resolved=len({res.memory_id for res in resolutions.values()}),
            returned=len(hits),
            dropped=dropped,
            privacy_source_mismatch=mismatch,
            resolver_errors=len(resolver_degraded),
        )
        self.last_metrics = metrics
        if self.metrics_sink is not None:
            try:
                self.metrics_sink(metrics)
            except Exception:  # noqa: BLE001 - metrics never break a search
                _log.warning("recall metrics sink failed")
        return RecallResult(hits=hits, labels=labels, metrics=metrics)

    # -- steps ---------------------------------------------------------

    @staticmethod
    def _verdict(resolution: Resolution, policy: FilterPolicy, now: datetime) -> Verdict:
        meta = resolution.meta
        if meta is not None and meta.memory_id != resolution.memory_id:
            return Verdict("id_mismatch")
        return evaluate(meta, policy, now=now)

    def _recall(
        self,
        query: str,
        names: Sequence[str],
        *,
        limit: int,
        policy: FilterPolicy,
        deadline: float,
    ) -> dict[str, _Outcome]:
        outcomes: dict[str, _Outcome] = {}
        running: list[tuple[str, _Slot, float, float, int]] = []
        for name in names:
            provider = self.providers[name]
            config = self.config(name)
            began = self.timer()
            fetch = min(limit * config.overfetch, config.budget.max_candidates)
            slot = self._spawn(
                f"provider:{name}",
                config.max_inflight,
                _provider_call(provider, query, fetch, config.budget, policy),
            )
            if slot is None:
                outcomes[name] = _Outcome(ProviderReport(name, "busy", "busy"), [])
                continue
            running.append((name, slot, began, began + config.budget.timeout_seconds, fetch))
        for name, slot, began, own_deadline, fetch in running:
            wait = min(own_deadline, deadline) - self.timer()
            if not slot.done.wait(timeout=max(0.0, wait)):
                outcomes[name] = _Outcome(
                    ProviderReport(name, "timeout", "timeout", self._ms(began)), []
                )
                continue
            latency = max(0.0, slot.finished_at - began) * 1000.0
            if slot.error is not None:
                if isinstance(slot.error, ProviderUnavailable):
                    report = ProviderReport(name, "unavailable", "unavailable", latency)
                else:
                    report = ProviderReport(name, "error", "exception", latency)
                outcomes[name] = _Outcome(report, [])
                continue
            if isinstance(slot.value, _Unavailable):
                report = ProviderReport(name, "unavailable", slot.value.reason, latency)
                outcomes[name] = _Outcome(report, [])
                continue
            outcomes[name] = self._validated(name, slot.value, latency, fetch)
        return outcomes

    def _validated(self, name: str, value: object, latency: float, fetch: int) -> _Outcome:
        """Check a provider's return value. Never raises; bad output is ``invalid``."""

        try:
            if isinstance(value, str | bytes) or not isinstance(value, Sequence):
                return _Outcome(ProviderReport(name, "invalid", "not_a_sequence", latency), [])
            items = list(value)
            good: list[Candidate] = []
            invalid = 0
            for item in items:
                try:
                    validate_candidate(item)
                except Exception:  # noqa: BLE001 - any failure is an invalid candidate
                    invalid += 1
                    continue
                if item.provider != name:
                    invalid += 1
                    continue
                good.append(item)
            total = len(items)
            good = good[:fetch]
            version = _version(getattr(self.providers[name], "projection_version", ""))
        except Exception:  # noqa: BLE001 - hostile return value
            return _Outcome(ProviderReport(name, "invalid", "bad_output", latency), [])
        if total and not good:
            return _Outcome(
                ProviderReport(name, "invalid", "no_valid_candidates", latency, total, invalid), []
            )
        return _Outcome(ProviderReport(name, "ok", "", latency, total, invalid), good, version)

    def _resolve(
        self, candidates: Sequence[Candidate], *, deadline: float
    ) -> tuple[dict[tuple[str, str], Resolution], dict[str, str]]:
        """Resolve per ref kind in bounded workers. Failures leave refs unresolved."""

        by_kind: dict[str, set[str]] = {}
        for candidate in candidates:
            by_kind.setdefault(candidate.ref_kind, set()).add(candidate.ref)
        out: dict[tuple[str, str], Resolution] = {}
        degraded: dict[str, str] = {}
        running: list[tuple[str, list[str], _Slot]] = []
        for kind in sorted(by_kind):
            resolver = self.resolvers.get(kind)  # type: ignore[call-overload]
            if resolver is None:
                continue
            refs = sorted(by_kind[kind])
            slot = self._spawn(f"resolver:{kind}", 1, _resolver_call(resolver, refs))
            if slot is None:
                degraded[kind] = "busy"
                continue
            running.append((kind, refs, slot))
        for kind, refs, slot in running:
            if not slot.done.wait(timeout=max(0.0, deadline - self.timer())):
                degraded[kind] = "timeout"
                continue
            if slot.error is not None:
                degraded[kind] = "error"
                _log.warning("recall resolver for %s failed", kind)
                continue
            try:
                found = slot.value
                if not isinstance(found, Mapping):
                    raise TypeError("resolver must return a mapping")
                for ref in refs:
                    resolution = found.get(ref)
                    if (
                        type(resolution) is Resolution
                        and isinstance(resolution.memory_id, str)
                        and MEMORY_ID_RE.fullmatch(resolution.memory_id)
                        and (resolution.meta is None or type(resolution.meta) is MemoryMeta)
                    ):
                        out[(kind, ref)] = resolution
            except Exception:  # noqa: BLE001 - malformed resolver output
                degraded[kind] = "invalid"
        return out, degraded

    def _ms(self, began: float) -> float:
        return max(0.0, self.timer() - began) * 1000.0


def _provider_call(
    provider: RecallProvider,
    query: str,
    limit: int,
    budget: RecallBudget,
    policy: FilterPolicy,
) -> Callable[[], object]:
    """``available()`` then ``search()``, both inside the bounded worker."""

    def call() -> object:
        try:
            availability = provider.available()
        except Exception:  # noqa: BLE001
            return _Unavailable("availability_error")
        if not isinstance(availability, Availability):
            return _Unavailable("availability_error")
        if not availability.available:
            return _Unavailable(_code(availability.reason, "unavailable"))
        return provider.search(
            query, limit=limit, budget=budget, tenant=policy.tenant, as_of=policy.as_of
        )

    return call


def _resolver_call(resolver: Resolver, refs: list[str]) -> Callable[[], object]:
    def call() -> object:
        return resolver.resolve(refs)

    return call


def _with(report: ProviderReport, *, kept: int) -> ProviderReport:
    return ProviderReport(
        report.name,
        report.status,
        report.reason,
        report.latency_ms,
        report.candidates,
        report.invalid,
        kept,
    )


def _degraded_code(report: ProviderReport) -> str:
    if not report.reason or report.reason == report.status:
        return report.status
    return f"{report.status}:{report.reason}"


__all__ = [
    "DEFAULT_TOTAL_TIMEOUT",
    "MAX_LIMIT",
    "Coordinator",
    "ProviderConfig",
    "ProviderReport",
    "ProviderStatus",
    "RecallHit",
    "RecallLabels",
    "RecallMetrics",
    "RecallResult",
    "Router",
]
