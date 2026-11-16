"""The session-start context pack (CAG, iteration C1). Stdlib only.

A ``ContextPack`` is what a client puts in front of a session once, instead
of calling a retrieval tool: ordered slots of memory entries, each slot with
its own budget accounting, plus a version key.

``pack_version`` is a sha256 over a canonical encoding of the pack's
*identity*: the policy name and, per slot in order, its name, every entry's
``(id, version, content_hash, chars)`` and the ``truncated_ids``. It never
covers body text, titles or the query that produced the pack, so it can be
logged or used as a cache key without leaking content. ``chars`` (how many
characters of the body the entry carries) is in it so that two packs that cut
the same memory at different lengths never share a version.

Every type is a frozen dataclass validated at construction, like the other
port types; ``ContextPack`` also checks that ``pack_version`` matches its
contents.

C2 adds slots that carry something other than memories (``ContextItem``: a
conversation handoff summary, a topic of the concept catalogue), a slot
marked ``degraded`` (with a reason code) when its producer failed or timed
out, and the ``ContextSlotProvider`` protocol: one provider per slot name,
assembled into one pack by the caller.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from .types import CAUSAL_VALUES, EPISTEMIC_VALUES, MEMORY_ID_RE, PATH_REF_RE, check_name

#: ``content_hash`` format: the same ``sha256:<hex>`` the index and
#: ``memory_search`` report for a memory file.
CONTENT_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
PACK_VERSION_RE = re.compile(r"^[0-9a-f]{64}$")
#: Bumped when the canonical encoding below changes.
PACK_ENCODING = "memfw-context-pack/1"


@dataclass(frozen=True, slots=True)
class ContextEntry:
    """One memory in a slot. ``text`` may be a prefix of the body (see the slot)."""

    id: str
    title: str
    type: str | None
    layer: str
    anchor: str | None
    epistemic: str | None
    causal: str
    text: str
    version: int
    content_hash: str

    def __post_init__(self) -> None:
        if type(self.id) is not str or not MEMORY_ID_RE.fullmatch(self.id):
            raise ValueError("entry id does not match the memory id pattern")
        if not isinstance(self.title, str):
            raise ValueError("entry title must be a string")
        if self.type is not None and not isinstance(self.type, str):
            raise ValueError("entry type must be a string or None")
        if not isinstance(self.layer, str) or not self.layer:
            raise ValueError("entry layer required")
        if self.anchor is not None and not isinstance(self.anchor, str):
            raise ValueError("entry anchor must be a string or None")
        if self.epistemic is not None and self.epistemic not in EPISTEMIC_VALUES:
            raise ValueError(f"entry epistemic must be one of {EPISTEMIC_VALUES}")
        if self.causal not in CAUSAL_VALUES:
            raise ValueError(f"entry causal must be one of {CAUSAL_VALUES}")
        if not isinstance(self.text, str):
            raise ValueError("entry text must be a string")
        if type(self.version) is not int or self.version < 0:
            raise ValueError("entry version must be a non-negative integer")
        if type(self.content_hash) is not str or not CONTENT_HASH_RE.fullmatch(self.content_hash):
            raise ValueError("entry content_hash must be sha256:<64 lowercase hex>")

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "type": self.type,
            "layer": self.layer,
            "anchor": self.anchor,
            "epistemic": self.epistemic,
            "causal": self.causal,
            "text": self.text,
            "version": self.version,
            "content_hash": self.content_hash,
        }


#: Upper bounds for a ``ContextItem``: the label and the text are short by contract.
ITEM_LABEL_CHARS = 200
ITEM_TEXT_CHARS = 1000
ITEM_MAX_REFS = 20


def item_content_hash(
    kind: str, item_id: str, label: str, text: str, count: int | None, refs: Sequence[str]
) -> str:
    """``sha256:<hex>`` over an item's content (canonical JSON). Only the hash is versioned."""

    document = [kind, item_id, label, text, count, list(refs)]
    data = json.dumps(document, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    return "sha256:" + hashlib.sha256(data).hexdigest()


@dataclass(frozen=True, slots=True)
class ContextItem:
    """One non-memory entry of a slot: a handoff summary or a topic.

    ``id`` is a stable reference (``conversation:<app>/<session_id>``, a
    concept oid such as ``concept:service-port``); ``label`` and ``text`` are
    short (``text`` is empty for a topic); ``count`` is what the item counts
    (turns of a session, visible memories of a topic) or ``None``; ``refs``
    point to where the full content is (turn refs for ``conversations_get``).
    ``content_hash`` must be ``item_content_hash`` of the other fields, so a
    changed label, text, count or ref changes ``pack_version``. Build one with
    ``ContextItem.build``.
    """

    kind: str
    id: str
    label: str
    text: str
    count: int | None
    refs: tuple[str, ...]
    content_hash: str

    def __post_init__(self) -> None:
        check_name(self.kind, what="item kind")
        object.__setattr__(self, "refs", tuple(self.refs))
        if type(self.id) is not str or not PATH_REF_RE.fullmatch(self.id):
            raise ValueError("item id must be a reference (no whitespace or quotes)")
        if not isinstance(self.label, str) or len(self.label) > ITEM_LABEL_CHARS:
            raise ValueError(f"item label must be a string of at most {ITEM_LABEL_CHARS} chars")
        if not isinstance(self.text, str) or len(self.text) > ITEM_TEXT_CHARS:
            raise ValueError(f"item text must be a string of at most {ITEM_TEXT_CHARS} chars")
        if self.count is not None and (type(self.count) is not int or self.count < 0):
            raise ValueError("item count must be a non-negative integer or None")
        if len(self.refs) > ITEM_MAX_REFS or any(
            type(ref) is not str or not PATH_REF_RE.fullmatch(ref) for ref in self.refs
        ):
            raise ValueError(f"item refs must be at most {ITEM_MAX_REFS} references")
        expected = item_content_hash(
            self.kind, self.id, self.label, self.text, self.count, self.refs
        )
        if self.content_hash != expected:
            raise ValueError("item content_hash does not match the item")

    @classmethod
    def build(
        cls,
        kind: str,
        item_id: str,
        *,
        label: str,
        text: str = "",
        count: int | None = None,
        refs: Sequence[str] = (),
    ) -> ContextItem:
        refs = tuple(refs)
        digest = item_content_hash(kind, item_id, label, text, count, refs)
        return cls(kind, item_id, label, text, count, refs, digest)

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "id": self.id,
            "label": self.label,
            "text": self.text,
            "count": self.count,
            "refs": list(self.refs),
            "content_hash": self.content_hash,
        }


