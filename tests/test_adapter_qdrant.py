"""memfw-adapter-qdrant: semantic recall from a Qdrant service, against fakes only.

The adapter lives in ``packages/adapters/memfw-adapter-qdrant`` and imports only
``memfw.ports``. No real Qdrant runs here: behaviour is checked against the
package's loopback ``FakeQdrant`` and the wire format against the recorded
v1.20.2 exchanges in ``tests/fixtures/qdrant_contract`` (replayed by a
server that answers exactly what was recorded).
"""

from __future__ import annotations

import ast
import hashlib
import json
import math
import re
import socket
import sys
import threading
import time
import urllib.request
from collections.abc import Iterator, Sequence
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

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

ROOT = Path(__file__).resolve().parents[1]
ADAPTER_SRC = ROOT / "packages" / "adapters" / "memfw-adapter-qdrant" / "src"
if str(ADAPTER_SRC) not in sys.path:  # a workspace member; CI installs only memfw[dev]
    sys.path.insert(0, str(ADAPTER_SRC))

from memfw_adapter_qdrant import SPEC, QdrantProvider, factory  # noqa: E402
from memfw_adapter_qdrant import embed as qembed  # noqa: E402
from memfw_adapter_qdrant import http as qhttp  # noqa: E402
from memfw_adapter_qdrant.cli import main as cli_main  # noqa: E402
from memfw_adapter_qdrant.embed import (  # noqa: E402
    EmbedderError,
    EnvEmbedder,
    embedder_from_env,
    fingerprint_of,
)
from memfw_adapter_qdrant.fake import FakeQdrant  # noqa: E402
from memfw_adapter_qdrant.sync import point_id, sync  # noqa: E402

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
        "MEMFW_EMBEDDING_TIMEOUT",
        "MEMFW_EMBEDDING_BATCH_SIZE",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))  # the sync lock


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


def vault_key_of(repository: Repository) -> str:
    from memfw_adapter_qdrant.vault import points_key

    return points_key(repository.root)


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
    assert body["with_payload"] == ["memory_id", "tenant", "vault", "embedder"]
    assert body["with_vector"] is False
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


def test_withdrawn_memories_are_deleted_before_anything_is_embedded(
    tmp_path: Path, fake: FakeQdrant
) -> None:
    repository = _vault(tmp_path)
    sync(_provider(repository, fake))
    repository.update("m-release", tags=["sensitivity:private"])
    (repository.root / "memories" / "m-hal.md").unlink()
    repository.update("m-backup", body="nightly homelab backup, now offsite too")

    class Broken(WordsEmbedder):
        def embed(self, texts: Sequence[str], *, role: str = "document") -> list[list[float]]:
            raise RuntimeError("embedding endpoint down")

    fake.requests.clear()
    with pytest.raises(RuntimeError):
        sync(_provider(repository, fake, Broken()))
    assert not {"m-release", "m-hal"} & _stored_ids(fake)  # gone although the run failed
    paths = [r["path"].rsplit("/", 1)[-1] for r in fake.requests if r["method"] != "GET"]
    assert "delete" in paths and "points" not in paths  # deleted; no upsert happened
    assert "m-backup" in _stored_ids(fake)  # the old version stays until a sync succeeds


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


@pytest.mark.parametrize(
    "text",
    [
        "legacy_privacy_default: public\n",
        "legacy_privacy_default: public\n  local-private\n",
        "x: [a,\nlegacy_privacy_default: public]\n",
        "legacy_privacy_default: public\nlegacy_privacy_default: local-private\n",
        "legacy_privacy_default: public\n---\nx: 1\n",
        "",
    ],
)
def test_legacy_default_is_read_exactly_as_memfw_reads_it(tmp_path: Path, text: str) -> None:
    from memfw_adapter_qdrant.vault import legacy_privacy

    from memfw.index.readonly import vault_legacy_privacy

    (tmp_path / "vault.yaml").write_text(text, encoding="utf-8")
    assert legacy_privacy(tmp_path) == vault_legacy_privacy(tmp_path), text


