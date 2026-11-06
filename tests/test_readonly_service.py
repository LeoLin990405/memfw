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


def test_readonly_service_related(tmp_path: Path) -> None:
    from scripts.memfw_readonly_service import related_vault

    repository = Repository(tmp_path / "vault")
    repository.init()
    repository.add("Corpus", "corpus KWIC collocations", memory_id="a")
    repository.add("Corpus methods", "corpus KWIC", memory_id="b")
    repository.add("NAS", "synology backup", memory_id="c")
    MemoryIndex(repository.index_path).rebuild(repository)
    result = related_vault(repository.root, "a", limit=5)
    assert any(item["id"] == "b" for item in result["results"])
    assert all(item["id"] != "a" for item in result["results"])
