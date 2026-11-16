"""Incremental refresh over a partial trailing line matches a full rebuild.

A trailing JSON line without its newline is indexed, and the next refresh
resumes before it. The parser state saved for that resume must be the state
from before the partial line, or the re-parse sees the line twice: a
password on the line after ``Enter password:`` slips through unredacted, and
line numbers, window lineage and skip counters drift from a full rebuild.

Fixtures are synthetic and live under ``tmp_path``; secret-shaped values are
assembled at runtime.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from memfw.history import SourceRoot, TranscriptIndex

PASSWORD = "Tr0ub4" + "dor&3x"
API_KEY = "api_key=" + "Zx9qWv3LmT8r"


def _json(**fields: Any) -> str:
    return json.dumps(fields, ensure_ascii=False)


def _msg(text: str) -> str:
    return _json(session_id="s1", message=text)


def _entries(index: TranscriptIndex) -> list[dict[str, Any]]:
    return list(index.list(session="s1")["entries"])


def _run(tmp_path: Path, parts: list[str]) -> tuple[TranscriptIndex, TranscriptIndex]:
    root = tmp_path / "root"
    root.mkdir()
    path = root / "a.jsonl"
    path.write_text("", "utf-8")
    incremental = TranscriptIndex(tmp_path / "inc.sqlite", [SourceRoot.create("s", root)])
    for part in parts:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(part)
        incremental.refresh()
    full = TranscriptIndex(tmp_path / "full.sqlite", [SourceRoot.create("s", root)])
    full.refresh()
    return incremental, full


def _assert_same(incremental: TranscriptIndex, full: TranscriptIndex) -> None:
    assert _entries(incremental) == _entries(full)
    assert incremental.list(session="s1")["windows"] == full.list(session="s1")["windows"]
    assert incremental.stats()["skipped_secret"] == full.stats()["skipped_secret"]
    assert incremental.stats()["skipped_other"] == full.stats()["skipped_other"]


CASES = {
    "trailing_new_window": [
        _msg("hello one") + "\n" + _json(session_id="s1", new_window=True),
        "\n" + _msg("after compaction") + "\n",
    ],
    "trailing_prompt": [
        _msg("hello one") + "\n" + _msg("Enter password:"),
        "\n" + _msg("next line ok") + "\n",
    ],
    "trailing_secret": [
        _msg("hello one") + "\n" + _msg(API_KEY),
        "\n" + _msg("next line ok") + "\n",
    ],
    "trailing_window_id": [
        _msg("hello one") + "\n" + _json(session_id="s1", window_id="wx", message="moved"),
        "\n" + _msg("same window") + "\n" + _msg("tail open"),
        "\n" + _msg("closed") + "\n",
    ],
}


@pytest.mark.parametrize("name", sorted(CASES))
def test_incremental_matches_full_rebuild(tmp_path: Path, name: str) -> None:
    incremental, full = _run(tmp_path, CASES[name])
    _assert_same(incremental, full)


def test_password_after_partial_prompt_is_never_indexed(tmp_path: Path) -> None:
    incremental, full = _run(
        tmp_path,
        [
            _msg("hello one") + "\n" + _msg("Enter password:"),
            "\n" + _msg(PASSWORD) + "\n" + _msg("after") + "\n",
        ],
    )
    _assert_same(incremental, full)
    for index in (incremental, full):
        snippets = [entry["snippet"] for entry in _entries(index)]
        assert all(PASSWORD not in snippet for snippet in snippets)
        assert index.search("Tr0ub4dor")["results"] == []
        assert PASSWORD.encode() not in index.db.read_bytes()


def test_line_numbers_follow_full_rebuild(tmp_path: Path) -> None:
    incremental, full = _run(
        tmp_path,
        [
            _msg("one") + "\n" + _msg(API_KEY),
            "\n" + _msg("three") + "\n" + _msg("four"),
            "\n",
        ],
    )
    lines = {entry["snippet"]: entry["line"] for entry in _entries(incremental)}
    assert lines == {"one": 1, "three": 3, "four": 4}
    _assert_same(incremental, full)
