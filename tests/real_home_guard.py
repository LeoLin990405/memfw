"""Guard against tests touching the real per-user data dirs (one module instance).

Imported as ``tests.real_home_guard`` everywhere (``conftest.py`` and tests), so
the switch and the audit hook exist once per process.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

#: The real HOME's live data (memfw's conversation store and worker state, the
#: fleet-memory deployment). Read once at import, before any test changes HOME.
_HOME = os.path.abspath(os.path.expanduser("~"))
_DIR_NAMES = ("memfw", "fleet-memory")


def _forms(path: str) -> tuple[str, ...]:
    """``path`` as written (absolute) and with symlinks resolved, normalised."""

    plain = os.path.normcase(os.path.abspath(path))
    real = os.path.normcase(os.path.realpath(path))
    return (plain,) if real == plain else (plain, real)


#: Each protected dir, in its absolute and its resolved form.
PROTECTED_DIRS: tuple[str, ...] = tuple(
    dict.fromkeys(
        form for name in _DIR_NAMES for form in _forms(os.path.join(_HOME, ".local", "share", name))
    )
)
#: Audit events whose path arguments (the first two) a test is about to use.
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
        "os.utime",
        "os.symlink",
        "os.link",
        "shutil.copyfile",
        "shutil.copytree",
        "shutil.move",
        "shutil.rmtree",
    }
)
_guard_active = False
_checking = False


def protected_path(value: Any) -> str | None:
    """The protected dir ``value`` falls under, else ``None`` (fds and odd values: ``None``).

    Both forms of ``value`` are compared with both forms of each dir, so a
    symlink into a protected dir (or a symlinked HOME) is caught too.
    """

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
    for path in _forms(os.path.expanduser(raw)):
        for root in PROTECTED_DIRS:
            if path == root or path.startswith(root + os.sep):
                return root
    return None


def _audit(event: str, args: tuple[Any, ...]) -> None:
    global _checking
    if not _guard_active or _checking or event not in _PATH_EVENTS or not args:
        return
    _checking = True
    try:
        root = next((found for found in map(protected_path, args[:2]) if found is not None), None)
    finally:
        _checking = False
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
