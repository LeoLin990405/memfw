"""``memfw.projection.load_lifecycle``: the one meta-index reader recall and dream share."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from memfw import projection
from memfw.dream import questions
from memfw.projection import DEFAULT_LIFECYCLE_SOURCE, Lifecycle, load_lifecycle
from memfw.recall import builtin


def _write(path: Path, items: list[dict[str, object]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"items": items}), encoding="utf-8")
    return path


def test_recall_and_dream_use_the_same_reader() -> None:
    assert builtin.load_lifecycle is questions.load_lifecycle is load_lifecycle
    assert builtin.Lifecycle is questions.Lifecycle is Lifecycle
    assert builtin.DEFAULT_LIFECYCLE_SOURCE == questions.DEFAULT_LIFECYCLE_SOURCE


def test_states_of_the_default_file(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    assert load_lifecycle(root) == Lifecycle()  # absent: the vault alone decides
    default = root / DEFAULT_LIFECYCLE_SOURCE
    default.mkdir(parents=True)
    assert load_lifecycle(root) == Lifecycle(unreadable=True)  # a directory fails closed
    default.rmdir()
    real = _write(tmp_path / "real.json", [{"path": "a.md"}])
    default.symlink_to(real)
    assert load_lifecycle(root) == Lifecycle(unreadable=True)  # a symlinked default
    default.unlink()
    default.symlink_to(tmp_path / "dangling.json")
    assert load_lifecycle(root) == Lifecycle(unreadable=True)
    default.unlink()
    default.write_text("{not json", encoding="utf-8")
    assert load_lifecycle(root) == Lifecycle(unreadable=True)
    _write(default, [{"path": "a.md", "lifecycle": "retired"}])
    loaded = load_lifecycle(root)
    assert loaded.meta is not None and loaded.meta.lookup("a.md") == {
        "path": "a.md",
        "lifecycle": "retired",
    }


def test_states_of_a_named_source(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    assert load_lifecycle(root, tmp_path / "absent.json") == Lifecycle(unreadable=True)
    (tmp_path / "dir.json").mkdir()
    assert load_lifecycle(root, tmp_path / "dir.json") == Lifecycle(unreadable=True)
    real = _write(tmp_path / "base" / "status" / "memory-meta-index.json", [{"path": "a.md"}])
    link = tmp_path / "current.json"
    link.symlink_to(real)
    named = load_lifecycle(root, link)  # the operator named it: symlinks are followed
    assert named.meta is not None and named.meta.lookup("a.md") is not None
    assert load_lifecycle(root, str(real)).meta is not None


def test_the_parsed_file_is_cached_until_it_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _write(tmp_path / "meta.json", [{"path": "a.md"}])
    reads: list[Path] = []
    real = projection.load_meta_index

    def counting(path: Path | None) -> object:
        assert path is not None
        reads.append(path)
        return real(path)

    monkeypatch.setattr(projection, "load_meta_index", counting)
    first = load_lifecycle(tmp_path, source)
    second = load_lifecycle(tmp_path, source)
    assert len(reads) == 1 and first.meta is second.meta
    _write(source, [{"path": "a.md"}, {"path": "b.md"}])  # new size
    third = load_lifecycle(tmp_path, source)
    assert len(reads) == 2 and third.meta is not None and third.meta.lookup("b.md") is not None
    info = source.stat()
    os.utime(source, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000))  # same size, new mtime
    load_lifecycle(tmp_path, source)
    assert len(reads) == 3
    source.write_text("{broken", encoding="utf-8")
    assert load_lifecycle(tmp_path, source) == Lifecycle(unreadable=True)  # never a stale hit


def test_dream_privacy_reads_a_directory_default_as_unreadable(tmp_path: Path) -> None:
    """The one behaviour change: dream used to read a directory as "no meta-index"."""

    from memfw.core.repository import Repository

    repository = Repository(tmp_path / "vault")
    repository.init()
    document = repository.add("t", "b", memory_id="m-a", tags=["fleet_path:a.md"])
    (repository.root / DEFAULT_LIFECYCLE_SOURCE).mkdir(parents=True)
    lifecycle = questions.load_lifecycle(repository.root)
    assert lifecycle.unreadable
    assert questions.is_private(document, lifecycle)
