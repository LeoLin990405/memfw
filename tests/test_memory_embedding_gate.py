"""Memory embedding locality gate: an off-host embedder never receives private memories.

``MEMFW_EMBEDDING_URL`` used to embed every memory, ``local-private``
included, wherever it pointed. Now an endpoint that is not a loopback literal
gets only ``public``/``shared`` memories unless the operator opts in
(``MEMFW_EMBEDDING_ALLOW_REMOTE`` or ``runtime.embedding.allow_remote``); the
search then reports ``semantic_partial``. A loopback endpoint is unchanged.
"""

from __future__ import annotations

import io
import json
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from memfw.core.repository import Repository
from memfw.index import MemoryIndex
from memfw.mcp.server import Gateway
from memfw.memory_hybrid import SEMANTIC_PARTIAL, MemoryHybridRetriever
from memfw.notes import HashEmbeddingProvider, HttpEmbeddingProvider
from memfw.notes import http as notes_http
from memfw.notes.http import embedder_sends_off_host
from memfw.runtime.settings import resolve_settings

pytest.importorskip("sqlite_vec")

DIM = 8
SECRET = "zebra vault keys stored offline"
REMOTE_URL = "https://embeddings.example.invalid/v1/embeddings"


def _vector(text: str) -> list[float]:
    return [float((len(text) + index) % 7) + 1.0 for index in range(DIM)]


def _answer(body: bytes) -> bytes:
    inputs = json.loads(body.decode("utf-8"))["input"]
    data = [{"index": i, "embedding": _vector(text)} for i, text in enumerate(inputs)]
    return json.dumps({"data": data}).encode("utf-8")


@pytest.fixture(autouse=True)
def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("MEMFW_EMBEDDING_ALLOW_REMOTE", raising=False)


LOOPBACK_URL = "http://127.0.0.1:8089/v1/embeddings"


class _Response(io.BytesIO):
    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def _fake_endpoint(monkeypatch: pytest.MonkeyPatch, url: str) -> list[str]:
    """A fake embedding endpoint at the transport (no socket): every text it was sent."""

    sent: list[str] = []

    def fake_open(request: urllib.request.Request, *, timeout: float) -> _Response:
        assert request.full_url == url
        body = request.data
        assert isinstance(body, bytes)
        sent.extend(json.loads(body.decode("utf-8"))["input"])
        return _Response(_answer(body))

    monkeypatch.setattr(notes_http, "open_http", fake_open)
    return sent


