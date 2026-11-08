from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from memfw.notes.ai_annotation import OpenAIJudge
from memfw.notes.http import RefusingRedirectHandler
from memfw.notes.index import HttpEmbeddingProvider, NoteHit, NotesIndex, _note_file_inside_root
from memfw.notes.language_model import HttpLanguageModelProvider
from memfw.notes.mcp import TOOLS, NotesGateway, handle_request, main
from memfw.notes.retrieval import HttpRerankerProvider
from memfw.related import RelatedIndex

_REAL_CONNECT = socket.socket.connect
_REAL_CONNECT_EX = socket.socket.connect_ex
_REAL_CREATE_CONNECTION = socket.create_connection
_REAL_GETADDRINFO = socket.getaddrinfo

_MARKER = "TOPSECRETLINKMARKER"
_DIR_MARKER = "OUTSIDEDIRMARKER"
_VISIBLE = "VISIBLECORPUSNOTE"
_LEAK = "LIKE or GLOB pattern too complex"
_SECRET = "super-secret-token"


def _allow_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket.socket, "connect", _REAL_CONNECT)
    monkeypatch.setattr(socket.socket, "connect_ex", _REAL_CONNECT_EX)
    monkeypatch.setattr(socket, "create_connection", _REAL_CREATE_CONNECTION)
    monkeypatch.setattr(socket, "getaddrinfo", _REAL_GETADDRINFO)


class _LoopbackServer(ThreadingHTTPServer):
    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.hits: list[dict[str, Any]] = []
        self.status_code = 200
        self.location = ""
        self.payload = b"{}"


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or "0")
        body = self.rfile.read(length) if length else b""
        server = self.server
        assert isinstance(server, _LoopbackServer)
        server.hits.append(
            {
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
                "body": body,
            }
        )
        outgoing = b"" if server.status_code >= 300 else server.payload
        self.send_response(server.status_code)
        if server.location:
            self.send_header("Location", server.location)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(outgoing)))
        self.send_header("Connection", "close")
        self.end_headers()
        if outgoing:
            self.wfile.write(outgoing)

    do_GET = do_POST

    def log_message(self, format: str, *args: object) -> None:
        return


def _start(server: _LoopbackServer) -> threading.Thread:
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return thread


def _stop(server: _LoopbackServer, thread: threading.Thread) -> None:
    server.shutdown()
    server.server_close()
    thread.join(timeout=3)


def _tool_text(gateway: NotesGateway, name: str, arguments: dict[str, Any], request_id: int) -> str:
    response = handle_request(
        gateway,
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    )
    assert response is not None
    return str(response["result"]["content"][0]["text"])


def test_redirect_handler_raises_on_every_3xx() -> None:
    assert issubclass(RefusingRedirectHandler, urllib.request.HTTPRedirectHandler)
    handler = RefusingRedirectHandler()
    request = urllib.request.Request("http://127.0.0.1/v1/embeddings", data=b"{}")
    for code in range(300, 400):
        with pytest.raises(urllib.error.HTTPError) as raised:
            handler.redirect_request(request, None, code, "moved", {}, "http://127.0.0.1/sink")
        assert raised.value.code == code
        method = getattr(handler, f"http_error_{code}")
        with pytest.raises(urllib.error.HTTPError) as refused:
            method(request, None, code, "moved", {})
        assert refused.value.code == code


def test_redirect_does_not_forward_authorization(monkeypatch: pytest.MonkeyPatch) -> None:
    _allow_loopback(monkeypatch)
    sink = _LoopbackServer()
    redirector = _LoopbackServer()
    sink.payload = json.dumps(
        {
            "data": [{"index": 0, "embedding": [0.1]}],
            "choices": [{"message": {"content": "{}"}}],
        }
    ).encode()
    sink_thread = _start(sink)
    redirect_thread = _start(redirector)
    try:
        sink_port = int(sink.server_address[1])
        redirect_port = int(redirector.server_address[1])
        redirector.location = f"http://127.0.0.1:{sink_port}/captured"
        endpoint = f"http://127.0.0.1:{redirect_port}/v1"
        for code in (301, 302, 303, 307, 308):
            redirector.status_code = code
            _exercise_clients(endpoint, redirector, sink)
    finally:
        _stop(redirector, redirect_thread)
        _stop(sink, sink_thread)


