"""M3c: tag filters and the two-stage conversation search (share, CLI, MCP)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from memfw.cli import app as cli_app
from memfw.conversations import ConversationError, open_share
from memfw.conversations.two_stage import TWO_STAGE_NOTICE, tag_filters
from memfw.conversations.worker import WorkState, state_path
from memfw.mcp import schema
from memfw.mcp.server import Gateway
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

    def __init__(self) -> None:
        self.dimension = len(self.VOCAB)
        self.fingerprint = "word-embedder"
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
