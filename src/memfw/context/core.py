"""The ``core`` slot: anchors first, then procedural memories, by id.

The selection, order and character budget are the ones ``memory_context`` has
always used; v1 output (``core_context_v1``) is byte-identical to it. v2
(``build_core_pack``) returns the same selection as a ``ContextPack`` and
adds what v1 left out: each entry's ``version`` and ``content_hash``, how
many memories did not fit (``overflow``/``truncated_ids``) and a
``pack_version`` key.

Which memories a caller may see is injected as ``keep`` (one predicate per
call). This package is an L3 capability and must not import another
capability (``memfw.recall`` included), so the composition root builds the
predicate: ``memfw.runtime`` (``Runtime.context_keep``), from the recall
``FilterPolicy`` (``compat_fts``: tenant and source, as v1; ``strict``: the
two-stage strict rules). On top of ``keep`` the slot always
applies v1's own rule that a memory with ``valid_to`` set is never resident
context.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from memfw.core.errors import MemfwError
from memfw.core.models import Layer, MemoryDocument
from memfw.core.repository import Repository
from memfw.ports.context import PACK_VERSION_RE, ContextEntry, ContextPack, ContextSlot
from memfw.ports.types import MEMORY_ID_RE

#: Whether the caller may see one memory (tenant, source, privacy, ...).
Keep = Callable[[MemoryDocument], bool]
#: ``(repository, keep) -> the core slot's candidates``: ``select_core`` over the
#: whole vault, in slot order. The default (``core_candidates``) reads
#: ``repository.list()``; the composition root may inject one that serves the
#: same list from a cache of ids (C3, ``memfw.runtime.context_cache``).
CoreCandidates = Callable[[Repository, Keep], list[MemoryDocument]]

#: ``memory_context`` ``policy`` values.
CONTEXT_POLICIES: tuple[str, ...] = ("compat_fts", "strict")
#: ``memory_context`` ``v`` values. 1 is the default and the original shape.
CONTEXT_VERSIONS: tuple[int, ...] = (1, 2)
CORE_SLOT = "core"

_MIN_CHARS = 100
_MAX_TOKENS = 100_000


def estimate_tokens(chars: int) -> int:
    """The budget's own estimate: four characters per token, rounded up."""

    return (max(0, chars) + 3) // 4


def limit_chars(limit_tokens: int) -> int:
    """Character budget for ``limit_tokens``, clamped as v1 always did."""

    return max(_MIN_CHARS, min(int(limit_tokens), _MAX_TOKENS) * 4)


def context_request(v: object, policy: object) -> tuple[int, str]:
    """Validate ``v`` and ``policy`` the way ``memory_search`` validates ``policy``.

    ``None`` means the default for either. ``policy`` must be a string
    (``""`` is the default ``compat_fts``) and applies only to ``v=2``.
    """

    if v is None:
        v = 1
    if type(v) is not int:
        raise ValueError("v must be an integer")
    if v not in CONTEXT_VERSIONS:
        raise ValueError("unknown context version")
    if policy is None or policy == "":
        policy = "compat_fts"
    if not isinstance(policy, str):
        raise ValueError("policy must be a string")
    if policy not in CONTEXT_POLICIES:
        raise ValueError("unknown policy")
    if v == 1 and policy != "compat_fts":
        raise ValueError("policy only applies to v=2")
    return v, policy


def if_version_request(v: int, if_version: object) -> str | None:
    """Validate ``memory_context`` ``if_version`` (after ``context_request`` checked ``v``).

    ``None`` or ``""`` means absent. Otherwise only for ``v=2``, and it must be
    a ``pack_version`` (64 lowercase hex characters): anything else raises
    ``ValueError`` before the vault is read.
    """

    if if_version is None or if_version == "":
        return None
    if v != 2:
        raise ValueError("if_version only applies to v=2")
    if not isinstance(if_version, str) or not PACK_VERSION_RE.fullmatch(if_version):
        raise ValueError("if_version must be a pack_version (64 lowercase hex characters)")
    return if_version


def _is_core(document: MemoryDocument) -> bool:
    return document.layer == Layer.ANCHOR or bool(document.anchor) or document.type == "procedural"


def is_core_shaped(document: MemoryDocument) -> bool:
    """A memory the ``core`` slot considers before ``keep``: no ``valid_to``, an
    anchor or a procedural memory. Depends on the memory's file only."""

    return document.valid_to is None and _is_core(document)


def select_core(documents: Sequence[MemoryDocument], keep: Keep) -> list[MemoryDocument]:
    """The slot's memories in slot order. ``documents`` is the whole vault."""

    selected = [document for document in documents if is_core_shaped(document) and keep(document)]
    selected.sort(
        key=lambda document: (
            0 if document.layer == Layer.ANCHOR or document.anchor else 1,
            0 if document.type == "procedural" else 1,
            document.id,
        )
    )
    return selected


@dataclass(frozen=True, slots=True)
class _Cut:
    carried: tuple[tuple[MemoryDocument, str], ...]
    truncated_ids: tuple[str, ...]
    used_chars: int


