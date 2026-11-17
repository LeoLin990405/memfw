"""Which memories may leave the vault: ``public`` or ``shared`` only.

A memory is sent to the model only when both readers agree it is shareable:
the fail-closed reference reader (``privacy_class() == "none"``) and the
MM0 privacy level (``metadata_level`` is ``public`` or ``shared``). A memory
without a ``privacy`` field takes the MM0 fallback ``local-private`` (the
vault's ``legacy_privacy_default`` is not read here, so it can only be
stricter), and ``local-private`` or ``unknown`` never leaves, whatever the
request options say. Unreadable files, symlinks and unknown ids are skipped.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from ._ports import MemoryFile, is_vault, metadata_level, read_memory

#: The MM0 levels that may be sent to a model or another process.
SHAREABLE_LEVELS: frozenset[str] = frozenset({"public", "shared"})


def is_shareable(memory: MemoryFile) -> bool:
    if memory.privacy_class() != "none":
        return False
    return metadata_level(memory.front) in SHAREABLE_LEVELS


def shareable_memories(vault: Path, memory_ids: Iterable[str]) -> list[MemoryFile]:
    """The requested memories that exist and may leave the vault, in request order."""

    root = Path(vault).expanduser()
    if not is_vault(root):
        raise ValueError("not a vault: vault.yaml is missing")
    found: list[MemoryFile] = []
    seen: set[str] = set()
    for memory_id in memory_ids:
        if memory_id in seen:
            continue
        seen.add(memory_id)
        memory = read_memory(root, memory_id)
        if memory is not None and is_shareable(memory):
            found.append(memory)
    return found


__all__ = ["SHAREABLE_LEVELS", "is_shareable", "shareable_memories"]
