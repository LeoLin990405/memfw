"""M3c: tag filters and the two-stage conversation search (share, CLI, MCP)."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from memfw.cli import app as cli_app
from memfw.conversations import ConversationError, open_share, two_stage
from memfw.conversations.share import ConversationShare
from memfw.conversations.two_stage import TWO_STAGE_NOTICE, embedder_is_local, tag_filters
from memfw.conversations.worker import WorkState, state_path
from memfw.mcp import schema
from memfw.mcp.server import Gateway
from memfw.notes import HttpEmbeddingProvider
from memfw.ports.models import TAG_OUTCOMES, TASK_TYPES
from tests.conversation_fixtures import (
    EMAIL,
    SECRET,
    isolated_home,  # noqa: F401  (fixture)
    make_vault,
    standard_rows,
    turn,
    write_store,
)

SHARE_ON = {"conversation_share": {"enabled": True}}
DAY = "2026-01-06T09:0"

EXTRA_ROWS = [
    turn("codex", "s-delta", 0, "user", "Investigate the slow database migration.", DAY + "0:00Z"),
    turn("codex", "s-delta", 1, "assistant", "The index rebuild blocks it.", DAY + "1:00Z"),
    turn("codex", "s-delta", 2, "assistant", "Filed the uploader follow-up.", DAY + "2:00Z"),
    turn("kimi", "s-eps", 0, "user", "Tune the cache eviction policy.", DAY + "3:00Z"),
]


def _tags(**values: Any) -> str:
    base = {"project": [], "task_type": "other", "components": [], "intent": "", "outcome": ""}
    base.update(values)
    base["outcome"] = base["outcome"] or "unknown"
    return json.dumps(base, sort_keys=True)


LABELS = [
    (
        "codex",
        "s-alpha",
        "Added exponential backoff retries to the uploader.",
        _tags(
            project=["uploader"],
            task_type="dev",
            components=["retry"],
            intent="add a retry wrapper",
            outcome="done",
        ),
    ),
    (
        "codex",
        "s-delta",
        "Debugged a slow schema migration.",
        _tags(
            project=["memfw"],
            task_type="debug",
            components=["migrations"],
            intent="find why the migration is slow",
            outcome="blocked",
        ),
    ),
    ("kimi", "s-eps", "Cache tuning for the eviction policy.", None),
    (
        "cursor",
        "s-gamma",
        "Private schema migration notes.",
        _tags(project=["memfw"], task_type="debug", intent="private migration", outcome="done"),
    ),
]


class WordEmbedder:
    """Bag-of-words vectors over a tiny vocabulary; records every text it sees."""

    VOCAB = ("uploader", "retry", "migration", "schema", "cache", "private", "backoff")

    def __init__(self, endpoint: str = "http://127.0.0.1:9/v1/embeddings") -> None:
        self.dimension = len(self.VOCAB)
        self.fingerprint = f"word-embedder-{endpoint}"
        self.endpoint = endpoint
        self.seen: list[str] = []

    def embed(self, texts: list[str], *, role: str = "document") -> list[list[float]]:
        self.seen.extend(texts)
        return [[float(text.lower().count(word)) for word in self.VOCAB] for text in texts]


class BrokenEmbedder(WordEmbedder):
    def embed(self, texts: list[str], *, role: str = "document") -> list[list[float]]:
        raise RuntimeError("endpoint down")


def _write_labels(path: Path, labels: list[tuple[str, str, str, str | None]]) -> None:
    state = WorkState(path)
    with state.open() as connection:
        for app, session, summary, tags in labels:
            connection.execute(
                "INSERT OR REPLACE INTO summaries (app, session_id, chunk_start, chunk_end,"
                " prompt_version, model, summary, intent, outcome, created_at, tags)"
                " VALUES (?, ?, -1, 9, ?, 'fake', ?, '', '', '2026-01-07T00:00:00+00:00', ?)",
                (app, session, "conv-v2" if tags else "conv-v1", summary, tags),
            )


def _setup(
    tmp_path: Path, share: dict[str, Any] | None = None, *, labels: bool = True
) -> tuple[Path, Path]:
    vault = make_vault(tmp_path / "vault", share if share is not None else SHARE_ON)
    store = write_store(tmp_path / "data" / "conversations.sqlite", [*standard_rows(), *EXTRA_ROWS])
    if labels:
        _write_labels(state_path({}), LABELS)
    return vault, store


def _share(
    tmp_path: Path,
    caller: str = "claude",
    *,
    embedder: Any = None,
    labels: bool = True,
    **policy: Any,
) -> Any:
    vault, store = _setup(
        tmp_path, {"conversation_share": {"enabled": True, **policy}}, labels=labels
    )
    return open_share(vault, caller, store=store, embedder=embedder)


def _keys(result: dict[str, Any]) -> list[tuple[str, str]]:
    return [(item["app"], item["session_id"]) for item in result["sessions"]]


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for item in value.values() for s in _strings(item)]
    if isinstance(value, list):
        return [s for item in value for s in _strings(item)]
    return []


@pytest.fixture(autouse=True)
def _home(isolated_home: Path) -> Path:  # noqa: F811
    return isolated_home


@pytest.fixture(autouse=True)
def _fresh_cache() -> Any:
    two_stage._cache.clear()
    two_stage._cache_bytes = 0
    yield
    two_stage._cache.clear()
    two_stage._cache_bytes = 0


# -- filters ---------------------------------------------------------------------------


def test_tag_filters_normalise_and_validate() -> None:
    assert tag_filters(task_type=" DEBUG ", project="MemFW", intent_like="Slow  Thing") == {
        "task_type": "debug",
        "project": "memfw",
        "intent_like": "slow thing",
    }
    with pytest.raises(ConversationError, match="task_type"):
        tag_filters(task_type="hacking")
    with pytest.raises(ConversationError, match="outcome"):
        tag_filters(outcome="maybe")
    assert tag_filters() == {}


def test_schema_vocabulary_is_the_port_one() -> None:
    tool = next(item for item in schema.TOOLS if item["name"] == "conversations_search")
    properties = tool["inputSchema"]["properties"]
    assert properties["task_type"]["enum"] == list(TASK_TYPES)
    assert properties["outcome"]["enum"] == list(TAG_OUTCOMES)


def test_default_mode_tag_filters_keep_matching_tagged_sessions(tmp_path: Path) -> None:
    share = _share(tmp_path)
    plain = share.search("uploader")
    assert {item["session_id"] for item in plain["results"]} >= {"s-alpha", "s-beta", "s-delta"}
    debug = share.search("uploader", task_type="debug")
    assert {item["session_id"] for item in debug["results"]} == {"s-delta", "s-gamma"}
    dev = share.search("uploader", project="uploader", outcome="done")
    assert {item["session_id"] for item in dev["results"]} == {"s-alpha"}
    # s-beta has no tags: never matches a tag filter.
    assert share.search("uploader", intent_like="retry")["results"][0]["session_id"] == "s-alpha"
    assert share.search("uploader", component="nothing")["results"] == []
    assert set(debug) == set(plain)  # same shape as the default output


def test_default_mode_rejects_two_stage_only_options(tmp_path: Path) -> None:
    share = _share(tmp_path)
    with pytest.raises(ConversationError, match="mode=two_stage"):
        share.search("uploader", windows=1)
    with pytest.raises(ConversationError, match="mode must be"):
        share.search("uploader", mode="fancy")
    with pytest.raises(ConversationError, match="query is required"):
        share.search("", task_type="debug")


# -- two_stage ---------------------------------------------------------------------------


def test_two_stage_union_and_ranking(tmp_path: Path) -> None:
    embedder = WordEmbedder()
    share = _share(tmp_path, embedder=embedder)
    result = share.search("uploader", mode="two_stage")
    assert result["notice"] == TWO_STAGE_NOTICE and result["untrusted_model_output"] is True
    assert result["degraded"] == []
    first = result["sessions"][0]
    # In the keyword and the semantic list: ranked first.
    assert (first["app"], first["session_id"]) == ("codex", "s-alpha")
    assert first["matched_by"] == ["fts", "semantic"]
    assert first["model_output"]["untrusted_model_output"] is True
    assert first["model_output"]["tags"]["project"] == ["uploader"]
    assert first["model_output"]["summary"].startswith("Added exponential backoff")
    assert first["archived"] is False
    assert first["windows"] and first["windows"][0]["turns"]
    ranks = [item["rank"] for item in result["sessions"]]
    assert ranks == list(range(1, len(ranks) + 1))
    scores = [item["score"] for item in result["sessions"]]
    assert scores == sorted(scores, reverse=True)

    # Semantic only: no turn holds both terms, the summary does.
    semantic = share.search("schema migration", mode="two_stage")
    assert _keys(semantic)[0] == ("codex", "s-delta")
    assert semantic["sessions"][0]["matched_by"] == ["semantic"]
    assert semantic["candidates"]["fts"] == 0
    # Windows come from the session's own turns holding a query term.
    window_texts = [t["text"] for w in semantic["sessions"][0]["windows"] for t in w["turns"]]
    assert any("migration" in text for text in window_texts)


def test_two_stage_tag_filters_alone_and_combined(tmp_path: Path) -> None:
    share = _share(tmp_path, embedder=WordEmbedder(), apps={"cursor": {"readable": False}})
    # The acceptance call: no query, only filters.
    tagged = share.search("", mode="two_stage", task_type="debug", project="memfw")
    assert _keys(tagged) == [("codex", "s-delta")]
    assert tagged["sessions"][0]["matched_by"] == ["tags"]
    assert tagged["sessions"][0]["windows"] == []  # nothing to centre a window on
    assert tagged["filters"] == {"task_type": "debug", "project": "memfw"}
    # Filters apply to every list: the keyword hit in s-alpha is dropped.
    both = share.search("uploader", mode="two_stage", task_type="debug")
    assert _keys(both) == [("codex", "s-delta")]
    assert both["sessions"][0]["matched_by"] == ["fts", "tags"]
    # Untagged sessions never match a tag filter: s-eps holds "cache" but has no tags.
    done = share.search("cache", mode="two_stage", outcome="done")
    assert _keys(done) == [("codex", "s-alpha")] and done["sessions"][0]["matched_by"] == ["tags"]
    with pytest.raises(ConversationError, match="query or a tag filter"):
        share.search("", mode="two_stage")


def test_two_stage_since_app_and_archived_filters(tmp_path: Path) -> None:
    share = _share(tmp_path)
    late = share.search("uploader", mode="two_stage", since="2026-01-06")
    assert _keys(late) == [("codex", "s-delta")]
    only = share.search("uploader", mode="two_stage", app="claude")
    assert {key[0] for key in _keys(only)} == {"claude"}
    assert share.search("uploader", mode="two_stage", archived_only=True)["sessions"] == []
    with pytest.raises(ConversationError, match="since"):
        share.search("uploader", mode="two_stage", since="yesterday")


def test_two_stage_degrades_without_embedder(tmp_path: Path) -> None:
    share = _share(tmp_path)
    result = share.search("schema migration", mode="two_stage")
    assert result["degraded"] == ["no_embedder"]
    assert result["sessions"] == [] and result["candidates"]["semantic"] == 0
    keyword = share.search("uploader", mode="two_stage")
    assert keyword["degraded"] == ["no_embedder"]
    assert all(item["matched_by"] == ["fts"] for item in keyword["sessions"])
    broken = _share(tmp_path / "b", embedder=BrokenEmbedder())
    failed = broken.search("uploader", mode="two_stage")
    assert failed["degraded"] == ["embedder_error"] and failed["sessions"]


def test_two_stage_without_worker_state(tmp_path: Path) -> None:
    share = _share(tmp_path, labels=False, embedder=WordEmbedder())
    result = share.search("uploader", mode="two_stage", task_type="debug")
    assert result["sessions"] == [] and "no_summaries" in result["degraded"]
    plain = share.search("uploader", mode="two_stage")
    assert plain["sessions"] and all(item["model_output"] is None for item in plain["sessions"])


def test_two_stage_visibility_before_ranking(tmp_path: Path) -> None:
    embedder = WordEmbedder()
    share = _share(tmp_path, embedder=embedder, apps={"cursor": {"readable": False}})
    for query, filters in (
        ("private migration", {}),
        ("", {"task_type": "debug"}),
        ("uploader", {}),
        ("schema migration", {"project": "memfw"}),
    ):
        result = share.search(query, mode="two_stage", **filters)
        assert "cursor" not in {key[0] for key in _keys(result)}
        assert "s-gamma" not in json.dumps(result)
        assert "Private" not in json.dumps(result)
    # Counts are over visible sessions only.
    counted = share.search("", mode="two_stage", task_type="debug", project="memfw")
    assert counted["candidates"] == {"fts": 0, "semantic": 0, "tags": 1, "total": 1}
    # The hidden app's summary never reaches the embedder.
    assert not any("Private" in text for text in embedder.seen)
    # Opt-out hides as well; a can_read list narrows.
    narrow = _share(tmp_path / "n", "kimi", apps={"kimi": {"can_read": ["kimi"]}})
    assert {
        key[0] for key in _keys(narrow.search("", mode="two_stage", outcome="blocked"))
    } == set()
    assert {key[0] for key in _keys(narrow.search("cache", mode="two_stage"))} == {"kimi"}


def test_two_stage_output_is_redacted_and_bounded(tmp_path: Path) -> None:
    vault, store = _setup(tmp_path)
    many = [
        turn("codex", f"s-many-{index}", position, "user", "uploader " + "x" * 3000, DAY + "5:00Z")
        for index in range(30)
        for position in range(4)
    ]
    write_store(store, many)
    _write_labels(
        state_path({}),
        [("claude", "s-beta", f"Mailed {EMAIL} the uploader key {SECRET}", _tags(task_type="ops"))],
    )
    share = open_share(vault, "claude", store=store)
    result = share.search("uploader", mode="two_stage", limit=50, windows=5, window_turns=3)
    size = len(json.dumps(result, ensure_ascii=False).encode())
    assert size <= 16 * 1024 and result["truncated"] is True
    small = share.search("uploader", mode="two_stage", limit=50, max_bytes=100)
    assert len(json.dumps(small, ensure_ascii=False).encode()) <= 4096
    every = share.search("uploader", mode="two_stage", task_type="ops", max_bytes=65536)
    text = json.dumps(every)
    assert SECRET not in text and EMAIL not in text
    beta = every["sessions"][0]
    assert beta["model_output"] is None or beta["model_output"]["summary"] == ""
    for item in every["sessions"]:
        for window in item["windows"]:
            for row in window["turns"]:
                assert len(row["text"]) <= 1200


def test_two_stage_pages(tmp_path: Path) -> None:
    share = _share(tmp_path, embedder=WordEmbedder())
    full = share.search("uploader", mode="two_stage", limit=50)
    first = share.search("uploader", mode="two_stage", limit=1)
    assert first["next_offset"] == 1 and _keys(first) == _keys(full)[:1]
    second = share.search("uploader", mode="two_stage", limit=1, offset=1)
    assert _keys(second) == _keys(full)[1:2] and second["sessions"][0]["rank"] == 2


# -- review fixes ----------------------------------------------------------------------


class RemoteEmbedder(WordEmbedder):
    """A non-loopback endpoint: must never be called unless conversations.cloud is on."""

    def __init__(self) -> None:
        super().__init__("https://embeddings.example.invalid/v1/embeddings")
        self.calls = 0

    def embed(self, texts: list[str], *, role: str = "document") -> list[list[float]]:
        self.calls += 1
        return super().embed(texts, role=role)


def test_embedder_locality() -> None:
    assert embedder_is_local(WordEmbedder())
    assert embedder_is_local(WordEmbedder("http://localhost:8080/v1/embeddings"))
    assert embedder_is_local(WordEmbedder("http://[::1]:8080/v1"))
    assert embedder_is_local(WordEmbedder("http://127.1.2.3/v1"))
    assert embedder_is_local(WordEmbedder("http://[::ffff:127.0.0.1]:8080/v1"))
    assert not embedder_is_local(WordEmbedder("http://[::ffff:10.0.0.1]/v1"))
    assert not embedder_is_local(RemoteEmbedder())
    assert not embedder_is_local(WordEmbedder("http://192.0.2.10/v1"))
    assert not embedder_is_local(WordEmbedder("not a url"))
    assert not embedder_is_local(object())

    class Local:
        local = True

    assert embedder_is_local(Local())


def test_semantic_list_needs_loopback_or_cloud(tmp_path: Path) -> None:
    remote = RemoteEmbedder()
    off = _share(tmp_path, embedder=remote).search("schema migration", mode="two_stage")
    assert off["degraded"] == ["semantic_off"] and remote.calls == 0 and not remote.seen
    assert off["sessions"] == []
    vault, store = _setup(
        tmp_path / "c", {**SHARE_ON, "conversations": {"cloud": True}}, labels=False
    )
    on = open_share(vault, "claude", store=store, embedder=remote)
    found = on.search("schema migration", mode="two_stage")
    assert found["degraded"] == [] and remote.calls >= 1
    assert found["sessions"][0]["matched_by"] == ["semantic"]
    # A malformed switch is off (fail closed).
    vault, store = _setup(
        tmp_path / "m", {**SHARE_ON, "conversations": {"cloud": "yes"}}, labels=False
    )
    calls = remote.calls
    bad = open_share(vault, "claude", store=store, embedder=remote)
    assert bad.search("schema migration", mode="two_stage")["degraded"] == ["semantic_off"]
    assert remote.calls == calls


def test_http_embedder_to_a_remote_host_is_never_contacted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from memfw.notes import http as notes_http

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the embedding endpoint was contacted")

    monkeypatch.setattr(notes_http, "open_http", refuse)
    embedder = HttpEmbeddingProvider("http://192.0.2.10/v1/embeddings", model="m", dimension=4)
    result = _share(tmp_path, embedder=embedder).search("uploader", mode="two_stage")
    assert result["degraded"] == ["semantic_off"] and result["sessions"]


def test_embedder_budget_and_batch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class Slow(WordEmbedder):
        def embed(self, texts: list[str], *, role: str = "document") -> list[list[float]]:
            time.sleep(1.0)
            return super().embed(texts, role=role)

    monkeypatch.setattr(two_stage, "EMBED_BUDGET_SECONDS", 0.2)
    started = time.monotonic()
    slow = _share(tmp_path, embedder=Slow()).search("uploader", mode="two_stage")
    assert time.monotonic() - started < 0.9
    assert slow["degraded"] == ["embedder_error"] and slow["sessions"]

    bounded = two_stage._bounded(
        HttpEmbeddingProvider("http://127.0.0.1:9/v1", model="m", dimension=4, timeout=120)
    )
    assert bounded.retries == 0 and bounded.timeout <= 8.0

    monkeypatch.setattr(two_stage, "EMBED_BUDGET_SECONDS", 8.0)
    monkeypatch.setattr(two_stage, "EMBED_BATCH", 1)
    embedder = WordEmbedder()
    share = _share(tmp_path / "b", embedder=embedder)
    share.search("schema migration", mode="two_stage")
    first = [text for text in embedder.seen if text != "schema migration"]
    assert len(first) == 1  # one uncached document per call
    share.search("schema migration", mode="two_stage")
    second = [text for text in embedder.seen if text != "schema migration"]
    assert len(second) == 2 and second[0] != second[1]  # cached ones are not re-sent


def test_vector_cache_is_packed_and_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(two_stage, "CACHE_BYTES", 3 * 7 * 4)
    embedder = WordEmbedder()
    texts = [f"uploader retry {index}" for index in range(10)]
    vectors = two_stage._embed_documents(embedder, texts, deadline=time.monotonic() + 60)
    assert vectors[0] == [1.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0]
    assert len(two_stage._cache) == 3 and two_stage._cache_bytes <= 3 * 7 * 4
    assert all(isinstance(value, bytes) for value in two_stage._cache.values())


def test_window_scan_is_bounded_across_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(two_stage, "SCAN_BUDGET", 3)
    share = _share(tmp_path, embedder=WordEmbedder())
    read: list[int] = []
    original = share.source.turns

    def counted(*args: Any, **kwargs: Any) -> Any:
        rows = original(*args, **kwargs)
        if kwargs.get("start") == 0 and kwargs.get("limit", 0) <= 3:
            read.append(len(rows))
        return rows

    monkeypatch.setattr(share.source, "turns", counted)
    share.search("schema migration cache", mode="two_stage", limit=50)
    assert read and sum(read) <= 3


def test_first_session_too_big_still_pages(tmp_path: Path) -> None:
    wide = "长" * 400
    rows = [
        {**turn("codex", f"s-wide-{index}", 0, "user", "uploader", DAY + "7:00Z"), "cwd": wide}
        for index in range(3)
    ]
    vault = make_vault(tmp_path / "vault", SHARE_ON)
    store = write_store(tmp_path / "data" / "conversations.sqlite", rows)
    big = _tags(
        project=["项目" * 20 for _ in range(8)],
        components=["组件" * 20 for _ in range(8)],
        intent="意图" * 90,
        task_type="dev",
        outcome="done",
    )
    _write_labels(state_path({}), [("codex", f"s-wide-{i}", "总结" * 500, big) for i in range(3)])
    share = open_share(vault, "claude", store=store)
    offset: int | None = 0
    seen: list[str] = []
    for _ in range(10):
        assert offset is not None
        page = share.search("uploader", mode="two_stage", limit=1, offset=offset, max_bytes=4096)
        assert len(json.dumps(page, ensure_ascii=False).encode()) <= 4096
        assert page["next_offset"] is None or page["next_offset"] > offset
        seen += [item["session_id"] for item in page["sessions"]]
        offset = page["next_offset"]
        if offset is None:
            break
    assert offset is None and len(seen) == 3
    assert page["truncated"] is True


def test_labels_use_the_newest_summary(tmp_path: Path) -> None:
    share = _share(tmp_path)
    with WorkState(state_path({})).open() as connection:
        connection.execute(
            "INSERT INTO summaries (app, session_id, chunk_start, chunk_end, prompt_version,"
            " model, summary, intent, outcome, created_at) VALUES ('codex', 's-delta', -1, 12,"
            " 'conv-v1', 'fake', 'Newer untagged summary.', '', '', '2026-01-08T00:00:00+00:00')"
        )
    # The newest row is untagged: the older tagged one no longer matches.
    assert share.search("", mode="two_stage", outcome="blocked")["sessions"] == []
    assert share.search("uploader", outcome="blocked")["results"] == []
    plain = share.search("database", mode="two_stage")
    assert plain["sessions"][0]["model_output"]["summary"] == "Newer untagged summary."
    assert plain["sessions"][0]["model_output"]["tags"] is None


def test_default_filters_recheck_parsed_tags(tmp_path: Path) -> None:
    share = _share(tmp_path)
    broken = json.loads(_tags(task_type="debug", project=["memfw"], outcome="blocked"))
    broken["unexpected"] = "x"  # not the tag schema: parse_tags rejects it
    _write_labels(state_path({}), [("codex", "s-delta", "Odd row.", json.dumps(broken))])
    assert share.search("uploader", task_type="debug", project="memfw")["results"] == [
        item
        for item in share.search("uploader", task_type="debug", project="memfw")["results"]
        if item["session_id"] != "s-delta"
    ]
    assert "s-delta" not in json.dumps(share.search("uploader", outcome="blocked"))


def test_archived_is_null_when_unknown(tmp_path: Path) -> None:
    share = _share(tmp_path)

    class NoArchive:
        def __init__(self, inner: Any) -> None:
            self.inner = inner

        def __getattr__(self, name: str) -> Any:
            if name == "archive_state":
                raise AttributeError(name)
            return getattr(self.inner, name)

    blind = ConversationShare(NoArchive(share.source), share.policy, "claude", labels=share.labels)
    result = blind.search("uploader", mode="two_stage")
    assert result["sessions"] and all(item["archived"] is None for item in result["sessions"])
    with pytest.raises(ConversationError, match="archive state"):
        blind.search("uploader", mode="two_stage", archived_only=True)


# -- CLI and MCP ------------------------------------------------------------------------


def test_cli_two_stage_and_filters(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    vault, store = _setup(tmp_path)
    monkeypatch.delenv("MEMFW_EMBEDDING_URL", raising=False)
    base = ["conversations", "search", "--root", str(vault), "--store", str(store)]
    runner = CliRunner()
    result = runner.invoke(
        cli_app, [*base, "", "--mode", "two_stage", "--task-type", "debug", "--outcome", "blocked"]
    )
    assert result.exit_code == 0, result.output
    body = json.loads(result.output)
    assert [item["session_id"] for item in body["sessions"]] == ["s-delta"]
    assert body["degraded"] == ["no_embedder"]
    filtered = runner.invoke(cli_app, [*base, "uploader", "--outcome", "blocked"])
    assert [item["session_id"] for item in json.loads(filtered.output)["results"]] == ["s-delta"]
    bad = runner.invoke(cli_app, [*base, "uploader", "--task-type", "nope"])
    assert bad.exit_code == 2 and "task_type" in bad.output


def _call(gateway: Gateway, arguments: dict[str, Any], capsys: Any) -> dict[str, Any]:
    gateway.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "conversations_search", "arguments": arguments},
        }
    )
    return dict(json.loads(capsys.readouterr().out)["result"])


def test_mcp_passes_the_options_through(
    tmp_path: Path, capsys: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, store = _setup(tmp_path)
    make_vault(vault, {"conversation_store": {"path": str(store)}, **SHARE_ON})
    monkeypatch.setenv("MEMFW_APP", "kimi")
    embedder = WordEmbedder()
    gateway = Gateway(vault, embedder=embedder)
    found = _call(
        gateway,
        {"query": "", "mode": "two_stage", "task_type": "debug", "project": "memfw"},
        capsys,
    )
    assert not found["isError"], found
    body = json.loads(found["content"][0]["text"])
    assert body["mode"] == "two_stage" and body["degraded"] == []
    assert ("codex", "s-delta") in {(i["app"], i["session_id"]) for i in body["sessions"]}
    semantic = json.loads(
        _call(gateway, {"query": "schema migration", "mode": "two_stage"}, capsys)["content"][0][
            "text"
        ]
    )
    assert semantic["sessions"][0]["matched_by"] == ["semantic"]
    plain = json.loads(_call(gateway, {"query": "uploader"}, capsys)["content"][0]["text"])
    assert "results" in plain and "mode" not in plain
    archived = json.loads(
        _call(gateway, {"query": "uploader", "with_archive_state": True}, capsys)["content"][0][
            "text"
        ]
    )
    assert all("archived" in item for item in archived["results"])
    bad = _call(gateway, {"query": "uploader", "outcome": "maybe"}, capsys)
    assert bad["isError"]


# -- default output byte-identical to the base ref ------------------------------------------


@pytest.mark.skipif(
    not os.environ.get("MEMFW_ARCH_BASE_REF"),
    reason="opt-in (slow): set MEMFW_ARCH_BASE_REF to compare against that ref",
)
def test_default_search_is_byte_identical_to_base_ref() -> None:
    from tests.harness import byte_identical

    if not byte_identical.git_available():
        pytest.skip("git is not available")
    variants = [
        {},
        {"labels": True},
        {"policy": {"apps": {"cursor": {"readable": False}}}, "labels": True},
        {"caller": "kimi", "labels": True},
    ]
    results = byte_identical.compare(
        "tests.harness.conversations_search_probe:search_default",
        variants,
        old_ref=os.environ["MEMFW_ARCH_BASE_REF"],
    )
    for result in results:
        assert result.identical, f"{result.payload}:\n{result.diff[:4000]}"
    assert "results" in results[0].new and "Retry wrapper" not in results[0].new


@pytest.mark.skipif(
    not os.environ.get("MEMFW_ARCH_BASE_REF"),
    reason="opt-in (slow): set MEMFW_ARCH_BASE_REF to compare against that ref",
)
def test_mcp_archive_flags_are_the_one_accepted_difference() -> None:
    """Before M3c the MCP tool ignored ``with_archive_state`` and ``archived_only``."""

    from tests.harness import byte_identical

    if not byte_identical.git_available():
        pytest.skip("git is not available")
    (result,) = byte_identical.compare(
        "tests.harness.conversations_search_probe:mcp_archive_flags",
        [{}],
        old_ref=os.environ["MEMFW_ARCH_BASE_REF"],
    )
    old, new = json.loads(result.old), json.loads(result.new)
    assert new["plain"] == old["plain"]
    if old["with_archive_state"] == old["plain"]:  # a base ref before M3c
        assert old["archived_only"] == old["plain"]
    plain = new["plain"]["results"]
    assert {item["session_id"] for item in plain} >= {"s-alpha", "s-beta"}
    flagged = new["with_archive_state"]["results"]
    assert [{k: v for k, v in item.items() if k != "archived"} for item in flagged] == plain
    assert {(item["session_id"], item["archived"]) for item in flagged} == {
        (item["session_id"], item["session_id"] == "s-alpha") for item in plain
    }
    only = new["archived_only"]["results"]
    assert only and {item["session_id"] for item in only} == {"s-alpha"}
