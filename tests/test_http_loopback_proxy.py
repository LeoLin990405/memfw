"""Loopback requests never go through an environment proxy (``memfw.notes.http``).

A fake proxy and a fake embedding service run on loopback; ``HTTP_PROXY``
points at the proxy and ``no_proxy`` is unset, the setting that used to send
loopback embedding requests (with memory text) to the proxy.
"""

from __future__ import annotations

import json
import socket
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from memfw.notes import HttpEmbeddingProvider
from memfw.notes import http as notes_http

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


@pytest.mark.parametrize(
    "host",
    [
        "localhost",
        "LOCALHOST",
        "localhost.",
        "127.0.0.1",
        "127.5.6.7",
        "::1",
        "[::1]",
        "::ffff:127.0.0.1",
        "0:0:0:0:0:ffff:7f00:1",
    ],
)
def test_loopback_hosts(host: str) -> None:
    assert notes_http.is_loopback_host(host)


@pytest.mark.parametrize(
    "host",
    ["", None, "192.0.2.10", "10.0.0.1", "::ffff:10.0.0.1", "localhost.example.com", "0.0.0.0"],
)
def test_non_loopback_hosts(host: str | None) -> None:
    assert not notes_http.is_loopback_host(host)


def test_loopback_request_bypasses_the_proxy(servers: tuple[_Recorder, _Recorder]) -> None:
    proxy, target = servers
    port = target.server_address[1]
    for host in ("localhost", "127.0.0.1"):
        request = urllib.request.Request(
            f"http://{host}:{port}/v1/embeddings",
            data=json.dumps({"input": ["private text"]}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with notes_http.open_http(request, timeout=5) as response:
            assert response.status == 200
    assert proxy.requests == []
    assert [path for path, _ in target.requests] == ["/v1/embeddings"] * 2


def test_non_loopback_request_still_uses_the_proxy(servers: tuple[_Recorder, _Recorder]) -> None:
    proxy, _target = servers
    request = urllib.request.Request(
        "http://192.0.2.10/v1/embeddings",
        data=json.dumps({"input": ["x"]}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with notes_http.open_http(request, timeout=5) as response:
        assert response.status == 200
    assert [path for path, _ in proxy.requests] == ["http://192.0.2.10/v1/embeddings"]


def test_memory_embedder_on_loopback_bypasses_the_proxy(
    servers: tuple[_Recorder, _Recorder],
) -> None:
    proxy, target = servers
    embedder = HttpEmbeddingProvider(
        f"http://localhost:{target.server_address[1]}/v1/embeddings",
        model="m",
        dimension=DIMENSION,
        retries=0,
        timeout=5,
    )
    assert embedder.embed(["memory body text"]) == [[1.0] * DIMENSION]
    assert proxy.requests == [] and "memory body text" in target.requests[0][1]


def test_a_refused_proxy_does_not_hide_loopback_failures(
    servers: tuple[_Recorder, _Recorder],
) -> None:
    proxy, _target = servers
    request = urllib.request.Request("http://localhost:1/v1/embeddings", data=b"{}")
    with pytest.raises(urllib.error.URLError):
        notes_http.open_http(request, timeout=2)
    assert proxy.requests == []
