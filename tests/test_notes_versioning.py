from __future__ import annotations

from pathlib import Path

import pytest

from memfw.notes import (
    HashEmbeddingProvider,
    IndexCompatibilityError,
    IndexManifest,
    NotesIndex,
    build_manifest,
    read_manifest,
    verify_manifest,
    write_manifest,
)


def test_manifest_roundtrip_and_compatibility(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    (root / "a.md").write_text("# A\n\ncorpus\n", encoding="utf-8")
    index = NotesIndex(tmp_path / "index.sqlite", embedder=HashEmbeddingProvider(8))
    index.index(root)
    manifest = build_manifest(index, labels={"purpose": "test"})
    path = tmp_path / "manifest.json"
    write_manifest(path, manifest)
    restored = read_manifest(path)
    assert restored.embedding_fingerprint == manifest.embedding_fingerprint
    assert restored.labels == {"purpose": "test"}
    verify_manifest(index, restored)
    broken = IndexManifest(
        index_id=restored.index_id,
        manifest_version=restored.manifest_version,
        chunker_version=restored.chunker_version,
        embedding_fingerprint="different",
        embedding_model=restored.embedding_model,
        dimension=restored.dimension,
        notes=restored.notes,
        sections=restored.sections,
        vectors=restored.vectors,
        created_at=restored.created_at,
        labels={},
    )
    with pytest.raises(IndexCompatibilityError):
        verify_manifest(index, broken)