#: What a slot can carry: memories (``core``) or items (``handoff``, ``topics``).
SlotEntry = ContextEntry | ContextItem


@dataclass(frozen=True, slots=True)
class ContextSlot:
    """A named, budgeted group of entries.

    ``overflow`` counts the selected memories this slot did not carry in
    full, and ``truncated_ids`` names them: a memory cut part-way and every
    memory left out once the budget ran out. A caller fetches them with
    ``memory_get`` or ``memory_search`` when it needs them; nothing is dropped
    silently.

    Contract: at most one memory is cut part-way; when there is one, its entry
    is the slot's last entry (with a prefix of the body) and its id is in
    ``truncated_ids``. The order of ``truncated_ids`` is the producer's
    (the built-in ``core`` slot uses selection order, so the partial one
    comes first).

    ``skipped`` counts selected memories the producer could not represent
    (for example an id outside the memory id pattern or a file that could
    not be read). They are neither carried nor overflow, and are not part of
    ``pack_version``.

    ``entries`` are all ``ContextEntry`` (memories) or all ``ContextItem``.
    An item slot's ``truncated_ids`` are item ids (references); a memory
    slot's are memory ids. ``degraded`` is a reason code (``timeout``,
    ``error``, ``busy``, ``invalid``, ``unavailable``) when the producer could
    not build the slot; a degraded slot carries nothing. It is part of
    ``pack_version`` and of ``as_dict()`` only when set, so a healthy slot
    serialises exactly as before C2.
    """

    name: str
    entries: tuple[SlotEntry, ...]
    used_tokens_est: int
    overflow: int = 0
    truncated_ids: tuple[str, ...] = ()
    skipped: int = 0
    degraded: str | None = None

    def __post_init__(self) -> None:
        check_name(self.name, what="slot name")
        object.__setattr__(self, "entries", tuple(self.entries))
        object.__setattr__(self, "truncated_ids", tuple(self.truncated_ids))
        kinds = {type(entry) for entry in self.entries}
        if not kinds <= {ContextEntry, ContextItem}:
            raise ValueError("slot entries must be ContextEntry or ContextItem values")
        if len(kinds) > 1:
            raise ValueError("slot entries must be all ContextEntry or all ContextItem")
        item_slot = kinds == {ContextItem}
        ids = [entry.id for entry in self.entries]
        if len(set(ids)) != len(ids):
            raise ValueError("slot entry ids must be unique")
        if type(self.used_tokens_est) is not int or self.used_tokens_est < 0:
            raise ValueError("used_tokens_est must be a non-negative integer")
        if type(self.overflow) is not int or self.overflow < 0:
            raise ValueError("overflow must be a non-negative integer")
        if type(self.skipped) is not int or self.skipped < 0:
            raise ValueError("skipped must be a non-negative integer")
        # An item slot names items by reference; with no entries either form is accepted.
        id_pattern = PATH_REF_RE if item_slot or not self.entries else MEMORY_ID_RE
        if any(
            type(item) is not str or not id_pattern.fullmatch(item) for item in self.truncated_ids
        ):
            raise ValueError("truncated_ids must be memory ids (or item ids in an item slot)")
        if len(set(self.truncated_ids)) != len(self.truncated_ids):
            raise ValueError("truncated_ids must be unique")
        if self.overflow != len(self.truncated_ids):
            raise ValueError("overflow must equal the number of truncated_ids")
        if self.degraded is not None:
            check_name(self.degraded, what="degraded reason")
            if self.entries or self.truncated_ids or self.used_tokens_est or self.skipped:
                raise ValueError("a degraded slot carries nothing")

    @classmethod
    def degraded_slot(cls, name: str, reason: str) -> ContextSlot:
        """An empty slot marked ``degraded`` with ``reason``."""

        return cls(name, (), 0, degraded=reason)

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "name": self.name,
            "entries": [entry.as_dict() for entry in self.entries],
            "used_tokens_est": self.used_tokens_est,
            "overflow": self.overflow,
            "truncated_ids": list(self.truncated_ids),
            "skipped": self.skipped,
        }
        if self.degraded is not None:
            out["degraded"] = self.degraded
        return out


