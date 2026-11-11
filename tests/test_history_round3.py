"""Third review round for ``memfw history``: each test fails on 6d55da4.

Fixtures are synthetic and live under ``tmp_path``; secret-shaped values are
assembled at runtime.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest

import memfw.history.index as index_module
from memfw.history import HistoryError, SourceRoot, TranscriptIndex, has_secret, redact

SECRET = "s3cr3t" + "value99"
VALUE = "hunter2" + "abc"
TOKEN = "abcdef" + "123456"
SK = "sk-" + "proj-" + "ABCDEFGHIJKLMNOPQRSTUVWX"


def _line(**fields: Any) -> str:
    return json.dumps(fields, ensure_ascii=False) + "\n"


def _setup(tmp_path: Path) -> tuple[Path, TranscriptIndex]:
    root = tmp_path / "root"
    root.mkdir()
    return root, TranscriptIndex(tmp_path / "h.sqlite", [SourceRoot.create("r", root)])


# -- blocker: a word glued in front of the key ---------------------------------------


GLUED_KEYS = [
    f"mypassword={VALUE}",
    f'dbPassword: "{VALUE}"',
    f'"newPassword": "{VALUE}"',
    f"userToken={TOKEN}",
    f'"githubToken": "{TOKEN}"',
    f'"slackToken":"{TOKEN}"',
    f"stripeSecret={TOKEN}",
    f'"clientSecret": "{TOKEN}"',
    f'"mysqlPassword": "{VALUE}"',
    f'"oauthToken": "{TOKEN}"',
    f'"x_auth_token": "{TOKEN}"',
    f"app.secret={TOKEN}",
    f"jwtSecret: {TOKEN}",
    f"adminPass={VALUE}",
    f'"dbPass": "{VALUE}"',
]


@pytest.mark.parametrize("text", GLUED_KEYS)
def test_keys_with_a_glued_prefix_are_caught(text: str) -> None:
    assert has_secret(text)
    redacted = redact(text)
    assert VALUE not in redacted and TOKEN not in redacted


@pytest.mark.parametrize(
    "text",
    [
        "max_tokens: 4096",
        "token_count=123456",
        "secretary: Jane Smith",
        "nextPageToken: null",
        "password: [REDACTED:secret]",
    ],
)
def test_glued_prefix_keeps_benign_shapes_clean(text: str) -> None:
    assert not has_secret(text)


@pytest.mark.parametrize(
    "text",
    [
        "a." * 500_000,
        "." * 1_000_000,
        "_" * 1_000_000,
        "a_" * 500_000,
        "-" * 1_000_000,
        "x dbPassword" * 80_000,
        "dbpass" * 170_000,
        "a.b " * 250_000,
    ],
)
def test_glued_prefix_scan_stays_linear(text: str) -> None:
    started = time.perf_counter()
    has_secret(text)
    assert time.perf_counter() - started < 1.0


def test_glued_key_in_a_camel_case_config_is_skipped(tmp_path: Path) -> None:
    root, index = _setup(tmp_path)
    config = "\n".join(GLUED_KEYS[:4])
    (root / "c.jsonl").write_text(
        _line(
            type="user",
            sessionId="s",
            uuid="u1",
            message={"role": "user", "content": [{"type": "tool_result", "content": config}]},
        )
        + _line(type="user", sessionId="s", uuid="u2", message={"role": "user", "content": "ok"}),
        "utf-8",
    )
    assert index.refresh()["skipped_secret"] == 1
    assert VALUE not in index.db.read_bytes().decode("utf-8", errors="replace")


# -- readers never wait for a refresh ------------------------------------------------------


def test_readers_do_not_wait_for_a_writer(tmp_path: Path) -> None:
    root, index = _setup(tmp_path)
    (root / "a.jsonl").write_text(_line(session_id="a", text="seed entry"), "utf-8")
    index.refresh()
    writer = sqlite3.connect(index.db, timeout=0)
    try:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("INSERT INTO meta (key, value) VALUES ('busy', '1')")
        started = time.perf_counter()
        assert index.search("seed")["results"]
        assert index.list()["sessions"]
        assert time.perf_counter() - started < 2.0
    finally:
        writer.rollback()
        writer.close()
    with sqlite3.connect(index.db) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


# -- lock file that is not a regular file -----------------------------------------------------


def test_fifo_lock_path_is_a_history_error(tmp_path: Path) -> None:
    root, index = _setup(tmp_path)
    (root / "a.jsonl").write_text(_line(session_id="a", text="entry"), "utf-8")
    os.mkfifo(str(index.db) + ".lock")
    with pytest.raises(HistoryError, match="lock"):
        index.refresh(blocking=False)


# -- masks ------------------------------------------------------------------------------------


def test_token_inside_an_email_masks_the_whole_address() -> None:
    domain = "example" + ".org"
    redacted = redact(f"mail {SK}@{domain} end")
    assert domain not in redacted and "ABCDEF" not in redacted
    assert redacted.startswith("mail [REDACTED:") and redacted.endswith("] end")


@pytest.mark.parametrize("mark", ["́", "⃝", "️", "⃣"])
def test_combining_marks_inside_a_token_do_not_evade(mark: str) -> None:
    assert has_secret("key " + SK[:14] + mark + SK[14:])
    assert has_secret("pass" + mark + "word: " + SECRET)


@pytest.mark.parametrize(
    "text",
    ["1" + SK, "①" + "password=" + SECRET, "１password=" + SECRET, "2hf_" + "a" * 32],
)
def test_digit_glued_in_front_is_a_boundary(text: str) -> None:
    assert has_secret(text)


# -- escaped keys past the decode bound ----------------------------------------------------------


@pytest.mark.parametrize("levels", [0, 4, 8, 11])
def test_unicode_escaped_key_is_scanned_as_a_pair(tmp_path: Path, levels: int) -> None:
    root, index = _setup(tmp_path)
    nested = '{"\\u0061pi_key": "' + SECRET + '"}'
    for _ in range(levels):
        nested = json.dumps({"k": nested})
    (root / "u.jsonl").write_text(
        _line(session_id="u", text=nested) + _line(session_id="u", text="clean"), "utf-8"
    )
    assert index.refresh()["skipped_secret"] == 1
    assert SECRET not in index.db.read_bytes().decode("utf-8", errors="replace")


# -- a file that keeps failing ------------------------------------------------------------------


def test_failing_file_is_skipped_until_it_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, index = _setup(tmp_path)
    (root / "good.jsonl").write_text(_line(session_id="g", text="good entry"), "utf-8")
    bad = root / "bad.jsonl"
    bad.write_text(_line(session_id="b", text="BOOM entry"), "utf-8")
    original = index_module.parse_line
    calls: list[str] = []

    def exploding(line: str, *, jsonl: bool) -> Any:
        if "BOOM" in line:
            calls.append(line)
            raise RuntimeError("synthetic parser failure")
        return original(line, jsonl=jsonl)

    monkeypatch.setattr(index_module, "parse_line", exploding)
    assert index.refresh()["files_failed"] == 1
    assert index.refresh()["files_failed"] == 1
    third = index.refresh()
    assert third["files_failed"] == 0 and third["files_skipped_failing"] == 1
    assert len(calls) == 2
    monkeypatch.setattr(index_module, "parse_line", original)
    with bad.open("a", encoding="utf-8") as handle:
        handle.write(_line(session_id="b", text="fixed entry"))
    assert index.refresh()["files_indexed"] == 1
    assert index.search("fixed")["results"]
