"""Per-invocation time and id overrides (``memfw --now`` / ``memfw --id-seed``).

The root CLI callback records the two options here once per command; every
command and sub-app that opens a vault reads them through this module (and
:func:`memfw.core.repository.open_vault`) instead of reaching into
``memfw.cli``. Stdlib only, so any layer may read it.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

_NOW: datetime | None = None
_ID_SEED: str | None = None


def set_invocation(*, now: datetime | None, id_seed: str | None) -> None:
    """Record this invocation's ``--now`` (already parsed, UTC) and ``--id-seed``."""

    global _NOW, _ID_SEED
    _NOW = now
    _ID_SEED = id_seed


def injected_now() -> datetime | None:
    """The ``--now`` time, or ``None`` when the wall clock is in use."""

    return _NOW


def injected_id_seed() -> str | None:
    """The ``--id-seed`` value, or ``None`` when ids are random."""

    return _ID_SEED


def id_factory(root: Path) -> Callable[[], str]:
    """Ids for a vault at ``root``: random UUID4, or seeded when ``--id-seed`` is set.

    A seeded factory yields ``sha256(f"{seed}:{n}")[:32]`` and resumes an
    existing vault at ``n = 2 * (non-empty lines in events.jsonl) + 1``
    (:class:`memfw.ports.clock.SeededIdGen` with that ``start``).
    """

    if _ID_SEED is None:
        return lambda: str(uuid.uuid4())
    sequence = 0
    event_log = root / "events.jsonl"
    if event_log.exists():
        try:
            sequence = len([line for line in event_log.read_bytes().splitlines() if line]) * 2
        except OSError:
            sequence = 0

    def next_id() -> str:
        nonlocal sequence
        sequence += 1
        return hashlib.sha256(f"{_ID_SEED}:{sequence}".encode()).hexdigest()[:32]

    return next_id


__all__ = ["id_factory", "injected_id_seed", "injected_now", "set_invocation"]
