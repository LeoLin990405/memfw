"""A local fake of the chat-completions endpoint, for tests. Standard library only.

It listens on ``127.0.0.1`` (an ephemeral port), records every request (path,
headers, decoded JSON body) and answers from a script of ``(status, body)``
pairs; when the script runs out it answers with :func:`tag_reply`. Nothing
here touches the network beyond the loopback interface.

    with FakeDeepSeek() as fake:
        backend = DeepSeekBackend(Settings(base_url=fake.url), key="test-key")
        ...
        fake.requests[0]["body"]["messages"]
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

DEFAULT_TAGS: dict[str, Any] = {
    "project": ["Uploader"],
    "task_type": "dev",
    "components": ["Retry", "Tests"],
    "intent": "Add a retry wrapper",
    "outcome": "done",
}


def tag_reply(
    summary: str = "Added a retry wrapper. Tests pass.",
    tags: dict[str, Any] | None = None,
    *,
    content: str | None = None,
    tokens_in: int = 120,
    tokens_out: int = 40,
    finish_reason: str = "stop",
) -> dict[str, Any]:
    """A chat-completions response whose message content is a ``conv-v2`` reply."""

    if content is None:
        content = json.dumps({"summary": summary, "tags": tags or DEFAULT_TAGS})
    return {
        "id": "fake-1",
        "object": "chat.completion",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": finish_reason,
            }
        ],
        "usage": {
            "prompt_tokens": tokens_in,
            "completion_tokens": tokens_out,
            "total_tokens": tokens_in + tokens_out,
        },
    }


class FakeDeepSeek:
    def __init__(self, script: list[tuple[int, Any]] | None = None) -> None:
        self.script: list[tuple[int, Any]] = list(script or [])
        self.requests: list[dict[str, Any]] = []
        self.default: Any = tag_reply()
        self.default_status = 200
        self._lock = threading.Lock()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 (http.server API)
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length)
                try:
                    body: Any = json.loads(raw.decode("utf-8"))
                except ValueError:
                    body = None
                with fake._lock:
                    fake.requests.append(
                        {
                            "path": self.path,
                            "headers": {k.lower(): v for k, v in self.headers.items()},
                            "raw": raw.decode("utf-8", "replace"),
                            "body": body,
                        }
                    )
                    status, reply = (
                        fake.script.pop(0) if fake.script else (fake.default_status, fake.default)
                    )
                data = reply if isinstance(reply, bytes) else json.dumps(reply).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return None  # quiet: never echo requests (they carry the test key)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> FakeDeepSeek:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self._thread.join(timeout=5)

    def bodies(self) -> str:
        """Every request body as received, concatenated (for leak checks)."""

        return "\n".join(item["raw"] for item in self.requests)
