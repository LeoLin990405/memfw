"""Time and id ports: ``Clock`` and ``IdGen``. Stdlib only.

The kernel takes time and ids as injected callables today
(``Repository(clock=..., id_factory=...)``). These protocols name that
contract so a composition root can inject one implementation everywhere and a
test can make every timestamp and id deterministic. ``clock.now`` and
``id_gen.new_id`` are exactly the zero-argument callables ``Repository``
already accepts.
"""

from __future__ import annotations

import hashlib
import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    """A source of the current time.

    ``now()`` returns a timezone-aware ``datetime``. Successive calls on one
    clock never go backwards.
    """

    def now(self) -> datetime: ...


@runtime_checkable
class IdGen(Protocol):
    """A source of new record ids.

    ``new_id()`` returns a fresh, non-empty ASCII id usable as a memory id:
    it matches ``^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$`` and is never repeated by
    the same generator.
    """

    def new_id(self) -> str: ...


def _aware(value: datetime, what: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{what} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{what} must be timezone-aware")
    return value


def _utc_now() -> datetime:
    return datetime.now(UTC)


class SystemClock:
    """The wall clock in UTC (``memfw.core.models.utc_now``), made monotonic.

    The wall clock can step backwards (NTP, a manual change). This clock keeps
    the last value it returned and never returns an earlier one: during a
    backward step it repeats that value (time appears to stall) until the
    wall clock catches up. ``source`` is the wall clock; tests inject a fake.
    """

    def __init__(self, source: Callable[[], datetime] = _utc_now) -> None:
        self._source = source
        self._last: datetime | None = None
        self._lock = threading.Lock()

    def now(self) -> datetime:
        value = _aware(self._source(), "the time source's value")
        with self._lock:
            if self._last is not None and value < self._last:
                return self._last
            self._last = value
            return value


class FixedClock:
    """A clock for tests: returns ``at`` until moved with ``advance`` or ``set``."""

    def __init__(self, at: datetime) -> None:
        self._at = _aware(at, "at")
        self._lock = threading.Lock()

    def now(self) -> datetime:
        with self._lock:
            return self._at

    def advance(self, delta: timedelta) -> datetime:
        """Move forward by ``delta`` (never backwards) and return the new time."""

        if delta < timedelta(0):
            raise ValueError("a clock never goes backwards")
        with self._lock:
            self._at = self._at + delta
            return self._at

    def set(self, at: datetime) -> None:
        """Jump to ``at``, which must not be earlier than the current time."""

        value = _aware(at, "at")
        with self._lock:
            if value < self._at:
                raise ValueError("a clock never goes backwards")
            self._at = value


class UuidIdGen:
    """Random UUID4 strings: ``Repository``'s default ``id_factory``."""

    def new_id(self) -> str:
        return str(uuid.uuid4())


class SeededIdGen:
    """Deterministic ids: ``sha256(f"{seed}:{n}")[:32]`` for n = start+1, start+2, ...

    The same scheme as ``memfw --id-seed`` (``memfw.core.invocation.id_factory``), so a
    test can predict the ids a seeded CLI run produces. That factory resumes an
    existing vault at ``start = 2 * (non-empty lines in events.jsonl)``; pass
    the same ``start`` to continue its sequence.
    """

    def __init__(self, seed: str, *, start: int = 0) -> None:
        if not isinstance(seed, str) or not seed:
            raise ValueError("seed must be a non-empty string")
        if type(start) is not int or start < 0:
            raise ValueError("start must be a non-negative integer")
        self._seed = seed
        self._sequence = start
        self._lock = threading.Lock()

    def new_id(self) -> str:
        with self._lock:
            self._sequence += 1
            sequence = self._sequence
        return hashlib.sha256(f"{self._seed}:{sequence}".encode()).hexdigest()[:32]


__all__ = ["Clock", "FixedClock", "IdGen", "SeededIdGen", "SystemClock", "UuidIdGen"]
