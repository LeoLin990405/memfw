"""A memory written by a newer memfw (unknown frontmatter keys) stays readable."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from memfw.core.errors import IntegrityError
from memfw.core.repository import Repository


def _add_keys(path: Path, **fields: object) -> None:
    _, raw, body = path.read_text(encoding="utf-8").split("---\n", 2)
    meta = yaml.safe_load(raw)
    meta.update(fields)
    dumped = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False).rstrip()
    path.write_text(f"---\n{dumped}\n---\n{body}", encoding="utf-8")


def test_unknown_frontmatter_keys_are_ignored(tmp_path: Path) -> None:
    repository = Repository(tmp_path / "vault")
    repository.init()
    repository.add("Future", "written by a newer version", memory_id="m-future")
    _add_keys(
        repository.root / "memories" / "m-future.md",
        future_flag=True,
        future_list=["a", "b"],
    )
    document = repository.get("m-future")
    assert document.body == "written by a newer version"
    assert "m-future" in [item.id for item in repository.list()]


def test_other_validation_errors_are_still_rejected(tmp_path: Path) -> None:
    repository = Repository(tmp_path / "vault")
    repository.init()
    repository.add("Bad", "body", memory_id="m-bad")
    _add_keys(repository.root / "memories" / "m-bad.md", strength=5, future_flag=True)
    with pytest.raises(IntegrityError):
        repository.get("m-bad")
