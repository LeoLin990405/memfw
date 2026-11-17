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
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ._ports import (
    REMOTE_CLEARANCE,
    MemoryFile,
    admits,
    effective_level,
    iter_memories,
    legacy_default_from_config,
)


def vault_key(root: Path | str) -> str:
    """The first 24 hex digits of sha256 of the vault's resolved path (as memfw's usage store)."""

    resolved = str(Path(root).expanduser().resolve())
    return hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:24]


def read_config(vault: Path) -> Any:
    """``yaml.safe_load`` of ``vault.yaml``, as memfw reads it; ``None`` if unreadable."""

    import yaml

    try:
        return yaml.safe_load((vault / "vault.yaml").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return None


_VAULT_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


def check_vault_id(value: Any) -> str:
    """An operator-chosen vault id: 1-64 of lower-case letters, digits, ``.``, ``_``, ``-``."""

    if not isinstance(value, str) or not _VAULT_ID.fullmatch(value):
        raise ValueError("vault_id must be 1-64 lower-case letters, digits, '.', '_' or '-'")
    return value


def configured_vault_id(vault: Path) -> str:
    """``vault_id`` from ``vault.yaml`` (the attachments' lower-case UUID), or ``""``."""

    config = read_config(vault)
    value = config.get("vault_id") if isinstance(config, dict) else None
    if not isinstance(value, str):
        return ""
    try:
        return value if str(uuid.UUID(value)) == value else ""
    except ValueError:
        return ""


def points_key(vault: Path | str, vault_id: str = "") -> str:
    """The key every point of this vault carries (24 hex digits).

    With a ``vault_id`` (required for a remote collection, which several hosts
    may share) it is derived from that id, so the same vault path on two hosts
    never collides; otherwise from the vault's resolved path on this host.
    """

    if vault_id:
        return hashlib.sha256(f"vault_id:{vault_id}".encode()).hexdigest()[:24]
    return vault_key(vault)


def legacy_privacy(vault: Path) -> str:
    """The vault's ``legacy_privacy_default``, read exactly as memfw reads it.

    As ``memfw.index.readonly.vault_legacy_privacy``: ``yaml.safe_load`` and
    the L0 ``legacy_default_from_config``; anything invalid is ``local-private``.
    """

    return str(legacy_default_from_config(read_config(vault)))


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