def test_a_remote_collection_is_keyed_by_an_explicit_vault_id(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    root = str(repository.root)
    remote = {"vault": root, "url": "https://qdrant.example:6333", "allow_remote": True}
    with pytest.raises(ValueError, match="needs vault_id"):
        factory(remote)
    with pytest.raises(ValueError, match="vault_id must be"):
        factory({**remote, "vault_id": "Not Valid"})
    one = factory({**remote, "vault_id": "homelab-main"})
    other_host = Repository(tmp_path / "elsewhere" / "vault")
    other_host.init()
    two = factory({**remote, "vault": str(other_host.root), "vault_id": "homelab-main"})
    assert one.vault_key == two.vault_key  # the same vault on two hosts: one key
    assert one.vault_key != factory({**remote, "vault_id": "other"}).vault_key
    # vault.yaml's own vault_id (a lower-case UUID, written by attachments) is the default.
    path = repository.root / "vault.yaml"
    vault_id = "0b9d1c4e-2f3a-4b5c-8d6e-7f8091a2b3c4"
    path.write_text(path.read_text(encoding="utf-8") + f"vault_id: {vault_id}\n", encoding="utf-8")
    assert factory(remote).settings.vault_id == vault_id
    local = factory({"vault": root, "url": "http://127.0.0.1:6333"})
    assert local.settings.vault_id == vault_id
    unset = factory({"vault": root, "url": "http://127.0.0.1:6333", "vault_id": ""})
    assert unset.settings.vault_id == "" and unset.vault_key != local.vault_key


def test_a_remote_api_key_needs_https_and_the_embedder_is_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _vault(tmp_path)
    remote = {
        "vault": str(repository.root),
        "allow_remote": True,
        "vault_id": "v1",
        "api_key_env": "QDRANT_TEST_KEY",
    }
    with pytest.raises(ValueError, match="https only"):
        factory({**remote, "url": "http://qdrant.example:6333"})
    assert factory({**remote, "url": "https://qdrant.example:6333"}).settings.api_key_env
    local = {"vault": str(repository.root), "api_key_env": "QDRANT_TEST_KEY"}
    assert factory({**local, "url": "http://127.0.0.1:6333"}).settings.api_key_env  # loopback
    for bad in ("file:///etc/passwd", "ftp://127.0.0.1/x", "127.0.0.1:9/v1"):
        with pytest.raises(ValueError, match="http"):
            EnvEmbedder(bad, model="m", dimension=4)


@pytest.mark.parametrize("chunked", [False, True], ids=["content-length", "chunked"])
def test_an_oversized_embedding_response_is_refused_as_too_large(chunked: bool) -> None:
    """Deterministic: refused by Content-Length up front, or by the running total."""

    from memfw_adapter_qdrant.embed import EmbedderError
    from memfw_adapter_qdrant.http import MAX_RESPONSE_BYTES

    big = _serve_big(MAX_RESPONSE_BYTES + (1 << 20), chunked=chunked)
    try:
        client = EnvEmbedder(
            f"http://127.0.0.1:{big.server_address[1]}/v1/embeddings", model="m", dimension=4
        )
        with pytest.raises(EmbedderError) as caught:
            client.embed(["q"])
        assert caught.value.code == "embedding_response_too_large"
    finally:
        big.shutdown()
        big.server_close()


def test_read_capped_is_exact_at_the_cap() -> None:
    import io

    from memfw_adapter_qdrant.http import ResponseTooLarge, read_capped

    class Body(io.BytesIO):
        def __init__(self, data: bytes, length: str | None) -> None:
            super().__init__(data)
            self.headers = {} if length is None else {"Content-Length": length}

    assert read_capped(Body(b"x" * 10, None), cap=10) == b"x" * 10
    assert read_capped(Body(b"x" * 10, "10"), cap=10) == b"x" * 10
    for data, length in ((b"x" * 11, None), (b"x" * 11, "5"), (b"", "11"), (b"x" * 11, "junk")):
        with pytest.raises(ResponseTooLarge):
            read_capped(Body(data, length), cap=10)
    assert not issubclass(ResponseTooLarge, OSError)  # never mapped to "unreachable"


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
    remote = factory(
        {
            "vault": root,
            "url": "https://qdrant.example:6333",
            "allow_remote": True,
            "vault_id": "v1",
        }
    )
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


class _QuietServer(ThreadingHTTPServer):
    """A test server that does not print a client hanging up early."""

    def handle_error(self, request: Any, client_address: Any) -> None:
        del request, client_address


def _serve_big(size: int, *, chunked: bool) -> ThreadingHTTPServer:
    """An embeddings endpoint whose answer is ``size`` bytes, with or without Content-Length."""

    payload = b'{"data": [{"index": 0, "embedding": [0, 0, 0, 0], "pad": "'
    tail = b'"}]}'
    pad = max(0, size - len(payload) - len(tail))

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            del format, args

        def do_POST(self) -> None:  # noqa: N802
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            try:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Connection", "close")
                if chunked:
                    self.send_header("Transfer-Encoding", "chunked")
                else:
                    self.send_header("Content-Length", str(len(payload) + pad + len(tail)))
                self.end_headers()
                pieces = [payload] + [b"x" * 65536] * (pad // 65536) + [b"x" * (pad % 65536), tail]
                for piece in pieces:
                    if not piece:
                        continue
                    if chunked:
                        self.wfile.write(f"{len(piece):x}\r\n".encode() + piece + b"\r\n")
                    else:
                        self.wfile.write(piece)
                if chunked:
                    self.wfile.write(b"0\r\n\r\n")
            except (BrokenPipeError, ConnectionResetError):
                pass  # the client refused the body part-way: expected

    server = _QuietServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _serve(status: int, body: Any, headers: dict[str, str] | None = None) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            del format, args

        def _reply(self) -> None:
            # Read the request body first: closing with unread input makes the
            # kernel send a reset, which the client would see as "unreachable".
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            data = json.dumps(body).encode()
            try:
                self.send_response(status)
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass  # the client stopped reading (a refused body): expected

        do_GET = do_POST = do_PUT = _reply  # noqa: N815

    server = _QuietServer(("127.0.0.1", 0), Handler)
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
    embedder = WordsEmbedder()
    mine = {"vault": vault_key_of(repository), "embedder": fingerprint_of(embedder)}
    for point in points:
        if isinstance(point, dict) and isinstance(point.get("payload"), dict):
            point["payload"].update(mine)
    points.insert(
        0,
        {
            "id": 8,
            "version": 1,
            "score": 0.99,
            "payload": {
                "memory_id": "m-hal",
                "tenant": "",
                "vault": "0" * 24,
                "embedder": mine["embedder"],
            },
        },
    )
    points.insert(
        0,
        {
            "id": 9,
            "version": 1,
            "score": 0.98,
            "payload": {
                "memory_id": "m-release",
                "tenant": "",
                "vault": mine["vault"],
                "embedder": "x",
            },
        },
    )
    server = _serve(200, {"result": {"points": points}, "status": "ok", "time": 0.001})
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        provider = factory({"vault": str(repository.root), "url": url, "embedder": embedder})
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
    """Through ``runtime.recall.adapters`` (the ``module:factory`` loader)."""

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


def test_a_tenant_change_moves_the_point_on_the_next_sync(tmp_path: Path, fake: FakeQdrant) -> None:
    repository = _vault(tmp_path)
    provider = _provider(repository, fake)
    sync(provider)
    assert "m-hal" in _search(provider, "HAL9000 rack")
    path = repository.root / "memories" / "m-hal.md"
    text = path.read_text(encoding="utf-8")
    assert "\ntenant: " not in text  # written without one: the empty tenant
    path.write_text(text.replace("\nid: m-hal\n", "\nid: m-hal\ntenant: beta\n", 1), "utf-8")
    report = sync(provider)
    assert report.upserted == 1
    assert "m-hal" not in _search(provider, "HAL9000 rack")
    assert _search(provider, "HAL9000 rack", tenant="beta") == ["m-hal"]


def test_adapter_imports_only_stdlib_yaml_and_the_ports() -> None:
    allowed_roots = set(sys.stdlib_module_names) | {"memfw_adapter_qdrant", "memfw_ports", "yaml"}
    for path in sorted((ADAPTER_SRC / "memfw_adapter_qdrant").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            elif isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            else:
                continue
            for name in names:
                ok = name.split(".")[0] in allowed_roots or name.startswith("memfw.ports.")
                assert ok, (path.name, name)
    kernel = ROOT / "src" / "memfw"
    for path in kernel.rglob("*.py"):
        assert "memfw_adapter_qdrant" not in path.read_text(encoding="utf-8"), path


def _serve_broken_chunked(kind: str) -> ThreadingHTTPServer:
    """A server whose chunked answer is malformed (bad size line) or cut short."""

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            del format, args

        def _reply(self) -> None:
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            try:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Transfer-Encoding", "chunked")
                self.send_header("Connection", "close")
                self.end_headers()
                if kind == "bad-size":
                    self.wfile.write(b"zz\r\n{}\r\n0\r\n\r\n")
                else:  # announces 64 bytes, sends 2, then closes
                    self.wfile.write(b"40\r\n{}")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

        do_GET = do_POST = do_PUT = _reply  # noqa: N815

    server = _QuietServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.mark.parametrize("kind", ["bad-size", "truncated"])
def test_a_broken_http_answer_is_unreachable_not_a_crash(tmp_path: Path, kind: str) -> None:
    from memfw_adapter_qdrant.embed import EmbedderError

    repository = _vault(tmp_path)
    server = _serve_broken_chunked(kind)
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        provider = factory({"vault": str(repository.root), "url": url, "embedder": WordsEmbedder()})
        with pytest.raises(qhttp.QdrantError) as caught:
            provider.client.call("POST", "/collections/memfw/points/query", {"limit": 1})
        assert caught.value.code == "unreachable"
        with pytest.raises(ProviderUnavailable, match="unreachable"):
            provider.search("homelab", limit=3, budget=RecallBudget(), tenant="", as_of=None)
        client = EnvEmbedder(f"{url}/v1/embeddings", model="m", dimension=4)
        with pytest.raises(EmbedderError) as broken:
            client.embed(["q"])
        assert broken.value.code == "embedding_unreachable"
    finally:
        server.shutdown()
        server.server_close()


# --- embedding timeout / batch size, incremental sync (slow CPU hosts) -------------------------


def _counting_embedding_server(
    sizes: list[int], *, per_text: float = 0.0, reject: str = "", status: int = 400
) -> ThreadingHTTPServer:
    """A loopback ``/v1/embeddings`` that records each request's size and sleeps per text."""

    embedder = WordsEmbedder()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            del format, args

        def do_POST(self) -> None:  # noqa: N802
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            sizes.append(len(body["input"]))
            if reject and any(reject in text for text in body["input"]):
                data = b'{"error": "input too long"}'
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            time.sleep(per_text * len(body["input"]))
            vectors = embedder.embed(body["input"])
            data = json.dumps(
                {"data": [{"index": i, "embedding": v} for i, v in enumerate(vectors)]}
            ).encode()
            try:
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except OSError:  # the client already gave up (timeout)
                pass

    server = _QuietServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _use_server(monkeypatch: pytest.MonkeyPatch, server: ThreadingHTTPServer) -> None:
    monkeypatch.setenv(
        "MEMFW_EMBEDDING_URL", f"http://127.0.0.1:{server.server_address[1]}/v1/embeddings"
    )
    monkeypatch.setenv("MEMFW_EMBEDDING_DIMENSION", "16")


def _bulk(repository: Repository, count: int) -> None:
    for index in range(count):
        repository.add(
            f"Bulk {index}", f"bulk note number {index}", memory_id=f"m-bulk-{index:03d}"
        )


def test_defaults_are_unchanged_30s_and_16_per_request(
    tmp_path: Path, fake: FakeQdrant, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _vault(tmp_path)
    _bulk(repository, 35)  # 5 + 35 = 40 sendable memories
    sizes: list[int] = []
    timeouts: list[float] = []
    real = qembed.open_url

    def spy(request: Any, timeout: float) -> Any:
        timeouts.append(timeout)
        return real(request, timeout)

    monkeypatch.setattr(qembed, "open_url", spy)
    server = _counting_embedding_server(sizes)
    try:
        _use_server(monkeypatch, server)
        provider = factory({"vault": str(repository.root), "url": fake.url})
        assert isinstance(provider.embedder, EnvEmbedder)
        assert (provider.embedder.timeout, provider.embedder.batch_size) == (30.0, 16)
        report = sync(provider)
    finally:
        server.shutdown()
        server.server_close()
    assert sizes == [16, 16, 8]  # exactly what one 64-memory embed call used to send
    assert set(timeouts) == {30.0}
    assert report.upserted == 40 and report.complete
    assert report.as_dict()["failed_requests"] == 0 and report.as_dict()["remaining"] == 0
    upserts = [r for r in fake.requests if r["path"] == "/collections/memfw/points"]
    assert [len(r["body"]["points"]) for r in upserts] == [16, 16, 8]


def test_a_slow_embedder_needs_batch_1_and_a_long_timeout(
    tmp_path: Path,
    fake: FakeQdrant,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The live bug in miniature: 0.4 s per memory, a timeout of 0.3 s."""

    repository = _vault(tmp_path)
    sizes: list[int] = []
    server = _counting_embedding_server(sizes, per_text=0.4)
    args = ["sync", "--vault", str(repository.root), "--url", fake.url]
    try:
        _use_server(monkeypatch, server)
        monkeypatch.setenv("MEMFW_EMBEDDING_TIMEOUT", "0.3")
        capsys.readouterr()
        assert cli_main(args) == 1
        captured = capsys.readouterr()
        failed = json.loads(captured.out)
        # The batch of 5 times out, then two of its memories alone: three in a row.
        assert sizes == [5, 1, 1]
        assert failed["failed_requests"] == 3 and failed["remaining"] == 5
        assert failed["failures"] == {"embedding_unreachable": 3} and failed["upserted"] == 0
        assert failed["stopped"] == "failures" and failed["rejected"] == 0
        assert "lower --embedding-batch-size" in captured.err
        sizes.clear()
        assert cli_main([*args, "--embedding-batch-size", "1", "--embedding-timeout", "5"]) == 0
        done = json.loads(capsys.readouterr().out)
        assert done["upserted"] == 5 and done["remaining"] == 0 and done["failed_requests"] == 0
        assert sizes == [1, 1, 1, 1, 1]
    finally:
        server.shutdown()
        server.server_close()
    assert _stored_ids(fake) == {"m-backup", "m-hal", "m-release", "m-shared", "m-alpha"}


class FlakyEmbedder(WordsEmbedder):
    """Fails (``embedding_unreachable``) on the given 1-based call numbers."""

    def __init__(self, failing: set[int] | None = None, *, always: bool = False) -> None:
        super().__init__()
        self.failing = failing or set()
        self.always = always
        self.calls = 0

    def embed(self, texts: Sequence[str], *, role: str = "document") -> list[list[float]]:
        self.calls += 1
        if self.always or self.calls in self.failing:
            raise EmbedderError("embedding_unreachable")
        return super().embed(texts, role=role)


def test_a_timed_out_batch_keeps_earlier_upserts_and_the_next_run_resumes(
    tmp_path: Path, fake: FakeQdrant
) -> None:
    repository = _vault(tmp_path)
    flaky = FlakyEmbedder({3})
    report = sync(_provider(repository, fake, flaky, embedding_batch_size=1))
    assert (report.embedded, report.upserted, report.failed_requests, report.remaining) == (
        4,
        4,
        1,
        1,
    )
    assert report.failures == {"embedding_unreachable": 1} and not report.stopped
    assert not report.complete
    assert len(_stored_ids(fake)) == 4  # what succeeded before and after the failure is kept
    again = WordsEmbedder()
    resumed = sync(_provider(repository, fake, again, embedding_batch_size=1))
    assert (resumed.upserted, resumed.unchanged, resumed.remaining) == (1, 4, 0)
    assert resumed.complete and len(again.seen) == 1  # only the missing memory is embedded
    assert len(_stored_ids(fake)) == 5


def test_three_failed_requests_in_a_row_stop_the_run(tmp_path: Path, fake: FakeQdrant) -> None:
    repository = _vault(tmp_path)
    down = FlakyEmbedder(always=True)
    report = sync(_provider(repository, fake, down, embedding_batch_size=1))
    assert down.calls == 3 and report.stopped == "failures"
    assert (report.failed_requests, report.remaining, report.upserted) == (3, 5, 0)


def test_max_seconds_stops_cleanly_between_batches(tmp_path: Path, fake: FakeQdrant) -> None:
    repository = _vault(tmp_path)
    ticks = iter(range(100))
    provider = _provider(repository, fake, embedding_batch_size=1)
    report = sync(provider, max_seconds=2.5, clock=lambda: float(next(ticks)))
    # start = 0; batches checked at 1 and 2 run; the check at 3 stops the run.
    assert (report.upserted, report.remaining, report.stopped) == (2, 3, "max_seconds")
    assert report.failed_requests == 0 and not report.complete
    finished = sync(_provider(repository, fake, embedding_batch_size=1), max_seconds=600)
    assert (finished.upserted, finished.unchanged) == (3, 2) and finished.complete
    for bad in (0, -1, 86401, float("nan"), True, "5"):
        with pytest.raises(ValueError):
            sync(provider, max_seconds=bad)  # type: ignore[arg-type]


def test_embedding_settings_are_validated_and_options_win_over_env(
    tmp_path: Path, fake: FakeQdrant, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _vault(tmp_path)
    base = {"vault": str(repository.root), "url": fake.url}
    for bad in (0, -1, 600.5, float("nan"), float("inf"), True, "30"):
        with pytest.raises(ValueError, match="embedding_timeout_seconds"):
            factory({**base, "embedding_timeout_seconds": bad})
    for bad in (0, 257, 1.5, True, "16"):
        with pytest.raises(ValueError, match="embedding_batch_size"):
            factory({**base, "embedding_batch_size": bad})
    with pytest.raises(ValueError):
        EnvEmbedder("http://127.0.0.1:1/v1/embeddings", model="m", dimension=4, batch_size=0)
    monkeypatch.setenv("MEMFW_EMBEDDING_URL", "http://127.0.0.1:1/v1/embeddings")
    for name, bad in (
        ("MEMFW_EMBEDDING_TIMEOUT", "abc"),
        ("MEMFW_EMBEDDING_TIMEOUT", "0"),
        ("MEMFW_EMBEDDING_TIMEOUT", "601"),
        ("MEMFW_EMBEDDING_BATCH_SIZE", "300"),
        ("MEMFW_EMBEDDING_BATCH_SIZE", "1.5"),
    ):
        monkeypatch.setenv(name, bad)
        with pytest.raises(ValueError, match=name):
            factory(base)
        monkeypatch.delenv(name)
    monkeypatch.setenv("MEMFW_EMBEDDING_TIMEOUT", "120")
    monkeypatch.setenv("MEMFW_EMBEDDING_BATCH_SIZE", "4")
    from_env = factory(base).embedder
    assert isinstance(from_env, EnvEmbedder)
    assert (from_env.timeout, from_env.batch_size) == (120.0, 4)
    chosen = factory({**base, "embedding_timeout_seconds": 600, "embedding_batch_size": 1})
    assert isinstance(chosen.embedder, EnvEmbedder)
    assert (chosen.embedder.timeout, chosen.embedder.batch_size) == (600.0, 1)
    edge = factory({**base, "embedding_batch_size": 256}).embedder
    assert isinstance(edge, EnvEmbedder) and edge.batch_size == 256
    # An injected (runtime) embedder is used as given, never modified.
    injected = WordsEmbedder()
    provider = factory({**base, "embedder": injected, "embedding_timeout_seconds": 5})
    assert provider.embedder is injected and not hasattr(injected, "timeout")
    args = ["sync", "--vault", str(repository.root), "--url", fake.url]
    assert cli_main([*args, "--embedding-batch-size", "0"]) == 2
    assert cli_main([*args, "--embedding-timeout", "0"]) == 2
    assert cli_main([*args, "--max-seconds", "0"]) == 2


def test_embedding_settings_do_not_change_the_fingerprint() -> None:
    url = "http://127.0.0.1:1/v1/embeddings"
    default = EnvEmbedder(url, model="bge-m3", dimension=1024)
    memfws = HttpEmbeddingProvider(url, model="bge-m3", dimension=1024)
    for timeout, batch in ((0.1, 1), (120.0, 1), (600.0, 256), (30.0, 16)):
        other = EnvEmbedder(url, model="bge-m3", dimension=1024, timeout=timeout, batch_size=batch)
        assert other.fingerprint == default.fingerprint == memfws.fingerprint
        assert fingerprint_of(other) == fingerprint_of(memfws)


def test_the_query_embedding_uses_the_same_configured_timeout(
    tmp_path: Path, fake: FakeQdrant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A search embeds its query with ``min(configured timeout, max(budget, 30 s))``."""

    repository = _vault(tmp_path)
    sizes: list[int] = []
    timeouts: list[float] = []
    real = qembed.open_url

    def spy(request: Any, timeout: float) -> Any:
        timeouts.append(timeout)
        return real(request, timeout)

    monkeypatch.setattr(qembed, "open_url", spy)
    server = _counting_embedding_server(sizes)
    try:
        _use_server(monkeypatch, server)
        provider = factory(
            {"vault": str(repository.root), "url": fake.url, "embedding_timeout_seconds": 90}
        )
        sync(provider)
        timeouts.clear()
        sizes.clear()
        assert _search(provider, "HAL9000 rack")[0] == "m-hal"  # default budget: 1.5 s
        wide = provider.search(
            "HAL9000 rack", limit=5, budget=RecallBudget(timeout_seconds=120), tenant="", as_of=None
        )
        assert wide[0].ref == "m-hal"
    finally:
        server.shutdown()
        server.server_close()
    # min(configured, max(budget, 30)): 30 s as before for the default budget, 90 s at most.
    assert timeouts == [30.0, 90.0] and sizes == [1, 1]


class PoisonEmbedder(WordsEmbedder):
    """Refuses (``embedding_rejected``) any request holding a text with ``POISON``."""

    def __init__(self) -> None:
        super().__init__()
        self.requests: list[int] = []

    def embed(self, texts: Sequence[str], *, role: str = "document") -> list[list[float]]:
        self.requests.append(len(texts))
        if any("POISON" in text for text in texts):
            raise EmbedderError("embedding_rejected")
        return super().embed(texts, role=role)


def test_one_rejected_memory_in_a_batch_of_16_lets_the_other_15_land(
    tmp_path: Path, fake: FakeQdrant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Through the real HTTP client: a 4xx is ``embedding_rejected``, the rest retried alone."""

    repository = _vault(tmp_path)
    _bulk(repository, 10)
    repository.add("Huge", "POISON too long for the model", memory_id="m-huge")  # 16 sendable
    sizes: list[int] = []
    server = _counting_embedding_server(sizes, reject="POISON", status=413)
    try:
        _use_server(monkeypatch, server)
        report = sync(factory({"vault": str(repository.root), "url": fake.url}))
    finally:
        server.shutdown()
        server.server_close()
    assert sizes == [16] + [1] * 16  # the batch, then each memory once on its own
    assert (report.upserted, report.remaining, report.rejected) == (15, 1, 1)
    assert report.failures == {"embedding_rejected": 2} and not report.stopped
    assert len(_stored_ids(fake)) == 15 and "m-huge" not in _stored_ids(fake)


def test_rejected_memories_at_the_head_never_stop_the_run(tmp_path: Path, fake: FakeQdrant) -> None:
    repository = _vault(tmp_path)
    for index in range(3):  # the shortest texts: first in line
        repository.add("P", f"POISON{index}", memory_id=f"m-bad-{index}")
    poison = PoisonEmbedder()
    report = sync(_provider(repository, fake, poison, embedding_batch_size=1))
    assert poison.requests == [1] * 8
    assert (report.upserted, report.rejected, report.remaining, report.stopped) == (5, 3, 3, "")
    assert _stored_ids(fake) == {"m-backup", "m-hal", "m-release", "m-shared", "m-alpha"}


def test_a_5xx_is_unreachable_and_a_4xx_is_rejected(tmp_path: Path) -> None:
    for status, code in (
        (400, "embedding_rejected"),
        (413, "embedding_rejected"),
        (422, "embedding_rejected"),
        (401, "embedding_refused"),
        (403, "embedding_refused"),
        (404, "embedding_refused"),
        (405, "embedding_refused"),
        (429, "embedding_refused"),
        (503, "embedding_unreachable"),
        (500, "embedding_unreachable"),
    ):
        server = _counting_embedding_server([], reject="x", status=status)
        try:
            client = EnvEmbedder(
                f"http://127.0.0.1:{server.server_address[1]}/v1/embeddings",
                model="m",
                dimension=16,
            )
            with pytest.raises(EmbedderError) as caught:
                client.embed(["x"])
            assert caught.value.code == code
        finally:
            server.shutdown()
            server.server_close()


def test_the_failure_stop_is_reported_even_with_nothing_after(
    tmp_path: Path, fake: FakeQdrant
) -> None:
    repository = _vault(tmp_path)
    sync(_provider(repository, fake))
    for memory_id in ("m-backup", "m-hal", "m-release"):
        repository.update(memory_id, body=f"changed {memory_id}")
    down = FlakyEmbedder(always=True)
    report = sync(_provider(repository, fake, down, embedding_batch_size=1))
    assert down.calls == 3 and report.unchanged == 2
    assert (report.failed_requests, report.remaining, report.stopped) == (3, 3, "failures")


def test_shortest_memories_are_embedded_first(tmp_path: Path, fake: FakeQdrant) -> None:
    repository = _vault(tmp_path)
    seen = WordsEmbedder()
    sync(_provider(repository, fake, seen, embedding_batch_size=1))
    assert [len(text) for text in seen.seen] == sorted(len(text) for text in seen.seen)


def test_a_second_sync_of_the_same_vault_exits_3(
    tmp_path: Path,
    fake: FakeQdrant,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from memfw_adapter_qdrant.cli import LOCKED, acquire_lock, lock_path

    repository = _vault(tmp_path)
    server = _counting_embedding_server([])
    args = ["sync", "--vault", str(repository.root), "--url", fake.url]
    try:
        _use_server(monkeypatch, server)
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
        provider = factory({"vault": str(repository.root), "url": fake.url})
        held = acquire_lock(provider)
        assert held is not None
        assert lock_path(provider).is_relative_to(tmp_path / "state")
        assert not lock_path(provider).is_relative_to(repository.root)
        assert cli_main(args) == LOCKED == 3
        assert "another sync" in capsys.readouterr().err
        held.close()
        assert cli_main(args) == 0  # released; and the lock is released after a run too
        assert cli_main(args) == 0
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize(
    ("status", "sizes", "stopped", "rejected"),
    [
        (404, [5, 1, 1], "failures", 0),  # a wrong URL path: stop, do not split the vault
        (429, [5, 1, 1], "failures", 0),  # rate limited: stop
        (413, [5, 1, 1, 1, 1, 1], "", 5),  # each input refused: skipped, never a stop
    ],
)
def test_only_400_413_422_are_per_memory_rejections(
    tmp_path: Path,
    fake: FakeQdrant,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    sizes: list[int],
    stopped: str,
    rejected: int,
) -> None:
    repository = _vault(tmp_path)
    seen: list[int] = []
    server = _counting_embedding_server(seen, reject="\n", status=status)  # every text
    try:
        _use_server(monkeypatch, server)
        report = sync(factory({"vault": str(repository.root), "url": fake.url}))
    finally:
        server.shutdown()
        server.server_close()
    assert seen == sizes
    assert (report.stopped, report.rejected, report.remaining) == (stopped, rejected, 5)
    assert report.failed_requests == len(sizes) and report.upserted == 0


def test_the_lock_ignores_a_relative_state_home_and_reports_enolck(
    tmp_path: Path,
    fake: FakeQdrant,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import errno
    import fcntl

    from memfw_adapter_qdrant.cli import lock_path

    repository = _vault(tmp_path)
    provider = factory(
        {"vault": str(repository.root), "url": fake.url, "embedder": WordsEmbedder()}
    )
    monkeypatch.setenv("XDG_STATE_HOME", "relative/state")
    assert lock_path(provider).is_relative_to(Path.home() / ".local" / "state")
    server = _counting_embedding_server([])

    def no_locks(fd: int, operation: int) -> None:
        raise OSError(errno.ENOLCK, "No locks available")

    try:
        _use_server(monkeypatch, server)
        monkeypatch.setattr(fcntl, "flock", no_locks)
        capsys.readouterr()
        assert cli_main(["sync", "--vault", str(repository.root), "--url", fake.url]) == 1
        assert "cannot take the sync lock (ENOLCK)" in capsys.readouterr().err
    finally:
        server.shutdown()
        server.server_close()
