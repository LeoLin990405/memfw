from __future__ import annotations

from pathlib import Path

from memfw.core.repository import Repository
from memfw.index import MemoryIndex
from scripts.memfw_readonly_service import search_vault


def test_readonly_service_filters_current_tenant(tmp_path: Path) -> None:
    repository = Repository(tmp_path / "vault")
    repository.init()
    repository.add("Alpha", "alpha corpus", memory_id="a", tenant="tenant-a")
    repository.add("Beta", "beta corpus", memory_id="b", tenant="tenant-b")
    MemoryIndex(repository.index_path).rebuild(repository)
    result = search_vault(repository.root, "corpus", tenant="tenant-a", limit=5)
    assert [item["id"] for item in result["results"]] == ["a"]
    assert result["mode"] == "fts"


def test_readonly_service_excludes_superseded(tmp_path: Path) -> None:
    repository = Repository(tmp_path / "vault")
    repository.init()
    repository.add("Old", "old unique phrase", memory_id="old")
    repository.supersede("old", "New", "new unique phrase", memory_id_new="new")
    MemoryIndex(repository.index_path).rebuild(repository)
    current = search_vault(repository.root, "unique phrase", limit=10)
    assert {item["id"] for item in current["results"]} == {"new"}
    history = search_vault(repository.root, "unique phrase", include_history=True, limit=10)
    assert {item["id"] for item in history["results"]} == {"new", "old"}
