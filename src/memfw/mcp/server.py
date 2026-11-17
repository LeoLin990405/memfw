"""Minimal stdio MCP server for a local memfw vault.

Tools:
    memory_search(query, limit)      -> FTS5-ranked memory ids and titles
    memory_get(memory_id)            -> one Markdown memory
    memory_remember(content, title)  -> stage a candidate under _candidates/remember/
    memory_status()                  -> vault and log summary
    history_list(source, session)    -> transcript sessions with lineage (read-only)
    history_search(query, limit)     -> redacted transcript snippets with refs
    history_read(ref)                -> redacted, size-capped window at a stored ref
    conversations_search(query, app) -> redacted turns from the shared conversation store
    conversations_list(app, since)   -> shared sessions, newest first
    conversations_get(session_id)    -> redacted, paginated range of one session's turns
    conversations_handoff(session_id, for_agent) -> continuation pack for another agent
    dream_run(dry_run, focus, ...)   -> verify/curate/distill proposals (dry run by default)
    dream_list(limit)                -> staged dreams under _candidates/dream/ (read-only)
    memory_used(ids, session_ref)    -> usage signal (only when enabled; off by default)

memory_context(v=2, slots) also serves each slot as an MCP resource,
memfw://context/<slot> (resources/list, resources/read).

The conversations_* tools are read-only and obey per-app visibility in vault.yaml
(``conversation_share``); the calling app names itself with --app or MEMFW_APP.

The server is deliberately local-only: it has no remote host or external search backend.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

# Kept as a module attribute: tests patch ``memfw.mcp.server.yaml.safe_load``
# to prove the search path never parses YAML.
import yaml  # noqa: F401

from memfw.mcp.protocol import ProtocolMixin, _flag, _rpc_error, client_endpoint_fields
from memfw.mcp.schema import INSTRUCTIONS, PROTOCOL, SEARCH_POLICIES, SERVER_INFO, TOOLS
from memfw.mcp.tools_attachments import ATTACHMENT_TOOLS, AttachmentTools
from memfw.mcp.tools_context import ContextTools
from memfw.mcp.tools_conversations import ConversationTools, conversation_search_options
from memfw.mcp.tools_dream import (
    DREAM_LIST_MAX,
    DREAM_MAX_CANDIDATES,
    DREAM_MAX_LIMIT,
    DREAM_MAX_SESSIONS,
    DREAM_MAX_STAGED,
    DREAM_STAGE_INTERVAL_SECONDS,
    DreamTools,
)
from memfw.mcp.tools_history import HistoryTools
from memfw.mcp.tools_memory import MemoryTools
from memfw.mcp.tools_search import (
    _FRESHNESS_READ_ATTEMPTS,
    _RAW_HASH_CACHE,
    SearchTools,
    _frontmatter_version,
    _policy_argument,
    _RawHashCache,
)
from memfw.mcp.tools_usage import UsageTools
from memfw.ports.privacy import clearance_is_valid

# Names that lived here before the A4a split stay importable from this module.
# They are read-only re-exports: the code reads each name from its defining module
# (``memfw.mcp.schema``, ``protocol``, ``tools_*``), so a test patches that module.
__all__ = [
    "DREAM_LIST_MAX",
    "DREAM_MAX_CANDIDATES",
    "DREAM_MAX_LIMIT",
    "DREAM_MAX_SESSIONS",
    "DREAM_MAX_STAGED",
    "DREAM_STAGE_INTERVAL_SECONDS",
    "INSTRUCTIONS",
    "PROTOCOL",
    "SEARCH_POLICIES",
    "SERVER_INFO",
    "TOOLS",
    "_FRESHNESS_READ_ATTEMPTS",
    "_RAW_HASH_CACHE",
    "Gateway",
    "_RawHashCache",
    "_frontmatter_version",
    "client_endpoint_fields",
    "main",
    "serve",
]


class Gateway(
    SearchTools,
    MemoryTools,
    ContextTools,
    HistoryTools,
    ConversationTools,
    DreamTools,
    UsageTools,
    AttachmentTools,
    ProtocolMixin,
):
    """MCP tool implementations bound to one local vault root."""

    def tools(self) -> list[dict[str, Any]]:
        """``tools/list``: the mixins' list, plus the attachment tools when enabled."""

        listed: list[dict[str, Any]] = super().tools()
        if self.attachment_tools_enabled():
            listed = [*listed, *ATTACHMENT_TOOLS]
        return listed

    def handlers(self) -> dict[str, Callable[[dict[str, Any]], Any]]:
        table = self._handlers()
        if self.usage_enabled():
            table["memory_used"] = lambda args: self.memory_used(
                args.get("ids"), args.get("session_ref")
            )
        if self.attachment_tools_enabled():
            table.update(self.attachment_handlers())
        return table

    def _handlers(self) -> dict[str, Callable[[dict[str, Any]], Any]]:
        return {
            "conversations_search": lambda args: self._conversations().search(
                str(args.get("query") or ""),
                app=str(args["app"]) if args.get("app") else None,
                agent=str(args["agent"]) if args.get("agent") else None,
                since=str(args["since"]) if args.get("since") else None,
                limit=int(args.get("limit") or 10),
                offset=int(args.get("offset") or 0),
                **conversation_search_options(args),
            ),
            "conversations_list": lambda args: self._conversations().list_sessions(
                app=str(args["app"]) if args.get("app") else None,
                agent=str(args["agent"]) if args.get("agent") else None,
                since=str(args["since"]) if args.get("since") else None,
                limit=int(args.get("limit") or 20),
                offset=int(args.get("offset") or 0),
            ),
            "conversations_get": lambda args: self._conversations().get(
                str(args.get("session_id") or ""),
                app=str(args["app"]) if args.get("app") else None,
                start=int(args.get("start") or 0),
                end=int(args["end"]) if args.get("end") is not None else None,
                max_bytes=int(args.get("max_bytes") or 16384),
            ),
            "conversations_handoff": lambda args: self._conversations().handoff(
                str(args.get("session_id") or ""),
                app=str(args["app"]) if args.get("app") else None,
                for_agent=str(args["for_agent"]) if args.get("for_agent") else None,
                max_bytes=int(args.get("max_bytes") or 16384),
            ),
            "memory_search": lambda args: self.search(
                str(args.get("query") or ""),
                int(args.get("limit") or 10),
                mode=str(args.get("mode") or "hybrid"),
                source=str(args.get("source") or ""),
                include_history=_flag(args, "include_history", False),
                as_of=(str(args["as_of"]) if args.get("as_of") else None),
                rounds=(int(args["rounds"]) if args.get("rounds") is not None else None),
                scope=args.get("scope"),
                policy=_policy_argument(args),
                labels=_flag(args, "labels", False),
            ),
            "memory_get": lambda args: self.get(str(args.get("memory_id") or "")),
            "memory_remember": lambda args: self.remember(
                str(args.get("content") or ""),
                str(args.get("title") or "memory"),
                tags=[str(item) for item in (args.get("tags") or [])],
                related=[str(item) for item in (args.get("related") or [])],
                privacy=args.get("privacy"),
            ),
            "memory_related": lambda args: self.related(
                str(args.get("memory_id") or ""), int(args.get("limit") or 10)
            ),
            "memory_context": lambda args: self.context(
                int(args.get("limit_tokens") or 2000),
                v=args.get("v"),
                policy=args.get("policy"),
                slots=args.get("slots"),
                if_version=args.get("if_version"),
            ),
            "memory_status": lambda _args: self.status(),
            "dream_run": lambda args: self.dream_run(
                dry_run=_flag(args, "dry_run", True),
                focus=str(args.get("focus") or ""),
                all_memories=_flag(args, "all", False),
                limit=int(args["limit"]) if args.get("limit") is not None else 200,
                max_sessions=(
                    int(args["max_sessions"]) if args.get("max_sessions") is not None else 20
                ),
                source=str(args.get("source") or ""),
                since=str(args.get("since") or ""),
                until=str(args.get("until") or ""),
                stale_days=int(args["stale_days"]) if args.get("stale_days") is not None else 180,
                min_events=int(args["min_events"]) if args.get("min_events") is not None else 2,
            ),
            "dream_list": lambda args: self.dream_list(int(args.get("limit") or 20)),
            "history_list": lambda args: self.history_list(
                source=str(args.get("source") or ""),
                session=str(args.get("session") or ""),
                since=str(args.get("since") or ""),
                until=str(args.get("until") or ""),
                limit=int(args.get("limit") or 50),
            ),
            "history_search": lambda args: self.history_search(
                str(args.get("query") or ""),
                source=str(args.get("source") or ""),
                session=str(args.get("session") or ""),
                since=str(args.get("since") or ""),
                until=str(args.get("until") or ""),
                limit=int(args.get("limit") or 10),
            ),
            "history_read": lambda args: self.history_read(
                str(args.get("ref") or ""),
                before=int(args["before"]) if args.get("before") is not None else 2,
                after=int(args["after"]) if args.get("after") is not None else 2,
                max_bytes=int(args.get("max_bytes") or 8192),
            ),
        }


