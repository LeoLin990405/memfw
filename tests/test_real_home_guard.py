"""The conftest guard: no test may touch the real per-user data dirs."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from memfw.conversations.adapter import data_dir
from memfw.conversations.worker import state_path
from tests.real_home_guard import PROTECTED_DIRS, protected_path

#: Under a protected dir, inside a subdir that does not exist: even without
#: the guard nothing could be created there.
PROBE = "guard-probe-does-not-exist"


@pytest.mark.parametrize("root", PROTECTED_DIRS)
def test_open_under_a_protected_dir_fails(root: str) -> None:
    target = os.path.join(root, PROBE, "x.txt")
    with pytest.raises(AssertionError, match="real per-user data dir"):
        open(target)  # noqa: SIM115 - the call itself is what is guarded


@pytest.mark.parametrize("root", PROTECTED_DIRS)
def test_sqlite_under_a_protected_dir_fails(root: str) -> None:
    target = os.path.join(root, PROBE, "x.sqlite")
    with pytest.raises(AssertionError, match="real per-user data dir"):
        sqlite3.connect(target)
    with pytest.raises(AssertionError, match="real per-user data dir"):
        sqlite3.connect(f"file:{target}?mode=ro", uri=True)


def test_protected_path_classification(tmp_path: Path) -> None:
    root = PROTECTED_DIRS[0]
    assert protected_path(root) == root
    assert protected_path(os.path.join(root, "a", "b")) == root
    assert protected_path(root + "-other") is None
    assert protected_path(tmp_path / "x") is None
    assert protected_path(3) is None
    assert protected_path(":memory:") is None


def test_the_default_worker_state_is_covered(monkeypatch: pytest.MonkeyPatch) -> None:
    """The 2026-11 leak: worker state resolved under the real HOME. Resolution only."""

    monkeypatch.setenv("HOME", str(Path(PROTECTED_DIRS[0]).parents[2]))
    assert protected_path(data_dir()) is not None
    assert protected_path(state_path({})) is not None


def test_isolated_tmp_home_is_allowed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    target = state_path({})
    target.parent.mkdir(parents=True, exist_ok=True)
    sqlite3.connect(target).close()
    assert target.exists()
