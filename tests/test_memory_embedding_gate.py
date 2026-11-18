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


# --- #104 re-review: reranker, rewriter, stale stored frontmatter ------------------------


class RemoteReranker:
    endpoint = "https://rerank.example.invalid/v1/rerank"
    name = "http-rerank:x"

    def __init__(self) -> None:
        self.sent: list[tuple[str, list[str]]] = []

    def rerank(self, query: str, candidates: Any, *, top_k: int) -> list[Any]:
        self.sent.append((query, [hit.content for hit in candidates]))
        return list(candidates[:top_k])


class LocalReranker(RemoteReranker):
    endpoint = "http://127.0.0.1:8090/v1/rerank"


class RemoteRewriter:
    endpoint = "https://llm.example.invalid/v1/chat"

    def __init__(self) -> None:
        self.sent: list[str] = []

    def rewrite_query(self, query: str, *, limit: int) -> Any:
        self.sent.append(query)
        raise AssertionError("must not be called")


def _two_notes(tmp_path: Path) -> Path:
    root = tmp_path / "notes"
    root.mkdir()
    (root / "open.md").write_text("---\nprivacy: public\n---\n# Open\nzebra public\n")
    (root / "secret.md").write_text("---\nprivacy: local-private\n---\n# S\n" + SECRET + "\n")
    return root


def test_reranker_off_host_is_withheld(tmp_path: Path) -> None:
    from memfw.notes import NotesIndex, NotesRetriever, RetrievalConfig
    from memfw.notes.retrieval import RERANK_WITHHELD

    index = NotesIndex(tmp_path / "n.sqlite", embedder=Remote())
    index.index(_two_notes(tmp_path))
    reranker = RemoteReranker()
    result = NotesRetriever(
        index, config=RetrievalConfig(mode="hybrid", rerank_candidates=10), reranker=reranker
    ).search("zebra vault keys", limit=5)
    assert reranker.sent == []  # neither the query nor a passage
    assert RERANK_WITHHELD in result.errors and result.degraded
    local = LocalReranker()
    NotesRetriever(
        index, config=RetrievalConfig(mode="fts", rerank_candidates=10), reranker=local
    ).search("zebra vault keys", limit=5)
    assert local.sent  # a loopback reranker works as before
    opted = RemoteReranker()
    NotesRetriever(
        index,
        config=RetrievalConfig(mode="fts", rerank_candidates=10),
        reranker=opted,
        allow_remote=True,
    ).search("zebra vault keys", limit=5)
    assert opted.sent


def test_query_rewriter_off_host_is_withheld(tmp_path: Path) -> None:
    from memfw.notes import NotesIndex, NotesRetriever, RetrievalConfig
    from memfw.notes.retrieval import REWRITE_WITHHELD

    index = NotesIndex(tmp_path / "n.sqlite")
    index.index(_two_notes(tmp_path))
    rewriter = RemoteRewriter()
    result = NotesRetriever(
        index, config=RetrievalConfig(mode="fts", rewrite=True), language_model=rewriter
    ).search("zebra", limit=5)
    assert rewriter.sent == [] and REWRITE_WITHHELD in result.errors


def test_gateway_clearance_applies_before_the_reranker(tmp_path: Path) -> None:
    from memfw.notes import NotesIndex
    from memfw.notes.mcp import NotesGateway

    root = _two_notes(tmp_path)
    db = tmp_path / "n.sqlite"
    NotesIndex(db).index(root)
    reranker = LocalReranker()  # even a local reranker never sees a withheld note
    gateway = NotesGateway(root, db, embedder=None, reranker=reranker, clearance="public")
    out = gateway.call(
        "notes_search", {"query": "zebra vault keys", "rerank_candidates": 10, "mode": "fts"}
    )
    assert all("secret" not in row["path"] for row in out["results"])
    assert SECRET not in json.dumps(reranker.sent)