def serve(root: Path | str, app: str | None = None) -> int:
    gateway = Gateway(root, app=app)
    sys.stderr.write("memfw MCP server ready\n")
    sys.stderr.flush()
    for line in sys.stdin:
        text = line.strip()
        if not text:
            continue
        try:
            message = json.loads(text)
        except json.JSONDecodeError:
            gateway.respond(_rpc_error(None, -32700, "parse error"))
            continue
        try:
            gateway.handle(message)
        except Exception:
            gateway.respond(_rpc_error(None, -32603, "internal error"))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="memfw-mcp", description="Local memfw MCP server")
    parser.add_argument(
        "--root",
        default=None,
        help="memfw vault root (or set MEMFW_ROOT). Required; cwd is not a default.",
    )
    parser.add_argument(
        "--app",
        default=None,
        help="name of the app running this server, e.g. claude (or set MEMFW_APP); "
        "conversation sharing rules in vault.yaml apply to it",
    )
    args = parser.parse_args(argv)
    root = args.root if args.root is not None else os.environ.get("MEMFW_ROOT")
    if root is None or not str(root).strip():
        parser.error("memfw vault root is required: pass --root or set MEMFW_ROOT")
    if not clearance_is_valid(os.environ.get("MEMFW_MCP_CLEARANCE")):
        parser.error("MEMFW_MCP_CLEARANCE must list only public, shared, local-private")
    return serve(Path(str(root)), app=args.app)


if __name__ == "__main__":
    raise SystemExit(main())