def _entry_key(entry: SlotEntry) -> list[Any]:
    if isinstance(entry, ContextItem):
        # The kind (a string) stands where a memory entry has its version (an int).
        return [entry.id, entry.kind, entry.content_hash, len(entry.text)]
    return [entry.id, entry.version, entry.content_hash, len(entry.text)]


def _slot_key(slot: ContextSlot) -> dict[str, Any]:
    key: dict[str, Any] = {
        "name": slot.name,
        "entries": [_entry_key(entry) for entry in slot.entries],
        "truncated_ids": list(slot.truncated_ids),
    }
    if slot.degraded is not None:
        key["degraded"] = slot.degraded
    return key


def pack_version_payload(slots: Sequence[ContextSlot], policy: str) -> bytes:
    """The exact bytes ``pack_version`` hashes. Ids, versions and hashes only.

    Exposed so a test (or an auditor) can see that no body text, title or
    query goes into the version key.
    """

    document = {
        "encoding": PACK_ENCODING,
        "policy": policy,
        "slots": [_slot_key(slot) for slot in slots],
    }
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "ascii"
    )


def compute_pack_version(slots: Sequence[ContextSlot], policy: str) -> str:
    return hashlib.sha256(pack_version_payload(slots, policy)).hexdigest()


@dataclass(frozen=True, slots=True)
class ContextPack:
    """Ordered slots, the filter policy name, a version key and a timestamp.

    Build one with ``ContextPack.build`` so ``pack_version`` is computed;
    the constructor rejects a ``pack_version`` that does not match.
    """

    slots: tuple[ContextSlot, ...]
    pack_version: str
    policy: str
    generated_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "slots", tuple(self.slots))
        if any(type(slot) is not ContextSlot for slot in self.slots):
            raise ValueError("pack slots must be ContextSlot values")
        names = [slot.name for slot in self.slots]
        if len(set(names)) != len(names):
            raise ValueError("slot names must be unique")
        check_name(self.policy, what="policy")
        if type(self.pack_version) is not str or not PACK_VERSION_RE.fullmatch(self.pack_version):
            raise ValueError("pack_version must be 64 lowercase hex characters")
        if not isinstance(self.generated_at, datetime) or self.generated_at.tzinfo is None:
            raise ValueError("generated_at must include a timezone")
        if self.pack_version != compute_pack_version(self.slots, self.policy):
            raise ValueError("pack_version does not match the pack contents")

    @classmethod
    def build(
        cls, slots: Sequence[ContextSlot], *, policy: str, generated_at: datetime
    ) -> ContextPack:
        slots = tuple(slots)
        return cls(slots, compute_pack_version(slots, policy), policy, generated_at)

    def slot(self, name: str) -> ContextSlot:
        for slot in self.slots:
            if slot.name == name:
                return slot
        raise KeyError(name)

    def as_dict(self) -> dict[str, Any]:
        return {
            "pack_version": self.pack_version,
            "policy": self.policy,
            "generated_at": self.generated_at.isoformat(),
            "slots": [slot.as_dict() for slot in self.slots],
        }


