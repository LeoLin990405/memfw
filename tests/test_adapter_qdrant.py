"""memfw-adapter-qdrant: semantic recall from a Qdrant service, against fakes only.

The adapter lives in ``packages/adapters/memfw-adapter-qdrant`` and imports only
``memfw.ports``. No real Qdrant runs here: behaviour is checked against the
package's loopback ``FakeQdrant`` and the wire format against the recorded
v1.20.2 exchanges in ``tests/fixtures/qdrant_contract`` (replayed by a
server that answers exactly what was recorded).
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import socket
import threading
import urllib.request
from collections.abc import Iterator, Sequence
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from memfw_adapter_qdrant import SPEC, QdrantProvider, factory
from memfw_adapter_qdrant import http as qhttp
from memfw_adapter_qdrant.cli import main as cli_main
from memfw_adapter_qdrant.embed import EnvEmbedder, embedder_from_env, fingerprint_of
from memfw_adapter_qdrant.fake import FakeQdrant
from memfw_adapter_qdrant.sync import point_id, sync

from memfw.core.repository import Repository
from memfw.index import MemoryIndex
from memfw.notes import HttpEmbeddingProvider
from memfw.ports import (
    AdapterRegistry,
    AdaptersConfig,
    Availability,
    ProviderUnavailable,
    RecallBudget,
    RecallProvider,
)
from memfw.ports.testing import check_recall_provider, tree_digest
from memfw.recall import Coordinator, FilterPolicy
from memfw.recall.builtin import FtsProvider, default_resolvers

FIXTURE = Path(__file__).parent / "fixtures" / "qdrant_contract" / "v1.20.2.json"
QUERIES = ("homelab backup", "HAL9000 rack", "release notes")
_WORD = re.compile(r"\w+")


class WordsEmbedder:
    """Deterministic bag-of-words vectors (16 buckets); records every text it saw."""

    dimension = 16
    fingerprint = "words-16-test"

    def __init__(self) -> None:
        self.seen: list[str] = []

    def embed(self, texts: Sequence[str], *, role: str = "document") -> list[list[float]]:
        del role
        out = []
        for text in texts:
            self.seen.append(text)
            vector = [0.0] * self.dimension
            for word in _WORD.findall(text.lower()):
                vector[int(hashlib.sha256(word.encode()).hexdigest(), 16) % self.dimension] += 1
            norm = math.sqrt(sum(v * v for v in vector)) or 1.0
            out.append([v / norm if any(vector) else 1.0 / 4 for v in vector])
        return out


_LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})
_CONNECT = socket.socket.connect
_CONNECT_EX = socket.socket.connect_ex
_CREATE = socket.create_connection
_GETADDRINFO = socket.getaddrinfo


@pytest.fixture(autouse=True)
def loopback_only(disable_network: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """Narrow the suite's no-network guard to the fakes: loopback only."""

    def host_of(address: Any) -> str:
        return str(address[0]) if isinstance(address, tuple) and address else str(address)

    def guard(real: Any, address_index: int) -> Any:
        def call(*args: Any, **kwargs: Any) -> Any:
            if host_of(args[address_index]) not in _LOOPBACK:
                raise AssertionError("Tests must not open network sockets")
            return real(*args, **kwargs)

        return call

    monkeypatch.setattr(socket.socket, "connect", guard(_CONNECT, 1))
    monkeypatch.setattr(socket.socket, "connect_ex", guard(_CONNECT_EX, 1))
    monkeypatch.setattr(socket, "create_connection", guard(_CREATE, 0))
    monkeypatch.setattr(
        socket, "getaddrinfo", lambda host, *a, **k: guard(_GETADDRINFO, 0)(host, *a, **k)
    )


@pytest.fixture(autouse=True)
def _env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "http_proxy",
        "https_proxy",
        "MEMFW_EMBEDDING_URL",
        "MEMFW_EMBEDDING_MODEL",
        "MEMFW_EMBEDDING_DIMENSION",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def fake() -> Iterator[FakeQdrant]:
    with FakeQdrant() as server:
        yield server


