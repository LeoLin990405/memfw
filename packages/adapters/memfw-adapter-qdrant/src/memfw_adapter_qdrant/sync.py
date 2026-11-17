"""``sync``: make the Qdrant collection hold exactly this vault's sendable memories.

Run by the operator (``memfw-adapter-qdrant sync``), never by a search; it
reads the vault and writes only to Qdrant. Steps:

1. the collection exists with the embedder's dimension and cosine distance
   (created with keyword payload indexes on ``vault``, ``tenant``,
   ``embedder`` and ``memory_id`` when missing; another dimension refuses);
2. the points of this vault are listed (``scroll``, payload ``memory_id``,
   ``sha`` and ``embedder`` only, no vectors);
3. every memory the clearance admits whose text hash or embedder changed is
   embedded with memfw's embedder and upserted; its payload is
   ``memory_id``, ``tenant``, ``vault`` (the vault key), ``embedder`` (the
   fingerprint) and ``sha`` (a hash of tenant and text). No text is stored;
4. every point of this vault that is no longer sendable (deleted, now
   ``local-private`` or ``unknown``, unreadable) is deleted, **before**
   anything is embedded or upserted (step 3 runs last), so a failing
   embedder or upsert cannot keep a withdrawn memory in Qdrant.

Point ids are UUIDv5 of the vault key and the memory id, so a re-run is
idempotent. The returned counts carry no id or text.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from .embed import dimension_of, fingerprint_of
from .http import QdrantClient, QdrantError, collection_path
from .provider import QdrantProvider
from .vault import Outgoing, outgoing

#: Payload fields with a keyword index (filters and the sync diff).
INDEXED_FIELDS = ("vault", "tenant", "embedder", "memory_id")
_NAMESPACE = uuid.UUID("0b6f1c8e-6d0a-5c5e-9a43-6d656d66770a")
UPSERT_BATCH = 64
SCROLL_PAGE = 256


def point_id(vault: str, memory_id: str) -> str:
    return str(uuid.uuid5(_NAMESPACE, f"{vault}:{memory_id}"))


@dataclass(slots=True)
class SyncReport:
    """Counts only."""

    upserted: int = 0
    unchanged: int = 0
    deleted: int = 0
    created_collection: bool = False
    skipped: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "upserted": self.upserted,
            "unchanged": self.unchanged,
            "deleted": self.deleted,
            "created_collection": self.created_collection,
            "skipped": dict(self.skipped),
        }


def _vector_size(info: Any) -> int | None:
    try:
        vectors = info["config"]["params"]["vectors"]
        size = vectors["size"]
    except (KeyError, TypeError):
        return None
    return size if isinstance(size, int) and not isinstance(size, bool) else None


def ensure_collection(client: QdrantClient, collection: str, dimension: int) -> bool:
    """Create the collection when missing; True when it was created."""

    info = client.call("GET", collection_path(collection), allow_missing=True)
    if info is not None:
        if _vector_size(info) != dimension:
            raise QdrantError("dimension_mismatch")
        return False
    client.call(
        "PUT",
        collection_path(collection),
        {"vectors": {"size": dimension, "distance": "Cosine"}},
    )
    for name in INDEXED_FIELDS:
        client.call(
            "PUT",
            collection_path(collection, "/index?wait=true"),
            {"field_name": name, "field_schema": "keyword"},
        )
    return True


def existing_points(client: QdrantClient, collection: str, vault: str) -> dict[str, dict[str, Any]]:
    """``{point id: payload}`` of this vault's points (references and hashes only)."""

    found: dict[str, dict[str, Any]] = {}
    offset: Any = None
    while True:
        body: dict[str, Any] = {
            "limit": SCROLL_PAGE,
            "filter": {"must": [{"key": "vault", "match": {"value": vault}}]},
            "with_payload": ["memory_id", "sha", "embedder"],
            "with_vector": False,
        }
        if offset is not None:
            body["offset"] = offset
        result = client.call("POST", collection_path(collection, "/points/scroll"), body)
        points = result.get("points") if isinstance(result, dict) else None
        if not isinstance(points, list):
            raise QdrantError("bad_response")
        for point in points:
            if isinstance(point, dict) and isinstance(point.get("payload"), dict):
                found[str(point.get("id"))] = point["payload"]
        offset = result.get("next_page_offset")
        if offset is None or not points:
            return found


def _upsert(
    provider: QdrantProvider, batch: Sequence[Outgoing], fingerprint: str, report: SyncReport
) -> None:
    vectors = provider.embedder.embed([item.text for item in batch], role="document")
    if len(vectors) != len(batch):
        raise QdrantError("embedding_bad_response")
    points = [
        {
            "id": point_id(provider.vault_key, item.memory_id),
            "vector": [float(value) for value in vector],
            "payload": {
                "memory_id": item.memory_id,
                "tenant": item.tenant,
                "vault": provider.vault_key,
                "embedder": fingerprint,
                "sha": item.sha,
            },
        }
        for item, vector in zip(batch, vectors, strict=True)
    ]
    provider.client.call(
        "PUT",
        collection_path(provider.settings.collection, "/points?wait=true"),
        {"points": points},
    )
    report.upserted += len(points)


def sync(provider: QdrantProvider) -> SyncReport:
    """Bring the collection in line with the vault (see the module docstring)."""

    if provider.embedder is None:
        raise QdrantError("no_embedder")
    settings = provider.settings
    client = provider.client
    dimension = dimension_of(provider.embedder)
    fingerprint = fingerprint_of(provider.embedder)
    report = SyncReport()
    report.created_collection = ensure_collection(client, settings.collection, dimension)
    existing = existing_points(client, settings.collection, provider.vault_key)
    # Decide what may stay before anything is embedded or sent, and delete the
    # rest first: a failing embedder or upsert later in the run must never
    # leave a memory in Qdrant after it was removed or became unsendable.
    sendable = list(outgoing(provider.root, settings.clearance, report.skipped))
    keep = {point_id(provider.vault_key, item.memory_id) for item in sendable}
    stale = sorted(set(existing) - keep)
    for start in range(0, len(stale), UPSERT_BATCH * 4):
        chunk = stale[start : start + UPSERT_BATCH * 4]
        client.call(
            "POST",
            collection_path(settings.collection, "/points/delete?wait=true"),
            {"points": chunk},
        )
        report.deleted += len(chunk)
    pending: list[Outgoing] = []
    for item in sendable:
        current = existing.get(point_id(provider.vault_key, item.memory_id))
        if (
            current is not None
            and current.get("sha") == item.sha
            and current.get("embedder") == fingerprint
            and current.get("memory_id") == item.memory_id
        ):
            report.unchanged += 1
            continue
        pending.append(item)
        if len(pending) >= UPSERT_BATCH:
            _upsert(provider, pending, fingerprint, report)
            pending = []
    if pending:
        _upsert(provider, pending, fingerprint, report)
    return report


__all__ = [
    "INDEXED_FIELDS",
    "SyncReport",
    "ensure_collection",
    "existing_points",
    "point_id",
    "sync",
]
