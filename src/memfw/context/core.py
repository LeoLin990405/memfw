"""The ``core`` slot: anchors first, then procedural memories, by id.

The selection, order and character budget are the ones ``memory_context`` has
always used; v1 output (``core_context_v1``) is byte-identical to it. v2
(``build_core_pack``) returns the same selection as a ``ContextPack`` and
adds what v1 left out: each entry's ``version`` and ``content_hash``, how
many memories did not fit (``overflow``/``truncated_ids``) and a
``pack_version`` key.

Filtering goes through ``memfw.recall.policy.evaluate``:

- ``compat_fts`` (default): tenant and source only, exactly what v1 applied
  (``FilterPolicy.compat_fts`` with ``include_history`` set, because the
  slot keeps its own ``valid_to`` rule below instead of the index's
  temporal clause);
- ``strict``: the two-stage strict rules: privacy clearance ``none``
  (``local-private`` and unknown privacy are dropped), status, lifecycle,
  supersession, validity and archive (cold tier or stub).

Both keep v1's own rule that a memory with ``valid_to`` set is never
resident context.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from memfw.core.models import Layer, MemoryDocument
from memfw.core.repository import Repository
from memfw.ports.context import ContextEntry, ContextPack, ContextSlot
from memfw.recall.builtin import meta_from_document
from memfw.recall.policy import FilterPolicy, evaluate

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


def filter_policy(name: str, *, tenant: str, allowed_sources: Sequence[str]) -> FilterPolicy:
    allowed = tuple(allowed_sources)
    if name == "compat_fts":
        return FilterPolicy.compat_fts(tenant=tenant, allowed_sources=allowed, include_history=True)
    if name == "strict":
        return FilterPolicy(tenant=tenant, allowed_sources=allowed)
    raise ValueError("unknown policy")


def _is_core(document: MemoryDocument) -> bool:
    return document.layer == Layer.ANCHOR or bool(document.anchor) or document.type == "procedural"


def select_core(
    documents: Sequence[MemoryDocument], policy: FilterPolicy, *, now: datetime
) -> list[MemoryDocument]:
    """The slot's memories in slot order. ``documents`` is the whole vault."""

    selected = [
        document
        for document in documents
        if document.valid_to is None
        and _is_core(document)
        and evaluate(meta_from_document(document), policy, now=now).kept
    ]
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
    tenant: str,
    allowed_sources: Sequence[str],
    limit_tokens: Any,
) -> dict[str, Any]:
    """The original ``memory_context`` payload, byte for byte."""

    budget = limit_chars(limit_tokens)  # before list(): v1 raised here first
    policy = filter_policy("compat_fts", tenant=tenant, allowed_sources=allowed_sources)
    selected = select_core(repository.list(), policy, now=datetime.now(UTC))
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


def _file_hash(repository: Repository, memory_id: str) -> str | None:
    try:
        raw = (repository.memories_dir / f"{memory_id}.md").read_bytes()
    except OSError:
        return None
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def build_core_pack(
    repository: Repository,
    *,
    tenant: str,
    allowed_sources: Sequence[str],
    limit_tokens: int,
    policy: str = "compat_fts",
    now: datetime | None = None,
) -> ContextPack:
    """A one-slot (``core``) pack under ``policy``.

    ``content_hash`` is the sha256 of the memory file, the same value
    ``memory_search`` reports. A memory whose file cannot be read for the
    hash is left out (it is not counted as overflow either: it was not
    selectable).
    """

    budget = limit_chars(limit_tokens)
    moment = now if now is not None else datetime.now(UTC)
    rules = filter_policy(policy, tenant=tenant, allowed_sources=allowed_sources)
    hashes: dict[str, str] = {}
    selected: list[MemoryDocument] = []
    for document in select_core(repository.list(), rules, now=moment):
        digest = _file_hash(repository, document.id)
        if digest is None:
            continue
        hashes[document.id] = digest
        selected.append(document)
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
    slot = ContextSlot(
        name=CORE_SLOT,
        entries=entries,
        used_tokens_est=estimate_tokens(cut.used_chars),
        overflow=len(cut.truncated_ids),
        truncated_ids=cut.truncated_ids,
    )
    return ContextPack.build((slot,), policy=policy, generated_at=moment)


__all__ = [
    "CONTEXT_POLICIES",
    "CONTEXT_VERSIONS",
    "CORE_SLOT",
    "build_core_pack",
    "context_request",
    "core_context_v1",
    "estimate_tokens",
    "filter_policy",
    "limit_chars",
    "select_core",
]
