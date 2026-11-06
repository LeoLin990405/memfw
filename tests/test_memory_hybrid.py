from __future__ import annotations

from pathlib import Path

from memfw.core.models import Layer
from memfw.core.repository import Repository
from memfw.mcp.server import Gateway
from memfw.memory_hybrid import MemoryHybridRetriever
from memfw.notes import HashEmbeddingProvider


def _vault(tmp_path: Path) -> Repository:
    repository = Repository(tmp_path / "vault")
    repository.init()
    repository.add("Corpus notes", "corpus collocations and KWIC", memory_id="m-corpus")
    repository.add("NAS notes", "synology hyper backup", memory_id="m-nas", source="nas")
    return repository


def test_memory_hybrid_search_and_fallback(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    embedder = HashEmbeddingProvider(8)
    hybrid = MemoryHybridRetriever(repository, embedder=embedder)
    rebuilt = hybrid.rebuild()
    assert rebuilt["sections"] >= 1
    result = hybrid.search("corpus collocations", limit=5, mode="hybrid")
    assert result["mode"] == "hybrid"
    assert result["degraded"] is False
    assert result["results"][0]["id"] == "m-corpus"
    assert result["model_versions"]["embedding"]

    fallback = MemoryHybridRetriever(repository)
    result = fallback.search("corpus", limit=5, mode="hybrid")
    assert result["mode"] == "fts"
    assert result["degraded"] is True
    assert result["results"][0]["id"] == "m-corpus"


def test_memory_source_filter_and_mcp(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    embedder = HashEmbeddingProvider(8)
    gateway = Gateway(repository.root, embedder=embedder)
    payload = gateway.search("backup", limit=5, mode="hybrid", source="nas")
    assert payload["mode"] == "hybrid"
    assert [item["id"] for item in payload["results"]] == ["m-nas"]
    assert payload["results"][0]["source"] == "nas"


def test_memory_context_prefers_anchors_and_procedural(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    repository.add(
        "Anchor rule",
        "Always cite sources.",
        memory_id="m-anchor",
        layer=Layer.ANCHOR,
        anchor="context",
        memory_type="semantic",
    )
    repository.add(
        "Procedure",
        "1. Search 2. Read 3. Cite",
        memory_id="m-proc",
        memory_type="procedural",
    )
    gateway = Gateway(repository.root)
    payload = gateway.context(limit_tokens=200)
    assert [item["id"] for item in payload["entries"]][:2] == ["m-anchor", "m-proc"]
    assert payload["entries"][0]["anchor"] == "context"


def test_entity_channel_can_be_toggled(tmp_path: Path) -> None:
    repository = Repository(tmp_path / "vault")
    repository.init()
    repository.add("Reflective notes", "reflective endorsement", memory_id="m-k")
    repository.add("Unrelated", "quantum refrigerator", memory_id="m-u")
    off = MemoryHybridRetriever(repository)
    off.rebuild()
    assert all(item["id"] != "m-k" for item in off.search("Korsgaard's", limit=5)["results"])
    on = MemoryHybridRetriever(repository, entity_enabled=True)
    on.rebuild()
    import sqlite3

    with sqlite3.connect(on.entity_index.path) as connection:
        connection.execute(
            "INSERT OR IGNORE INTO entities(entity, memory_id) VALUES (?,?)",
            ("korsgaard", "m-k"),
        )
    result = on.search("Korsgaard's", limit=5)
    assert result["mode"] == "fts"
    assert "m-k" in {item["id"] for item in result["results"]}


def test_memory_remember_adds_duplicate_suggestions_without_writing(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    gateway = Gateway(repository.root)
    payload = gateway.remember("corpus collocations and KWIC", title="Corpus notes")
    assert payload["authority"] is False
    assert payload["suggestions"]
    assert payload["suggestions"][0]["memory_id"] == "m-corpus"
    assert payload["suggestions"][0]["kind"] in {"duplicate", "extends"}
    candidate = Path(payload["staged_candidate"]).read_text(encoding="utf-8")
    assert "suggestions:" in candidate
    assert len(repository.list()) == 2


def test_candidate_apply_requires_explicit_human_apply(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from memfw.cli import app

    repository = _vault(tmp_path)
    gateway = Gateway(repository.root)
    payload = gateway.remember("new body", title="New memory", tags=["new"])
    candidate = payload["staged_candidate"]
    runner = CliRunner()
    preview = runner.invoke(app, ["candidate-apply", candidate, "--root", str(repository.root)])
    assert preview.exit_code == 0, preview.output
    assert len(repository.list()) == 2
    applied = runner.invoke(
        app,
        ["candidate-apply", candidate, "--root", str(repository.root), "--apply"],
    )
    assert applied.exit_code == 0, applied.output
    assert len(repository.list()) == 3
    assert any("new" in doc.tags for doc in repository.list())
