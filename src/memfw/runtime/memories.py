"""``memory_remember``, ``memory_related`` and ``memory_status`` below the interface (A3b).

Moved from ``memfw.mcp.tools_memory`` unchanged; ``Runtime.remember``,
``Runtime.related`` and ``Runtime.status`` call these.
"""

from __future__ import annotations

import re
import time
import uuid
from pathlib import Path
from typing import Any

from memfw.capture.staging import (
    CANDIDATE_DIR,
    MAX_CANDIDATE_BYTES,
    candidate_document,
    publish_candidate,
)
from memfw.capture.suggestions import suggest_related
from memfw.core.errors import NotFoundError
from memfw.core.models import Layer, MemoryDocument, Tier
from memfw.core.repository import Repository
from memfw.index.readonly import vault_legacy_privacy
from memfw.notes.http import embedder_sends_off_host
from memfw.ports.privacy import effective_level
from memfw.related import RelatedIndex

#: The largest ``memory_remember`` content, in UTF-8 bytes (64 KiB).
REMEMBER_MAX_BYTES = MAX_CANDIDATE_BYTES


def _slug(text: str) -> str:
    value = re.sub(r"[^a-z0-9_-]+", "-", text.lower()).strip("-")
    return value[:48] or "memory"


def stage_remember(
    root: Path,
    repository: Repository,
    text: str,
    title: str,
    *,
    tenant: str,
    embedder: Any,
    tags: list[str] | None,
    related: list[str] | None,
    privacy: str | None = None,
    embedding_allow_remote: bool = False,
) -> dict[str, Any]:
    """Stage one candidate under ``_candidates/remember/`` of ``root``.

    ``text`` is already stripped and non-empty; ``repository`` is the open
    vault the related suggestions are read from. The suggestions embed the
    candidate's title or text as the query: with an off-host embedder and no
    ``embedding_allow_remote``, a candidate that is not ``public``/``shared``
    (its ``privacy``, tags and the vault default) gets lexical suggestions
    only, so its text never leaves the machine.
    """

    if (
        embedder is not None
        and not embedding_allow_remote
        and embedder_sends_off_host(embedder)
        and effective_level(privacy, list(tags or []), legacy=vault_legacy_privacy(repository.root))
        not in ("public", "shared")
    ):
        embedder = None
    suggestions = suggest_related(
        repository, title, text, embedder=embedder, embedding_allow_remote=embedding_allow_remote
    )
    suggestion_rows = [item.as_dict() for item in suggestions]
    related_ids = list(related) if related else [item.memory_id for item in suggestions]
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    published: str | None = None
    for _ in range(5):
        proposal_id = f"mcp-{uuid.uuid4().hex}"
        filename = f"{stamp}-{_slug(title)}-{proposal_id}.md"
        document = candidate_document(
            title=title,
            text=text,
            proposal_id=proposal_id,
            tenant=tenant,
            tags=list(tags or []),
            related=list(related_ids),
            suggestions=suggestion_rows,
            privacy=privacy,
        )
        try:
            publish_candidate(root, filename, document)
        except FileExistsError:
            continue
        published = filename
        break
    if published is None:
        raise RuntimeError("could not publish candidate")
    return {
        "staged_candidate": (CANDIDATE_DIR / published).as_posix(),
        "authority": False,
        "suggestions": suggestion_rows,
    }


def related_memories(
    root: Path,
    repository: Repository,
    memory_id: str,
    limit: int,
    *,
    tenant: str,
    visible: Any,
    require: Any = None,
) -> dict[str, Any]:
    """``memory_related``: neighbours of a visible memory, only visible ones.

    ``require`` (when given) raises for a memory the caller may not see.
    """

    def same_tenant_neighbour(row: dict[str, Any]) -> bool:
        if str(row.get("source") or "") != "memory":
            return False
        try:
            neighbour = repository.get(str(row.get("id") or ""))
        except NotFoundError:
            return False
        return bool(visible(neighbour))

    document = repository.get(memory_id)
    if require is not None:
        require(document)
    elif not visible(document):
        raise ValueError("memory is not visible in this tenant")
    bounded = max(1, min(limit, 50))
    rows = RelatedIndex(root / ".memfw-related.sqlite").related(
        "memory",
        memory_id,
        limit=50,
        tenant=tenant,
    )
    found = [row for row in rows if same_tenant_neighbour(row)]
    return {"memory_id": memory_id, "related": found[:bounded]}


def vault_status(repository: Repository, *, visible: Any) -> dict[str, Any]:
    """``memory_status``: counts of the visible memories and the log head."""

    documents: list[MemoryDocument] = [
        document for document in repository.list() if visible(document)
    ]
    layers = {layer.value: 0 for layer in Layer}
    tiers = {tier.value: 0 for tier in Tier}
    for document in documents:
        layers[document.layer.value] += 1
        tiers[document.tier.value] += 1
    events = repository.events()
    return {
        "memories": len(documents),
        "layers": layers,
        "tiers": tiers,
        "index_exists": repository.index_path.exists(),
        "log": {
            "event_count": len(events),
            "head": events[-1].event_hash if events else "0" * 64,
        },
    }


__all__ = ["REMEMBER_MAX_BYTES", "related_memories", "stage_remember", "vault_status"]
