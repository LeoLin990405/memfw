"""Per-CLI conversation readers and their registry.

Each module in this package reads one worker CLI's local session store and
exposes ``READER`` (see :class:`base.Reader`). The registry maps agent names
to readers; :func:`get_reader` accepts the CLI's other common names too
(``zcode`` for ``glm``, ``dsh`` for ``deepseek`` ...).
"""

from __future__ import annotations

from pathlib import Path

from . import claude, codex, cursor, deepseek, glm, grok, kimi, minimax, opencode, stepfun
from .base import ConversationRecord, Reader, SessionLocator, StoreRoot, ToolCall

READERS: dict[str, Reader] = {
    module.READER.agent: module.READER
    for module in (claude, codex, kimi, grok, cursor, deepseek, minimax, glm, stepfun, opencode)
}
ALIASES = {
    "claude-code": "claude",
    "cursor-agent": "cursor",
    "dsh": "deepseek",
    "mmx": "minimax",
    "zcode": "glm",
    "zhipu": "glm",
    "step": "stepfun",
    "stepcode": "stepfun",
}


class UnknownAgent(KeyError):
    """No reader is registered under this name."""


def agent_names() -> list[str]:
    """Registered agent names, in registry order."""

    return list(READERS)


def get_reader(name: str) -> Reader:
    """The reader for ``name`` or one of its aliases."""

    key = name.strip().lower()
    reader = READERS.get(ALIASES.get(key, key))
    if reader is None:
        raise UnknownAgent(name)
    return reader


WORKER_HOMES = Path(".conductor-daemon/cli-state")
"""Conductor Worker homes: ``<WORKER_HOMES>/<id>/home`` mirrors a home directory."""


def worker_homes(home: Path | None = None) -> list[Path]:
    """Conductor Worker home directories under ``home`` (real directories only)."""

    base = (home if home is not None else Path.home()) / WORKER_HOMES
    if not base.is_dir() or base.is_symlink():
        return []
    out = []
    for entry in sorted(base.iterdir()):
        candidate = entry / "home"
        if not entry.is_symlink() and candidate.is_dir() and not candidate.is_symlink():
            out.append(candidate)
    return out


def store_roots(reader: Reader, home: Path | None = None) -> list[StoreRoot]:
    """The reader's existing roots: the interactive home first, then every Worker home."""

    base = home if home is not None else Path.home()
    roots = [StoreRoot(path.resolve(), "interactive") for path in default_roots(reader, base)]
    for worker in worker_homes(base):
        roots.extend(StoreRoot(path.resolve(), "worker") for path in default_roots(reader, worker))
    return roots


def default_roots(reader: Reader, home: Path | None = None) -> list[Path]:
    """The reader's store roots under ``home`` that exist as real directories."""

    base = home if home is not None else Path.home()
    return [
        base / relative
        for relative in reader.default_roots
        if (base / relative).is_dir() and not (base / relative).is_symlink()
    ]


__all__ = [
    "ALIASES",
    "READERS",
    "ConversationRecord",
    "Reader",
    "SessionLocator",
    "StoreRoot",
    "ToolCall",
    "UnknownAgent",
    "agent_names",
    "default_roots",
    "get_reader",
    "store_roots",
    "worker_homes",
]