def test_frontmatter_stored_by_the_old_split_is_re_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An index written before the YAML-aware split: re-read before anything is embedded."""

    from memfw.notes import NotesIndex

    root = tmp_path / "notes"
    root.mkdir()
    (root / "b.md").write_text(
        "---\nprivacy: public\nnote: |\n  a\n  ---\nprivacy: local-private\n---\n# B\n"
        + SECRET
        + "\n"
    )
    db = tmp_path / "n.sqlite"
    _old_index(db, root, monkeypatch)
    embedder = Remote()
    NotesIndex(db, embedder=embedder).index(root)  # upgraded, remote, no opt-in
    assert not any(SECRET in text for _role, text in embedder.sent), embedder.sent


def test_related_index_refuses_stale_notes_frontmatter(tmp_path: Path) -> None:
    import sqlite3

    from memfw.notes import NotesIndex
    from memfw.related import shareable_item

    root = _two_notes(tmp_path)
    db = tmp_path / "n.sqlite"
    NotesIndex(db).index(root)
    admit = shareable_item(notes_db=db)
    assert admit("notes", "open.md", "open.md")
    with sqlite3.connect(db) as connection:
        connection.execute("DELETE FROM index_versions")
    assert not shareable_item(notes_db=db)("notes", "open.md", "open.md")


def test_build_semantic_defaults_to_nothing_off_host(tmp_path: Path) -> None:
    from memfw.related import RelatedIndex

    repository = _vault(tmp_path)
    index = RelatedIndex(tmp_path / "rel.sqlite")
    index.rebuild_memory(repository)
    embedder = Remote()
    index.build_semantic(embedder)  # no admit: an off-host embedder gets nothing
    assert embedder.sent == []


def test_memory_related_build_honours_the_vault_opt_in(tmp_path: Path, remote: list[str]) -> None:
    from typer.testing import CliRunner

    from memfw.cli import app

    repository = _vault(tmp_path, allow_remote=True)
    args = ["memory-related-build", "--root", str(repository.root)]
    args += ["--related-db", str(tmp_path / "rel.sqlite"), "--embedding-url", REMOTE_URL]
    result = CliRunner().invoke(app, [*args, "--dimension", str(DIM)])
    assert result.exit_code == 0, result.output
    assert all(word in "\n".join(remote) for word in PRIVATE_WORDS)


def test_notes_servers_take_allow_remote() -> None:
    import inspect

    from memfw.cli import notes_serve
    from memfw.notes import mcp

    assert "allow_remote" in inspect.signature(notes_serve).parameters
    assert "--allow-remote" in inspect.getsource(mcp.main)


STALE = (
    "---\nprivacy: public\nnote: |\n  a\n  ---\nprivacy: local-private\n---\n# B\n" + SECRET + "\n"
)


def _old_split(text: str) -> tuple[dict[str, Any], str, int]:
    """The split before the fix: the first ``---`` line closes the block."""

    import yaml

    lines = text.splitlines(keepends=True)
    end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    return dict(yaml.safe_load("".join(lines[1:end])) or {}), "".join(lines[end + 1 :]), end + 1


def _old_index(db: Path, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An index as the old version wrote it: old split, no ``index_versions`` table."""

    import sqlite3

    from memfw.notes import NotesIndex
    from memfw.notes import index as notes_index

    with monkeypatch.context() as patch:
        patch.setattr(notes_index, "split_frontmatter", _old_split)
        NotesIndex(db).index(root)
    with sqlite3.connect(db) as connection:
        connection.execute("DROP TABLE IF EXISTS index_versions")
        stored = connection.execute("SELECT frontmatter FROM notes WHERE path='b.md'").fetchone()
    if stored is not None:
        assert json.loads(stored[0]).get("privacy") == "public"  # the stale reading


