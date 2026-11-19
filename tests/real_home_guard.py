"""Guard against tests touching the real per-user data dirs (one module instance).

Imported as ``tests.real_home_guard`` everywhere (``conftest.py`` and tests), so
the switch and the audit hook exist once per process.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

#: The real HOME's live data (memfw's conversation store and worker state, the
#: fleet-memory deployment). Read once at import, before any test changes HOME.
_REAL_HOME = Path(os.path.expanduser("~")).resolve()
PROTECTED_DIRS: tuple[str, ...] = tuple(
    os.path.normcase(os.fspath(_REAL_HOME / ".local" / "share" / name))
    for name in ("memfw", "fleet-memory")
)
#: Audit events whose first argument is a path a test is about to use.
_PATH_EVENTS = frozenset(
    {
        "open",
        "sqlite3.connect",
        "os.mkdir",
        "os.listdir",
        "os.scandir",
        "os.remove",
        "os.rename",
        "os.rmdir",
        "os.truncate",
        "os.chmod",
        "shutil.copyfile",
        "shutil.rmtree",
    }
)
_guard_active = False


def protected_path(value: Any) -> str | None:
    """The protected dir ``value`` falls under, else ``None`` (fds and odd values: ``None``)."""

    if isinstance(value, int) or value is None:
        return None
    try:
        raw = os.fsdecode(os.fspath(value))
    except TypeError:
        return None
    if not raw or raw == ":memory:" or raw.startswith("file::memory:"):
        return None
    if raw.startswith("file:"):
        raw = raw[5:].split("?", 1)[0]
    path = os.path.normcase(os.path.abspath(os.path.expanduser(raw)))
    for root in PROTECTED_DIRS:
        if path == root or path.startswith(root + os.sep):
            return root
    return None


def _audit(event: str, args: tuple[Any, ...]) -> None:
    if not _guard_active or event not in _PATH_EVENTS or not args:
        return
    root = protected_path(args[0])
    if root is not None:
        raise AssertionError(
            f"test touched the real per-user data dir {root} ({event}); "
            "isolate HOME (the isolated_home fixture) or pass a tmp path"
        )


sys.addaudithook(_audit)  # cannot be removed; ``_guard_active`` switches it


@contextmanager
def guarding() -> Iterator[None]:
    """The guard is on inside (the autouse fixture in ``conftest.py``)."""

    global _guard_active
    _guard_active = True
    try:
        yield
    finally:
        _guard_active = False


@contextmanager
def inspecting_real_home() -> Iterator[None]:
    """Let a fixture *list* the real data dirs to prove a test left them alone.

    For the before/after snapshot fixtures only; never wrap test code in it.
    """

    global _guard_active
    was, _guard_active = _guard_active, False
    try:
        yield
    finally:
        _guard_active = was