def _cut(selected: Sequence[MemoryDocument], budget: int) -> _Cut:
    """v1's fill loop, also recording which memories did not fit in full.

    A memory with an empty body has nothing to carry and is never counted.
    At most one memory is cut part-way; it is the last one carried and the
    first in ``truncated_ids``.
    """

    carried: list[tuple[MemoryDocument, str]] = []
    truncated: list[str] = []
    used = 0
    for index, document in enumerate(selected):
        full = document.body or ""
        text = full
        if used + len(text) > budget:
            text = text[: max(0, budget - used)]
        if not text:
            if full:
                truncated.append(document.id)
            continue
        carried.append((document, text))
        if len(text) < len(full):
            truncated.append(document.id)
        used += len(text)
        if used >= budget:
            truncated.extend(item.id for item in selected[index + 1 :] if item.body)
            break
    return _Cut(tuple(carried), tuple(truncated), used)


def core_context_v1(
    repository: Repository,
    *,
    keep: Keep,
    limit_tokens: Any,
) -> dict[str, Any]:
    """The original ``memory_context`` payload, byte for byte.

    ``keep`` is v1's visibility rule (tenant and allowed sources).
    """

    budget = limit_chars(limit_tokens)  # before list(): v1 raised here first
    selected = select_core(repository.list(), keep)
    cut = _cut(selected, budget)
    entries = [
        {
            "id": document.id,
            "title": document.title,
            "type": document.type,
            "layer": document.layer.value,
            "anchor": document.anchor,
            "epistemic": document.epistemic,
            "causal": document.causal,
            "text": text,
        }
        for document, text in cut.carried
    ]
    return {
        "limit_tokens": limit_tokens,
        "used_chars": cut.used_chars,
        "entries": entries,
    }


def _snapshot(
    repository: Repository, document: MemoryDocument
) -> tuple[MemoryDocument, str] | None:
    """Re-read one memory file once: the document parsed from it and its hash.

    Body, version and ``content_hash`` then come from the same bytes, so a
    concurrent write can never pair one version's text with another's hash.
    ``None`` when the id is outside the memory id pattern (``ContextEntry``
    cannot carry it) or the file cannot be read or parsed.
    """

    if not MEMORY_ID_RE.fullmatch(document.id):
        return None
    try:
        raw = (repository.memories_dir / f"{document.id}.md").read_bytes()
        parsed = repository.parse(raw.decode("utf-8"), expected_id=document.id)
    except (OSError, ValueError, MemfwError):
        return None
    return parsed, "sha256:" + hashlib.sha256(raw).hexdigest()


def core_candidates(repository: Repository, keep: Keep) -> list[MemoryDocument]:
    """The default ``CoreCandidates``: ``select_core`` over ``repository.list()``."""

    return select_core(repository.list(), keep)


def build_core_slot(
    repository: Repository,
    *,
    keep: Keep,
    limit_tokens: int,
    candidates: CoreCandidates | None = None,
) -> ContextSlot:
    """The ``core`` slot of ``build_core_pack`` (see there), without the pack around it.

    ``candidates`` (default ``core_candidates``) gives the selection the slot
    starts from; whatever it returns is re-read from the vault and selected
    again with ``keep`` before anything is carried.
    """

    budget = limit_chars(limit_tokens)
    candidates_of = candidates if candidates is not None else core_candidates
    pool = candidates_of(repository, keep)
    hashes: dict[str, str] = {}
    snapshots: list[MemoryDocument] = []
    for document in pool:
        snapshot = _snapshot(repository, document)
        if snapshot is None:
            continue
        hashes[snapshot[0].id] = snapshot[1]
        snapshots.append(snapshot[0])
    selected = select_core(snapshots, keep)
    skipped = len(pool) - len(selected)
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
        name=CORE_SLOT,
        entries=entries,
        used_tokens_est=estimate_tokens(cut.used_chars),
        overflow=len(cut.truncated_ids),
        truncated_ids=cut.truncated_ids,
        skipped=skipped,
    )


def build_core_pack(
    repository: Repository,
    *,
    keep: Keep,
    policy: str,
    limit_tokens: int,
    now: datetime | None = None,
) -> ContextPack:
    """A one-slot (``core``) pack; ``keep`` applies ``policy`` (its name goes in the pack).

    ``content_hash`` is the sha256 of the memory file, the same value
    ``memory_search`` reports. Each selected memory's file is read once more
    and the entry is built from that read (body, version and hash from the
    same bytes); the snapshot is selected and ordered again, so a memory
    changed in between is judged by its new content.

    Selected memories that cannot be represented are counted in the slot's
    ``skipped``, never carried and never overflow: an id outside the memory
    id pattern (a hand-placed file such as ``memories/a.dot.md``, which v1
    still returns), an unreadable or unparsable file, or one that no longer
    qualifies when re-read. Because ``skipped`` is the difference between the
    two selections, a memory that changed between the two reads (for example
    retagged or moved out of the slot) is counted too.

    Because the hash covers the whole file, a frontmatter-only change
    (``updated_at``, ``strength``, ``reference_count`` ...) changes
    ``pack_version`` too, even when the carried text did not change.
    """

    limit_chars(limit_tokens)
    moment = now if now is not None else datetime.now(UTC)
    if policy not in CONTEXT_POLICIES:
        raise ValueError("unknown policy")
    slot = build_core_slot(repository, keep=keep, limit_tokens=limit_tokens)
    return ContextPack.build((slot,), policy=policy, generated_at=moment)


__all__ = [
    "CONTEXT_POLICIES",
    "CONTEXT_VERSIONS",
    "CORE_SLOT",
    "CoreCandidates",
    "build_core_pack",
    "build_core_slot",
    "context_request",
    "core_candidates",
    "core_context_v1",
    "estimate_tokens",
    "if_version_request",
    "is_core_shaped",
    "limit_chars",
    "select_core",
]