def _exercise_clients(endpoint: str, redirector: _LoopbackServer, sink: _LoopbackServer) -> None:
    hit = NoteHit("a.md", "A", "h", "body", 0.1, 1, "fts")
    embedder = HttpEmbeddingProvider(
        endpoint + "/embeddings",
        model="m",
        dimension=1,
        api_key=_SECRET,
        retries=0,
        timeout=2,
    )
    language = HttpLanguageModelProvider(
        endpoint + "/chat",
        model="m",
        api_key=_SECRET,
        timeout=2,
    )
    judge = OpenAIJudge(endpoint + "/chat", model="m", api_key=_SECRET, timeout=2)
    reranker = HttpRerankerProvider(endpoint + "/rerank", model="m", retries=0, timeout=2)
    calls: list[tuple[str, Callable[[], object]]] = [
        ("Bearer " + _SECRET, lambda: embedder.embed(["hello"])),
        ("Bearer " + _SECRET, lambda: language.rewrite_query("hello")),
        (
            "Bearer " + _SECRET,
            lambda: judge.judge(
                "hello",
                [{"path": "a.md", "title": "A", "heading": "h", "content": "c"}],
            ),
        ),
        ("", lambda: reranker.rerank("hello", [hit], top_k=1)),
    ]
    for authorization, call in calls:
        redirector.hits.clear()
        sink.hits.clear()
        with pytest.raises((urllib.error.URLError, RuntimeError)):
            call()
        assert sink.hits == []
        assert not any(item.get("authorization") for item in sink.hits)
        assert redirector.hits
        assert (redirector.hits[0]["authorization"] or "") == authorization


def test_index_skips_symlink_outside_notes_root(tmp_path: Path) -> None:
    root = tmp_path / "notes"
    course = root / "course1"
    course.mkdir(parents=True)
    (course / "inside.md").write_text(f"# Inside\n\n{_VISIBLE}\n", encoding="utf-8")
    outside = tmp_path / "outside.md"
    outside.write_text(f"# Secret\n\n{_MARKER}\n", encoding="utf-8")
    link = course / "link.md"
    link.symlink_to(outside)
    outside_dir = tmp_path / "secret-dir"
    outside_dir.mkdir()
    (outside_dir / "hidden.md").write_text(f"# Hidden\n\n{_DIR_MARKER}\n", encoding="utf-8")
    (root / "alias").symlink_to(outside_dir, target_is_directory=True)

    assert _note_file_inside_root(link, root) is False
    assert _note_file_inside_root(root / "alias" / "hidden.md", root) is False
    assert _note_file_inside_root(course / "inside.md", root) is True

    db = tmp_path / "notes.sqlite"
    NotesIndex(db).index(root)
    index = NotesIndex(db)
    assert index.search(_MARKER, mode="fts") == []
    assert index.search(_DIR_MARKER, mode="fts") == []
    visible = index.search(_VISIBLE, mode="fts")
    assert any(hit.path == "course1/inside.md" for hit in visible)

    gateway = NotesGateway(root, db)
    searched = json.loads(_tool_text(gateway, "notes_search", {"query": _MARKER, "mode": "fts"}, 1))
    assert searched["results"] == []
    fetched = json.loads(_tool_text(gateway, "notes_get", {"path": "course1/link.md"}, 2))
    assert _MARKER not in json.dumps(fetched)
    assert "error" in fetched
    inside = _tool_text(gateway, "notes_get", {"path": "course1/inside.md"}, 3)
    assert _VISIBLE in inside


