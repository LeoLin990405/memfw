"""The usage-signal port: which memories a session says it used (C5). Stdlib only.

``memory_used(ids, session_ref)`` only leaves a signal: it never changes a
memory, the vault or its event chain. The Runtime checks the ids (unknown,
other-tenant and unreadable ids are dropped; the answer does not list them), builds one
``UsageSignal`` and hands it to a ``UsageSink``, the first event kind of the
architecture's ``EventSink`` port. memfw ships three sinks:

- ``memfw.usage.SqliteUsageSink``: a disposable SQLite store outside the
  vault (file mode 0600, in memfw's local data directory, keyed by vault);
- ``memfw.ports.reference.NullUsageSink``: drops every signal (the wiring
  when the signal is off);
- ``memfw.ports.reference.MemoryUsageSink``: keeps signals in memory (tests).

A signal carries ids, a session reference, the calling app and a time, never
query or body text: ``session_ref`` and ``app`` are short tokens without
spaces, so free text cannot be smuggled in. Any sink that passes
``memfw.ports.testing.check_usage_sink`` can be injected instead.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Protocol, runtime_checkable

USAGE_PORT_VERSION: Final = 1

#: At most this many ids per signal (and per ``memory_used`` call).
MAX_USAGE_IDS: Final = 100

#: The memory id rule ``Repository`` enforces (``memfw.core.repository._ID_RE``).
MEMORY_ID_RULE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
#: A session reference: an opaque token (a session id, ``conv:APP:SESSION``,
#: ``APP:SESSION@OFFSET``), never free text: no spaces, at most 200 characters.
SESSION_REF_RULE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@+=-]{0,199}$")
#: An app name as ``conversation_share`` spells it (lower case), or empty.
APP_RULE: Final = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")


@dataclass(frozen=True, slots=True)
class UsageSignal:
    """One ``memory_used`` call: ids the caller may see, a session, an app, a time.

    ``memory_ids`` is a non-empty tuple of distinct memory ids (at most
    ``MAX_USAGE_IDS``), in the caller's order. ``session_ref`` matches
    ``SESSION_REF_RULE``; ``app`` is ``""`` or matches ``APP_RULE``; ``at`` is
    timezone-aware. Anything else raises ``ValueError`` (``TypeError`` for a
    wrong type) at construction.
    """

    memory_ids: tuple[str, ...]
    session_ref: str
    app: str
    at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.memory_ids, tuple) or not all(
            isinstance(item, str) for item in self.memory_ids
        ):
            raise TypeError("memory_ids must be a tuple of str")
        if not self.memory_ids:
            raise ValueError("memory_ids must not be empty")
        if len(self.memory_ids) > MAX_USAGE_IDS:
            raise ValueError(f"at most {MAX_USAGE_IDS} memory ids per signal")
        if len(set(self.memory_ids)) != len(self.memory_ids):
            raise ValueError("memory_ids must be distinct")
        if not all(MEMORY_ID_RULE.fullmatch(item) for item in self.memory_ids):
            raise ValueError("memory_ids must be valid memory ids")
        if not isinstance(self.session_ref, str) or not SESSION_REF_RULE.fullmatch(
            self.session_ref
        ):
            raise ValueError("session_ref must match SESSION_REF_RULE")
        if not isinstance(self.app, str) or (self.app and not APP_RULE.fullmatch(self.app)):
            raise ValueError("app must be empty or match APP_RULE")
        if not isinstance(self.at, datetime):
            raise TypeError("at must be a datetime")
        if self.at.tzinfo is None or self.at.utcoffset() is None:
            raise ValueError("at must be timezone-aware")


@runtime_checkable
class UsageSink(Protocol):
    """Where usage signals go. Never the vault.

    ``record(signal)`` stores (or drops) one ``UsageSignal`` and returns
    ``None``; anything that is not a ``UsageSignal`` raises ``TypeError``.
    A sink writes only its own store: never a memory, ``events.jsonl`` or
    any other vault file. Recording the same signal twice is allowed (each
    call is one signal). A sink may raise ``OSError`` when its store is
    unusable; the Runtime treats a signal as best effort.
    """

    name: str

    def record(self, signal: UsageSignal) -> None: ...


__all__ = [
    "APP_RULE",
    "MAX_USAGE_IDS",
    "MEMORY_ID_RULE",
    "SESSION_REF_RULE",
    "USAGE_PORT_VERSION",
    "UsageSignal",
    "UsageSink",
]
