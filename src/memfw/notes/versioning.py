"""Index manifest and compatibility checks.

Model changes must not reuse an old index, even when dimensions match. This
module records the model fingerprint, chunker version, document count and index
identity, and verifies a candidate index before any future cutover.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .index import NotesIndex

CHUNKER_VERSION = "section-v1"
MANIFEST_VERSION = 1


class IndexCompatibilityError(RuntimeError):
    """The index cannot be used with the requested model/chunker generation."""


@dataclass(frozen=True)
class IndexManifest:
    index_id: str
    manifest_version: int
    chunker_version: str
    embedding_fingerprint: str
    embedding_model: str
    dimension: int
    notes: int
    sections: int
    vectors: int
    created_at: float
    labels: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "index_id": self.index_id,
            "manifest_version": self.manifest_version,
            "chunker_version": self.chunker_version,
            "embedding_fingerprint": self.embedding_fingerprint,
            "embedding_model": self.embedding_model,
            "dimension": self.dimension,
            "notes": self.notes,
            "sections": self.sections,
            "vectors": self.vectors,
            "created_at": self.created_at,
            "labels": self.labels,
        }


def build_manifest(
    index: NotesIndex,
    *,
    index_id: str | None = None,
    labels: dict[str, str] | None = None,
) -> IndexManifest:
    status = index.status()
    meta = {key: status.get(key, "") for key in ("embedding_model", "embedding_fingerprint")}
    return IndexManifest(
        index_id=index_id or str(uuid.uuid4()),
        manifest_version=MANIFEST_VERSION,
        chunker_version=CHUNKER_VERSION,
        embedding_fingerprint=str(meta["embedding_fingerprint"] or ""),
        embedding_model=str(meta["embedding_model"] or ""),
        dimension=int(status.get("dimension") or 0),
        notes=int(status.get("notes") or 0),
        sections=int(status.get("sections") or 0),
        vectors=int(status.get("vectors") or 0),
        created_at=time.time(),
        labels=dict(labels or {}),
    )


def write_manifest(path: Path | str, manifest: IndexManifest) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(manifest.as_dict(), ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def read_manifest(path: Path | str) -> IndexManifest:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("manifest must be a JSON object")
    if int(data.get("manifest_version", -1)) != MANIFEST_VERSION:
        raise IndexCompatibilityError("unsupported manifest version")
    return IndexManifest(
        index_id=str(data["index_id"]),
        manifest_version=int(data["manifest_version"]),
        chunker_version=str(data["chunker_version"]),
        embedding_fingerprint=str(data.get("embedding_fingerprint") or ""),
        embedding_model=str(data.get("embedding_model") or ""),
        dimension=int(data.get("dimension") or 0),
        notes=int(data.get("notes") or 0),
        sections=int(data.get("sections") or 0),
        vectors=int(data.get("vectors") or 0),
        created_at=float(data.get("created_at") or 0.0),
        labels={str(k): str(v) for k, v in (data.get("labels") or {}).items()},
    )


def verify_manifest(index: NotesIndex, manifest: IndexManifest) -> None:
    status = index.status()
    actual_fingerprint = str(status.get("embedding_fingerprint") or "")
    if manifest.embedding_fingerprint != actual_fingerprint:
        raise IndexCompatibilityError("embedding fingerprint mismatch")
    if manifest.dimension != int(status.get("dimension") or 0):
        raise IndexCompatibilityError("embedding dimension mismatch")
    if manifest.chunker_version != CHUNKER_VERSION:
        raise IndexCompatibilityError("chunker version mismatch")


__all__ = [
    "CHUNKER_VERSION",
    "IndexCompatibilityError",
    "IndexManifest",
    "MANIFEST_VERSION",
    "build_manifest",
    "read_manifest",
    "verify_manifest",
    "write_manifest",
]