def test_a_partial_run_over_an_old_index_embeds_only_what_it_re_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reviewer repro (round 3): ``--max-files`` must not backfill stale notes."""

    from memfw.notes import NotesIndex

    root = tmp_path / "notes"
    root.mkdir()
    (root / "a.md").write_text("---\nprivacy: public\n---\n# A\nopen text\n")
    (root / "b.md").write_text(STALE)
    db = tmp_path / "n.sqlite"
    _old_index(db, root, monkeypatch)
    embedder = Remote()
    NotesIndex(db, embedder=embedder).index(root, max_files=1)
    assert not any(SECRET in text for _role, text in embedder.sent), embedder.sent
    NotesIndex(db, embedder=embedder).index(root)  # the full re-read
    assert not any(SECRET in text for _role, text in embedder.sent), embedder.sent


def test_an_interrupted_re_read_leaves_the_version_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sqlite3

    from memfw.notes import NotesIndex
    from memfw.notes import index as notes_index
    from memfw.related import shareable_item

    root = tmp_path / "notes"
    root.mkdir()
    for number in range(5):
        (root / f"n{number}.md").write_text(f"---\nprivacy: public\n---\n# N{number}\nbody\n")
    (root / "z.md").write_text(STALE)
    db = tmp_path / "n.sqlite"
    _old_index(db, root, monkeypatch)
    real = notes_index.parse_markdown
    calls = {"n": 0}

    def interrupted(path: Path, base: Path) -> Any:
        calls["n"] += 1
        if calls["n"] == 3:
            raise KeyboardInterrupt
        return real(path, base)

    with monkeypatch.context() as patch:
        patch.setattr(notes_index, "parse_markdown", interrupted)
        with pytest.raises(KeyboardInterrupt):
            NotesIndex(db).index(root, commit_every=1)
    with sqlite3.connect(db) as connection:
        try:
            row = connection.execute("SELECT value FROM index_versions").fetchone()
        except sqlite3.Error:
            row = None
    assert row is None
    admit = shareable_item(notes_db=db)
    assert not admit("notes", "z.md", "z.md") and not admit("notes", "n0.md", "n0.md")


def test_gateway_all_withheld_reads_like_no_match(tmp_path: Path) -> None:
    """No oracle: every hit withheld and nothing matching give the same answer."""

    from memfw.notes import NotesIndex
    from memfw.notes.mcp import NotesGateway

    root = tmp_path / "notes"
    root.mkdir()
    (root / "s.md").write_text("---\nprivacy: local-private\n---\n# S\n" + SECRET + "\n")
    db = tmp_path / "n.sqlite"
    NotesIndex(db).index(root)
    gateway = NotesGateway(root, db, embedder=None, clearance="public")
    withheld = gateway.call("notes_search", {"query": "zebra vault", "mode": "hybrid"})
    nothing = gateway.call("notes_search", {"query": "quokka", "mode": "hybrid"})
    assert withheld["results"] == [] == nothing["results"]
    for key in ("errors", "degraded", "mode", "candidates"):
        assert withheld[key] == nothing[key], key
    assert "vector recall unavailable; used FTS" not in withheld["errors"]


def test_gateway_reranker_order_and_opt_in(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from memfw.notes import NotesIndex, NotesRetriever, RetrievalConfig
    from memfw.notes.mcp import NotesGateway

    root = tmp_path / "notes"
    root.mkdir()
    for number in range(4):
        text = f"---\nprivacy: public\n---\n# P{number}\nzebra {'zebra ' * number}\n"
        (root / f"p{number}.md").write_text(text)
    (root / "s.md").write_text("---\nprivacy: local-private\n---\n# S\n" + SECRET + "\n")
    db = tmp_path / "n.sqlite"
    NotesIndex(db).index(root)
    withheld = RemoteReranker()
    gateway = NotesGateway(root, db, embedder=None, reranker=withheld, clearance="public")
    out = gateway.call("notes_search", {"query": "zebra", "mode": "fts", "rerank_candidates": 10})
    assert withheld.sent == [] and "rerank_withheld" in out["errors"] and out["degraded"]
    opted = RemoteReranker()
    gateway = NotesGateway(
        root, db, embedder=None, reranker=opted, clearance="public", allow_remote=True
    )
    gateway.call("notes_search", {"query": "zebra", "mode": "fts", "rerank_candidates": 10})
    assert opted.sent and SECRET not in json.dumps(opted.sent)
    plain = NotesGateway(root, db, embedder=None).call(
        "notes_search", {"query": "zebra", "mode": "fts"}
    )
    direct = NotesRetriever(NotesIndex(db), config=RetrievalConfig(mode="fts")).search(
        "zebra", limit=10
    )
    assert [row["path"] for row in plain["results"]] == [hit.path for hit in direct.hits]
    monkeypatch.setenv("MEMFW_EMBEDDING_ALLOW_REMOTE", "1")
    by_env = RemoteReranker()
    NotesGateway(root, db, embedder=None, reranker=by_env).call(
        "notes_search", {"query": "zebra", "mode": "fts", "rerank_candidates": 10}
    )
    assert by_env.sent
