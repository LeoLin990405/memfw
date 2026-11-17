"""The ``current`` slot (U6): short-lived "what I'm working on now" context.

Identity and standing rules live in ``core`` (anchors, procedural memories).
``current`` is the other kind of resident context: the active ticket, the
meeting this afternoon, the task of the week. It is replaced quickly, so every
entry has a lifetime and the slot only ever carries what is valid at the
call's ``now``.

Which memories feed it (``is_current``):

- tagged ``current`` (any case, surrounding spaces ignored). A tag, not a new
  frontmatter key, so no memory file, schema or render changes; the ``layer``
  field is no help here because ``current`` is already every memory's default
  layer;
- valid at ``now``: ``valid_from`` absent or ``<= now``; with ``valid_to``,
  ``now < valid_to``; without it, ``now < created_at + ttl_days`` (the TTL,
  ``DEFAULT_CURRENT_TTL_DAYS`` unless the vault sets
  ``runtime.context.current_ttl_days``). A TTL of 0 means only memories with
  an explicit ``valid_to`` count. The TTL is anchored on ``created_at``, which
  no write ever changes after ``add``: ``updated_at`` is not usable, because
  mechanical writes (tier changes, autowarm, strength, privacy re-classing)
  refresh it and would bring an expired memory back. To extend a memory, set
  its ``valid_to``. A ``created_at`` more than ``CLOCK_SKEW`` after ``now``
  (a skewed clock, a hand-edited date) would never expire, so such a memory
  is not current;
- not superseded (``superseded_by`` unset): a replaced task is not current;
- kept by ``keep``, the call's visibility predicate (tenant, sources, privacy
  clearance and, under ``strict``, the recall strict policy), exactly as for
  ``core``.

Order: newest first (``created_at`` descending, then id), on the same anchor
so maintenance never reorders the slot. Budget and overflow
are ``core``'s: the same character budget, at most one memory cut part-way
(the last carried), the rest listed in ``truncated_ids``. Each entry is built
from one fresh read of its file (body, version and ``content_hash`` from the
same bytes), and the snapshot is checked again, at the same ``now``.

Never cached: validity depends on the clock, so a cached selection could
serve an item that expired after it was built; the slot is rebuilt from the
vault on every call (like ``handoff`` and ``topics``).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from memfw.core.models import MemoryDocument
from memfw.core.repository import Repository
from memfw.ports.context import ContextEntry, ContextSlot, SlotRequest

from .core import Keep, _cut, _snapshot, estimate_tokens, limit_chars

CURRENT_SLOT = "current"
#: The tag that puts a memory in the ``current`` slot.
CURRENT_TAG = "current"
#: Lifetime of a ``current`` memory without ``valid_to``, from its ``created_at``.
DEFAULT_CURRENT_TTL_DAYS = 7
#: Upper bound accepted for ``runtime.context.current_ttl_days``.
MAX_CURRENT_TTL_DAYS = 365
#: How far a TTL anchor (``created_at``) may lie after ``now`` and still count.
CLOCK_SKEW = timedelta(minutes=5)


def has_current_tag(document: MemoryDocument) -> bool:
    return any(isinstance(tag, str) and tag.strip().lower() == CURRENT_TAG for tag in document.tags)


def is_current(document: MemoryDocument, *, now: datetime, ttl_days: int) -> bool:
    """Tagged ``current``, not superseded and valid at ``now`` (see the module docstring)."""

    if not has_current_tag(document) or document.superseded_by:
        return False
    if document.valid_from is not None and document.valid_from > now:
        return False
    if document.valid_to is not None:
        return now < document.valid_to
    anchor = document.created_at
    if anchor > now + CLOCK_SKEW:
        return False
    return now < anchor + timedelta(days=max(0, ttl_days))


def select_current(
    documents: Sequence[MemoryDocument], keep: Keep, *, now: datetime, ttl_days: int
) -> list[MemoryDocument]:
    """The slot's memories, newest first (``created_at`` descending, then id)."""

    selected = [
        document
        for document in documents
        if is_current(document, now=now, ttl_days=ttl_days) and keep(document)
    ]
    selected.sort(key=lambda document: document.id)
    selected.sort(key=lambda document: document.created_at, reverse=True)
    return selected


@dataclass(frozen=True, slots=True)
class CurrentSlotProvider:
    """The ``current`` slot of one vault and caller (``ContextSlotProvider``).

    ``ttl_days`` is the lifetime of a memory without ``valid_to``; the
    composition root passes the vault's ``runtime.context.current_ttl_days``.
    """

    repository: Repository
    keep: Keep
    ttl_days: int = DEFAULT_CURRENT_TTL_DAYS
    name: str = CURRENT_SLOT

    def build(self, request: SlotRequest) -> ContextSlot:
        from .slots import _vault_read

        return _vault_read(self.repository, lambda: self._build(request))

    def _build(self, request: SlotRequest) -> ContextSlot:
        budget = limit_chars(request.limit_tokens)
        now = request.now
        pool = select_current(self.repository.list(), self.keep, now=now, ttl_days=self.ttl_days)
        hashes: dict[str, str] = {}
        snapshots: list[MemoryDocument] = []
        for document in pool:
            snapshot = _snapshot(self.repository, document)
            if snapshot is None:
                continue
            hashes[snapshot[0].id] = snapshot[1]
            snapshots.append(snapshot[0])
        selected = select_current(snapshots, self.keep, now=now, ttl_days=self.ttl_days)
        cut = _cut(selected, budget)
        entries = tuple(
            ContextEntry(
                id=document.id,
                title=document.title,
                type=document.type,
                layer=document.layer.value,
                anchor=document.anchor,
                epistemic=document.epistemic,
                causal=document.causal,
                text=text,
                version=document.version,
                content_hash=hashes[document.id],
            )
            for document, text in cut.carried
        )
        return ContextSlot(
            name=self.name,
            entries=entries,
            used_tokens_est=estimate_tokens(cut.used_chars),
            overflow=len(cut.truncated_ids),
            truncated_ids=cut.truncated_ids,
            skipped=len(pool) - len(selected),
        )


__all__ = [
    "CLOCK_SKEW",
    "CURRENT_SLOT",
    "CURRENT_TAG",
    "DEFAULT_CURRENT_TTL_DAYS",
    "MAX_CURRENT_TTL_DAYS",
    "CurrentSlotProvider",
    "has_current_tag",
    "is_current",
    "select_current",
]
