"""``FakeQdrant``: a loopback-only stand-in for the Qdrant v1.20 REST API, for tests.

It implements the calls the adapter makes, with the response envelopes the
recorded contract fixtures show (``tests/fixtures/qdrant_contract``):
``GET /``, ``GET|PUT /collections/{c}``, ``GET /collections/{c}/exists``,
``PUT /collections/{c}/index``, ``PUT /collections/{c}/points``,
``POST /collections/{c}/points/{scroll,delete,query}``. Search is exact
cosine over the stored vectors with ``must`` keyword filters. It binds
``127.0.0.1`` only, keeps every request (method, path, JSON body) in
``requests`` and can require an ``api-key``. Standard library only; not a
Qdrant implementation, just enough to exercise the adapter.
"""

from __future__ import annotations

import json
import math
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import unquote, urlsplit

VERSION = "1.20.2"


def _ok(result: Any) -> dict[str, Any]:
    return {"result": result, "status": "ok", "time": 0.000123}


def _error(message: str) -> dict[str, Any]:
    return {"status": {"error": message}, "time": 0.000045}


def _matches(payload: dict[str, Any], flt: Any) -> bool:
    if not flt:
        return True
    for condition in flt.get("must") or []:
        key = condition.get("key")
        wanted = (condition.get("match") or {}).get("value")
        if payload.get(key) != wanted:
            return False
    return True


