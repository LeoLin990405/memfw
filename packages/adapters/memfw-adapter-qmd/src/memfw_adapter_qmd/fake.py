"""A loopback-only fake of QMD's MCP HTTP endpoint, for tests. Stdlib only.

It speaks the subset this adapter uses, in the shape QMD 2.5.3 answers
(``dist/mcp/server.js``: Streamable HTTP with JSON responses, one session per
``initialize``, ``404`` for an unknown session, ``400`` without one):
``initialize``, ``notifications/initialized`` and ``tools/call`` ``query``.
Answers come from a recorded fixture (:meth:`FakeQmd.from_recording`) or from
a ``results`` callable. It binds ``127.0.0.1`` only and records every request
it receives (headers that matter, parsed body) in :attr:`FakeQmd.requests`.

Knobs for failure tests: ``delay`` (seconds before each ``tools/call``
answer), ``tool_error`` (answer ``isError: true``), ``status`` (HTTP status
for ``tools/call``), ``event_stream`` (answer as ``text/event-stream``),
``restart()`` (forget every session, like QMD reloading an index generation).
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

__all__ = ["FakeQmd"]

Results = Callable[[Mapping[str, Any]], list[dict[str, Any]]]

_DEFAULT_INIT: dict[str, Any] = {
    "protocolVersion": "2025-06-18",
    "capabilities": {"tools": {"listChanged": True}, "resources": {"listChanged": True}},
    "serverInfo": {"name": "qmd", "version": "2.5.3"},
    "instructions": "QMD is your local search engine over 0 markdown documents.",
}


class FakeQmd:
    """``with FakeQmd(results=...) as fake: fake.url`` → ``http://127.0.0.1:<port>/mcp``."""

    def __init__(
        self,
        *,
        results: Results | None = None,
        initialize: Mapping[str, Any] | None = None,
    ) -> None:
        self.results: Results = results or (lambda arguments: [])
        self.initialize = dict(initialize or _DEFAULT_INIT)
        self.requests: list[dict[str, Any]] = []
        self.sessions: set[str] = set()
        self.delay = 0.0
        self.tool_error = False
        self.status = 200
        self.event_stream = False
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @classmethod
    def from_recording(cls, recording: Mapping[str, Any]) -> FakeQmd:
        """Replay a contract fixture: each exchange answers its recorded arguments."""

        answers: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
        for exchange in recording.get("exchanges", []):
            arguments = exchange["request"]["params"]["arguments"]
            rows = exchange["response"]["body"]["result"]["structuredContent"]["results"]
            answers.append((arguments, rows))

        def results(arguments: Mapping[str, Any]) -> list[dict[str, Any]]:
            for recorded, rows in answers:
                if recorded == dict(arguments):
                    return [dict(row) for row in rows]
            return []

        init = recording.get("initialize", {}).get("response", {}).get("body", {}).get("result")
        return cls(results=results, initialize=init)

    # -- lifecycle -------------------------------------------------------

    @property
    def url(self) -> str:
        assert self._server is not None, "start the fake first"
        return f"http://127.0.0.1:{self._server.server_address[1]}/mcp"

    def __enter__(self) -> FakeQmd:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                del format, args

            def do_GET(self) -> None:  # noqa: N802
                if self.path == "/health":
                    self._send(200, {"status": "ok", "uptime": 1})
                else:
                    self._send(404, None, raw=b"Not Found")

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"null")
                session = self.headers.get("Mcp-Session-Id")
                with fake._lock:
                    fake.requests.append(
                        {
                            "path": self.path,
                            "session": session,
                            "accept": self.headers.get("Accept"),
                            "protocol": self.headers.get("MCP-Protocol-Version"),
                            "body": body,
                        }
                    )
                if self.path != "/mcp":
                    self._send(404, None, raw=b"Not Found")
                    return
                method = body.get("method") if isinstance(body, dict) else None
                request_id = body.get("id") if isinstance(body, dict) else None
                if session:
                    with fake._lock:
                        known = session in fake.sessions
                    if not known:
                        self._send(
                            404,
                            {
                                "jsonrpc": "2.0",
                                "error": {"code": -32001, "message": "Session not found"},
                                "id": request_id,
                            },
                        )
                        return
                elif method != "initialize":
                    self._send(
                        400,
                        {
                            "jsonrpc": "2.0",
                            "error": {"code": -32000, "message": "Bad Request: Missing session ID"},
                            "id": request_id,
                        },
                    )
                    return
                if method == "initialize":
                    new = str(uuid.uuid4())
                    with fake._lock:
                        fake.sessions.add(new)
                    payload = {"jsonrpc": "2.0", "id": request_id, "result": fake.initialize}
                    self._send(200, payload, session=new)
                elif method == "notifications/initialized":
                    self._send(202, None, raw=b"")
                elif method == "tools/call":
                    self._tool(body, request_id)
                else:
                    self._send(
                        200,
                        {
                            "jsonrpc": "2.0",
                            "id": request_id,
                            "error": {"code": -32601, "message": "Method not found"},
                        },
                    )

            def _tool(self, body: dict[str, Any], request_id: Any) -> None:
                if fake.delay:
                    time.sleep(fake.delay)
                if fake.status != 200:
                    self._send(fake.status, None, raw=b"error")
                    return
                params = body.get("params") or {}
                if params.get("name") != "query":
                    self._send(200, {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32602}})
                    return
                rows = fake.results(params.get("arguments") or {})
                text = f"Found {len(rows)} results"
                result: dict[str, Any] = {
                    "content": [{"type": "text", "text": text}],
                    "structuredContent": {"results": rows},
                }
                if fake.tool_error:
                    result = {"content": [{"type": "text", "text": "boom"}], "isError": True}
                self._send(200, {"jsonrpc": "2.0", "id": request_id, "result": result})

            def _send(
                self,
                status: int,
                payload: Any,
                *,
                raw: bytes | None = None,
                session: str | None = None,
            ) -> None:
                if raw is not None:
                    data, kind = raw, "text/plain"
                elif fake.event_stream and status == 200:
                    data = ("event: message\ndata: " + json.dumps(payload) + "\n\n").encode()
                    kind = "text/event-stream"
                else:
                    data, kind = json.dumps(payload).encode(), "application/json"
                self.send_response(status)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(data)))
                if session:
                    self.send_header("mcp-session-id", session)
                self.end_headers()
                self.wfile.write(data)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def restart(self) -> None:
        """Forget every session (QMD restarts for each new index generation)."""

        with self._lock:
            self.sessions.clear()

    def tool_calls(self) -> list[dict[str, Any]]:
        """The ``tools/call`` request bodies received so far."""

        with self._lock:
            return [
                r["body"]
                for r in self.requests
                if isinstance(r["body"], dict) and r["body"].get("method") == "tools/call"
            ]
