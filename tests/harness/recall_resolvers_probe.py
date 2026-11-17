"""Probe for the recall-resolvers PR: ``memory_search`` on a vault with imported memories.

Each memory carries a ``fleet_path`` tag (as imported documents do). The
payload picks the meta-index: ``"meta": "none"`` (no file) or ``"plain"``
(every document listed, with only fields that are not lifecycle fields).
Output must be identical across trees for both. Uses only APIs present
before this PR (``Repository.add`` with tags, ``MemoryIndex.rebuild`` and the
MCP ``Gateway``).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tests.harness.probes import T0, VAULT, _zero_timings

SEARCHES: tuple[dict[str, Any], ...] = (
    {"query": "backup", "mode": "fts"},
    {"query": "backup", "mode": "two_stage"},
    {"query": "backup", "mode": "two_stage", "policy": "strict"},
    {"query": "backup", "mode": "two_stage", "policy": "strict", "include_history": True},
    {"query": "迁移", "mode": "two_stage", "policy": "strict"},
)


def imported_memory_search(payload: dict[str, Any], workdir: Path) -> str:
    from memfw.core.repository import Repository
    from memfw.index import MemoryIndex
    from memfw.mcp.server import Gateway

    counter = iter(range(1, 1_000_000))
    repository = Repository(
        workdir / "vault", clock=lambda: T0, id_factory=lambda: f"id-{next(counter):06d}"
    )
    repository.init()
    items: list[dict[str, Any]] = []
    for memory_id, title, body, source, tags in VAULT:
        rel = f"{memory_id}.md"
        repository.add(
            title, body, memory_id=memory_id, source=source, tags=[*tags, f"fleet_path:{rel}"]
        )
        items.append({"path": rel, "scope": "fleet", "type": "fact", "status": "confirmed"})
    repository.add("Native backup", "native backup notes", memory_id="m-native")
    if payload.get("meta") == "plain":
        status = repository.root / "status"
        status.mkdir(exist_ok=True)
        (status / "memory-meta-index.json").write_text(
            json.dumps({"generated_at": "2026-01-01T00:00:00Z", "items": items}),
            encoding="utf-8",
        )
    MemoryIndex(repository.index_path).rebuild(repository)
    root = repository.root
    gateway = Gateway(root, tenant="", entity_enabled=False, allowed_sources=())
    sent: list[dict[str, Any]] = []
    gateway.respond = sent.append  # type: ignore[method-assign]
    for index, arguments in enumerate(SEARCHES, start=1):
        gateway.handle(
            {
                "jsonrpc": "2.0",
                "id": index,
                "method": "tools/call",
                "params": {"name": "memory_search", "arguments": arguments},
            }
        )
    for item in sent:
        for block in item.get("result", {}).get("content", []):
            try:
                block["text"] = _zero_timings(json.loads(block["text"]))
            except (TypeError, ValueError):
                pass
    text = "\n".join(
        json.dumps(item, ensure_ascii=False, sort_keys=True, indent=1) for item in sent
    )
    return text.replace(str(root), "<vault>").replace(str(workdir), "<workdir>")
