"""Second review round for ``memfw history``: each test fails on 6e531c8.

Fixtures are synthetic and live under ``tmp_path``; secret-shaped values are
assembled at runtime.
"""

from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

import pytest

import memfw.history.index as index_module
from memfw.core.repository import Repository
from memfw.history import HistoryError, SourceRoot, TranscriptIndex, has_secret, redact
from memfw.mcp.server import Gateway

SECRET = "s3cr3t" + "value99"
SK = "sk-" + "proj-" + "ABCDEFGHIJKLMNOPQRSTUVWX"


def _line(**fields: Any) -> str:
    return json.dumps(fields, ensure_ascii=False) + "\n"


def _setup(tmp_path: Path) -> tuple[Path, TranscriptIndex]:
    root = tmp_path / "root"
    root.mkdir()
    return root, TranscriptIndex(tmp_path / "h.sqlite", [SourceRoot.create("r", root)])


def _dump(index: TranscriptIndex) -> str:
    parts = [index.db.read_bytes().decode("utf-8", errors="replace")]
    for session in index.list()["sessions"]:
        for entry in index.list(session=session["session_id"])["entries"]:
            parts.append(json.dumps(index.read(entry["ref"], before=5, after=5)))
    return "\n".join(parts)


# -- deep nesting never aborts a refresh ----------------------------------------------


def test_deeply_nested_string_does_not_abort_refresh(tmp_path: Path) -> None:
    root, index = _setup(tmp_path)
    (root / "a_good.jsonl").write_text(_line(session_id="good", text="good entry"), "utf-8")
    (root / "b_evil.jsonl").write_text(
        _line(
            type="user",
            sessionId="evil",
            uuid="u",
            message={
                "role": "user",
                "content": [{"type": "tool_result", "content": "[" * 100_000}],
            },
        )
        + "[" * 200_000
        + "\n"
        + _line(session_id="evil", text="survivor entry"),
        "utf-8",
    )
    counts = index.refresh()
    assert counts["skipped_other"] >= 1
    assert index.search("good entry")["results"]
    assert index.search("survivor")["results"]


def test_deeply_nested_tool_input_is_rendered_with_a_bound(tmp_path: Path) -> None:
    root, index = _setup(tmp_path)
    deep = json.loads('{"a":' * 900 + "1" + "}" * 900)
    (root / "d.jsonl").write_text(
        _line(
            type="assistant",
            sessionId="d",
            uuid="u",
            message={
                "role": "assistant",
                "content": [{"type": "tool_use", "name": "x", "input": deep}],
            },
        ),
        "utf-8",
    )
    index.refresh()
    snippet = index.list(session="d")["entries"][0]["snippet"]
    assert snippet.startswith("[tool x] a: a:") and snippet.endswith("…")


def test_one_failing_file_is_isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, index = _setup(tmp_path)
    (root / "a.jsonl").write_text(_line(session_id="a", text="alpha entry"), "utf-8")
    (root / "b.jsonl").write_text(_line(session_id="b", text="BOOM entry"), "utf-8")
    (root / "c.jsonl").write_text(_line(session_id="c", text="gamma entry"), "utf-8")
    original = index_module.parse_line

    def exploding(line: str, *, jsonl: bool) -> Any:
        if "BOOM" in line:
            raise RuntimeError("synthetic parser failure")
        return original(line, jsonl=jsonl)

    monkeypatch.setattr(index_module, "parse_line", exploding)
    counts = index.refresh()
    assert counts["files_failed"] == 1
    assert {row["session_id"] for row in index.list()["sessions"]} == {"a", "c"}
    monkeypatch.setattr(index_module, "parse_line", original)
    assert index.refresh()["files_indexed"] == 1
    assert index.search("BOOM")["results"]


@pytest.mark.parametrize("levels", [6, 8, 10, 12])
def test_json_nested_past_the_decode_bound_is_still_scanned(tmp_path: Path, levels: int) -> None:
    root, index = _setup(tmp_path)
    nested = json.dumps({"api_key": SECRET})
    for _ in range(levels):
        nested = json.dumps({"k": nested})
    (root / "n.jsonl").write_text(
        _line(session_id="n", text=nested) + _line(session_id="n", text="clean"), "utf-8"
    )
    assert index.refresh()["skipped_secret"] == 1
    assert SECRET not in _dump(index)


