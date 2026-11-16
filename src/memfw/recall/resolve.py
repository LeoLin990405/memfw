"""Resolve provider refs to memory ids plus filter metadata.

A resolver is batch-shaped so an implementation can make one call for many
refs (R6 ``memfw resolve --ids-stdin``). This module touches no memfw
internals. Here:

- ``MemoryPathResolver`` for ``ref_kind="path"`` of the form
  ``memories/<id>.md`` (optionally under ``fleet-memory/``); other path forms
  are left unresolved. It delegates ids to any id resolver.

The vault-reading ``VaultIdResolver`` lives in ``memfw.recall.builtin``.

The full path resolver (``files/<rel>`` → ``stable_id`` → meta-index,
§3.8) is R6 PR-1's ``join_meta``/``resolve``; it plugs in through the same
``Resolver`` protocol.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .policy import MemoryMeta

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_MEMORY_PATH_RE = re.compile(r"^(?:\./)?(?:fleet-memory/)?memories/([^/]+)\.md$")


@dataclass(frozen=True, slots=True)
class Resolution:
    """A ref that names a memory. ``meta`` is ``None`` when it cannot be read."""

    memory_id: str
    meta: MemoryMeta | None


@runtime_checkable
class Resolver(Protocol):
    """Batch ref → resolution. A ref missing from the result is unresolved."""

    def resolve(self, refs: Sequence[str]) -> Mapping[str, Resolution]: ...


#: Path refs use the same protocol; this name marks the R6 PR-1 seam.
PathResolver = Resolver


class MemoryPathResolver:
    """``memories/<id>.md`` path refs → ids, then the id resolver."""

    def __init__(self, ids: Resolver) -> None:
        self.ids = ids

    @staticmethod
    def memory_id_of(path: str) -> str | None:
        match = _MEMORY_PATH_RE.fullmatch(path.strip())
        if match is None or not _ID_RE.fullmatch(match.group(1)):
            return None
        return match.group(1)

    def resolve(self, refs: Sequence[str]) -> Mapping[str, Resolution]:
        wanted = {ref: self.memory_id_of(ref) for ref in refs}
        ids = sorted({value for value in wanted.values() if value})
        found = self.ids.resolve(ids) if ids else {}
        out: dict[str, Resolution] = {}
        for ref, memory_id in wanted.items():
            if memory_id and memory_id in found:
                out[ref] = found[memory_id]
        return out


__all__ = [
    "MemoryPathResolver",
    "PathResolver",
    "Resolution",
    "Resolver",
]