@pytest.fixture
def remote(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    return _fake_endpoint(monkeypatch, REMOTE_URL)


@pytest.fixture
def loopback(monkeypatch: pytest.MonkeyPatch) -> tuple[str, list[str]]:
    return LOOPBACK_URL, _fake_endpoint(monkeypatch, LOOPBACK_URL)


def _vault(tmp_path: Path, *, allow_remote: bool | None = None) -> Repository:
    repository = Repository(tmp_path / "vault")
    repository.init()
    config = repository.root / "vault.yaml"
    extra = "legacy_privacy_default: public\n"
    if allow_remote is not None:
        extra += f"runtime:\n  embedding:\n    allow_remote: {str(allow_remote).lower()}\n"
    config.write_text(config.read_text(encoding="utf-8") + extra, encoding="utf-8")
    add = repository.add
    add("Zebra public", "zebra vault public runbook", memory_id="m-public")
    add("Zebra shared", "zebra vault shared notes", memory_id="m-shared", privacy="shared")
    add("Zebra private", SECRET, memory_id="m-private", tags=["privacy:local-private"])
    add(
        "Zebra tagged",
        "zebra vault keys tagged copy",
        memory_id="m-tag",
        tags=["sensitivity:private"],
    )
    add("Zebra odd", "zebra vault odd level copy", memory_id="m-odd", tags=["privacy:secret"])
    MemoryIndex(repository.index_path).rebuild(repository)
    return repository


def _embedder(url: str) -> HttpEmbeddingProvider:
    return HttpEmbeddingProvider(url, model="test", dimension=DIM, retries=0)


def _gateway(repository: Repository, embedder: Any) -> Gateway:
    return Gateway(repository.root, embedder=embedder, entity_enabled=False, allowed_sources=())


PRIVATE_WORDS = ("offline", "tagged copy", "odd level")


def test_off_host_detection() -> None:
    assert embedder_sends_off_host(_embedder(REMOTE_URL))
    assert embedder_sends_off_host(_embedder("http://10.0.0.5:8080/v1"))
    assert embedder_sends_off_host(_embedder("http://localhost.example.com/v1"))
    assert not embedder_sends_off_host(_embedder("http://127.0.0.1:8080/v1"))
    assert not embedder_sends_off_host(_embedder("http://localhost:8080/v1"))
    assert not embedder_sends_off_host(_embedder("http://[::1]:8080/v1"))
    assert not embedder_sends_off_host(HashEmbeddingProvider(dimension=DIM))  # in-process


@pytest.mark.parametrize("mode", ["hybrid", "vector"])
def test_remote_endpoint_never_receives_private_memories(
    tmp_path: Path, remote: list[str], mode: str
) -> None:
    repository = _vault(tmp_path)
    payload = _gateway(repository, _embedder(REMOTE_URL)).search("zebra vault", limit=10, mode=mode)
    sent = "\n".join(remote)
    assert "public runbook" in sent and "shared notes" in sent
    for word in PRIVATE_WORDS:
        assert word not in sent, word
    assert payload["degraded"] is True and SEMANTIC_PARTIAL in payload["errors"]


def test_opt_in_embeds_everything(tmp_path: Path, remote: list[str]) -> None:
    repository = _vault(tmp_path, allow_remote=True)
    payload = _gateway(repository, _embedder(REMOTE_URL)).search(
        "zebra vault", limit=10, mode="hybrid"
    )
    sent = "\n".join(remote)
    assert all(word in sent for word in PRIVATE_WORDS)
    assert SEMANTIC_PARTIAL not in payload["errors"]


def test_environment_opt_in_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEMFW_EMBEDDING_ALLOW_REMOTE", "1")
    assert resolve_settings().embedding_allow_remote is True
    monkeypatch.setenv("MEMFW_EMBEDDING_ALLOW_REMOTE", "0")
    assert resolve_settings().embedding_allow_remote is False
    monkeypatch.delenv("MEMFW_EMBEDDING_ALLOW_REMOTE")
    assert resolve_settings().embedding_allow_remote is None
    monkeypatch.setenv("MEMFW_EMBEDDING_ALLOW_REMOTE", "maybe")
    with pytest.raises(ValueError):
        resolve_settings()


def test_turning_the_gate_on_drops_vectors_already_stored(
    tmp_path: Path, remote: list[str]
) -> None:
    repository = _vault(tmp_path)
    embedder = _embedder(REMOTE_URL)
    opted_in = MemoryHybridRetriever(repository, embedder=embedder, embedding_allow_remote=True)
    opted_in.search("zebra vault", limit=10, mode="hybrid")
    assert opted_in.notes_index.unembedded_sections() == 0
    gated = MemoryHybridRetriever(repository, embedder=embedder)
    remote.clear()
    result = gated.search("zebra vault", limit=10, mode="hybrid")
    assert gated.notes_index.unembedded_sections() > 0  # private vectors removed
    assert SEMANTIC_PARTIAL in result["errors"]
    assert not any(word in "\n".join(remote) for word in PRIVATE_WORDS)
    # Opting in again re-embeds what was withheld.
    again = MemoryHybridRetriever(repository, embedder=embedder, embedding_allow_remote=True)
    again.search("zebra vault", limit=10, mode="hybrid")
    assert again.notes_index.unembedded_sections() == 0


def test_remember_suggestions_never_embed_a_private_candidate(
    tmp_path: Path, remote: list[str]
) -> None:
    repository = _vault(tmp_path)
    gateway = _gateway(repository, _embedder(REMOTE_URL))
    remote.clear()
    gateway.runtime.remember(
        "the offline recovery passphrase hint",
        "Recovery hint",
        tags=None,
        related=None,
        privacy="local-private",
    )
    assert not any("recovery" in text.lower() for text in remote)


def test_loopback_endpoint_is_unchanged(tmp_path: Path, loopback: tuple[str, list[str]]) -> None:
    url, sent = loopback
    repository = _vault(tmp_path)
    payload = _gateway(repository, _embedder(url)).search("zebra vault", limit=10, mode="hybrid")
    text = "\n".join(sent)
    assert payload["mode"] == "hybrid", payload["errors"]
    assert all(word in text for word in PRIVATE_WORDS)  # every memory, as before
    assert SEMANTIC_PARTIAL not in payload["errors"]
    manifest = json.loads((repository.root / ".memory-hybrid.manifest.json").read_text("utf-8"))
    assert manifest["labels"] == {"kind": "memory-vault"}
