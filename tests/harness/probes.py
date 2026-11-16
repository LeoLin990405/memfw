"""Probes for ``byte_identical``: each runs inside the tree under comparison.

Use only memfw APIs present in every tree you compare against (here: the
``Repository`` constructor's ``clock``/``id_factory`` callables, ``add``,
``MemoryIndex.rebuild`` and the MCP ``Gateway`` JSON-RPC entry point).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

T0 = datetime(2026, 1, 1, tzinfo=UTC)

#: Small fixed vault: (memory id, title, body, source, tags).
VAULT: tuple[tuple[str, str, str, str, tuple[str, ...]], ...] = (
    ("m-backup", "Homelab backup", "nightly homelab backup to the NAS", "manual", ()),
    ("m-cron", "Cron pause", "pause cron before the disk migration", "manual", ()),
    ("m-notes", "Backup notes", "backup notes and restore drills", "nas", ()),
    ("m-release", "Release", "release notes for memfw", "manual", ()),
    ("m-zh", "迁移计划", "迁移之前先暂停定时任务，再做备份", "manual", ()),
    ("m-priv", "Private backup", "private backup keys", "manual", ("sensitivity:private",)),
)


def _vault(root: Path) -> Path:
    from memfw.core.repository import Repository
    from memfw.index import MemoryIndex

    counter = iter(range(1, 1_000_000))
    repository = Repository(root, clock=lambda: T0, id_factory=lambda: f"id-{next(counter):06d}")
    repository.init()
    for memory_id, title, body, source, tags in VAULT:
        repository.add(title, body, memory_id=memory_id, source=source, tags=list(tags))
    MemoryIndex(repository.index_path).rebuild(repository)
    return repository.root


def mcp_memory_search(payload: dict[str, Any], workdir: Path) -> str:
    """One MCP ``tools/call memory_search`` on a fresh fixture vault.

    Returns the JSON-RPC responses, pretty-printed, with the tool's JSON text
    result decoded in place (so a diff points at the changed field).
    """

    from memfw.mcp.server import Gateway

    root = _vault(workdir / "vault")
    gateway = Gateway(root, tenant="", entity_enabled=False, allowed_sources=())
    sent: list[dict[str, Any]] = []
    gateway.respond = sent.append  # type: ignore[method-assign]
    gateway.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "memory_search", "arguments": payload},
        }
    )
    for item in sent:  # the tool result is JSON inside a string: unpack it for readable diffs
        for block in item.get("result", {}).get("content", []):
            try:
                block["text"] = json.loads(block["text"])
            except (TypeError, ValueError):
                pass
    text = "\n".join(
        json.dumps(item, ensure_ascii=False, sort_keys=True, indent=1) for item in sent
    )
    return text.replace(str(root), "<vault>").replace(str(workdir), "<workdir>")


def loaded_from(payload: dict[str, Any], workdir: Path) -> str:
    """The ``src`` directory ``memfw`` was imported from (harness self-test)."""

    import memfw

    return str(Path(memfw.__file__).resolve().parents[1])
