"""M3c: the two-stage semantic list on a loopback embedder never uses an env proxy.

The generic ``open_http`` checks live in ``tests/test_http_loopback_proxy.py``;
this one covers the conversation summaries path. A fake proxy and a fake
embedding service run on loopback, ``HTTP_PROXY`` points at the proxy and
``no_proxy`` is unset.
"""

from __future__ import annotations

import json
import socket
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from memfw.conversations import open_share
from memfw.notes import HttpEmbeddingProvider
from tests.conversation_fixtures import (
    isolated_home,  # noqa: F401  (fixture)
    make_vault,
    standard_rows,
    write_store,
)

DIMENSION = 4

# The suite-wide guard (conftest.py) refuses every socket; these tests talk to
# their own loopback servers only, so connections to anything else still fail.
_REAL_CONNECT = socket.socket.connect
_REAL_GETADDRINFO = socket.getaddrinfo
_REAL_CREATE_CONNECTION = socket.create_connection
_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def _loopback_only(monkeypatch: pytest.MonkeyPatch) -> None:
    def connect(self: socket.socket, address: Any) -> Any:
        assert address[0] in _LOOPBACK, f"non-loopback connect to {address[0]}"
        return _REAL_CONNECT(self, address)

    def getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        assert host in _LOOPBACK, f"non-loopback lookup of {host}"
        return _REAL_GETADDRINFO(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    # The real create_connection resolves and connects through the two guards above.
    monkeypatch.setattr(socket, "create_connection", _REAL_CREATE_CONNECTION)


class _Recorder(ThreadingHTTPServer):
    def __init__(self, handler: type[BaseHTTPRequestHandler]) -> None:
        super().__init__(("127.0.0.1", 0), handler)
        self.requests: list[tuple[str, str]] = []


class _Embeddings(BaseHTTPRequestHandler):
    """An OpenAI-compatible ``/v1/embeddings`` answer for any POST (the proxy too)."""

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
        server: Any = self.server
        server.requests.append((self.path, body))
        try:
            count = len(json.loads(body).get("input") or [])
        except ValueError:
            count = 0
        payload = json.dumps(
            {"data": [{"index": i, "embedding": [1.0] * DIMENSION} for i in range(count)]}
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args: Any) -> None:
        del args


def _serve() -> _Recorder:
    server = _Recorder(_Embeddings)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.fixture
def servers(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[_Recorder, _Recorder]]:
    _loopback_only(monkeypatch)
    proxy, target = _serve(), _serve()
    for name in ("NO_PROXY", "no_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    url = f"http://127.0.0.1:{proxy.server_address[1]}"
    monkeypatch.setenv("HTTP_PROXY", url)
    monkeypatch.setenv("http_proxy", url)
    try:
        yield proxy, target
    finally:
        proxy.shutdown()
        target.shutdown()


def test_two_stage_semantic_on_loopback_bypasses_the_proxy(
    tmp_path: Path,
    servers: tuple[_Recorder, _Recorder],
    isolated_home: Path,  # noqa: F811
) -> None:
    from memfw.conversations.worker import WorkState, state_path

    proxy, target = servers
    vault = make_vault(tmp_path / "vault", {"conversation_share": {"enabled": True}})
    store = write_store(tmp_path / "data" / "conversations.sqlite", standard_rows())
    with WorkState(state_path({})).open() as connection:
        connection.execute(
            "INSERT INTO summaries (app, session_id, chunk_start, chunk_end, prompt_version,"
            " model, summary, intent, outcome, created_at) VALUES ('codex', 's-alpha', -1, 3,"
            " 'conv-v1', 'fake', 'Uploader retry summary.', '', '', '2026-01-07T00:00:00Z')"
        )
    embedder = HttpEmbeddingProvider(
        f"http://localhost:{target.server_address[1]}/v1/embeddings",
        model="m",
        dimension=DIMENSION,
    )
    result = open_share(vault, "claude", store=store, embedder=embedder).search(
        "uploader", mode="two_stage"
    )
    assert result["degraded"] == []
    assert "semantic" in result["sessions"][0]["matched_by"]
    assert proxy.requests == []
    assert any("Uploader retry summary." in body for _, body in target.requests)
