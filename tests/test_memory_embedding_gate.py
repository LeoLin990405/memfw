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
from memfw.notes.index import SEMANTIC_QUERY_WITHHELD
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
    query = "zebra vault querymarker"
    payload = _gateway(repository, _embedder(REMOTE_URL)).search(query, limit=10, mode=mode)
    sent = "\n".join(remote)
    assert "public runbook" in sent and "shared notes" in sent
    for word in PRIVATE_WORDS:
        assert word not in sent, word
    assert "querymarker" not in sent  # the query may be private: never sent either
    assert payload["mode"] == "fts" and payload["degraded"] is True
    assert SEMANTIC_QUERY_WITHHELD in payload["errors"]
    assert SEMANTIC_PARTIAL in payload["errors"]
    assert {row["id"] for row in payload["results"]} >= {"m-public", "m-private"}  # FTS


def test_opt_in_embeds_everything(tmp_path: Path, remote: list[str]) -> None:
    repository = _vault(tmp_path, allow_remote=True)
    payload = _gateway(repository, _embedder(REMOTE_URL)).search(
        "zebra vault", limit=10, mode="hybrid"
    )
    sent = "\n".join(remote)
    assert all(word in sent for word in PRIVATE_WORDS)
    assert "zebra vault" in remote  # the query too, opted in
    assert payload["mode"] == "hybrid"
    assert SEMANTIC_PARTIAL not in payload["errors"]
    assert SEMANTIC_QUERY_WITHHELD not in payload["errors"]


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
    assert SEMANTIC_PARTIAL in result["errors"] and SEMANTIC_QUERY_WITHHELD in result["errors"]
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


# --- #104 review: every other route to an off-host embedder ------------------------------


class Remote:
    """An off-host embedder that records every text and role (no transport)."""

    endpoint = REMOTE_URL
    dimension = DIM
    fingerprint = "remote:8"
    model = "m"

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def embed(self, texts: list[str], role: str = "document") -> list[list[float]]:
        self.sent.extend((role, text) for text in texts)
        return [_vector(text) for text in texts]