# -- normalisation ---------------------------------------------------------------------


def test_output_keeps_the_original_text() -> None:
    text = "ｶﾀ ﬁle ① x² mail " + "someone" + "@" + "example.org"
    redacted = redact(text)
    assert redacted.startswith("ｶﾀ ﬁle ① x² mail ")
    assert redacted.endswith("[REDACTED:email]")
    assert redact("password：" + SECRET) == "password：[REDACTED:secret]"


@pytest.mark.parametrize("char", ["⁢", "⁣", "‎", "‮", "͏", "᠎"])
def test_format_characters_inside_a_token_do_not_evade(char: str) -> None:
    text = "key " + SK[:12] + char + SK[12:]
    assert has_secret(text)
    assert "GHIJKLMNOP" not in redact(text)
    assert has_secret("pass" + char + "word: " + SECRET)


def test_long_blob_is_masked() -> None:
    blob = "Ab1" * 1700
    assert redact("blob " + blob + " end") == "blob [REDACTED:blob] end"


# -- lock --------------------------------------------------------------------------------


def test_held_lock_raises_history_error_or_serves_existing(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "a.jsonl").write_text(_line(session_id="a", text="early entry"), "utf-8")
    vault = tmp_path / "vault"
    repository = Repository(vault)
    repository.init()
    config = repository.read_config()
    config["history_sources"] = {"logs": str(logs)}
    repository.configure(config)
    index = TranscriptIndex(vault / ".memfw-history.sqlite", [SourceRoot.create("logs", logs)])
    index.refresh()
    (logs / "b.jsonl").write_text(_line(session_id="b", text="later entry"), "utf-8")
    holder = os.open(str(index.db) + ".lock", os.O_RDWR)
    try:
        fcntl.flock(holder, fcntl.LOCK_EX)
        with pytest.raises(HistoryError, match="refreshed elsewhere"):
            index.refresh(timeout=0.2)
        assert index.refresh(blocking=False)["skipped_locked"] is True
        gateway = Gateway(vault)
        gateway.history_refresh_seconds = 0
        assert gateway.history_search("early")["results"]
        assert gateway.history_search("later")["results"] == []
    finally:
        os.close(holder)
    assert index.refresh()["files_indexed"] == 1


def test_symlinked_lock_file_is_refused(tmp_path: Path) -> None:
    root, index = _setup(tmp_path)
    (root / "a.jsonl").write_text(_line(session_id="a", text="entry"), "utf-8")
    victim = tmp_path / "victim.txt"
    victim.write_text("important\n", "utf-8")
    os.symlink(victim, str(index.db) + ".lock")
    with pytest.raises(HistoryError, match="lock file"):
        index.refresh()
    assert victim.read_text("utf-8") == "important\n"


# -- partial lines, stale inode -------------------------------------------------------------


def test_partial_log_line_waits_for_its_newline(tmp_path: Path) -> None:
    root, index = _setup(tmp_path)
    path = root / "w.log"
    path.write_text("ok line\npartial li", "utf-8")
    index.refresh()
    assert [entry["snippet"] for entry in index.list(session="w")["entries"]] == ["ok line"]
    with path.open("a", encoding="utf-8") as handle:
        handle.write("ne done\n")
    index.refresh()
    snippets = [entry["snippet"] for entry in index.list(session="w")["entries"]]
    assert snippets == ["ok line", "partial line done"]


def test_inode_mismatch_on_read_drops_the_file_durably(tmp_path: Path) -> None:
    root, index = _setup(tmp_path)
    path = root / "s.jsonl"
    path.write_text(_line(session_id="s", text="first copy"), "utf-8")
    index.refresh()
    ref = index.list(session="s")["entries"][0]["ref"]
    replacement = tmp_path / "new.jsonl"
    replacement.write_bytes(path.read_bytes())
    os.replace(replacement, path)
    with pytest.raises(HistoryError, match="changed since indexing"):
        index.read(ref)
    with sqlite3.connect(index.db) as connection:
        assert connection.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 0


def test_lock_file_is_git_ignored() -> None:
    repo = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        ["git", "-C", str(repo), "check-ignore", "-q", ".memfw-history.sqlite.lock"],
        check=False,
    )
    assert result.returncode == 0
