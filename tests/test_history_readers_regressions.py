"""Regressions for conversation readers: one bad field, file or link never loses a session.

Every fixture is synthetic and built under ``tmp_path``.
"""

from __future__ import annotations

import json
import os
import random
import sqlite3
import time
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

import pytest
import zstandard

from memfw.history.sources import (
    antigravity,
    claude,
    codex,
    cursor_ide,
    deepseek,
    gemini,
    glm,
    grok,
    kimi,
    minimax,
    opencode,
    stepfun,
)
from memfw.history.sources.base import (
    MAX_FILE_BYTES,
    MAX_LINE_BYTES,
    SessionTooLarge,
    inside,
    iter_jsonl,
    iter_lines,
)

BIG = "1" + "0" * 400
"""A JSON integer too large for ``float()``."""


def _big(template: str) -> str:
    """``template`` with ``BIG`` in place of ``@``."""

    return template.replace("@", BIG)


def _lines(path: Path, rows: Iterable[Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "".join((row if isinstance(row, str) else json.dumps(row)) + "\n" for row in rows)
    path.write_text(text, encoding="utf-8")
    return path


def _texts(reader: Any, root: Path) -> list[str]:
    return [r.text for loc in reader.sessions(root) for r in reader.read(loc)]


def _sparse(path: Path, size: int = MAX_FILE_BYTES + 1) -> Path:
    """A file whose size is over the limit, without writing the bytes."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        handle.truncate(size)
    return path


# -- 1. one malformed field ------------------------------------------------------


def _deepseek_turn(text: str, **extra: Any) -> dict[str, Any]:
    data = {"source": {"kind": "user", "rpcId": "r"}, "content": [{"type": "text", "text": text}]}
    return {"type": "user/message", "time": 1731300000000, "data": data, **extra}


@pytest.mark.parametrize(
    "bad",
    [
        {"type": ["user/message"], "data": {"content": "bad"}},
        {"type": {"x": 1}, "data": {"content": "/message"}},
        {"type": "user/message", "data": {"source": {"kind": ["x"]}, "content": "bad2"}},
        {"type": "model/selection", "data": {"model": ["m"]}},
    ],
)
def test_deepseek_unhashable_type_skips_only_that_line(tmp_path: Path, bad: dict[str, Any]) -> None:
    lines = [
        {"type": "session", "id": "abc", "cwd": "/w/proj"},
        _deepseek_turn("good turn 1"),
        bad,
        _deepseek_turn("good turn 2"),
    ]
    raw = ("\n".join(json.dumps(line) for line in lines) + "\n").encode()
    session = tmp_path / "enc" / "session-abc"
    session.mkdir(parents=True)
    (session / "session.v4.jsonl.zstd").write_bytes(zstandard.ZstdCompressor().compress(raw))
    texts = _texts(deepseek.READER, tmp_path)
    assert [t for t in texts if t.startswith("good")] == ["good turn 1", "good turn 2"]


def test_huge_integer_timestamps_do_not_abort_sessions(tmp_path: Path) -> None:
    def kimi_message(text: str, time_value: str) -> str:
        envelope = {"message": {"role": "user", "content": [{"type": "text", "text": text}]}}
        envelope["meta"] = {"source": "input"}
        line = {"type": "agent.message.appended", "time": "@", "message": envelope}
        return json.dumps(line).replace('"@"', time_value)

    kimi_root = tmp_path / "kimi"
    _lines(
        kimi_root / "wd" / "session_a" / "agents" / "main" / "wire.jsonl",
        [kimi_message("ok1", "1700000000"), kimi_message("bad", BIG), kimi_message("ok2", "1")],
    )
    assert _texts(kimi.READER, kimi_root) == ["ok1", "bad", "ok2"]

    step_root = tmp_path / "step"
    _lines(
        step_root / "cwd" / "1_a.jsonl",
        [
            '{"type":"session","id":"s1","cwd":"/x"}',
            '{"type":"message","timestamp":1,"message":{"role":"user","content":"ok1"}}',
            _big('{"type":"message","timestamp":@,"message":{"role":"user","content":"bad"}}'),
            '{"type":"message","message":{"role":"user","content":"ok2"}}',
        ],
    )
    assert _texts(stepfun.READER, step_root) == ["ok1", "bad", "ok2"]

    mm_root = tmp_path / "mm"
    _lines(
        mm_root / "2026" / "01" / "01" / "x-session_a" / "messages.jsonl",
        [
            '{"message":{"role":"user","content":"ok1","timestamp":1}}',
            _big('{"message":{"role":"user","content":"bad","timestamp":@}}'),
            '{"message":{"role":"user","content":"ok2","timestamp":2}}',
        ],
    )
    assert _texts(minimax.READER, mm_root) == ["ok1", "bad", "ok2"]

    grok_root = tmp_path / "grok"
    session = grok_root / "%2Fx" / "sid1"
    _lines(session / "chat_history.jsonl", ['{"type":"user","content":"ok1"}'])
    _lines(session / "events.jsonl", [_big('{"type":"turn_started","ts":@}')])
    (session / "summary.json").write_text(_big('{"created_at": @}'), encoding="utf-8")
    assert _texts(grok.READER, grok_root) == ["ok1"]

    ag_root = tmp_path / "ag"
    _lines(
        ag_root / "brain" / "c1" / ".system_generated" / "logs" / "transcript.jsonl",
        [_big('{"step_index":0,"type":"USER_INPUT","content":"ok1","created_at":@}')],
    )
    [record] = [
        r for loc in antigravity.READER.sessions(ag_root) for r in antigravity.READER.read(loc)
    ]
    assert record.text == "ok1" and record.timestamp == ""

    oc_root = tmp_path / "oc"
    oc_root.mkdir()
    db = sqlite3.connect(oc_root / "opencode.db")
    db.executescript(
        "create table session_v2(id,time_created,directory,project_id);"
        "create table session_message(session_id,type,seq,time_created,data);"
    )
    db.execute("insert into session_v2 values('s',1,'/x','p')")
    db.execute("insert into session_message values('s','user',0,1,?)", ('{"text":"ok1"}',))
    db.execute(
        "insert into session_message values('s','user',1,1,?)",
        (_big('{"text":"bad","time":{"created":@}}'),),
    )
    db.commit()
    db.close()
    assert _texts(opencode.READER, oc_root) == ["ok1", "bad"]


def test_list_valued_type_in_other_jsonl_readers(tmp_path: Path) -> None:
    kimi_root = tmp_path / "kimi"
    good = {
        "type": "agent.message.appended",
        "message": {"message": {"role": "user", "content": "ok"}, "meta": {}},
    }
    _lines(
        kimi_root / "wd" / "session_a" / "agents" / "main" / "wire.jsonl",
        [{"type": ["llm.request"]}, good],
    )
    assert _texts(kimi.READER, kimi_root) == ["ok"]

    grok_root = tmp_path / "grok"
    _lines(
        grok_root / "%2Fx" / "sid1" / "chat_history.jsonl",
        [{"type": ["system"], "content": "x"}, {"type": "user", "content": "ok"}],
    )
    assert _texts(grok.READER, grok_root) == ["ok"]

    step_root = tmp_path / "step"
    content = [{"type": ["thinking"], "text": "kept"}, {"type": "text", "text": "ok"}]
    _lines(
        step_root / "cwd" / "1_a.jsonl",
        [
            {"type": "session", "id": "s1"},
            {"type": "message", "message": {"role": "user", "content": content}},
        ],
    )
    [text] = _texts(stepfun.READER, step_root)
    assert "ok" in text


def test_antigravity_malformed_workspace_uri_keeps_the_root(tmp_path: Path) -> None:
    for conversation in ("c1", "c2"):
        _lines(
            tmp_path / "brain" / conversation / ".system_generated" / "logs" / "transcript.jsonl",
            [{"step_index": 0, "type": "USER_INPUT", "content": f"hi {conversation}"}],
        )
    db = sqlite3.connect(tmp_path / "conversation_summaries.db")
    db.execute("create table conversation_summaries(conversation_id, workspace_uris, project_id)")
    db.execute(
        "insert into conversation_summaries values('c1', ?, 'p')", ('["file://[broken/path"]',)
    )
    db.execute("insert into conversation_summaries values('c2', ?, 'p')", ('["file:///w/ok"]',))
    db.commit()
    db.close()
    locators = list(antigravity.READER.sessions(tmp_path))
    assert [loc.session_id for loc in locators] == ["c1", "c2"]
    assert locators[1].extra["cwd"] == "/w/ok"
    assert _texts(antigravity.READER, tmp_path) == ["hi c1", "hi c2"]


def _gemini_chat(chats: Path, name: str, extra: list[dict[str, Any]]) -> None:
    header = {"sessionId": "s1", "startTime": "2026-11-11T00:00:00Z", "kind": "main"}
    good = [
        {"id": "m1", "type": "user", "content": "hello", "timestamp": "2026-11-11T00:00:01Z"},
        {"id": "m2", "type": "gemini", "content": "hi there"},
    ]
    _lines(chats / name, [header, *good, *extra])


@pytest.mark.parametrize(
    ("extra", "expected"),
    [
        (
            [{"id": "m3", "type": "gemini", "content": "x", "toolCalls": 7}],
            ["hello", "hi there", "x"],
        ),
        ([{"$patch": {"id": "m2", "updates": 5}}], ["hello", "hi there"]),
        ([{"$patch": {"id": "m2", "removeIds": True}}], ["hello", "hi there"]),
        (
            [
                {"$patch": {"id": "m1", "updates": 1}},
                {"id": "m4", "type": "user", "content": "new"},
            ],
            ["hello", "hi there", "new"],
        ),
        (
            [
                {"$patch": {"removeIds": ["m2", ["bad"]]}},
                {"id": "m5", "type": "user", "content": "z"},
            ],
            ["hello", "z"],
        ),
    ],
)
def test_gemini_bad_record_degrades_per_line(
    tmp_path: Path, extra: list[dict[str, Any]], expected: list[str]
) -> None:
    chats = tmp_path / "proj" / "chats"
    _gemini_chat(chats, "session-a.jsonl", extra)
    assert _texts(gemini.READER, tmp_path) == expected


# -- 2. over-limit session files -------------------------------------------------


def test_gemini_snapshot_over_limit_raises(tmp_path: Path) -> None:
    _sparse(tmp_path / "proj" / "chats" / "session-1.json")
    [locator] = list(gemini.READER.sessions(tmp_path))
    with pytest.raises(SessionTooLarge):
        list(gemini.READER.read(locator))


def test_glm_json_snapshot_over_limit_is_listed_and_raises(tmp_path: Path) -> None:
    _sparse(tmp_path / "sess_big.json")
    [locator] = list(glm.READER.sessions(tmp_path))
    assert locator.session_id == "sess_big"
    with pytest.raises(SessionTooLarge):
        list(glm.READER.read(locator))


def test_deepseek_log_over_limit_is_listed_and_raises(tmp_path: Path) -> None:
    _sparse(tmp_path / "enc" / "session-abc" / "session.v4.jsonl.zstd")
    [locator] = list(deepseek.READER.sessions(tmp_path))
    assert locator.session_id == "abc"
    with pytest.raises(SessionTooLarge):
        list(deepseek.READER.read(locator))


def test_opencode_json_session_over_limit_raises(tmp_path: Path) -> None:
    _sparse(tmp_path / "storage" / "session" / "p" / "ses_1.json")
    [locator] = list(opencode.READER.sessions(tmp_path))
    with pytest.raises(SessionTooLarge):
        list(opencode.READER.read(locator))


# -- 3. iter_jsonl: BOM, offsets, linear time ------------------------------------


def test_bom_first_line_is_read(tmp_path: Path) -> None:
    bom = b"\xef\xbb\xbf"
    rollout = tmp_path / "codex" / "2026" / "11" / "11" / "rollout-x.jsonl"
    rollout.parent.mkdir(parents=True)
    meta: dict[str, Any] = {"timestamp": "2026-11-11T00:00:00Z", "type": "session_meta"}
    meta["payload"] = {"id": "real-id", "cwd": "/w/proj", "source": "exec"}
    message = {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]}
    item = {"timestamp": "2026-11-11T00:00:01Z", "type": "response_item", "payload": message}
    rollout.write_bytes(bom + (json.dumps(meta) + "\n" + json.dumps(item) + "\n").encode())
    [record] = [
        r for loc in codex.READER.sessions(tmp_path / "codex") for r in codex.READER.read(loc)
    ]
    assert record.session_id == "real-id" and record.cwd == "/w/proj"

    project = tmp_path / "claude" / "-w-proj"
    project.mkdir(parents=True)
    first = {"sessionId": "s", "uuid": "u1", "message": {"role": "user", "content": "first prompt"}}
    second = {"sessionId": "s", "uuid": "u2", "message": {"role": "assistant", "content": "answer"}}
    (project / "s.jsonl").write_bytes(
        bom + (json.dumps(first) + "\n" + json.dumps(second) + "\n").encode()
    )
    assert _texts(claude.READER, tmp_path / "claude") == ["first prompt", "answer"]

    assert [offset for offset, _ in iter_jsonl(project / "s.jsonl")] == [
        0,
        len(bom) + len(json.dumps(first)) + 1,
    ]


def test_iter_jsonl_offsets_are_byte_exact(tmp_path: Path) -> None:
    rows = [b'{"a":1}', b"", b"not json", b'{"b":"\xc3\xa9"}', b"[1]", b'{"c":3}']
    long_line = b'{"x":"' + b"y" * (MAX_LINE_BYTES + 10) + b'"}'
    data = b"\n".join([*rows[:3], long_line, *rows[3:]]) + b'\n{"partial":'
    path = tmp_path / "x.jsonl"
    path.write_bytes(data)
    got = list(iter_jsonl(path))
    expected: list[tuple[int, dict[str, Any]]] = []
    position = 0
    for line in data.split(b"\n")[:-1]:
        if len(line) <= MAX_LINE_BYTES:
            try:
                value = json.loads(line)
            except ValueError:
                value = None
            if isinstance(value, dict):
                expected.append((position, value))
        position += len(line) + 1
    assert got == expected
    assert [value for _, value in got] == [{"a": 1}, {"b": "é"}, {"c": 3}]
    for offset, value in got:
        assert json.loads(data[offset : data.index(b"\n", offset)]) == value
    assert [o for o, _ in iter_jsonl(path, after=got[0][0])] == [o for o, _ in got[1:]]


def _reference_lines(chunks: Iterable[bytes]) -> Iterator[tuple[int, bytes]]:
    """The previous (quadratic) splitter, kept as the behaviour reference."""

    buffer = b""
    offset = 0
    skipping = False
    for chunk in chunks:
        buffer += chunk
        while True:
            newline = buffer.find(b"\n")
            if newline < 0:
                if len(buffer) > MAX_LINE_BYTES:
                    skipping = True
                    offset += len(buffer)
                    buffer = b""
                break
            line, buffer = buffer[:newline], buffer[newline + 1 :]
            start = offset
            offset += newline + 1
            if skipping:
                skipping = False
                continue
            if len(line) > MAX_LINE_BYTES:
                continue
            yield start, line


def _chunked(data: bytes, sizes: Iterable[int]) -> Iterator[bytes]:
    position = 0
    for size in sizes:
        if position >= len(data):
            return
        yield data[position : position + size]
        position += size
    if position < len(data):
        yield data[position:]


def test_iter_lines_matches_reference_on_any_chunking() -> None:
    rng = random.Random(7)
    pieces = []
    for _ in range(40):
        length = rng.choice(
            [0, 1, 5, 100, MAX_LINE_BYTES, MAX_LINE_BYTES + 1, MAX_LINE_BYTES + 3000]
        )
        pieces.append(bytes([rng.choice(b"ab{}")]) * length)
    data = b"\n".join(pieces) + b"\ntrailing"
    for _ in range(5):
        sizes = [rng.choice([1, 7, 4096, 300_000, 1024 * 1024]) for _ in range(len(data))]
        assert list(iter_lines(_chunked(data, sizes))) == list(
            _reference_lines(_chunked(data, sizes))
        )


def test_iter_jsonl_is_linear_on_short_lines(tmp_path: Path) -> None:
    path = tmp_path / "many.jsonl"
    line = b'{"a":1}\n'
    path.write_bytes(line * (4 * 1024 * 1024 // len(line)))
    started = time.perf_counter()
    count = sum(1 for _ in iter_lines(_chunked(path.read_bytes(), [1024 * 1024] * 8)))
    lines_elapsed = time.perf_counter() - started
    started = time.perf_counter()
    assert sum(1 for _ in iter_jsonl(path)) == count == 524_288
    elapsed = time.perf_counter() - started
    # The quadratic splitter took minutes here; linear splitting takes well under a second.
    assert lines_elapsed < 5.0
    assert elapsed < 30.0


# -- 4. symlinked directories escaping the root -----------------------------------


def _cursor_ide_store(directory: Path) -> None:
    directory.mkdir(parents=True)
    db = sqlite3.connect(directory / "state.vscdb")
    db.execute("CREATE TABLE cursorDiskKV (key TEXT UNIQUE, value BLOB)")
    composer = {"fullConversationHeadersOnly": [{"bubbleId": "b1"}]}
    db.execute("INSERT INTO cursorDiskKV VALUES ('composerData:c1', ?)", (json.dumps(composer),))
    bubble = {"type": 1, "text": "store text"}
    db.execute("INSERT INTO cursorDiskKV VALUES ('bubbleId:c1:b1', ?)", (json.dumps(bubble),))
    db.commit()
    db.close()


def _glm_store(directory: Path) -> None:
    directory.mkdir(parents=True)
    db = sqlite3.connect(directory / "db.sqlite")
    db.executescript(
        "CREATE TABLE session(id, directory, parent_id, task_type, time_deleted, time_created);"
        "CREATE TABLE message(id, session_id, sequence, time_created, data);"
        "CREATE TABLE part(id, message_id, session_id, sequence, time_created, data);"
        "INSERT INTO session VALUES('s1','/w',NULL,'interactive',NULL,1);"
        "INSERT INTO message VALUES('m1','s1',0,1,'{\"role\":\"user\"}');"
        "INSERT INTO part VALUES('p1','m1','s1',0,1,'{\"type\":\"text\",\"text\":\"store text\"}');"
    )
    db.commit()
    db.close()


def test_symlinked_store_directories_outside_root_are_not_read(tmp_path: Path) -> None:
    inside_root = tmp_path / "plain"
    _cursor_ide_store(inside_root / "globalStorage")
    _glm_store(inside_root / "db")
    assert _texts(cursor_ide.READER, inside_root) == ["store text"]
    assert _texts(glm.READER, inside_root) == ["store text"]

    outside = tmp_path / "outside"
    _cursor_ide_store(outside / "gs")
    _glm_store(outside / "db")
    root = tmp_path / "root"
    root.mkdir()
    os.symlink(outside / "gs", root / "globalStorage")
    os.symlink(outside / "db", root / "db")
    assert list(cursor_ide.READER.sessions(root)) == []
    assert list(glm.READER.sessions(root)) == []
    # A locator built by hand (stale state) is refused as well.
    [ide_locator] = list(cursor_ide.READER.sessions(inside_root))
    escaped = type(ide_locator)(
        ide_locator.agent, root / "globalStorage" / "state.vscdb", ide_locator.key, "c1"
    )
    assert list(cursor_ide.READER.read(escaped)) == []
    [glm_locator] = list(glm.READER.sessions(inside_root))
    escaped = type(glm_locator)(
        glm_locator.agent, root / "db" / "db.sqlite", glm_locator.key, "s1", dict(glm_locator.extra)
    )
    assert list(glm.READER.read(escaped)) == []


def test_inside_helper(tmp_path: Path) -> None:
    (tmp_path / "root" / "a").mkdir(parents=True)
    (tmp_path / "out").mkdir()
    os.symlink(tmp_path / "out", tmp_path / "root" / "link")
    assert inside(tmp_path / "root", tmp_path / "root" / "a" / "x")
    assert inside(tmp_path / "root", tmp_path / "root")
    assert not inside(tmp_path / "root", tmp_path / "root" / "link" / "x")
    assert not inside(tmp_path / "root", tmp_path / "root" / ".." / "out")


# -- 5. antigravity offsets ------------------------------------------------------


def test_antigravity_unindexed_step_gets_its_own_offset(tmp_path: Path) -> None:
    logs = tmp_path / "brain" / "c1" / ".system_generated" / "logs"
    rows: list[dict[str, Any]] = [
        {"type": "USER_INPUT", "content": "before any index"},
        {"step_index": 0, "type": "USER_INPUT", "content": "first question"},
        {"step_index": 1, "type": "PLANNER_RESPONSE", "content": "real answer"},
        {"type": "PLANNER_RESPONSE", "content": "step without index"},
        {"type": "PLANNER_RESPONSE", "content": "another without index"},
        {"step_index": 2, "type": "USER_INPUT", "content": "second question"},
    ]
    _lines(logs / "transcript.jsonl", rows)
    _lines(logs / "transcript_full.jsonl", rows)
    [locator] = list(antigravity.READER.sessions(tmp_path))
    records = list(antigravity.READER.read(locator))
    assert [r.text for r in records] == [str(row["content"]) for row in rows]
    offsets = [r.offset for r in records]
    assert offsets == sorted(set(offsets)) and len(offsets) == len(rows)
    assert [r.turn for r in records] == list(range(len(rows)))
    later = list(antigravity.READER.read(locator, after=offsets[2]))
    assert [r.text for r in later] == [str(row["content"]) for row in rows[3:]]
