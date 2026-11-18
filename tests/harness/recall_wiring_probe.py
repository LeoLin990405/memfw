"""Probe for the v0.4 wiring PR: default outputs with no ``runtime.recall`` wiring keys.

An imported vault (``fleet_path`` tags), with no meta-index (``"meta":
"none"``) or one in the vault's default place (``"vault"``) that retires
some memories and flags another (``lifecycle_override: true``, which ``main``
reads differently, so that row is left out here), plus an open question.
Runs every ``memory_search`` mode, ``memory_context`` v2 (``compat_fts`` and
``strict``), the open-question queue and records the ``memory_access`` events
the calls appended. No clearance: the outputs and events must be identical
across trees. Uses only APIs present before the wiring PR.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tests.harness.probes import T0, VAULT, _zero_timings

SEARCHES: tuple[dict[str, Any], ...] = (
    {"query": "backup", "mode": "fts"},
    {"query": "backup", "mode": "hybrid"},
    {"query": "backup", "mode": "vector"},
    {"query": "backup", "mode": "multihop"},
    {"query": "backup", "mode": "two_stage"},
    {"query": "backup", "mode": "two_stage", "policy": "strict"},
    {"query": "backup", "mode": "two_stage", "policy": "strict", "labels": True},
    {"query": "backup", "mode": "fts", "labels": True},
)
CONTEXTS: tuple[dict[str, Any], ...] = (
    {"v": 2, "limit_tokens": 4000},
    {"v": 2, "policy": "strict", "limit_tokens": 4000},
)


def default_outputs(payload: dict[str, Any], workdir: Path) -> str:
    from memfw.core.epistemic import OPEN_QUESTION_TYPE
    from memfw.core.models import EventKind
    from memfw.core.repository import Repository
    from memfw.index import MemoryIndex
    from memfw.mcp.server import Gateway
    from memfw.runtime import build_runtime

    counter = iter(range(1, 1_000_000))
    repository = Repository(
        workdir / "vault", clock=lambda: T0, id_factory=lambda: f"id-{next(counter):06d}"
    )
    repository.init()
    items: list[dict[str, Any]] = []
    for memory_id, title, body, source, tags in VAULT:
        rel = f"{memory_id}.md"
        repository.add(
            title,
            body,
            memory_id=memory_id,
            source=source,
            about=["concept:backup"],
            tags=[*tags, f"fleet_path:{rel}"],
        )
        items.append({"path": rel, "lifecycle": "active"})
    items[0]["lifecycle"] = "retired"
    items[2]["valid_until"] = "2020-01-01T00:00:00Z"
    repository.add(
        "Backup target unknown",
        "Which backup target holds?",
        memory_id="q-backup",
        memory_type=OPEN_QUESTION_TYPE,
        about=["concept:backup"],
        open_question={"question": "Which backup target holds?", "conflicts": ["m-backup"]},
    )
    repository.add("Native backup", "native backup notes", memory_id="m-native")
    if payload.get("meta") == "vault":
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
    calls = [("memory_search", arguments) for arguments in SEARCHES]
    calls += [("memory_context", arguments) for arguments in CONTEXTS]
    for index, (name, arguments) in enumerate(calls, start=1):
        gateway.handle(
            {
                "jsonrpc": "2.0",
                "id": index,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }
        )
    for item in sent:
        for block in item.get("result", {}).get("content", []):
            try:
                text = _zero_timings(json.loads(block["text"]))
            except (TypeError, ValueError):
                continue
            if isinstance(text, dict) and "generated_at" in text:
                text["generated_at"] = "<now>"  # the call's clock, not the vault's
            block["text"] = text
    queue = build_runtime(root).open_question_queue(
        Repository(root), app="operator", allow_operator=True, now=T0
    )
    events = [
        (event.memory_id, str(event.details.get("operation") or ""))
        for event in Repository(root).events()
        if event.kind == EventKind.MEMORY_ACCESS
    ]
    text = "\n".join(
        json.dumps(item, ensure_ascii=False, sort_keys=True, indent=1)
        for item in [*sent, queue, events]
    )
    return text.replace(str(root), "<vault>").replace(str(workdir), "<workdir>")
