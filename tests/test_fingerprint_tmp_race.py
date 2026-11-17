"""``_tree_fingerprint`` and atomic-write temporaries that vanish during the walk.

A concurrent writer (an atomic write, a projection rebuild in an older build
that wrote into the vault) creates ``*.tmp`` files and renames or removes
them. A temporary the walk listed but can no longer read is skipped; any
other file that vanishes still raises.
"""

from __future__ import annotations

import secrets
import threading
from pathlib import Path

import pytest

from memfw.core.repository import Repository


def _vault(tmp_path: Path) -> Repository:
    repository = Repository(tmp_path / "vault")
    repository.init()
    repository.add("Backup", "nightly backup", memory_id="m-backup")
    return repository


def test_a_tmp_file_that_vanishes_mid_walk_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _vault(tmp_path)
    clean = repository._tree_fingerprint()
    doomed = repository.root / f".edges.sqlite3.{secrets.token_hex(8)}.tmp"
    doomed.write_bytes(b"half-built")
    real = Path.read_bytes

    def vanish_first(self: Path) -> bytes:
        if self == doomed and self.exists():
            self.unlink()  # removed between the listing and the read
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", vanish_first)
    assert repository._tree_fingerprint() == clean


def test_a_non_tmp_file_that_vanishes_mid_walk_still_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _vault(tmp_path)
    doomed = repository.root / "stray.bin"
    doomed.write_bytes(b"x")
    real = Path.read_bytes

    def vanish(self: Path) -> bytes:
        if self == doomed and self.exists():
            self.unlink()
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", vanish)
    with pytest.raises(FileNotFoundError):
        repository._tree_fingerprint()


def test_fingerprint_survives_a_concurrent_tmp_creator_and_deleter(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    stop = threading.Event()
    errors: list[BaseException] = []

    def churn() -> None:
        while not stop.is_set():
            path = repository.root / f".edges.sqlite3.{secrets.token_hex(8)}.tmp"
            try:
                path.write_bytes(b"x" * 512)
                path.unlink()
            except BaseException as exc:  # pragma: no cover - reported below
                errors.append(exc)
                return

    workers = [threading.Thread(target=churn) for _ in range(4)]
    for worker in workers:
        worker.start()
    try:
        for _ in range(300):
            repository._tree_fingerprint()
    finally:
        stop.set()
        for worker in workers:
            worker.join()
    assert not errors
    assert not list(repository.root.glob("*.tmp"))
