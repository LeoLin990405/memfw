"""Record a QMD contract fixture from a live loopback ``qmd mcp --http`` server.

    python -m memfw_adapter_qmd.record --endpoint http://127.0.0.1:8181/mcp \\
        --collection memfw-memory --query "cron migration" [--query ...] > qmd-x.y.z.json

Run it against an index built from synthetic data (the fixtures are public).
Text fields QMD returns (``title``, ``snippet``, ``context``, the text
``content`` summary, the server ``instructions``) are replaced with
``"<redacted>"``; ``file``, ``score``, ``line`` and ``docid`` are kept. The
output has the layout ``FakeQmd.from_recording`` replays: ``initialize`` plus
one ``exchanges[]`` entry per query, each with the exact ``tools/call``
request this adapter sends. Fill in ``expected_refs`` by hand.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

from .mcp import McpHttpClient
from .provider import DEFAULT_ENDPOINT, _Common, _query_arguments

REDACTED = "<redacted>"


def _redact(result: dict[str, Any]) -> dict[str, Any]:
    rows = []
    for row in (result.get("structuredContent") or {}).get("results") or []:
        if isinstance(row, dict):
            clean = dict(row)
            for key in ("title", "snippet", "context"):
                if key in clean:
                    clean[key] = REDACTED
            rows.append(clean)
    return {
        "content": [{"type": "text", "text": REDACTED}],
        "structuredContent": {"results": rows},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--collection", required=True)
    parser.add_argument("--query", action="append", required=True)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args(argv)
    settings = _Common(**_Common.parse({"collection": args.collection}, collection=""))
    client = McpHttpClient(args.endpoint)  # loopback only: no allow_remote here
    exchanges = []
    for query in args.query:
        arguments = _query_arguments(settings, query, args.limit)
        result = client.call_tool("query", arguments, deadline=time.monotonic() + args.timeout)
        exchanges.append(
            {
                "name": query,
                "request": {
                    "method": "tools/call",
                    "params": {"name": "query", "arguments": arguments},
                },
                "response": {"status": 200, "body": {"jsonrpc": "2.0", "result": _redact(result)}},
                "expected_refs": [],
            }
        )
    info = client.server_info
    recording = {
        "external": "qmd",
        "external_version": info.get("version", ""),
        "provenance": "recorded with memfw_adapter_qmd.record",
        "initialize": {
            "response": {
                "status": 200,
                "body": {
                    "jsonrpc": "2.0",
                    "result": {
                        "protocolVersion": client._protocol,
                        "capabilities": {},
                        "serverInfo": info,
                        "instructions": REDACTED,
                    },
                },
            }
        },
        "exchanges": exchanges,
    }
    json.dump(recording, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
