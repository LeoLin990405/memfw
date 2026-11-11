"""Review hardening for ``memfw history``: each test fails on the first version.

All fixtures are synthetic and live under ``tmp_path``; secret-shaped values
are assembled at runtime.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

import memfw.history.index as index_module
from memfw.core.repository import Repository
from memfw.history import (
    HistoryError,
    SourceRoot,
    TranscriptIndex,
    has_secret,
    redact,
)
from memfw.mcp.server import Gateway

PASSWORD = "hunter2" + "abcXYZ"
SK = "sk-" + "proj-" + "ABCDEFGHIJKLMNOPQRSTUVWX"


def _line(**fields: Any) -> str:
    return json.dumps(fields) + "\n"


def _index(tmp_path: Path, root: Path, **kwargs: Any) -> TranscriptIndex:
    return TranscriptIndex(tmp_path / "h.sqlite", [SourceRoot.create("r", root)], **kwargs)


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    root.mkdir()
    return root


def _everything(index: TranscriptIndex) -> str:
    """Every value the index can hand back: DB bytes, list, search and read output."""

    parts = [index.db.read_bytes().decode("utf-8", errors="replace")]
    listed = index.list()
    parts.append(json.dumps(listed))
    for session in listed["sessions"]:
        detail = index.list(session=session["session_id"])
        parts.append(json.dumps(detail))
        for entry in detail["entries"]:
            parts.append(json.dumps(index.read(entry["ref"], before=5, after=5)))
    return "\n".join(parts)


# -- 1. linear scanning ----


@pytest.mark.parametrize(
    "text",
    ["a." * 500_000, "." * 1_000_000, "a" * 1_000_000, "-" * 1_000_000, "token:\n" * 150_000],
)
def test_scan_is_linear_on_a_pathological_megabyte(text: str) -> None:
    started = time.perf_counter()
    has_secret(text)
    assert time.perf_counter() - started < 1.0


# -- 2. JSON-escaped secrets ---------------------------------------------------------------


def test_json_escaped_secret_in_tool_input_is_skipped(tmp_path: Path) -> None:
    root = _root(tmp_path)
    command = "curl -d '" + json.dumps({"password": PASSWORD}) + "' https://api.invalid"
    nested = json.dumps({"body": json.dumps({"api_key": PASSWORD})})
    (root / "s.jsonl").write_text(
        _line(
            type="assistant",
            sessionId="s",
            uuid="u1",
            message={
                "role": "assistant",
                "content": [{"type": "tool_use", "name": "Bash", "input": {"command": command}}],
            },
        )
        + _line(
            type="assistant",
            sessionId="s",
            uuid="u2",
            message={
                "role": "assistant",
                "content": [{"type": "tool_use", "name": "x", "input": {"payload": nested}}],
            },
        )
        + _line(type="user", sessionId="s", uuid="u3", message={"role": "user", "content": "ok"}),
        encoding="utf-8",
    )
    index = _index(tmp_path, root)
    assert index.refresh()["skipped_secret"] == 2
    assert PASSWORD not in _everything(index)


def test_tool_input_is_rendered_decoded(tmp_path: Path) -> None:
    root = _root(tmp_path)
    (root / "s.jsonl").write_text(
        _line(
            type="assistant",
            sessionId="s",
            uuid="u1",
            message={
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "name": "Bash", "input": {"command": 'echo "hi there"'}}
                ],
            },
        ),
        encoding="utf-8",
    )
    index = _index(tmp_path, root)
    index.refresh()
    snippet = index.list(session="s")["entries"][0]["snippet"]
    assert snippet == '[tool Bash] command: echo "hi there"'


# -- 3. metadata fields ----


def test_secret_in_metadata_field_skips_the_entry(tmp_path: Path) -> None:
    root = _root(tmp_path)
    escaped = "\\u0073" + SK[1:]
    (root / "meta.jsonl").write_text(
        '{"session_id": "'
        + escaped
        + '", "text": "hi there"}\n'
        + _line(session_id="fine", entry_id=SK, text="also hi")
        + _line(session_id="fine", text="plain entry"),
        encoding="utf-8",
    )
    index = _index(tmp_path, root)
    assert index.refresh()["skipped_secret"] == 2
    assert SK not in _everything(index)
    assert SK[3:] not in _everything(index)


def test_email_in_metadata_is_masked(tmp_path: Path) -> None:
    root = _root(tmp_path)
    email = "someone" + "@" + "example.org"
    (root / "m.jsonl").write_text(
        _line(
            session_id=email, worker_id=email, role=email, ts="2026-03-01T00:00:00Z", text="hello"
        )
        + _line(session_id=email, ts="2026-03-01T00:00:01Z", text="hello again"),
        encoding="utf-8",
    )
    index = _index(tmp_path, root)
    index.refresh()
    assert email not in _everything(index)
    assert index.search("hello")["results"][0]["session_id"] == "[REDACTED:email]"


# -- TOCTOU and root re-check ----------------------------------------------------------------


def test_directory_swapped_for_symlink_during_indexing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _root(tmp_path)
    (root / "sub").mkdir()
    (root / "sub" / "a.jsonl").write_text(_line(session_id="in", text="inside"), encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "a.jsonl").write_text(
        _line(session_id="OUT", text="OUTSIDE ROOT CONTENT"), encoding="utf-8"
    )
    original = TranscriptIndex._walk

    def racing(self: TranscriptIndex, source: SourceRoot) -> Iterator[Any]:
        for item in original(self, source):
            if item[0] == "sub/a.jsonl":
                # The walk has already lstat'ed the real file; swap its parent now.
                os.rename(root / "sub", tmp_path / "moved")
                os.symlink(outside, root / "sub")
            yield item

    monkeypatch.setattr(TranscriptIndex, "_walk", racing)
    index = _index(tmp_path, root)
    index.refresh()
    assert "OUTSIDE" not in index.db.read_bytes().decode("utf-8", errors="replace")


def test_root_that_becomes_a_symlink_is_refused_on_refresh(tmp_path: Path) -> None:
    root = _root(tmp_path)
    (root / "x.jsonl").write_text(_line(session_id="in", text="hello inside"), encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "y.jsonl").write_text(_line(session_id="OUT", text="outside two"), encoding="utf-8")
    index = _index(tmp_path, root)
    index.refresh()
    shutil.rmtree(root)
    os.symlink(outside, root)
    counts = index.refresh()
    assert counts["sources_refused"] == 1
    assert index.search("outside")["results"] == []
    assert index.list()["sessions"] == []


def test_read_bounds_bytes_per_row(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _root(tmp_path)
    (root / "s.jsonl").write_text(_line(session_id="s", text="word " * 100), encoding="utf-8")
    index = _index(tmp_path, root)
    index.refresh()
    ref = index.list(session="s")["entries"][0]["ref"]
    # With a smaller per-row bound, read must not read the whole stored length.
    monkeypatch.setattr(index_module, "MAX_LINE_BYTES", 64)
    with pytest.raises(HistoryError, match="changed since indexing"):
        index.read(ref)


# -- incremental append, refresh interval, lock ----


def test_append_resumes_from_last_complete_line(tmp_path: Path) -> None:
    root = _root(tmp_path)
    path = root / "live.jsonl"
    path.write_text(
        _line(session_id="live", text="first line") + '{"session_id": "live", "text": "second li',
        encoding="utf-8",
    )
    index = _index(tmp_path, root)
    assert index.refresh()["entries_indexed"] == 1
    with path.open("a", encoding="utf-8") as handle:
        handle.write('ne complete"}\n' + _line(session_id="live", text="third line"))
    counts = index.refresh()
    assert counts["files_resumed"] == 1
    assert counts["entries_indexed"] == 2
    snippets = [entry["snippet"] for entry in index.list(session="live")["entries"]]
    assert snippets == ["first line", "second line complete", "third line"]
    lines = [entry["line"] for entry in index.list(session="live")["entries"]]
    assert lines == [1, 2, 3]


def test_rewritten_file_is_fully_reindexed(tmp_path: Path) -> None:
    root = _root(tmp_path)
    path = root / "f.jsonl"
    path.write_text(_line(session_id="f", text="original words"), encoding="utf-8")
    index = _index(tmp_path, root)
    index.refresh()
    path.write_text(
        _line(session_id="f", text="replaced words") + _line(session_id="f", text="more"),
        encoding="utf-8",
    )
    counts = index.refresh()
    assert counts["files_indexed"] == 1 and counts["files_resumed"] == 0
    assert index.search("original")["results"] == []


def test_min_refresh_interval_and_concurrent_refresh(tmp_path: Path) -> None:
    root = _root(tmp_path)
    for number in range(5):
        (root / f"{number}.jsonl").write_text(
            _line(session_id=f"s{number}", text=f"entry number {number}"), encoding="utf-8"
        )
    index = _index(tmp_path, root)
    errors: list[BaseException] = []

    def run() -> None:
        try:
            _index(tmp_path, root).refresh()
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert index.stats()["entries"] == 5
    (root / "9.jsonl").write_text(_line(session_id="s9", text="late entry"), encoding="utf-8")
    assert index.refresh(min_interval=3600)["skipped_recent"] is True
    assert index.search("late")["results"] == []
    assert index.refresh()["files_indexed"] == 1


# -- bounded search fallback, size caps ----


def test_like_fallback_scans_a_bounded_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _root(tmp_path)
    (root / "s.jsonl").write_text(
        _line(session_id="s", text="xq old row") + _line(session_id="s", text="new row"),
        encoding="utf-8",
    )
    index = _index(tmp_path, root)
    index.refresh()
    assert index.search("xq")["results"]
    monkeypatch.setattr(index_module, "LIKE_SCAN_ROWS", 1)
    result = index.search("xq")
    assert result["fallback"] is True and result["results"] == []


def test_size_limits(tmp_path: Path) -> None:
    root = _root(tmp_path)
    for number in range(3):
        (root / f"{number}.jsonl").write_text(
            "".join(_line(session_id=f"s{number}", text=f"row {i}") for i in range(4)),
            encoding="utf-8",
        )
    files = _index(tmp_path, root, limits=index_module.HistoryLimits(max_files=1))
    assert files.refresh()["limit_reached"] == "max_files"
    assert files.stats()["files"] == 1

    entries_db = tmp_path / "e.sqlite"
    capped = TranscriptIndex(
        entries_db, [SourceRoot.create("r", root)], limits=index_module.HistoryLimits(max_entries=6)
    )
    assert capped.refresh()["limit_reached"] == "max_entries"
    assert capped.stats()["entries"] == 6
    raised = TranscriptIndex(entries_db, [SourceRoot.create("r", root)])
    raised.refresh()
    assert raised.stats()["entries"] == 12

    tiny = _index(tmp_path / "t", root, limits=index_module.HistoryLimits(max_db_bytes=1))
    (tmp_path / "t").mkdir(exist_ok=True)
    assert tiny.refresh()["limit_reached"] == "max_db_bytes"
    assert (
        index_module.HistoryLimits.from_config({"history_limits": {"max_files": 7}}).max_files == 7
    )
    with pytest.raises(HistoryError):
        index_module.HistoryLimits.from_config({"history_limits": {"max_files": 0}})


# -- change detection ----


def test_same_size_same_mtime_rewrite_is_not_stuck(tmp_path: Path) -> None:
    root = _root(tmp_path)
    path = root / "s.jsonl"
    path.write_text(_line(session_id="s", text="alpha words"), encoding="utf-8")
    index = _index(tmp_path, root)
    index.refresh()
    before = path.stat()
    path.write_text(_line(session_id="s", text="omega words"), encoding="utf-8")
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    ref = index.list(session="s")["entries"][0]["ref"]
    with pytest.raises(HistoryError, match="changed since indexing"):
        index.read(ref)
    assert index.refresh()["files_indexed"] == 1
    assert index.search("omega")["results"]


# -- cross-entry key/value -----------------------------------------------------------------------


def test_key_and_value_split_across_entries_with_empty_between(tmp_path: Path) -> None:
    root = _root(tmp_path)
    (root / "x.jsonl").write_text(
        _line(session_id="s", id="a", text="the password:")
        + _line(session_id="s", id="b", text="")
        + _line(session_id="s", id="c", text=PASSWORD),
        encoding="utf-8",
    )
    index = _index(tmp_path, root)
    assert index.refresh()["skipped_secret"] == 1
    window = index.read(index.list(session="s")["entries"][0]["ref"], after=5)
    assert [entry["text"] for entry in window["entries"]] == ["the password:"]
    assert PASSWORD not in _everything(index)


# -- more credential formats ----


@pytest.mark.parametrize(
    "text",
    [
        "SECRET_KEY=" + "django-insecure-abc123xyz789",
        "STRIPE_SECRET_KEY=" + "sk_" + "live_51Habcdefghijklmnopqrstuv",
        "sk_" + "live_51Habcdefghijklmnopqrstuv",
        "mysql -u root -p" + "hunter2abc",
        "curl -u admin:" + "hunter2abc https://x.invalid",
        "glpat-" + "abcdefghijklmnopqrst",
        "hf_" + "abcdefghijklmnopqrstuvwxyzABCDEF",
        "npm_" + "abcdefghijklmnopqrstuvwxyz0123456789",
        "Authorization: Basic " + "YWRtaW46aHVudGVyMmFiYw==",
        "PGPASSWORD=" + "hunter2abc psql",
        '"x-api-key": "' + 'abcdefghijk12345"',
        "key" + "AKIA" + "ABCDEFGHIJKLMNOP",
        "password：" + "hunter2Secret99",
        "key sk-​" + "proj-ABCDEFGHIJKLMNOPQRSTUVWX",
        "password = " + "hunter2Secret99",
        'password = "' + 'hunter2 Secret99"',
        "token:\n\n\n" + "abcdefghij1234567890",
    ],
)
def test_more_credential_formats(text: str) -> None:
    assert has_secret(text)
    redacted = redact(text)
    assert "hunter2" not in redacted
    assert "abcdefghij" not in redacted
    assert "ABCDEFGHIJKLMNOP" not in redacted


@pytest.mark.parametrize(
    "text",
    ['{"max_tokens": 4096}', "token_count=123456", "auth: required", "password: [REDACTED:secret]"],
)
def test_benign_shapes_stay_clean(text: str) -> None:
    assert not has_secret(text)


def test_redaction_is_idempotent_without_stray_brackets() -> None:
    once = redact("password: " + PASSWORD + " and " + SK)
    assert redact(once) == once
    assert "]]" not in once and "]]" not in redact(once)


# -- user names in paths ----


def test_user_names_in_project_paths_are_masked(tmp_path: Path) -> None:
    root = _root(tmp_path)
    name = "al" + "ice"
    project = root / ("-" + "Users" + "-" + name + "-work-proj")
    project.mkdir()
    (project / "s.jsonl").write_text(_line(session_id="s", text="hello path"), encoding="utf-8")
    index = _index(tmp_path, root)
    index.refresh()
    hit = index.search("hello path")["results"][0]
    assert name not in hit["ref"] and name not in hit["path"]
    assert "[REDACTED:user]" in hit["path"]
    window = index.read(hit["ref"])
    assert name not in json.dumps(window)
    assert name not in json.dumps(index.list())


# -- unicode61 fallback ----


def test_unicode61_fallback_segments_stored_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(index_module, "_supports_trigram", lambda: False)
    root = _root(tmp_path)
    (root / "s.jsonl").write_text(
        _line(session_id="s", text="温室传感器校准完成"), encoding="utf-8"
    )
    index = _index(tmp_path, root)
    index.refresh()
    with sqlite3.connect(index.db) as connection:
        tokenizer = connection.execute("SELECT value FROM meta WHERE key='tokenizer'").fetchone()
    assert tokenizer[0] == "unicode61"
    result = index.search("传感器")
    assert result["results"] and result["fallback"] is False


# -- MCP refresh interval ----


def test_mcp_refresh_respects_min_interval(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "a.jsonl").write_text(_line(session_id="a", text="early entry"), encoding="utf-8")
    vault = tmp_path / "vault"
    repository = Repository(vault)
    repository.init()
    config = repository.read_config()
    config["history_sources"] = {"logs": str(logs)}
    repository.configure(config)
    monkeypatch.setenv("MEMFW_HISTORY_REFRESH_SECONDS", "3600")
    gateway = Gateway(vault)
    assert gateway.history_search("early")["results"]
    (logs / "b.jsonl").write_text(_line(session_id="b", text="later entry"), encoding="utf-8")
    assert gateway.history_search("later")["results"] == []
    monkeypatch.setenv("MEMFW_HISTORY_REFRESH_SECONDS", "0")
    assert Gateway(vault).history_search("later")["results"]
