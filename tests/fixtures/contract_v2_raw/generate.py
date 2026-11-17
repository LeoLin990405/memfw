"""Record raw-byte v2 goldens from the checkout on sys.path (a pre-v3 service).

Run from that checkout (see README.md); not collected by pytest.
"""

import json
import os
import socket
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import memfw
import scripts.memfw_readonly_service as service
from tests.test_readonly_service import (
    READ_TOKEN,
    SCENARIOS,
    WRITE_TOKEN,
    _canonical,
    build_related,
    seed_vault,
)

PLACEHOLDER = {READ_TOKEN: "{read_token}", WRITE_TOKEN: "{write_token}"}
EXTRA = {
    "get.not_found": {
        "method": "GET",
        "path": "/nope",
        "body": None,
        "token": None,
        "related": True,
    },
    "post.not_found": {
        "method": "POST",
        "path": "/nope",
        "body": {},
        "token": READ_TOKEN,
        "related": True,
    },
    "post.forget_not_found": {
        "method": "POST",
        "path": "/forget",
        "body": {"request_id": "a", "memory_id": "mem-alpha"},
        "token": WRITE_TOKEN,
        "related": True,
    },
    "post.context_not_found": {
        "method": "POST",
        "path": "/context",
        "body": {"tenant": "tenant-a"},
        "token": READ_TOKEN,
        "related": True,
    },
    "search.bad_json": {
        "method": "POST",
        "path": "/search",
        "raw": b"{{{",
        "token": READ_TOKEN,
        "related": True,
    },
    "search.empty_query": {
        "method": "POST",
        "path": "/search",
        "body": {"query": ""},
        "token": READ_TOKEN,
        "related": True,
    },
    "search.wrong_token": {
        "method": "POST",
        "path": "/search",
        "body": {"query": "corpus keyword"},
        "token": "wrong-token",
        "related": True,
    },
    "put.not_found": {
        "method": "PUT",
        "path": "/search",
        "body": {},
        "token": READ_TOKEN,
        "related": True,
    },
    "delete.not_found": {
        "method": "DELETE",
        "path": "/stage",
        "body": {},
        "token": READ_TOKEN,
        "related": True,
    },
    "head.health": {
        "method": "HEAD",
        "path": "/health",
        "body": None,
        "token": None,
        "related": True,
    },
}
CASES = {**SCENARIOS, **EXTRA}


def request_bytes(case):
    if "raw" in case:
        body = case["raw"]
    elif case["body"] is None or case["method"] in ("GET", "HEAD"):
        body = b""
    else:
        body = _canonical(case["body"])
    lines = [f"{case['method']} {case['path']} HTTP/1.1", "Host: localhost"]
    if case["token"]:
        lines.append(f"{service.TOKEN_HEADER}: {PLACEHOLDER.get(case['token'], case['token'])}")
    if body or case["method"] not in ("GET", "HEAD"):
        lines += ["Content-Type: application/json", f"Content-Length: {len(body)}"]
    lines.append("Connection: close")
    return ("\r\n".join(lines) + "\r\n\r\n").encode() + body


def fill(raw):
    for token, ph in PLACEHOLDER.items():
        raw = raw.replace(ph.encode(), token.encode())
    return raw


def exchange(port, raw, headers_only=False):
    if headers_only:
        # The service answers 413 from Content-Length alone and closes; an
        # unread body would turn the close into a reset.
        raw = raw.partition(b"\r\n\r\n")[0] + b"\r\n\r\n"
    with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
        s.sendall(raw)
        chunks = []
        while True:
            piece = s.recv(65536)
            if not piece:
                break
            chunks.append(piece)
    head, sep, body = b"".join(chunks).partition(b"\r\n\r\n")
    lines = [line for line in head.split(b"\r\n") if not line.lower().startswith(b"date:")]
    head = b"\r\n".join(lines)
    return head + sep + body


def main() -> None:
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    print("memfw from", memfw.__file__, "service from", service.__file__, file=sys.stderr)
    os.environ["MEMFW_READONLY_TOKEN"] = READ_TOKEN
    os.environ["MEMFW_WRITE_TOKEN"] = WRITE_TOKEN
    for k in ("MEMFW_TENANT", "MEMFW_READONLY_CLEARANCE", "MEMFW_RELATED_DB"):
        os.environ.pop(k, None)
    server = ThreadingHTTPServer(("127.0.0.1", 0), service.Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    meta = {}
    tmp = Path(tempfile.mkdtemp())
    for name in sorted(CASES):
        case = CASES[name]
        root = seed_vault(tmp / name)
        build_related(root)
        os.environ["MEMFW_READONLY_ROOT"] = str(root)
        if case["related"]:
            os.environ["MEMFW_RELATED_DB"] = str(root / "related.sqlite3")
        else:
            os.environ.pop("MEMFW_RELATED_DB", None)
        for prev in case.get("before", []):
            exchange(port, fill(request_bytes(CASES[prev])))
        req = request_bytes(case)
        limit = service.STAGE_BODY_BYTES if case["path"] == "/stage" else service.SEARCH_BODY_BYTES
        headers_only = len(req.partition(b"\r\n\r\n")[2]) > limit
        resp = exchange(port, fill(req), headers_only)
        (out / f"{name}.request.http").write_bytes(req)
        (out / f"{name}.response.http").write_bytes(resp)
        meta[name] = {
            "related": case["related"],
            "before": case.get("before", []),
            "headers_only": headers_only,
        }
        print(name, resp.split(b"\r\n", 1)[0].decode(), file=sys.stderr)
    (out / "cases.json").write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n")
    server.shutdown()


if __name__ == "__main__":
    main()
