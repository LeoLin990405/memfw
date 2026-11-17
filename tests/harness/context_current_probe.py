"""Probe for ``byte_identical``: ``memory_context`` and resources with ``current`` memories (U6).

The vault has memories tagged ``current`` but no ``runtime.context`` section,
so the ``current`` slot is off: every reply must be what the base tree gives
(only ``generated_at`` is masked). Uses only APIs present before U6
(``Repository`` with a ``clock``, ``add``, the MCP ``Gateway`` JSON-RPC entry).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

T0 = datetime(2026, 11, 1, tzinfo=UTC)


def _messages() -> list[dict[str, Any]]:
    def tool(arguments: dict[str, Any]) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": "ctx",
            "method": "tools/call",
            "params": {"name": "memory_context", "arguments": arguments},
        }

    def rpc(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        message: dict[str, Any] = {"jsonrpc": "2.0", "id": method, "method": method}
        if params is not None:
            message["params"] = params
        return message

    messages = [
        rpc("initialize", {}),
        rpc("tools/list"),
        rpc("resources/list"),
        tool({}),
        tool({"limit_tokens": 10}),
        tool({"v": 2}),
        tool({"v": 2, "policy": "strict"}),
        tool({"v": 2, "slots": ["core"]}),
        tool({"v": 2, "slots": ["core", "handoff", "topics"]}),
        tool({"v": 2, "slots": ["current"]}),
        tool({"v": 2, "slots": ["core", "current"]}),
        tool({"v": 2, "slots": ["nope"]}),
    ]
    for slot in ("core", "handoff", "topics", "current"):
        messages.append(rpc("resources/read", {"uri": f"memfw://context/{slot}"}))
    messages.append(rpc("resources/read", {"uri": "memfw://context/current?policy=strict"}))
    return messages


def _mask(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: "" if key == "generated_at" else _mask(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_mask(item) for item in value]
    return value


def default_context(payload: dict[str, Any], workdir: Path) -> str:
    """Every reply as JSON lines; ``payload`` ``clearance`` / ``tenant`` pick the caller."""

    from memfw.core.models import Layer
    from memfw.core.repository import Repository
    from memfw.mcp.server import Gateway

    repository = Repository(workdir / "vault", clock=lambda: T0)
    repository.init()
    repository.add("Anchor", "Always cite sources.", memory_id="a-1", layer=Layer.ANCHOR)
    repository.add("Proc", "1. Search 2. Read", memory_id="p-1", memory_type="procedural")
    repository.add("Ticket", "Working on ticket ABC-1", memory_id="c-1", tags=["current"])
    repository.add(
        "Meeting", "Design review at 3pm", memory_id="c-2", tags=["current", "privacy:shared"]
    )
    kwargs: dict[str, Any] = {"app": "claude"}
    if "tenant" in payload:
        kwargs["tenant"] = payload["tenant"]
    if "clearance" in payload:
        kwargs["clearance"] = payload["clearance"]
    gateway = Gateway(repository.root, **kwargs)
    sent: list[Any] = []
    gateway.respond = sent.append  # type: ignore[method-assign]
    for message in _messages():
        gateway.handle(message)
    for item in sent:
        for block in item.get("result", {}).get("content", []) or item.get("result", {}).get(
            "contents", []
        ):
            try:
                block["text"] = _mask(json.loads(block["text"]))
            except (TypeError, ValueError):
                continue
    text = "\n".join(json.dumps(item, ensure_ascii=False, sort_keys=True) for item in sent)
    return text.replace(str(workdir), "<workdir>")