def _vault(tmp_path: Path) -> Repository:
    repository = Repository(tmp_path / "vault")
    repository.init()
    path = repository.root / "vault.yaml"
    path.write_text(
        path.read_text(encoding="utf-8") + "legacy_privacy_default: public\n", encoding="utf-8"
    )
    add = repository.add
    add("Homelab backup", "nightly homelab backup to the NAS", memory_id="m-backup")
    add("HAL9000 rack", "HAL9000 rack in the homelab, backup power", memory_id="m-hal")
    add("Release", "release v0.2.0 notes for memfw", memory_id="m-release")
    add("Private backup", "zebrafish backup keys", memory_id="m-priv", tags=["sensitivity:private"])
    add("Local note", "okapi homelab note", memory_id="m-local", tags=["privacy:local-private"])
    add("Odd backup", "odd backup marker", memory_id="m-odd", tags=["privacy:whatever"])
    add(
        "Shared backup",
        "shared fleet backup schedule",
        memory_id="m-shared",
        tags=["privacy:shared"],
    )
    add("Alpha backup", "tenant alpha homelab backup notes", memory_id="m-alpha", tenant="alpha")
    MemoryIndex(repository.index_path).rebuild(repository)
    return repository


def _provider(
    repository: Repository, fake: FakeQdrant, embedder: Any = None, **options: Any
) -> QdrantProvider:
    settings = {"vault": str(repository.root), "url": fake.url, **options}
    settings["embedder"] = embedder if embedder is not None else WordsEmbedder()
    return factory(settings)


def _search(provider: QdrantProvider, query: str, tenant: str = "", limit: int = 10) -> list[str]:
    found = provider.search(query, limit=limit, budget=RecallBudget(), tenant=tenant, as_of=None)
    return [candidate.ref for candidate in found]


def _stored_ids(fake: FakeQdrant) -> set[str]:
    return {point["payload"]["memory_id"] for point in fake.collections["memfw"]["points"].values()}


# --- the contract and the search request ---------------------------------------------


def test_contract_kit_passes_after_sync_and_the_vault_is_untouched(
    tmp_path: Path, fake: FakeQdrant
) -> None:
    repository = _vault(tmp_path)
    tenants = {doc.id: doc.tenant for doc in repository.list()}
    provider = _provider(repository, fake)
    assert isinstance(provider, RecallProvider) and provider.lexical is False
    assert provider.available() == Availability.missing("no_collection")
    before = tree_digest(repository.root, exclude=(".memfw.lock",))
    report = sync(provider)
    assert report.created_collection and report.upserted == 5 and report.deleted == 0
    assert report.skipped == {"privacy": 3}
    assert tree_digest(repository.root, exclude=(".memfw.lock",)) == before
    assert provider.available() == Availability.ok()
    seen = check_recall_provider(
        provider,
        queries=QUERIES,
        limit=5,
        vault=repository.root,
        vault_exclude=(".memfw.lock",),
        tenant_of=lambda candidate: tenants.get(candidate.ref),
    )
    assert seen["HAL9000 rack"][0].ref == "m-hal"
    assert all(c.extra == {"why": "semantic"} for hits in seen.values() for c in hits)
    alpha = check_recall_provider(provider, queries=["homelab backup"], tenant="alpha")
    assert [c.ref for c in alpha["homelab backup"]] == ["m-alpha"]
    assert provider.projection_version == "qdrant-" + fingerprint_of(provider.embedder)


