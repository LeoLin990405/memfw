"""Input aliases valid_until and superseded store the canonical temporal names."""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from memfw.core.models import MemoryDocument
from memfw.core.repository import Repository
from memfw.index import MemoryIndex
from memfw.storage.markdown import MarkdownStorageMixin
from scripts.memfw_readonly_service import _document_payload, search_vault

BASE = datetime(2026, 11, 6, tzinfo=UTC)
UNTIL = datetime(2026, 11, 6, 1, tzinfo=UTC)


def _document(**extra: object) -> MemoryDocument:
    payload: dict[str, object] = {
        "id": "mem-alias",
        "title": "Alias note",
        "body": "alias corpus keyword",
        "created_at": BASE,
        "updated_at": BASE,
    }
    payload.update(extra)
    return MemoryDocument.model_validate(payload)


def test_model_accepts_aliases_and_emits_canonical_names() -> None:
    document = _document(valid_until=UNTIL.isoformat(), superseded="mem-next")
    assert document.valid_to == UNTIL
    assert document.superseded_by == "mem-next"
    dumped = document.model_dump(mode="json")
    assert dumped["valid_to"].startswith("2026-11-06T01:00:00")
    assert dumped["superseded_by"] == "mem-next"
    assert "valid_until" not in dumped
    assert "superseded" not in dumped
    rendered = MarkdownStorageMixin.render(document)
    assert "valid_until" not in rendered
    assert "\nsuperseded:" not in rendered
    assert "valid_to:" in rendered
    assert "superseded_by: mem-next" in rendered


def test_conflicting_alias_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _document(valid_to=UNTIL, valid_until="2026-11-06T02:00:00+00:00")
    with pytest.raises(ValidationError):
        _document(superseded_by="mem-a", superseded="mem-b")


def test_same_alias_value_is_accepted() -> None:
    document = _document(
        valid_to=UNTIL,
        valid_until=UNTIL.isoformat(),
        superseded_by="mem-next",
        superseded="mem-next",
    )
    assert document.valid_to == UNTIL
    assert document.superseded_by == "mem-next"


def test_parse_and_reader_fold_alias_frontmatter(tmp_path: Path) -> None:
    rendered = MarkdownStorageMixin.render(_document(valid_to=UNTIL, superseded_by="mem-next"))
    aliased = rendered.replace("valid_to:", "valid_until:", 1).replace(
        "superseded_by:", "superseded:", 1
    )
    parsed = MarkdownStorageMixin.parse(aliased, expected_id="mem-alias")
    assert parsed.valid_to == UNTIL
    assert parsed.superseded_by == "mem-next"

    root = tmp_path / "vault"
    repository = Repository(root)
    repository.init()
    (root / "memories").mkdir(exist_ok=True)
    (root / "memories" / "mem-alias.md").write_text(aliased, encoding="utf-8")
    current = MarkdownStorageMixin.render(
        _document(id="mem-live", title="Live note", body="alias corpus keyword")
    )
    (root / "memories" / "mem-live.md").write_text(current, encoding="utf-8")
    payload = _document_payload(root, "mem-alias")
    assert payload is not None
    assert payload["valid_to"].startswith("2026-11-06T01:00:00")
    assert payload["superseded_by"] == "mem-next"
    assert "valid_until" not in payload
    assert "superseded" not in payload

    MemoryIndex(repository.index_path).rebuild(repository)
    found = search_vault(root, "alias corpus keyword", limit=10)
    assert {item["id"] for item in found["results"]} == {"mem-live"}
    history = search_vault(root, "alias corpus keyword", limit=10, include_history=True)
    old = next(item for item in history["results"] if item["id"] == "mem-alias")
    assert old["valid_to"].startswith("2026-11-06T01:00:00")
    assert old["superseded_by"] == "mem-next"
    assert "valid_until" not in old


def test_update_stores_alias_under_the_canonical_name(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    repository = Repository(root)
    repository.init()
    repository.add("Alias note", "alias corpus keyword", memory_id="mem-alias")
    updated = repository.update(
        "mem-alias",
        actor="human",
        valid_until=UNTIL.isoformat(),
        superseded="mem-next",
    )
    assert updated.valid_to == UNTIL
    assert updated.superseded_by == "mem-next"
    stored = (root / "memories" / "mem-alias.md").read_text(encoding="utf-8")
    assert "valid_until" not in stored
    assert "\nsuperseded:" not in stored
    assert "superseded_by: mem-next" in stored


def test_alias_replaces_and_clears_a_stored_canonical_value(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    repository = Repository(root)
    repository.init()
    repository.add("Alias note", "alias corpus keyword", memory_id="mem-alias")
    repository.update(
        "mem-alias",
        actor="human",
        valid_to=UNTIL,
        superseded_by="mem-a",
    )
    later = datetime(2026, 11, 7, tzinfo=UTC)
    replaced = repository.update(
        "mem-alias",
        actor="human",
        valid_until=later.isoformat(),
        superseded="mem-b",
    )
    assert replaced.valid_to == later
    assert replaced.superseded_by == "mem-b"
    cleared = repository.update(
        "mem-alias",
        actor="human",
        valid_until=None,
        superseded=None,
    )
    assert cleared.valid_to is None
    assert cleared.superseded_by is None
    stored = (root / "memories" / "mem-alias.md").read_text(encoding="utf-8")
    assert "2026-11-07" not in stored
    assert "mem-b" not in stored


def test_same_request_conflict_does_not_change_file_or_event(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    repository = Repository(root)
    repository.init()
    repository.add("Alias note", "alias corpus keyword", memory_id="mem-alias")
    repository.update("mem-alias", actor="human", valid_to=UNTIL, superseded_by="mem-a")
    path = root / "memories" / "mem-alias.md"
    before = path.read_bytes()
    events_before = len(repository.events())
    with pytest.raises(ValueError, match="valid_until conflicts with valid_to"):
        repository.update(
            "mem-alias",
            actor="human",
            valid_to=UNTIL,
            valid_until="2026-11-06T02:00:00+00:00",
        )
    assert path.read_bytes() == before
    assert len(repository.events()) == events_before


def test_distinct_supersession_ids_conflict_on_model_and_update(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        _document(superseded_by="2026-11-06", superseded="20261106")
    root = tmp_path / "vault"
    repository = Repository(root)
    repository.init()
    repository.add("Alias note", "alias corpus keyword", memory_id="mem-alias")
    path = root / "memories" / "mem-alias.md"
    before = path.read_bytes()
    events_before = len(repository.events())
    with pytest.raises(ValueError, match="superseded conflicts with superseded_by"):
        repository.update(
            "mem-alias",
            actor="human",
            superseded_by="2026-11-06",
            superseded="20261106",
        )
    assert path.read_bytes() == before
    assert len(repository.events()) == events_before


def test_readonly_service_help_starts_without_memfw() -> None:
    script = Path(__file__).resolve().parents[1] / "scripts" / "memfw_readonly_service.py"
    probe = """
import importlib.abc
import sys

class RejectMemfw(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname == "memfw" or fullname.startswith("memfw."):
            raise ModuleNotFoundError(fullname)
        return None

sys.meta_path.insert(0, RejectMemfw())
script = sys.argv[1]
sys.argv = [script, "--help"]
import runpy
runpy.run_path(script, run_name="__main__")
"""
    completed = subprocess.run(
        [sys.executable, "-I", "-c", probe, str(script)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "usage" in completed.stdout.lower()
