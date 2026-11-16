"""JSON-RPC handling for the MCP server: requests, errors and client-safe messages."""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from memfw.mcp.base import GatewayBase
from memfw.mcp.schema import INSTRUCTIONS, PROTOCOL, RESOURCES, SERVER_INFO, TOOLS
from memfw.mcp.tools_context import ResourceQueryError

_CONCEPT_ERROR = "alias expansion skipped: a concept file is invalid"


def _client_message(message: str, root: Path) -> str:
    """A message safe to return to an MCP client.

    Errors raised below the gateway (the ontology loader, for one) can name
    absolute vault paths. Those never reach the client: the detail goes to
    stderr and the client gets the message with vault paths made relative,
    or a fixed message when an absolute path would remain.
    """

    text = message
    for base in {str(root.expanduser()), str(root.expanduser().resolve())}:
        if base and base != "/":
            text = text.replace(base.rstrip("/") + "/", "").replace(base, ".")
    if text != message or _ABSOLUTE_PATH.search(text):
        sys.stderr.write(f"memfw MCP: {message}\n")
        sys.stderr.flush()
    if _ABSOLUTE_PATH.search(text):
        return _CONCEPT_ERROR if "concept" in text else "internal error (details in server log)"
    return text


_ABSOLUTE_PATH = re.compile(r"(?:^|[\s'\"(=:])(?:/|~/|[A-Za-z]:\\)[^\s'\"]+")


def _flag(arguments: dict[str, Any], key: str, default: bool) -> bool:
    value = arguments.get(key, default)
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be a boolean")
    return value


def _rpc_error(msg_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


_ENDPOINT_KEYS = frozenset(
    {
        "endpoint",
        "base_url",
        "embedding_endpoint",
        "embedding_url",
        "rerank_endpoint",
        "rerank_url",
        "judge_endpoint",
        "judge_url",
        "rewrite_endpoint",
        "rewrite_url",
        "model_endpoint",
        "openai_base_url",
    }
)


def client_endpoint_fields(arguments: dict[str, Any]) -> list[str]:
    """Return request keys that try to name a model endpoint."""

    found: list[str] = []
    for key in arguments:
        normalized = str(key).strip().lower().replace("-", "_")
        if (
            normalized in _ENDPOINT_KEYS
            or normalized.endswith("_endpoint")
            or normalized.endswith("_url")
        ):
            found.append(str(key))
    return found


def _capabilities() -> dict[str, Any]:
    """``initialize`` capabilities: ``resources`` only when there are resources."""

    capabilities: dict[str, Any] = {"tools": {"listChanged": False}}
    if RESOURCES:
        capabilities["resources"] = {"listChanged": False}
    return capabilities


class ProtocolMixin(GatewayBase):
    """``initialize``, ``tools/*`` and ``resources/*`` over the gateway's handlers."""

    def handlers(self) -> dict[str, Callable[[dict[str, Any]], Any]]:
        """The tool table; ``memfw.mcp.server.Gateway`` defines it."""
        raise NotImplementedError

    def resources(self) -> list[dict[str, Any]]:
        """``resources/list``; ``memfw.mcp.tools_context.ContextTools`` defines it."""
        raise NotImplementedError

    def read_resource(self, uri: str) -> dict[str, Any] | None:
        """``resources/read``; ``None`` for an unknown URI (``ContextTools``)."""
        raise NotImplementedError

    def respond(self, payload: dict[str, Any]) -> None:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    def handle(self, message: Any) -> None:
        if isinstance(message, list):
            if not message:
                self.respond(_rpc_error(None, -32600, "invalid request"))
                return
            for item in message:
                if isinstance(item, dict):
                    self.handle(item)
                else:
                    self.respond(_rpc_error(None, -32600, "invalid request"))
            return
        if not isinstance(message, dict):
            self.respond(_rpc_error(None, -32600, "invalid request"))
            return
        method = message.get("method")
        msg_id = message.get("id")
        params = message.get("params", None)
        if params is None:
            params = {}
        if not isinstance(params, dict):
            if msg_id is not None:
                self.respond(_rpc_error(msg_id, -32602, "invalid params"))
            return
        if method == "initialize":
            self.respond(
                {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "protocolVersion": PROTOCOL,
                        "capabilities": _capabilities(),
                        "serverInfo": SERVER_INFO,
                        "instructions": INSTRUCTIONS,
                    },
                }
            )
            return
        if method in ("notifications/initialized", "initialized"):
            return
        if method == "ping":
            self.respond({"jsonrpc": "2.0", "id": msg_id, "result": {}})
            return
        if method == "tools/list":
            self.respond({"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOLS}})
            return
        if method == "resources/list" and RESOURCES:
            self.respond(
                {"jsonrpc": "2.0", "id": msg_id, "result": {"resources": self.resources()}}
            )
            return
        if method == "resources/read" and RESOURCES:
            uri = params.get("uri")
            if not isinstance(uri, str) or not uri:
                self.respond(_rpc_error(msg_id, -32602, "invalid params"))
                return
            try:
                result = self.read_resource(uri)
            except ResourceQueryError:
                self.respond(_rpc_error(msg_id, -32602, "invalid params"))
                return
            except Exception as exc:  # pragma: no cover - protocol boundary
                text = _client_message(str(exc), self.root)[:500]
                self.respond(_rpc_error(msg_id, -32603, text))
                return
            if result is None:
                self.respond(_rpc_error(msg_id, -32002, "resource not found"))
                return
            self.respond({"jsonrpc": "2.0", "id": msg_id, "result": result})
            return
        if method == "tools/call":
            name = str(params.get("name") or "")
            handler = self.handlers().get(name)
            if handler is None:
                self.respond(_rpc_error(msg_id, -32601, f"unknown tool: {name}"))
                return
            if "arguments" not in params or params["arguments"] is None:
                arguments: Any = {}
            else:
                arguments = params["arguments"]
            if not isinstance(arguments, dict):
                self.respond(_rpc_error(msg_id, -32602, "invalid params"))
                return
            if client_endpoint_fields(arguments):
                self.respond(_rpc_error(msg_id, -32602, "endpoint fields are not accepted"))
                return
            try:
                result = handler(arguments)
                self.respond(
                    {
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "result": {
                            "content": [
                                {
                                    "type": "text",
                                    "text": json.dumps(result, ensure_ascii=False),
                                }
                            ],
                            "isError": False,
                        },
                    }
                )
            except Exception as exc:  # pragma: no cover - protocol boundary
                text = _client_message(str(exc), self.root)[:500]
                self.respond(
                    {
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "result": {
                            "content": [{"type": "text", "text": text}],
                            "isError": True,
                        },
                    }
                )
            return
        if msg_id is not None:
            self.respond(_rpc_error(msg_id, -32601, f"unknown method: {method}"))