def test_search_asks_for_references_only_and_never_sends_the_query_text(
    tmp_path: Path, fake: FakeQdrant
) -> None:
    repository = _vault(tmp_path)
    provider = _provider(repository, fake)
    sync(provider)
    fake.requests.clear()
    _search(provider, "nightly homelab backup", tenant="alpha")
    (query,) = [r for r in fake.requests if r["path"].endswith("/points/query")]
    body = query["body"]
    assert set(body) == {"query", "limit", "filter", "with_payload", "with_vector"}
    assert body["with_payload"] == ["memory_id", "tenant"] and body["with_vector"] is False
    assert body["filter"] == {
        "must": [
            {"key": "vault", "match": {"value": provider.vault_key}},
            {"key": "embedder", "match": {"value": fingerprint_of(provider.embedder)}},
            {"key": "tenant", "match": {"value": "alpha"}},
        ]
    }
    assert "nightly" not in json.dumps(fake.requests)


def test_local_private_and_unknown_memories_never_reach_qdrant_or_the_embedder(
    tmp_path: Path, fake: FakeQdrant
) -> None:
    repository = _vault(tmp_path)
    embedder = WordsEmbedder()
    provider = _provider(repository, fake, embedder)
    sync(provider)
    assert _stored_ids(fake) == {"m-backup", "m-hal", "m-release", "m-shared", "m-alpha"}
    wire = json.dumps(fake.requests)
    for word in ("zebrafish", "okapi", "marker", "nightly", "HAL9000"):
        assert word not in wire  # no memory text at all goes to the service
    assert not [text for text in embedder.seen if "zebrafish" in text or "okapi" in text]
    public = factory({"vault": str(repository.root), "url": fake.url, "clearance": "public"})
    assert public.settings.clearance == frozenset({"public"})
    for bad in ("local-private", "public,local-private", "public,secret", ""):
        with pytest.raises(ValueError):
            factory({"vault": str(repository.root), "url": fake.url, "clearance": bad})


def test_sync_is_idempotent_and_follows_changes_privacy_and_deletes(
    tmp_path: Path, fake: FakeQdrant
) -> None:
    repository = _vault(tmp_path)
    provider = _provider(repository, fake)
    sync(provider)
    again = sync(provider)
    assert (again.upserted, again.unchanged, again.deleted) == (0, 5, 0)
    assert not again.created_collection
    repository.update("m-backup", body="nightly homelab backup to the NAS and offsite")
    changed = sync(provider)
    assert (changed.upserted, changed.unchanged) == (1, 4)
    repository.update("m-release", tags=["sensitivity:private"])
    private = sync(provider)
    assert private.deleted == 1 and "m-release" not in _stored_ids(fake)
    (repository.root / "memories" / "m-hal.md").unlink()
    removed = sync(provider)
    assert removed.deleted == 1 and "m-hal" not in _stored_ids(fake)
    assert point_id(provider.vault_key, "m-backup") in fake.collections["memfw"]["points"]


def test_another_vault_and_another_embedder_never_match(tmp_path: Path, fake: FakeQdrant) -> None:
    repository = _vault(tmp_path)
    provider = _provider(repository, fake)
    sync(provider)
    other = Repository(tmp_path / "other")
    other.init()
    elsewhere = _provider(other, fake)
    assert _search(elsewhere, "homelab backup") == []

    class Other(WordsEmbedder):
        fingerprint = "words-16-other"

    renamed = _provider(repository, fake, Other())
    assert _search(renamed, "homelab backup") == []  # until sync re-embeds
    assert sync(renamed).upserted == 5
    assert _search(renamed, "homelab backup")
    dimension = _provider(repository, fake, type("Wide", (WordsEmbedder,), {"dimension": 8})())
    with pytest.raises(qhttp.QdrantError, match="dimension_mismatch"):
        sync(dimension)


# --- settings, network rules -------------------------------------------------------------


