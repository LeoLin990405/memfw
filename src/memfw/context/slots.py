"""Several slots in one pack (C2): the providers, the request check and the assembler.

``assemble_pack`` runs one ``ContextSlotProvider`` per requested slot, in
parallel, each with its own timeout. A provider that raises, returns
something other than its own ``ContextSlot``, is still running from an
earlier call (``busy``) or misses its deadline comes back as an empty slot
marked ``degraded`` with a reason code; the other slots are returned as
usual. Slots keep the requested order and ``pack_version`` covers each one.

The providers here need only ``memfw.core`` and ``memfw.ports``: ``core``
reads the vault itself; ``handoff`` and ``topics`` take what they need from
another capability (the conversation share, the concept catalogue) as an
injected callable, which the composition root (``memfw.runtime``) builds.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from memfw.core.errors import IntegrityError, MemfwError, NotFoundError
from memfw.core.models import MemoryDocument
from memfw.core.repository import Repository
from memfw.ports.context import (
    ITEM_LABEL_CHARS,
    ITEM_MAX_REFS,
    ITEM_TEXT_CHARS,
    ContextItem,
    ContextPack,
    ContextSlot,
    ContextSlotProvider,
    SlotRequest,
)
from memfw.ports.types import PATH_REF_RE

from .core import CORE_SLOT, Keep, build_core_slot, estimate_tokens, limit_chars

HANDOFF_SLOT = "handoff"
TOPICS_SLOT = "topics"
#: Every slot ``memory_context`` knows, in the order ``resources/list`` shows them.
SLOT_NAMES: tuple[str, ...] = (CORE_SLOT, HANDOFF_SLOT, TOPICS_SLOT)
DEFAULT_SLOTS: tuple[str, ...] = (CORE_SLOT,)

#: Reason codes of a degraded slot.
DEGRADED_TIMEOUT = "timeout"
DEGRADED_ERROR = "error"
DEGRADED_BUSY = "busy"
DEGRADED_INVALID = "invalid"
DEGRADED_UNAVAILABLE = "unavailable"
#: The vault has a pending transaction (``memfw recover`` first).
DEGRADED_PENDING_RECOVERY = "pending_recovery"
#: Other vault errors (``memfw.core.errors``), by class.
DEGRADED_INTEGRITY = "integrity"
DEGRADED_NOT_FOUND = "not_found"
DEGRADED_VAULT_ERROR = "vault_error"

_log = logging.getLogger(__name__)

#: How many characters of a session's goal a handoff item carries.
HANDOFF_TEXT_CHARS = 280
#: How many of the latest visible sessions the ``handoff`` slot summarises.
HANDOFF_SESSIONS = 3
#: How many of a session's latest turn refs a handoff item lists.
HANDOFF_REFS = 3


class SlotDegraded(Exception):  # noqa: N818 - a signal, not a failure of the caller
    """Raised by a provider to degrade its slot with a specific ``reason`` code."""

    def __init__(self, reason: str, message: str = "") -> None:
        super().__init__(message or reason)
        self.reason = reason


class SlotUnavailable(SlotDegraded):
    """Raised by a provider whose backing store is not there (``degraded: unavailable``)."""

    def __init__(self, message: str = "") -> None:
        super().__init__(DEGRADED_UNAVAILABLE, message)


def degraded_reason(exc: BaseException) -> str:
    """The reason code for a provider failure: specific for vault errors, else ``error``."""

    if isinstance(exc, SlotDegraded):
        return exc.reason
    if isinstance(exc, NotFoundError):
        return DEGRADED_NOT_FOUND
    if isinstance(exc, IntegrityError):
        return DEGRADED_INTEGRITY
    if isinstance(exc, MemfwError):
        return DEGRADED_VAULT_ERROR
    return DEGRADED_ERROR


def _vault_read(repository: Repository, build: Callable[[], ContextSlot]) -> ContextSlot:
    """Run ``build``; a vault error with a pending transaction is ``pending_recovery``."""

    try:
        return build()
    except IntegrityError as exc:
        if repository.pending_path.exists():
            raise SlotDegraded(DEGRADED_PENDING_RECOVERY) from exc
        raise


def slots_request(v: int, slots: object) -> tuple[str, ...]:
    """Validate ``memory_context`` ``slots`` (after ``context_request`` checked ``v``).

    ``None`` is the default ``["core"]``. Otherwise a non-empty list of known
    slot names without repeats, kept in the caller's order; only for ``v=2``.
    """

    if slots is None:
        return DEFAULT_SLOTS
    if v != 2:
        raise ValueError("slots only applies to v=2")
    if not isinstance(slots, list) or not slots:
        raise ValueError("slots must be a non-empty list of slot names")
    if any(not isinstance(name, str) for name in slots):
        raise ValueError("slots must be a non-empty list of slot names")
    unknown = [name for name in slots if name not in SLOT_NAMES]
    if unknown:
        raise ValueError(f"unknown slot (known: {', '.join(SLOT_NAMES)})")
    if len(set(slots)) != len(slots):
        raise ValueError("slots must not repeat a name")
    return tuple(slots)


# --- providers ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CoreSlotProvider:
    """The ``core`` slot (anchors, then procedural memories) of one vault and caller."""

    repository: Repository
    keep: Keep
    name: str = CORE_SLOT

    def build(self, request: SlotRequest) -> ContextSlot:
        return _vault_read(
            self.repository,
            lambda: build_core_slot(
                self.repository, keep=self.keep, limit_tokens=request.limit_tokens
            ),
        )

    def build_raw(self, request: SlotRequest) -> ContextSlot:
        """``build`` without the reason mapping: vault errors propagate as in C1."""

        return build_core_slot(self.repository, keep=self.keep, limit_tokens=request.limit_tokens)


def cut_items(name: str, items: Sequence[ContextItem], limit_tokens: int) -> ContextSlot:
    """An item slot: whole items in order until the character budget runs out.

    An item is never cut part-way (its text is already short); each one that
    no longer fits, and every one after it, is overflow. An item's size is its
    label plus its text. The budget is ``memory_context``'s own
    (``limit_tokens * 4`` characters, at least 100).
    """

    budget = limit_chars(limit_tokens)
    carried: list[ContextItem] = []
    truncated: list[str] = []
    used = 0
    for item in items:
        size = len(item.label) + len(item.text)
        if truncated or used + size > budget:
            truncated.append(item.id)
            continue
        carried.append(item)
        used += size
    return ContextSlot(
        name=name,
        entries=tuple(carried),
        used_tokens_est=estimate_tokens(used),
        overflow=len(truncated),
        truncated_ids=tuple(truncated),
    )


@dataclass(frozen=True, slots=True)
class SessionSummary:
    """One shared session as the conversation share hands it off (already redacted).

    ``goal`` is the handoff pack's system-derived goal, ``refs`` its latest
    turn refs (newest last). The composition root builds these from
    ``ConversationShare.handoff``, which applies ``conversation_share`` for
    the calling app; this package never sees the store.
    """

    app: str
    session_id: str
    turns: int
    updated_at: str
    goal: str
    refs: tuple[str, ...] = ()


#: ``limit -> the latest visible sessions`` (newest first) for the calling app.
#: An empty sequence when the caller may read nothing; ``SlotUnavailable`` when
#: the store is missing.
SessionFeed = Callable[[int], Sequence[SessionSummary]]


def _short(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: max(0, limit - 1)] + "…"


@dataclass(frozen=True, slots=True)
class HandoffSlotProvider:
    """The ``handoff`` slot: one item per latest visible session, goal and turn refs only.

    Items are ``kind: session``, ``id: conversation:<app>/<session_id>``,
    ``label: <app> <updated_at>``, ``text``: the goal (at most
    ``HANDOFF_TEXT_CHARS``), ``count``: the session's turns, ``refs``: its
    latest turn refs. Fetch the full pack with ``conversations_handoff``.
    """

    feed: SessionFeed
    sessions: int = HANDOFF_SESSIONS
    name: str = HANDOFF_SLOT

    def build(self, request: SlotRequest) -> ContextSlot:
        items: list[ContextItem] = []
        seen: set[str] = set()
        for summary in self.feed(self.sessions):
            item_id = f"conversation:{summary.app}/{summary.session_id}"
            refs = tuple(ref for ref in summary.refs if PATH_REF_RE.fullmatch(ref))
            if item_id in seen or not PATH_REF_RE.fullmatch(item_id):
                continue
            seen.add(item_id)
            items.append(
                ContextItem.build(
                    "session",
                    item_id,
                    label=_short(f"{summary.app} {summary.updated_at}", ITEM_LABEL_CHARS),
                    text=_short(summary.goal, min(HANDOFF_TEXT_CHARS, ITEM_TEXT_CHARS)),
                    count=max(0, int(summary.turns)),
                    refs=refs[-min(HANDOFF_REFS, ITEM_MAX_REFS) :],
                )
            )
        return cut_items(self.name, items, request.limit_tokens)


@dataclass(frozen=True, slots=True)
class TopicCatalogue:
    """The concept catalogue as the topics slot needs it (built by the composition root).

    ``labels`` maps each listed concept (canonical oid) to its ``pref_label``;
    ``canonical`` maps every oid a memory may name (a merged concept
    included) to the listed oid it counts for.
    """

    labels: Mapping[str, str]
    canonical: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class TopicsSlotProvider:
    """The ``topics`` slot: concepts with how many visible memories are about them.

    A memory counts when ``keep`` (the call's policy) keeps it and it has no
    ``valid_to`` (the ``core`` slot's rule); it counts once per concept named
    in its ``about`` or ``applies_to`` (merged concepts follow
    ``canonical``). Only concepts with at least one visible memory are listed,
    most memories first, then by oid. Items are ``kind: topic``, ``id``: the
    oid, ``label``: the ``pref_label``, ``count``: the memories; no text and
    no memory body.
    """

    repository: Repository
    keep: Keep
    catalogue: Callable[[], TopicCatalogue]
    name: str = TOPICS_SLOT

    def build(self, request: SlotRequest) -> ContextSlot:
        return _vault_read(self.repository, lambda: self._build(request))

    def _build(self, request: SlotRequest) -> ContextSlot:
        catalogue = self.catalogue()
        counts: dict[str, int] = {}
        for document in self.repository.list():
            if not _counts(document, self.keep):
                continue
            named = {*document.about, *document.applies_to}
            for oid in {catalogue.canonical[item] for item in named if item in catalogue.canonical}:
                if oid in catalogue.labels:
                    counts[oid] = counts.get(oid, 0) + 1
        ordered = sorted(
            (oid for oid in counts if PATH_REF_RE.fullmatch(oid)),
            key=lambda oid: (-counts[oid], oid),
        )
        items = [
            ContextItem.build(
                "topic",
                oid,
                label=_short(catalogue.labels[oid] or oid, ITEM_LABEL_CHARS),
                count=counts[oid],
            )
            for oid in ordered
        ]
        return cut_items(self.name, items, request.limit_tokens)


def _counts(document: MemoryDocument, keep: Keep) -> bool:
    return document.valid_to is None and keep(document)


# --- assembly ----------------------------------------------------------------


class SlotInflight:
    """Running builds per slot name, shared by every call of one Runtime.

    A provider that missed its deadline keeps its worker thread until it
    returns; while ``limit`` of them run for one slot, a new call reports
    that slot ``busy`` instead of starting another thread.
    """

    def __init__(self, limit: int = 2) -> None:
        self.limit = limit
        self._lock = threading.Lock()
        self._running: dict[str, int] = {}

    def acquire(self, name: str) -> bool:
        with self._lock:
            if self._running.get(name, 0) >= self.limit:
                return False
            self._running[name] = self._running.get(name, 0) + 1
            return True

    def release(self, name: str) -> None:
        with self._lock:
            self._running[name] = max(0, self._running.get(name, 0) - 1)

    def running(self, name: str) -> int:
        with self._lock:
            return self._running.get(name, 0)


def assemble_pack(
    providers: Sequence[ContextSlotProvider],
    request: SlotRequest,
    *,
    timeouts: Mapping[str, float],
    default_timeout: float,
    inflight: SlotInflight | None = None,
) -> ContextPack:
    """One pack from ``providers``, in their order; a failing slot is degraded, not fatal."""

    names = [provider.name for provider in providers]
    if len(set(names)) != len(names):
        raise ValueError("slot providers must have unique names")
    inflight = inflight if inflight is not None else SlotInflight()
    results: dict[str, Any] = {}
    done: dict[str, threading.Event] = {}
    started = time.monotonic()

    def run(provider: ContextSlotProvider, finished: threading.Event) -> None:
        try:
            results[provider.name] = provider.build(request)
        except Exception as exc:  # noqa: BLE001 - any failure degrades this slot only
            reason = degraded_reason(exc)
            # The class name only: a message can carry vault paths or content.
            _log.warning(
                "context slot %s degraded: %s (%s)", provider.name, reason, type(exc).__name__
            )
            results[provider.name] = reason
        finally:
            inflight.release(provider.name)
            finished.set()

    for provider in providers:
        if not inflight.acquire(provider.name):
            _log.warning("context slot %s degraded: %s", provider.name, DEGRADED_BUSY)
            results[provider.name] = DEGRADED_BUSY
            continue
        finished = threading.Event()
        worker = threading.Thread(
            target=run, args=(provider, finished), name=f"memfw-slot-{provider.name}", daemon=True
        )
        try:
            worker.start()
        except Exception as exc:  # noqa: BLE001 - e.g. RuntimeError: can't start new thread
            inflight.release(provider.name)
            _log.warning(
                "context slot %s degraded: %s (%s)",
                provider.name,
                DEGRADED_ERROR,
                type(exc).__name__,
            )
            results[provider.name] = DEGRADED_ERROR
            continue
        done[provider.name] = finished

    slots: list[ContextSlot] = []
    for provider in providers:
        name = provider.name
        if name in done:
            deadline = started + float(timeouts.get(name, default_timeout))
            if not done[name].wait(max(0.0, deadline - time.monotonic())):
                _log.warning("context slot %s degraded: %s", name, DEGRADED_TIMEOUT)
                slots.append(ContextSlot.degraded_slot(name, DEGRADED_TIMEOUT))
                continue
        outcome = results.get(name, DEGRADED_ERROR)
        if isinstance(outcome, str):
            slots.append(ContextSlot.degraded_slot(name, outcome))
        elif type(outcome) is not ContextSlot or outcome.name != name:
            _log.warning("context slot %s degraded: %s", name, DEGRADED_INVALID)
            slots.append(ContextSlot.degraded_slot(name, DEGRADED_INVALID))
        else:
            slots.append(outcome)
    return ContextPack.build(slots, policy=request.policy, generated_at=request.now)


__all__ = [
    "DEFAULT_SLOTS",
    "DEGRADED_BUSY",
    "DEGRADED_ERROR",
    "DEGRADED_INTEGRITY",
    "DEGRADED_INVALID",
    "DEGRADED_NOT_FOUND",
    "DEGRADED_PENDING_RECOVERY",
    "DEGRADED_TIMEOUT",
    "DEGRADED_UNAVAILABLE",
    "DEGRADED_VAULT_ERROR",
    "HANDOFF_SLOT",
    "SLOT_NAMES",
    "TOPICS_SLOT",
    "CoreSlotProvider",
    "HandoffSlotProvider",
    "SessionFeed",
    "SessionSummary",
    "SlotDegraded",
    "SlotInflight",
    "SlotUnavailable",
    "TopicCatalogue",
    "TopicsSlotProvider",
    "assemble_pack",
    "cut_items",
    "degraded_reason",
    "slots_request",
]