def test_reviewer_repro_query_text_is_not_sent(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    embedder = Remote()
    retriever = MemoryHybridRetriever(repository, embedder=embedder, entity_enabled=False)
    assert retriever.embedding_gated
    retriever.search("what are the " + SECRET, mode="hybrid")
    assert not any(SECRET in text for _role, text in embedder.sent)
    assert not [text for role, text in embedder.sent if role == "query"]


def test_memory_related_build_is_gated(tmp_path: Path) -> None:
    from memfw.related import RelatedIndex, shareable_item

    repository = _vault(tmp_path)
    index = RelatedIndex(tmp_path / "rel.sqlite")
    index.rebuild_memory(repository)
    embedder = Remote()
    index.build_semantic(embedder, admit=shareable_item(repository=repository))
    sent = "\n".join(text for _role, text in embedder.sent)
    assert "public runbook" in sent
    assert not any(word in sent for word in PRIVATE_WORDS)


def test_memory_related_build_cli_gate(tmp_path: Path, remote: list[str]) -> None:
    from typer.testing import CliRunner

    from memfw.cli import app

    repository = _vault(tmp_path)
    related = tmp_path / "rel.sqlite"
    args = ["memory-related-build", "--root", str(repository.root), "--related-db", str(related)]
    args += ["--embedding-url", REMOTE_URL, "--dimension", str(DIM)]
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, result.output
    assert not any(word in "\n".join(remote) for word in PRIVATE_WORDS)
    remote.clear()
    args[args.index(str(related))] = str(tmp_path / "rel-opted.sqlite")
    result = CliRunner().invoke(app, [*args, "--allow-remote"])
    assert result.exit_code == 0, result.output
    assert all(word in "\n".join(remote) for word in PRIVATE_WORDS)


def _notes_tree(tmp_path: Path) -> Path:
    root = tmp_path / "notes"
    root.mkdir()
    (root / "open.md").write_text(
        "---\nprivacy: public\n---\n# Open\nzebra public note text\n", encoding="utf-8"
    )
    (root / "secret.md").write_text(
        "---\nprivacy: local-private\n---\n# Secret\n" + SECRET + "\n", encoding="utf-8"
    )
    (root / "plain.md").write_text("# Plain\nzebra unlabelled note text\n", encoding="utf-8")
    return root


def test_notes_index_and_search_are_gated(tmp_path: Path) -> None:
    from memfw.notes import NotesIndex, NotesRetriever, RetrievalConfig

    root = _notes_tree(tmp_path)
    embedder = Remote()
    index = NotesIndex(tmp_path / "notes.sqlite", embedder=embedder)
    assert index.remote_gated
    index.index(root)
    sent = "\n".join(text for _role, text in embedder.sent)
    assert "zebra public note text" in sent
    assert SECRET not in sent and "unlabelled" not in sent  # no privacy: local-private
    embedder.sent.clear()
    result = NotesRetriever(index, config=RetrievalConfig(mode="hybrid")).search(
        "zebra " + SECRET, limit=5
    )
    assert embedder.sent == []
    assert SEMANTIC_QUERY_WITHHELD in result.errors
    opted = NotesIndex(tmp_path / "notes2.sqlite", embedder=embedder, allow_remote=True)
    opted.index(root)
    assert any(SECRET in text for _role, text in embedder.sent)


def test_notes_related_build_is_gated(tmp_path: Path) -> None:
    from memfw.notes import NotesIndex
    from memfw.related import RelatedIndex, shareable_item

    root = _notes_tree(tmp_path)
    db = tmp_path / "notes.sqlite"
    NotesIndex(db).index(root)
    index = RelatedIndex(tmp_path / "rel.sqlite")
    index.rebuild_notes(db)
    embedder = Remote()
    index.build_semantic(embedder, admit=shareable_item(notes_db=db))
    sent = "\n".join(text for _role, text in embedder.sent)
    assert "Open" in sent and "Secret" not in sent and "Plain" not in sent


def test_missing_manifest_still_purges_private_vectors_under_the_gate(
    tmp_path: Path, remote: list[str]
) -> None:
    repository = _vault(tmp_path)
    embedder = _embedder(REMOTE_URL)
    MemoryHybridRetriever(repository, embedder=embedder, embedding_allow_remote=True).search(
        "zebra vault", limit=10, mode="hybrid"
    )
    (repository.root / ".memory-hybrid.manifest.json").unlink()  # a pre-upgrade index
    gated = MemoryHybridRetriever(repository, embedder=embedder)
    gated.search("zebra vault", limit=10, mode="hybrid")
    assert gated.notes_index.unembedded_sections() > 0


def test_remember_suggestions_follow_the_callers_visibility(tmp_path: Path) -> None:
    """Reviewer repro: no private or cross-tenant suggestion; a withheld one is never logged."""

    from memfw.core.models import EventKind, Tier

    repository = Repository(tmp_path / "vault")
    repository.init()
    config = repository.root / "vault.yaml"
    config.write_text(
        config.read_text(encoding="utf-8") + "legacy_privacy_default: public\n", encoding="utf-8"
    )
    add = repository.add
    add(
        "Zebra vault keys alpha", "zebra vault keys alpha copy", memory_id="m-alpha", tenant="alpha"
    )
    add(
        "Zebra vault keys private",
        "zebra vault keys private copy",
        memory_id="m-alpha-priv",
        tenant="alpha",
        tier=Tier.COLD,
        tags=["sensitivity:private"],
    )
    add("Zebra vault keys other", "zebra vault keys other tenant", memory_id="m-other")
    MemoryIndex(repository.index_path).rebuild(repository)
    gateway = Gateway(
        repository.root,
        entity_enabled=False,
        allowed_sources=(),
        tenant="alpha",
        clearance=frozenset({"public", "shared"}),
    )
    out = gateway.remember("zebra vault keys new note", "zebra vault keys")
    ids = {item["memory_id"] for item in out["suggestions"]}
    assert ids == {"m-alpha"}
    logged = [e.memory_id for e in repository.events() if e.kind == EventKind.MEMORY_ACCESS]
    assert logged == ["m-alpha"]  # only the suggestion that was shown
    assert repository.get("m-alpha-priv").tier == Tier.COLD