def _cosine(left: list[float], right: list[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    norm = math.sqrt(sum(a * a for a in left)) * math.sqrt(sum(b * b for b in right))
    return dot / norm if norm else 0.0


def _select(payload: dict[str, Any], selector: Any) -> dict[str, Any] | None:
    if selector is True:
        return dict(payload)
    if isinstance(selector, list):
        return {key: payload[key] for key in selector if key in payload}
    return None


class FakeQdrant:
    """``with FakeQdrant() as fake:`` then point the adapter at ``fake.url``."""

    def __init__(self, *, api_key: str | None = None, version: str = VERSION) -> None:
        self.api_key = api_key
        self.version = version
        self.collections: dict[str, dict[str, Any]] = {}
        self.requests: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                del format, args

            def _send(self, status: int, payload: Any) -> None:
                data = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _handle(self, method: str) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                body = json.loads(raw) if raw else None
                parts = urlsplit(self.path)
                with fake._lock:
                    fake.requests.append(
                        {
                            "method": method,
                            "path": parts.path,
                            "query": parts.query,
                            "body": body,
                            "api_key": self.headers.get("api-key"),
                        }
                    )
                if fake.api_key is not None and self.headers.get("api-key") != fake.api_key:
                    self._send(
                        401, _error("Must provide an API key or an Authorization bearer token")
                    )
                    return
                status, payload = fake.route(method, parts.path, body)
                self._send(status, payload)

            def do_GET(self) -> None:  # noqa: N802
                self._handle("GET")

            def do_PUT(self) -> None:  # noqa: N802
                self._handle("PUT")

            def do_POST(self) -> None:  # noqa: N802
                self._handle("POST")

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> FakeQdrant:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()

    # --- the API ----------------------------------------------------------------

    def route(self, method: str, path: str, body: Any) -> tuple[int, Any]:
        if method == "GET" and path == "/":
            return 200, {
                "title": "qdrant - vector search engine",
                "version": self.version,
                "commit": "0" * 40,
            }
        pieces = [unquote(piece) for piece in path.strip("/").split("/")]
        if len(pieces) < 2 or pieces[0] != "collections":
            return 404, _error("Not found")
        name, rest = pieces[1], pieces[2:]
        with self._lock:
            collection = self.collections.get(name)
            if not rest:
                if method == "PUT":
                    if collection is not None:
                        return 409, _error(f"Wrong input: Collection `{name}` already exists!")
                    vectors = body["vectors"]
                    self.collections[name] = {
                        "vectors": vectors,
                        "points": {},
                        "indexes": {},
                        "op": 0,
                    }
                    return 200, _ok(True)
                if collection is None:
                    return 404, _error(f"Not found: Collection `{name}` doesn't exist!")
                return 200, _ok(self._info(collection))
            if rest == ["exists"]:
                return 200, _ok({"exists": collection is not None})
            if collection is None:
                return 404, _error(f"Not found: Collection `{name}` doesn't exist!")
            collection["op"] += 1
            update = {"operation_id": collection["op"], "status": "completed"}
            if rest == ["index"] and method == "PUT":
                collection["indexes"][body["field_name"]] = body.get("field_schema")
                return 200, _ok(update)
            if rest == ["points"] and method == "PUT":
                size = collection["vectors"]["size"]
                for point in body["points"]:
                    if len(point["vector"]) != size:
                        return 400, _error("Wrong input: Vector dimension error")
                    collection["points"][str(point["id"])] = {
                        "vector": [float(v) for v in point["vector"]],
                        "payload": dict(point.get("payload") or {}),
                        "version": collection["op"],
                    }
                return 200, _ok(update)
            if rest == ["points", "delete"] and method == "POST":
                for identifier in body["points"]:
                    collection["points"].pop(str(identifier), None)
                return 200, _ok(update)
            if rest == ["points", "scroll"] and method == "POST":
                return 200, _ok(self._scroll(collection, body))
            if rest == ["points", "query"] and method == "POST":
                return 200, _ok(self._query(collection, body))
        return 404, _error("Not found")

    @staticmethod
    def _info(collection: dict[str, Any]) -> dict[str, Any]:
        return {
            "status": "green",
            "optimizer_status": "ok",
            "indexed_vectors_count": 0,
            "points_count": len(collection["points"]),
            "segments_count": 1,
            "config": {
                "params": {
                    "vectors": dict(collection["vectors"]),
                    "shard_number": 1,
                    "replication_factor": 1,
                    "write_consistency_factor": 1,
                    "on_disk_payload": True,
                },
                "hnsw_config": {"m": 16, "ef_construct": 100, "full_scan_threshold": 10000},
                "optimizer_config": {"default_segment_number": 0},
                "wal_config": {"wal_capacity_mb": 32, "wal_segments_ahead": 0},
            },
            "payload_schema": {
                key: {"data_type": schema, "points": len(collection["points"])}
                for key, schema in collection["indexes"].items()
            },
        }

    @staticmethod
    def _scroll(collection: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        ids = sorted(
            identifier
            for identifier, point in collection["points"].items()
            if _matches(point["payload"], body.get("filter"))
        )
        offset = body.get("offset")
        if offset is not None:
            ids = [identifier for identifier in ids if identifier >= str(offset)]
        limit = int(body.get("limit") or 10)
        page, rest = ids[:limit], ids[limit:]
        selector = body.get("with_payload", True)
        points = []
        for identifier in page:
            record: dict[str, Any] = {"id": identifier}
            record["payload"] = _select(collection["points"][identifier]["payload"], selector)
            if body.get("with_vector"):
                record["vector"] = collection["points"][identifier]["vector"]
            points.append(record)
        return {"points": points, "next_page_offset": rest[0] if rest else None}

    @staticmethod
    def _query(collection: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        vector = [float(v) for v in body["query"]]
        scored = []
        for identifier, point in collection["points"].items():
            if _matches(point["payload"], body.get("filter")):
                scored.append((_cosine(vector, point["vector"]), identifier, point))
        scored.sort(key=lambda item: (-item[0], item[1]))
        selector = body.get("with_payload", False)
        points = []
        for score, identifier, point in scored[: int(body.get("limit") or 10)]:
            record: dict[str, Any] = {"id": identifier, "version": point["version"], "score": score}
            record["payload"] = _select(point["payload"], selector)
            if body.get("with_vector"):
                record["vector"] = point["vector"]
            points.append(record)
        return {"points": points}


__all__ = ["VERSION", "FakeQdrant"]