@dataclass(frozen=True, slots=True)
class SlotRequest:
    """What every slot provider of one ``memory_context`` call is asked for.

    ``limit_tokens`` is the per-slot budget (each slot is cut on its own),
    ``policy`` the filter policy name and ``now`` the one instant of the call.
    Who the caller is (tenant, app) and which vault are bound into the
    provider by whoever builds it.
    """

    limit_tokens: int
    policy: str
    now: datetime

    def __post_init__(self) -> None:
        if type(self.limit_tokens) is not int:
            raise ValueError("limit_tokens must be an integer")
        check_name(self.policy, what="policy")
        if not isinstance(self.now, datetime) or self.now.tzinfo is None:
            raise ValueError("now must include a timezone")


@runtime_checkable
class ContextSlotProvider(Protocol):
    """Builds one named slot of a context pack.

    ``build`` returns a ``ContextSlot`` whose ``name`` is the provider's. It
    must not write anything. It may raise; whoever assembles the pack turns a
    raising or slow provider into a ``degraded`` slot and still returns the
    other slots.
    """

    @property
    def name(self) -> str:
        """The slot name this provider builds (``check_name`` rules)."""
        ...

    def build(self, request: SlotRequest) -> ContextSlot: ...


__all__ = [
    "CONTENT_HASH_RE",
    "ITEM_LABEL_CHARS",
    "ITEM_MAX_REFS",
    "ITEM_TEXT_CHARS",
    "PACK_ENCODING",
    "ContextEntry",
    "ContextItem",
    "ContextPack",
    "ContextSlot",
    "ContextSlotProvider",
    "SlotEntry",
    "SlotRequest",
    "compute_pack_version",
    "item_content_hash",
    "pack_version_payload",
]
