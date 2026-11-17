"""Reference ``UsageSink`` implementations. Stdlib only.

- ``NullUsageSink``: drops every signal. The Runtime wires it when the usage
  signal is off.
- ``MemoryUsageSink``: keeps the signals in memory, in arrival order (tests,
  and an in-process eval that wants the signals without a file).

``memfw.usage.SqliteUsageSink`` is the persistent one.
"""

from __future__ import annotations

import threading
from typing import Any

from ..usage import UsageSignal


def _checked(signal: object) -> UsageSignal:
    if not isinstance(signal, UsageSignal):
        raise TypeError("record() takes a UsageSignal")
    return signal


class NullUsageSink:
    """Accepts and discards every signal."""

    name = "null"

    def record(self, signal: UsageSignal) -> None:
        _checked(signal)


class MemoryUsageSink:
    """Keeps every signal in memory; ``signals`` is a snapshot in arrival order."""

    name = "memory"

    def __init__(self) -> None:
        self._signals: list[UsageSignal] = []
        self._lock = threading.Lock()

    def record(self, signal: UsageSignal) -> None:
        checked = _checked(signal)
        with self._lock:
            self._signals.append(checked)

    @property
    def signals(self) -> tuple[UsageSignal, ...]:
        with self._lock:
            return tuple(self._signals)

    def rows(self) -> list[dict[str, Any]]:
        """One row per (signal, id), the shape ``SqliteUsageSink.rows`` returns."""

        return [
            {
                "memory_id": memory_id,
                "session_ref": signal.session_ref,
                "app": signal.app,
                "at": signal.at.isoformat(),
            }
            for signal in self.signals
            for memory_id in signal.memory_ids
        ]


__all__ = ["MemoryUsageSink", "NullUsageSink"]
