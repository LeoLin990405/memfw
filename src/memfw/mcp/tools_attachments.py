"""``attachment_search`` / ``attachment_get`` (MM2b): listed only with ``mcp.attachments: true``.

With the switch off (the default) ``tools/list`` and every handler are exactly
what they were, so no client sees a new tool. The Runtime does the work
(``memfw.runtime.attachments``); originals additionally need
``mcp.local_originals: true``.
"""

from __future__ import annotations

from typing import Any

from memfw.mcp.base import GatewayBase
from memfw.runtime import attachments as runtime_attachments

ATTACHMENT_TOOLS: list[dict[str, Any]] = [
    {
        "name": "attachment_search",
        "description": (
            "Search the text proxies of attachments (OCR, transcripts, extracts). Returns "
            "segments with their attachment id, proxy kind, coverage (partial proxies say "
            "so), canonical locator (PDF pt from the bottom-left, image px from the top-left, "
            "seconds) and text. Never returns the original. Results are filtered by tenant "
            "and privacy clearance."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
            },
            "required": ["query"],
        },
    },
    {
        "name": "attachment_get",
        "description": (
            "One attachment's metadata and the segments of its active proxies, optionally "
            "only those within a locator (e.g. page=3, t=10,20). original=path|bytes "
            "returns the original only when the vault sets mcp.local_originals: true, after "
            "re-checking its sha256; otherwise it is refused."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "locator": {"type": "string"},
                "original": {"type": "string", "enum": ["path", "bytes"]},
                "offset": {"type": "integer", "minimum": 0},
                "length": {"type": "integer", "minimum": 0},
            },
            "required": ["id"],
        },
    },
]


class AttachmentTools(GatewayBase):
    """The two attachment tools and the switch that lists them."""

    def attachment_tools_enabled(self) -> bool:
        return runtime_attachments.tools_enabled(self.runtime)

    def attachment_search(self, query: str, limit: int = 10) -> dict[str, Any]:
        return runtime_attachments.attachment_search(self.runtime, query, limit=limit)

    def attachment_get(
        self,
        attachment_id: str,
        *,
        locator: str | None = None,
        original: str | None = None,
        offset: int = 0,
        length: int | None = None,
    ) -> dict[str, Any]:
        return runtime_attachments.attachment_get(
            self.runtime,
            attachment_id,
            locator=locator,
            original=original,
            offset=offset,
            length=length,
        )

    def attachment_handlers(self) -> dict[str, Any]:
        return {
            "attachment_search": lambda args: self.attachment_search(
                str(args.get("query") or ""), int(args.get("limit") or 10)
            ),
            "attachment_get": lambda args: self.attachment_get(
                str(args.get("id") or ""),
                locator=str(args["locator"]) if args.get("locator") else None,
                original=str(args["original"]) if args.get("original") else None,
                offset=int(args.get("offset") or 0),
                length=int(args["length"]) if args.get("length") is not None else None,
            ),
        }


__all__ = ["ATTACHMENT_TOOLS", "AttachmentTools"]