def test_settings_are_checked(tmp_path: Path, fake: FakeQdrant) -> None:
    repository = _vault(tmp_path)
    root = str(repository.root)
    for bad, message in (
        ({}, "needs a vault"),
        ({"vault": root, "api_key": "x"}, "unknown qdrant settings"),
        ({"vault": root, "url": "http://qdrant.example:6333"}, "not loopback"),
        ({"vault": root, "url": "http://10.0.0.5:6333"}, "not loopback"),
        ({"vault": root, "url": "http://user:pw@127.0.0.1:6333"}, "credentials"),
        ({"vault": root, "url": "http://127.0.0.1:6333/x"}, "path"),
        ({"vault": root, "url": "http://127.0.0.1:6333/?a=1"}, "query"),
        ({"vault": root, "url": "file:///etc/passwd"}, "http"),
        ({"vault": root, "allow_remote": "yes"}, "allow_remote"),
        ({"vault": root, "collection": "a/b"}, "collection"),
        ({"vault": root, "api_key_env": "lower"}, "api_key_env"),
        ({"vault": root, "timeout_seconds": 0}, "timeout_seconds"),
    ):
        with pytest.raises(ValueError, match=message):
            factory(bad)
    remote = factory({"vault": root, "url": "https://qdrant.example:6333", "allow_remote": True})
    assert remote.settings.url == "https://qdrant.example:6333"
    for loopback in ("http://localhost:6333", "http://[::1]:6333", "http://127.1.2.3:6333/"):
        assert factory({"vault": root, "url": loopback}).settings.url.startswith("http")


