"""DeepSeek harness (``dsh``) session labels: model, project, origin, parent session.

Synthetic ``session.v4.jsonl.zstd`` logs only; key names mirror the real store.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import zstandard

from memfw.history.conversations import ConversationStore
from memfw.history.sources.deepseek import PARSER_VERSION, READER

T0 = 1_760_000_000_000  # epoch ms
CWD = "/srv/work/demo"


def _header(**extra: Any) -> dict[str, Any]:
    return {
        "type": "session",
        "version": 4,
        "id": "sess-0001",
        "cwd": CWD,
        "createdAt": T0,
        "delegationDepth": 0,
        "isSeeded": False,
        **extra,
    }


def _event(kind: str, seq: int, data: dict[str, Any]) -> dict[str, Any]:
    return {"type": kind, "seq": seq, "time": T0 + seq * 1000, "data": data}


def _user(seq: int, text: str, **source: Any) -> dict[str, Any]:
    source.setdefault("kind", "user")
    return _event(
        "user/message",
        seq,
        {
            "id": f"m{seq}",
            "role": "user",
            "content": [{"type": "text", "text": text}],
            "source": source,
        },
    )


def _client_prompt(seq: int, text: str) -> dict[str, Any]:
    return _user(seq, text, rpcId=f"rpc-{seq}", clientTimeZone="UTC")


def _assistant(seq: int, text: str) -> dict[str, Any]:
    message = {"id": f"m{seq}", "role": "assistant", "content": [{"type": "text", "text": text}]}
    return _event("assistant/message", seq, {"turn": 0, "step": 0, "message": message})


def _context(seq: int, model: str) -> dict[str, Any]:
    data = {"provider": "demo-provider", "model": model, "contextWindow": 1000}
    return _event("request/context", seq, data)


def _store(tmp_path: Path, events: list[dict[str, Any]]) -> Path:
    root = tmp_path / "sessions"
    path = root / "--home-user-work-demo--" / "session-sess-0001" / "session.v4.jsonl.zstd"
    path.parent.mkdir(parents=True)
    payload = b"".join(json.dumps(event).encode() + b"\n" for event in events)
    path.write_bytes(zstandard.ZstdCompressor().compress(payload))
    return root


def _records(tmp_path: Path, events: list[dict[str, Any]], after: int = -1) -> list[Any]:
    root = _store(tmp_path, events)
    (locator,) = list(READER.sessions(root))
    return list(READER.read(locator, after=after))


def test_parser_version_bumped() -> None:
    assert PARSER_VERSION == "2"


def test_model_from_request_context_and_selection(tmp_path: Path) -> None:
    records = _records(
        tmp_path,
        [
            _header(),
            _client_prompt(1, "hello"),
            _context(2, "demo-flash"),
            _assistant(3, "hi"),
            _event("model/selection", 4, {"provider": "demo-provider", "model": "demo-pro"}),
            _client_prompt(5, "again"),
            _assistant(6, "hi again"),
        ],
    )
    assert [r.model for r in records] == ["", "demo-flash", "demo-pro", "demo-pro"]


def test_subagent_model_from_descriptor(tmp_path: Path) -> None:
    records = _records(
        tmp_path,
        [
            _header(origin="subagent", delegationDepth=1, parentSession="sess-parent"),
            _event("subagent/descriptor", 1, {"mode": "continuable", "agentModel": "demo-child"}),
            _user(2, "do the sub task"),
            _assistant(3, "done"),
        ],
    )
    assert {r.model for r in records} == {"demo-child"}


def test_project_is_cwd_basename(tmp_path: Path) -> None:
    records = _records(tmp_path, [_header(), _client_prompt(1, "hello"), _assistant(2, "hi")])
    assert {r.project for r in records} == {"demo"}


def test_subagent_origin_and_parent(tmp_path: Path) -> None:
    records = _records(
        tmp_path,
        [
            _header(origin="subagent", delegationDepth=1, parentSession="sess-parent"),
            _user(1, "do the sub task"),
            _assistant(2, "done"),
        ],
    )
    assert {r.origin for r in records} == {"subagent"}
    assert {r.parent_session for r in records} == {"sess-parent"}


def test_seeded_fork_links_parent_but_is_not_subagent(tmp_path: Path) -> None:
    records = _records(
        tmp_path,
        [
            _header(isSeeded=True, parentSession="sess-original"),
            _client_prompt(1, "continue from the fork"),
            _assistant(2, "ok"),
        ],
    )
    assert {r.origin for r in records} == {""}
    assert {r.parent_session for r in records} == {"sess-original"}


def test_headless_prompt_without_rpc_is_automated(tmp_path: Path) -> None:
    records = _records(tmp_path, [_header(), _user(1, "run the tests"), _assistant(2, "ok")])
    assert {r.origin for r in records} == {"automated"}


def test_client_prompt_is_unlabelled(tmp_path: Path) -> None:
    records = _records(tmp_path, [_header(), _client_prompt(1, "hello"), _assistant(2, "hi")])
    assert {r.origin for r in records} == {""}


def test_goal_round_labels_later_records_on_incremental_read(tmp_path: Path) -> None:
    events = [
        _header(),
        _user(1, "continue toward the goal", kind="goal", goalId="g1", revision=1, round=1),
        _assistant(2, "working"),
        _user(3, "continue toward the goal", kind="goal", goalId="g1", revision=1, round=2),
        _assistant(4, "still working"),
    ]
    full = _records(tmp_path / "full", events)
    assert {r.origin for r in full} == {"automated"}
    later = _records(tmp_path / "later", events, after=full[1].offset)
    assert [r.origin for r in later] == ["automated", "automated"]
    assert {r.model for r in later} == {""} and {r.project for r in later} == {"demo"}


def test_goal_rounds_after_a_typed_prompt_stay_unlabelled(tmp_path: Path) -> None:
    events = [
        _header(),
        _client_prompt(1, "hello"),
        _assistant(2, "hi"),
        _user(3, "continue toward the goal", kind="goal", goalId="g1", revision=1, round=2),
        _assistant(4, "working"),
    ]
    assert {r.origin for r in _records(tmp_path, events)} == {""}


def test_schedule_prompt_is_automated(tmp_path: Path) -> None:
    records = _records(
        tmp_path, [_header(), _user(1, "nightly check", kind="schedule"), _assistant(2, "ok")]
    )
    assert {r.origin for r in records} == {"automated"}


def test_store_keeps_origin_model_project(tmp_path: Path) -> None:
    root = _store(
        tmp_path,
        [
            _header(origin="subagent", delegationDepth=1, parentSession="sess-parent"),
            _context(1, "demo-flash"),
            _user(2, "do the sub task"),
            _assistant(3, "done"),
        ],
    )
    store = ConversationStore(tmp_path / "store" / "conversations.sqlite")
    counts = store.ingest(READER, [root])
    assert counts.sessions_failed == 0
    turns = store.turns("deepseek", "sess-0001")
    assert turns
    assert {t["origin"] for t in turns} == {"subagent"}
    assert {t["parent_session"] for t in turns} == {"sess-parent"}
    assert {t["model"] for t in turns} == {"demo-flash"}
    assert {t["project"] for t in turns} == {"demo"}
