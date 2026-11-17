"""Reading the vault for ``sync``: which memories may go to Qdrant, and their text.

Memory files are read with the fail-closed reference reader
(``memfw.ports.reference``); a file it cannot parse is left out. A memory's
privacy level follows the L0 rules (``memfw.ports.privacy.effective_level``):
the strictest of the ``privacy`` field (missing: the vault's
``legacy_privacy_default``, ``local-private`` when unset), the ``privacy:`` and
``sensitivity:`` tags and a top-level ``sensitivity`` key. Only levels in the
clearance (``public``/``shared``, never ``local-private``) are sent.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from ._ports import (
    LEGACY_PRIVACY_FALLBACK,
    LEGACY_PRIVACY_KEY,
    REMOTE_CLEARANCE,
    MemoryFile,
    admits,
    effective_level,
    iter_memories,
    legacy_default,
)

_LEGACY_LINE = re.compile(rf"^{LEGACY_PRIVACY_KEY}:[ \t]*(.*?)[ \t]*$")


def vault_key(root: Path | str) -> str:
    """The first 24 hex digits of sha256 of the vault's resolved path (as memfw's usage store)."""

    resolved = str(Path(root).expanduser().resolve())
    return hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:24]


def legacy_privacy(vault: Path) -> str:
    """``legacy_privacy_default`` from a line scan of ``vault.yaml`` (unsure: local-private)."""

    try:
        text = (vault / "vault.yaml").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return LEGACY_PRIVACY_FALLBACK
    found = [match.group(1) for line in text.splitlines() if (match := _LEGACY_LINE.match(line))]
    if len(found) != 1:
        return LEGACY_PRIVACY_FALLBACK
    value = found[0]
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        value = value[1:-1]
    return legacy_default(value)


def privacy_level(memory: MemoryFile, legacy: str) -> str:
    """The level an exit acts on; a present but empty ``privacy`` is ``unknown``."""

    raw = memory.front.get("privacy")
    privacy = raw if isinstance(raw, str) else (None if raw is None else "")
    return effective_level(
        privacy,
        memory.tags(),
        present="privacy" in memory.front,
        legacy=legacy,
        sensitivity=memory.front.get("sensitivity"),
    )


@dataclass(frozen=True, slots=True)
class Outgoing:
    """One memory that may be sent: id, tenant, the text to embed and its hash."""

    memory_id: str
    tenant: str
    text: str
    sha: str


def outgoing(vault: Path, clearance: frozenset[str], skipped: dict[str, int]) -> Iterator[Outgoing]:
    """The memories ``clearance`` admits, in id order; counts the rest in ``skipped``.

    ``clearance`` must be within ``public``/``shared``: anything wider raises,
    so no caller can widen what leaves the machine.
    """

    if not clearance or not clearance <= REMOTE_CLEARANCE:
        raise ValueError("only public and shared memories are ever sent")
    legacy = legacy_privacy(vault)
    for memory_id, memory in iter_memories(vault):
        if memory is None:
            skipped["unreadable"] = skipped.get("unreadable", 0) + 1
            continue
        level = privacy_level(memory, legacy)
        if level == "local-private" or not admits(clearance, level):
            skipped["privacy"] = skipped.get("privacy", 0) + 1
            continue
        tenant = memory.text("tenant")
        text = f"{memory.text('title')}\n{memory.body}"
        digest = hashlib.sha256(f"{tenant}\0{text}".encode()).hexdigest()[:32]
        yield Outgoing(memory_id, tenant, text, digest)


__all__ = ["Outgoing", "legacy_privacy", "outgoing", "privacy_level", "vault_key"]
