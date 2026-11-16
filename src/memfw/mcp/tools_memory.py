"""``memory_get``, ``memory_remember``, ``memory_related`` and ``memory_status``."""

from __future__ import annotations

import re
import time
import uuid
from typing import Any

from memfw.capture.staging import (
    CANDIDATE_DIR,
    MAX_CANDIDATE_BYTES,
    candidate_document,
    open_candidate_directory,
    publish_candidate,
)
from memfw.capture.suggestions import suggest_related
from memfw.mcp.base import GatewayBase, Layer, NotFoundError, Repository, Tier
from memfw.related import RelatedIndex

_MAX_CONTENT_BYTES = MAX_CANDIDATE_BYTES
_CANDIDATE_DIR = CANDIDATE_DIR


def _slug(text: str) -> str:
    value = re.sub(r"[^a-z0-9_-]+", "-", text.lower()).strip("-")
    return value[:48] or "memory"


# The staging writer is shared with every Ingestor (``memfw okf import``).
_candidate_document = candidate_document
_open_candidate_directory = open_candidate_directory
_publish_candidate = publish_candidate


class MemoryTools(GatewayBase):
    """Single-memory tools and the vault summary."""

    def get(self, memory_id: str) -> dict[str, Any]:
        document = self._repo().get(memory_id)
        if not self._visible(document):
            raise ValueError("memory is not visible in this tenant")
        return {
            "id": document.id,
            "title": document.title,
            "body": document.body,
            "layer": document.layer.value,
            "tier": document.tier.value,
            "type": document.type,
            "source": document.source,
            "provenance": document.provenance,
            "tags": list(document.tags),
            # So a caller can see that a causal claim is only a hypothesis.
            "epistemic": document.epistemic,
            "causal": document.causal,
            "causal_record": (
                document.causal_record.model_dump(mode="json")
                if document.causal_record is not None
                else None
            ),
            "limitations": list(document.limitations),
            "experience": (
                document.experience.model_dump(mode="json")
                if document.experience is not None
                else None
            ),
        }

    def remember(
        self,
        content: str,
        title: str = "memory",
        *,
        tags: list[str] | None = None,
        related: list[str] | None = None,
    ) -> dict[str, Any]:
        if len(content.encode("utf-8")) > _MAX_CONTENT_BYTES:
            raise ValueError("content exceeds 64 KiB")
        text = content.strip()
        if not text:
            raise ValueError("content required")
        repository = self._repo()
        suggestions = suggest_related(repository, title, text, embedder=self.embedder)
        suggestion_rows = [item.as_dict() for item in suggestions]
        related_ids = list(related) if related else [item.memory_id for item in suggestions]
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        published: str | None = None
        for _ in range(5):
            proposal_id = f"mcp-{uuid.uuid4().hex}"
            filename = f"{stamp}-{_slug(title)}-{proposal_id}.md"
            document = _candidate_document(
                title=title,
                text=text,
                proposal_id=proposal_id,
                tenant=self.tenant,
                tags=list(tags or []),
                related=list(related_ids),
                suggestions=suggestion_rows,
            )
            try:
                _publish_candidate(self.root, filename, document)
            except FileExistsError:
                continue
            published = filename
            break
        if published is None:
            raise RuntimeError("could not publish candidate")
        return {
            "staged_candidate": (_CANDIDATE_DIR / published).as_posix(),
            "authority": False,
            "suggestions": suggestion_rows,
        }

    def _same_tenant_neighbour(self, repository: Repository, row: dict[str, Any]) -> bool:
        if str(row.get("source") or "") != "memory":
            return False
        try:
            neighbour = repository.get(str(row.get("id") or ""))
        except NotFoundError:
            return False
        return self._visible(neighbour)

    def related(self, memory_id: str, limit: int = 10) -> dict[str, Any]:
        repository = self._repo()
        document = repository.get(memory_id)
        if not self._visible(document):
            raise ValueError("memory is not visible in this tenant")
        bounded = max(1, min(limit, 50))
        rows = RelatedIndex(self.root / ".memfw-related.sqlite").related(
            "memory",
            memory_id,
            limit=50,
            tenant=self.tenant,
        )
        visible = [row for row in rows if self._same_tenant_neighbour(repository, row)]
        return {"memory_id": memory_id, "related": visible[:bounded]}

    def status(self) -> dict[str, Any]:
        repository = self._repo()
        documents = [document for document in repository.list() if self._visible(document)]
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