def test_caller_rerank_endpoint_is_ignored(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    search = next(tool for tool in TOOLS if tool["name"] == "notes_search")
    assert "rerank_endpoint" not in search["inputSchema"]["properties"]

    _allow_loopback(monkeypatch)
    attacker = _LoopbackServer()
    configured = _LoopbackServer()
    configured.payload = json.dumps({"results": [{"index": 0, "relevance_score": 0.9}]}).encode()
    attacker_thread = _start(attacker)
    configured_thread = _start(configured)
    try:
        attacker_port = int(attacker.server_address[1])
        configured_port = int(configured.server_address[1])
        root = tmp_path / "notes"
        root.mkdir()
        (root / "a.md").write_text(f"# A\n\n{_VISIBLE}\n", encoding="utf-8")
        db = tmp_path / "notes.sqlite"
        NotesIndex(db).index(root)
        gateway = NotesGateway(
            root,
            db,
            reranker=HttpRerankerProvider(
                f"http://127.0.0.1:{configured_port}/rerank",
                model="server-reranker",
                retries=0,
                timeout=2,
            ),
        )
        listed = handle_request(gateway, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert listed is not None
        listed_search = next(
            tool for tool in listed["result"]["tools"] if tool["name"] == "notes_search"
        )
        assert "rerank_endpoint" not in listed_search["inputSchema"]["properties"]
        text = _tool_text(
            gateway,
            "notes_search",
            {
                "query": _VISIBLE,
                "mode": "fts",
                "rerank_candidates": 5,
                "rerank_endpoint": f"http://127.0.0.1:{attacker_port}/exfil",
                "rerank_model": "evil",
            },
            2,
        )
        assert attacker.hits == []
        assert configured.hits
        body = configured.hits[0]["body"]
        assert isinstance(body, bytes)
        assert _VISIBLE.encode() in body
        assert _VISIBLE in text
    finally:
        _stop(attacker, attacker_thread)
        _stop(configured, configured_thread)


def test_module_accepts_related_db_and_hides_internal_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "notes"
    root.mkdir()
    (root / "a.md").write_text("# Alpha\n\nSee [[b]].\n", encoding="utf-8")
    (root / "b.md").write_text("# Beta\n\nneighbor note.\n", encoding="utf-8")
    db = tmp_path / "notes.sqlite"
    NotesIndex(db).index(root)
    related = tmp_path / "related.sqlite"
    RelatedIndex(related).rebuild_notes(db)
    captured: dict[str, NotesGateway] = {}

    def fake_serve(gateway: NotesGateway) -> None:
        captured["gateway"] = gateway

    monkeypatch.setattr("memfw.notes.mcp.serve", fake_serve)
    main(
        [
            "--root",
            str(root),
            "--db",
            str(db),
            "--related-db",
            str(related),
            "--rerank-endpoint",
            "http://127.0.0.1:9/rerank",
            "--rerank-model",
            "server-reranker",
        ]
    )
    gateway = captured["gateway"]
    assert gateway.related is not None
    assert gateway.related.path == related
    assert gateway.reranker is not None
    assert gateway.reranker.endpoint == "http://127.0.0.1:9/rerank"

    ok = json.loads(_tool_text(gateway, "notes_related", {"path": "a.md"}, 1))
    assert ok["path"] == "a.md"
    assert _LEAK not in json.dumps(ok)

    missing = NotesGateway(root, db)
    bare = json.loads(_tool_text(missing, "notes_related", {"path": "a.md"}, 2))
    assert bare["error"] == "related index is not configured"
    unsafe = json.loads(_tool_text(missing, "notes_get", {"path": "../outside.md"}, 3))
    assert unsafe["error"] == "unsafe note path"

    complex_db = tmp_path / "complex.sqlite"
    _write_complex_related_db(complex_db)
    leaked = _tool_text(
        NotesGateway(root, db, related_db=complex_db),
        "notes_related",
        {"path": "a.md"},
        4,
    )
    assert _LEAK not in leaked
    assert "pattern too complex" not in leaked
    assert "related_edges" not in leaked
    assert json.loads(leaked) == {"error": "internal error"}

    huge = "a" * 50001
    assert NotesIndex(db).search(huge, mode="fts") == []
    searched = _tool_text(NotesGateway(root, db), "notes_search", {"query": huge, "mode": "fts"}, 5)
    assert _LEAK not in searched
    assert "pattern too complex" not in searched


def _write_complex_related_db(path: Path) -> None:
    pattern = "_" * 50001
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE dummy (c TEXT, src_id TEXT)")
    connection.execute("INSERT INTO dummy VALUES ('a', 'a.md')")
    connection.execute(
        "CREATE VIEW related_edges AS "
        "SELECT 'notes' AS src_source, src_id, 'notes' AS dst_source, "
        "'q' AS dst_id, 'lexical' AS method, 1.0 AS score, '{}' AS evidence "
        "FROM dummy WHERE c LIKE '" + pattern + "'"
    )
    connection.execute(
        "CREATE TABLE related_items ("
        "source TEXT NOT NULL, id TEXT NOT NULL, title TEXT NOT NULL, "
        "path TEXT NOT NULL, hash TEXT NOT NULL, text TEXT NOT NULL DEFAULT '', "
        "tenant TEXT NOT NULL DEFAULT '', scope TEXT NOT NULL DEFAULT '', "
        "PRIMARY KEY (source, id))"
    )
    connection.commit()
    connection.close()


def test_python_module_help_lists_related_db(tmp_path: Path) -> None:
    env = {key: value for key, value in os.environ.items() if not _secret_env(key)}
    env["HOME"] = str(tmp_path)
    env["LANG"] = "en_US.UTF-8"
    env["LC_ALL"] = "en_US.UTF-8"
    completed = subprocess.run(
        [sys.executable, "-m", "memfw.notes.mcp", "--help"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    assert completed.returncode == 0, completed.stderr
    assert "--related-db" in completed.stdout


def _secret_env(key: str) -> bool:
    upper = key.upper()
    return (
        upper.endswith("_API_KEY")
        or upper.endswith("_TOKEN")
        or "SECRET" in upper
        or upper.endswith("_PROXY")
    )