def test_loopback_bypasses_env_proxies_and_remote_keeps_them(
    tmp_path: Path, fake: FakeQdrant, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _vault(tmp_path)
    for name in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
        monkeypatch.setenv(name, "http://192.0.2.1:9")  # TEST-NET: unroutable
    provider = _provider(repository, fake)
    sync(provider)
    assert _search(provider, "homelab backup")
    handlers: list[Any] = []
    real = urllib.request.build_opener

    def spy(*items: Any) -> Any:
        handlers.append(items)
        return real(*items)

    monkeypatch.setattr(urllib.request, "build_opener", spy)
    request = urllib.request.Request("http://qdrant.example:6333/")
    with pytest.raises((OSError, AssertionError)):  # it went to the (unroutable) proxy
        qhttp.open_url(request, 0.01)
    assert not any(isinstance(item, urllib.request.ProxyHandler) for item in handlers[-1])
    with qhttp.open_url(urllib.request.Request(fake.url + "/"), 2.0) as response:
        assert json.loads(response.read())["version"] == "1.20.2"
    assert any(isinstance(item, urllib.request.ProxyHandler) for item in handlers[-1])


def _serve(status: int, body: Any, headers: dict[str, str] | None = None) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            del format, args

        def _reply(self) -> None:
            data = json.dumps(body).encode()
            self.send_response(status)
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_POST = do_PUT = _reply  # noqa: N815

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_redirects_are_refused(tmp_path: Path, fake: FakeQdrant) -> None:
    repository = _vault(tmp_path)
    server = _serve(302, {}, {"Location": fake.url + "/"})
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        provider = factory({"vault": str(repository.root), "url": url, "embedder": WordsEmbedder()})
        assert provider.available() == Availability.missing("redirect_refused")
    finally:
        server.shutdown()
        server.server_close()
    assert fake.requests == []


def test_availability_reports_unavailable_cleanly(
    tmp_path: Path, fake: FakeQdrant, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _vault(tmp_path)
    assert _provider(repository, fake).available() == Availability.missing("no_collection")
    nowhere = factory({"vault": str(tmp_path / "nowhere"), "url": fake.url})
    assert nowhere.available() == Availability.missing("no_vault")
    no_embedder = factory({"vault": str(repository.root), "url": fake.url})
    assert no_embedder.available() == Availability.missing("no_embedder")
    with pytest.raises(ProviderUnavailable):
        no_embedder.search("x", limit=3, budget=RecallBudget(), tenant="", as_of=None)
    sync(_provider(repository, fake))
    old = FakeQdrant(version="1.19.2")
    with old:
        sync(_provider(repository, old))
        assert _provider(repository, old).available() == Availability.missing("unsupported_version")
    with FakeQdrant(api_key="sekret-test-key") as locked:
        assert _provider(repository, locked).available() == Availability.missing("unauthorized")
        monkeypatch.setenv("QDRANT_TEST_KEY", "sekret-test-key")
        keyed = _provider(repository, locked, api_key_env="QDRANT_TEST_KEY")
        sync(keyed)
        assert keyed.available() == Availability.ok()
        assert "sekret" not in repr(keyed) and "sekret" not in repr(keyed.client)
    down = FakeQdrant()
    port = down.port
    down._server.server_close()  # never started: the port is closed
    dead = _provider(repository, fake, url=f"http://127.0.0.1:{port}")
    assert dead.available() == Availability.missing("unreachable")
    with pytest.raises(ProviderUnavailable):
        dead.search("homelab", limit=3, budget=RecallBudget(), tenant="", as_of=None)


def test_down_qdrant_degrades_to_fts_in_the_coordinator(tmp_path: Path, fake: FakeQdrant) -> None:
    """Compat framework T7: Qdrant unavailable, FTS answers, the result says degraded."""

    repository = _vault(tmp_path)
    root = repository.root
    down = FakeQdrant()
    port = down.port
    down._server.server_close()
    dead = _provider(repository, fake, url=f"http://127.0.0.1:{port}")
    coordinator = Coordinator([FtsProvider(root), dead], resolvers=default_resolvers(root))
    result = coordinator.search("homelab backup power", limit=10, policy=FilterPolicy())
    assert result.labels.degraded == {"qdrant": "unavailable:unreachable"}
    assert result.labels.providers == ("fts",) and not result.labels.fail_closed
    assert result.ids and result.ids[0] in {"m-hal", "m-backup"}
    live = _provider(repository, fake)
    sync(live)
    fused = Coordinator([FtsProvider(root), live], resolvers=default_resolvers(root))
    both = fused.search("homelab backup power", limit=10, policy=FilterPolicy())
    assert both.labels.providers == ("fts", "qdrant") and not both.labels.degraded
    assert "m-alpha" not in both.ids


def test_the_response_is_data(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    points = [
        {
            "id": 1,
            "version": 1,
            "score": 0.9,
            "payload": {"memory_id": "m-alpha", "tenant": "alpha"},
        },
        {"id": 2, "version": 1, "score": 0.8, "payload": {"memory_id": "../etc", "tenant": ""}},
        {"id": 3, "version": 1, "score": "high", "payload": {"memory_id": "m-hal", "tenant": ""}},
        {"id": 4, "version": 1, "score": 0.7, "payload": {"memory_id": "m-backup", "tenant": ""}},
        {"id": 5, "version": 1, "score": 0.6, "payload": {"memory_id": "m-backup", "tenant": ""}},
        {"id": 6, "version": 1, "score": 0.5, "payload": None},
        {"id": 7, "version": 1, "score": True, "payload": {"memory_id": "m-release", "tenant": ""}},
        "junk",
    ]
    server = _serve(200, {"result": {"points": points}, "status": "ok", "time": 0.001})
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        provider = factory({"vault": str(repository.root), "url": url, "embedder": WordsEmbedder()})
        assert _search(provider, "homelab") == ["m-backup"]
    finally:
        server.shutdown()
        server.server_close()
    for body in ({"status": "ok"}, {"result": {}, "status": {"error": "x"}}, ["x"]):
        server = _serve(200, body)
        try:
            url = f"http://127.0.0.1:{server.server_address[1]}"
            provider = factory(
                {"vault": str(repository.root), "url": url, "embedder": WordsEmbedder()}
            )
            with pytest.raises(ProviderUnavailable, match="bad_response"):
                provider.search("x", limit=3, budget=RecallBudget(), tenant="", as_of=None)
        finally:
            server.shutdown()
            server.server_close()


# --- recorded v1.20.2 exchanges -----------------------------------------------------------


class Replay:
    """Answers each (method, path) with the recorded response; keeps what it was sent."""

    def __init__(self, exchanges: list[dict[str, Any]]) -> None:
        self.answers = {
            (item["request"]["method"], item["request"]["path"]): item for item in exchanges
        }
        self.sent: list[dict[str, Any]] = []
        replay = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                del format, args

            def _reply(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                path, _, query = self.path.partition("?")
                replay.sent.append(
                    {
                        "method": self.command,
                        "path": path,
                        "query": query,
                        "body": json.loads(raw) if raw else None,
                    }
                )
                item = replay.answers.get((self.command, path))
                status, body = (
                    (404, {"status": {"error": "unrecorded"}})
                    if item is None
                    else (
                        item["status"],
                        item["response"],
                    )
                )
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = do_PUT = _reply  # noqa: N815

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def _shape(value: Any) -> Any:
    """Keys and types, recursively (list items by their first element)."""

    if isinstance(value, dict):
        return {key: _shape(item) for key, item in sorted(value.items())}
    if isinstance(value, list):
        return [_shape(value[0])] if value else []
    if isinstance(value, bool) or value is None:
        return type(value).__name__
    if isinstance(value, (int, float)):
        return "number"
    return type(value).__name__


def _recorded() -> dict[str, Any]:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert data["qdrant_version"].startswith("1.20.")
    return data


class FixedEmbedder(WordsEmbedder):
    dimension = 4

    def embed(self, texts: Sequence[str], *, role: str = "document") -> list[list[float]]:
        return [[0.5, 0.5, 0.5, 0.5] for _ in texts]


def test_search_matches_the_recorded_v1_20_exchanges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded = _recorded()
    by_name = {item["name"]: item for item in recorded["exchanges"]}
    repository = _vault(tmp_path)
    replay = Replay(recorded["exchanges"])
    try:
        provider = factory(
            {"vault": str(repository.root), "url": replay.url, "embedder": FixedEmbedder()}
        )
        monkeypatch.setattr(provider, "vault_key", recorded["vault_key"])
        monkeypatch.setattr(provider, "embedder_fingerprint", lambda: recorded["embedder"])
        assert provider.available() == Availability.ok()
        found = provider.search("homelab", limit=5, budget=RecallBudget(), tenant="", as_of=None)
        assert [c.ref for c in found] == recorded["expected_refs"]
        assert [c.score for c in found] == [1.0, 0.866025]  # recorded 0.99999994, rounded
        sent = {(item["method"], item["path"]): item for item in replay.sent}
        for name in ("root", "exists", "query"):
            request = by_name[name]["request"]
            assert sent[(request["method"], request["path"])]["body"] == request["body"], name
    finally:
        replay.close()


def test_sync_matches_the_recorded_v1_20_exchanges(tmp_path: Path) -> None:
    recorded = _recorded()
    by_name = {item["name"]: item for item in recorded["exchanges"]}
    exchanges = [item for item in recorded["exchanges"] if item["name"] != "collection_info"]
    repository = _vault(tmp_path)
    replay = Replay(exchanges)
    try:
        provider = factory(
            {"vault": str(repository.root), "url": replay.url, "embedder": FixedEmbedder()}
        )
        report = sync(provider)
        assert report.created_collection
        sent = replay.sent
        create = [r for r in sent if r["method"] == "PUT" and r["path"] == "/collections/memfw"]
        assert create[0]["body"] == by_name["create_collection"]["request"]["body"]
        indexes = [r for r in sent if r["path"] == "/collections/memfw/index"]
        assert [r["body"]["field_name"] for r in indexes] == [
            "vault",
            "tenant",
            "embedder",
            "memory_id",
        ]
        assert all(r["query"] == "wait=true" for r in indexes)
        assert _shape(indexes[0]["body"]) == _shape(by_name["create_index"]["request"]["body"])
        (scroll,) = [r for r in sent if r["path"].endswith("/points/scroll")]
        assert _shape(scroll["body"]) == _shape(by_name["scroll"]["request"]["body"])
        (upsert,) = [r for r in sent if r["path"] == "/collections/memfw/points"]
        assert upsert["query"] == "wait=true"
        assert _shape(upsert["body"]) == _shape(by_name["upsert"]["request"]["body"])
        assert set(upsert["body"]["points"][0]["payload"]) == {
            "memory_id",
            "tenant",
            "vault",
            "embedder",
            "sha",
        }
    finally:
        replay.close()


def test_the_fake_answers_in_the_recorded_shapes(tmp_path: Path) -> None:
    """``FakeQdrant`` is only trusted because it speaks the recorded wire format."""

    recorded = _recorded()
    order = ["root", "collection_missing", "create_collection", "exists", "create_index"]
    order += ["upsert", "scroll", "query", "delete"]
    by_name = {item["name"]: item for item in recorded["exchanges"]}
    assert set(order) | {"collection_info"} == set(by_name)
    with FakeQdrant() as fake:
        for name in order:  # in an order where each call has something to answer
            item = by_name[name]
            request = item["request"]
            status, body = fake.route(request["method"], request["path"], request["body"])
            assert status == item["status"], item["name"]
            assert _shape(body) == _shape(item["response"]), item["name"]
        status, info = fake.route("GET", "/collections/memfw", None)
        expected = recorded["exchanges"][
            [e["name"] for e in recorded["exchanges"]].index("collection_info")
        ]
        assert status == 200
        params = info["result"]["config"]["params"]["vectors"]
        assert params == expected["response"]["result"]["config"]["params"]["vectors"]
        assert set(info["result"]) <= set(expected["response"]["result"])


# --- embedder, registry, CLI, runtime -----------------------------------------------------------


def test_env_embedder_matches_memfws_fingerprint_and_is_used_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ours = EnvEmbedder("http://127.0.0.1:1/v1/embeddings", model="bge-m3", dimension=1024)
    memfws = HttpEmbeddingProvider(
        "http://127.0.0.1:1/v1/embeddings", model="bge-m3", dimension=1024
    )
    assert ours.fingerprint == memfws.fingerprint
    assert fingerprint_of(ours) == fingerprint_of(memfws)
    assert embedder_from_env({}) is None
    monkeypatch.setenv("MEMFW_EMBEDDING_URL", "http://127.0.0.1:1/v1/embeddings")
    monkeypatch.setenv("MEMFW_EMBEDDING_DIMENSION", "16")
    repository = _vault(tmp_path)
    provider = factory({"vault": str(repository.root)})
    assert isinstance(provider.embedder, EnvEmbedder) and provider.embedder.dimension == 16
    injected = WordsEmbedder()
    assert factory({"vault": str(repository.root), "embedder": injected}).embedder is injected


def _embedding_server() -> ThreadingHTTPServer:
    embedder = WordsEmbedder()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            del format, args

        def do_POST(self) -> None:  # noqa: N802
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            vectors = embedder.embed(body["input"])
            data = json.dumps(
                {"data": [{"index": i, "embedding": v} for i, v in enumerate(vectors)]}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_cli_sync_and_status_print_counts_only(
    tmp_path: Path,
    fake: FakeQdrant,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repository = _vault(tmp_path)
    args = ["--vault", str(repository.root), "--url", fake.url]
    assert cli_main(["sync", *args]) == 1  # no embedder configured
    server = _embedding_server()
    try:
        monkeypatch.setenv(
            "MEMFW_EMBEDDING_URL", f"http://127.0.0.1:{server.server_address[1]}/v1/embeddings"
        )
        monkeypatch.setenv("MEMFW_EMBEDDING_DIMENSION", "16")
        capsys.readouterr()
        assert cli_main(["status", *args]) == 1
        assert json.loads(capsys.readouterr().out) == {
            "available": False,
            "reason": "no_collection",
        }
        assert cli_main(["sync", *args]) == 0
        report = json.loads(capsys.readouterr().out)
        assert report["upserted"] == 5 and report["skipped"] == {"privacy": 3}
        assert cli_main(["status", *args]) == 0
        out = capsys.readouterr().out
        assert json.loads(out) == {"available": True, "reason": ""}
        assert "m-" not in out
        provider = factory({"vault": str(repository.root), "url": fake.url})
        assert _search(provider, "HAL9000 rack")[0] == "m-hal"
    finally:
        server.shutdown()
        server.server_close()
    assert cli_main(["sync", "--vault", str(repository.root), "--url", "http://example.org"]) == 2


def test_registry_spec_is_off_until_enabled(tmp_path: Path, fake: FakeQdrant) -> None:
    repository = _vault(tmp_path)
    registry = AdapterRegistry()
    registry.register(SPEC)
    assert SPEC.license == "Apache-2.0" and SPEC.version.startswith("1.20.")
    assert registry.load("qdrant", AdaptersConfig()).reason == "disabled"
    config = AdaptersConfig.from_mapping(
        {"enabled": ["qdrant"], "qdrant": {"vault": str(repository.root), "url": fake.url}}
    )
    status = registry.load("qdrant", config)
    assert not status.available and status.reason == "no_embedder"


def test_runtime_wires_qdrant_through_module_factory(tmp_path: Path, fake: FakeQdrant) -> None:
    """Through ``runtime.recall.adapters`` (the bm25s adapter PR adds the loader)."""

    pytest.importorskip("memfw.runtime.recall_adapters")
    from memfw.runtime import build_runtime

    repository = _vault(tmp_path)
    embedder = WordsEmbedder()
    sync(_provider(repository, fake, embedder))
    path = repository.root / "vault.yaml"
    entry = {"factory": "memfw_adapter_qdrant:factory", "options": {"url": fake.url}}
    path.write_text(
        path.read_text(encoding="utf-8")
        + "runtime:\n  recall:\n    adapters: "
        + json.dumps([entry])
        + "\n",
        encoding="utf-8",
    )
    runtime = build_runtime(repository.root, embedder=embedder, record_access=False)
    payload = runtime.search("homelab backup", limit=10, mode="two_stage")
    assert "qdrant" in payload["recall"]["providers"]
    assert "m-alpha" not in [row["id"] for row in payload["results"]]
    (loaded,) = runtime.recall_adapters(repository)
    assert loaded.provider.embedder is embedder  # the runtime's embedder, no new model


# --- swap drill: the same contract as the built-in FTS ---------------------------------------


@pytest.mark.parametrize("name", ["fts", "qdrant"])
def test_qdrant_passes_the_contract_fts_passes(tmp_path: Path, fake: FakeQdrant, name: str) -> None:
    repository = _vault(tmp_path)
    tenants = {doc.id: doc.tenant for doc in repository.list()}
    if name == "fts":
        provider: Any = FtsProvider(repository.root)
    else:
        provider = _provider(repository, fake)
        sync(provider)
    seen = check_recall_provider(
        provider,
        queries=QUERIES,
        limit=5,
        vault=repository.root,
        vault_exclude=(".memfw.lock",),
        tenant_of=lambda candidate: tenants.get(candidate.ref),
    )
    assert seen["HAL9000 rack"][0].ref == "m-hal"
    coordinator = Coordinator([provider], resolvers=default_resolvers(repository.root))
    policy = FilterPolicy()
    result = coordinator.search("homelab backup", limit=10, policy=policy)
    assert result.labels.providers == (name,) and not result.labels.degraded
    assert "m-alpha" not in result.ids
    assert {"m-priv", "m-local", "m-odd"}.isdisjoint(result.ids)
    alpha = coordinator.search("homelab backup", limit=10, policy=policy.with_tenant("alpha"))
    assert alpha.ids == ["m-alpha"]
