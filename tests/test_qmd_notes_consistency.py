"""T6: the builtin notes index vs the QMD notes adapter on fixtures (a report, not a gate).

``scripts/qmd_notes_consistency.py`` replays the recorded QMD answers
(``tests/fixtures/qmd_contract/qmd-2.5.3.json``) through the adapter's loopback
fake and compares them with memfw's own notes index over the same tree. These
tests check the report's shape and arithmetic only; no overlap value is
required.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any

import pytest

import scripts.qmd_notes_consistency as t6

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "qmd_contract"
QUERIES = ["migration checklist", "暂停定时任务", "reading list"]

_CONNECT = socket.socket.connect
_CREATE = socket.create_connection
_GETADDRINFO = socket.getaddrinfo


@pytest.fixture(autouse=True)
def loopback_only(disable_network: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """The fake listens on 127.0.0.1; nothing else may be reached."""

    def guard(real: Any, index: int) -> Any:
        def call(*args: Any, **kwargs: Any) -> Any:
            target = args[index]
            host = target[0] if isinstance(target, tuple) else target
            if str(host) not in {"127.0.0.1", "localhost"}:
                raise AssertionError("Tests must not open network sockets")
            return real(*args, **kwargs)

        return call

    monkeypatch.setattr(socket.socket, "connect", guard(_CONNECT, 1))
    monkeypatch.setattr(socket, "create_connection", guard(_CREATE, 0))
    monkeypatch.setattr(socket, "getaddrinfo", guard(_GETADDRINFO, 0))


def _run(capsys: pytest.CaptureFixture[str], *extra: str) -> dict[str, Any]:
    argv = [
        "--notes-root",
        str(FIXTURES / "notes"),
        "--recording",
        str(FIXTURES / "qmd-2.5.3.json"),
        "--legacy-privacy",
        "shared",
        *extra,
    ]
    for query in QUERIES:
        argv += ["--query", query]
    assert t6.main(argv) == 0
    return json.loads(capsys.readouterr().out)


def test_report_shape_and_bounds(capsys: pytest.CaptureFixture[str]) -> None:
    report = _run(capsys)
    assert report["schema"] == t6.SCHEMA and report["gate"] is False
    assert report["qmd_source"] == "recording" and report["qmd_available"] is True
    assert len(report["queries"]) == len(QUERIES)
    for row in report["queries"]:
        assert row["qmd_status"] == "ok"
        assert row["qmd"], row  # the recording answers every query
        for level in ("notes", "sections"):
            for key in ("jaccard", "coverage"):
                value = row[level][key]
                assert value is None or 0.0 <= value <= 1.0
    for value in report["mean"].values():
        assert value is None or 0.0 <= value <= 1.0
    text = json.dumps(report, ensure_ascii=False)
    assert "SNIPPET-MARKER" not in text and "Synthetic" not in text  # refs and numbers only


def test_builtin_side_is_held_to_the_same_privacy_rule(capsys: pytest.CaptureFixture[str]) -> None:
    report = _run(capsys)
    for row in report["queries"]:
        for ref in row["builtin"] + row["qmd"]:
            assert not ref.startswith(("kh:private/", "kh:weird/")), ref


def test_overlap_arithmetic() -> None:
    assert t6._overlap(["a", "b"], ["b", "c"]) == {"jaccard": 0.3333, "coverage": 0.5, "shared": 1}
    assert t6._overlap([], []) == {"jaccard": None, "coverage": None, "shared": 0}
    assert t6._overlap(["a"], []) == {"jaccard": 0.0, "coverage": None, "shared": 0}
    assert t6._mean([None, 0.5, 1.0]) == 0.75 and t6._mean([None]) is None


def test_unreachable_qmd_is_reported_not_raised(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    argv = [
        "--notes-root",
        str(FIXTURES / "notes"),
        "--endpoint",
        f"http://127.0.0.1:{port}/mcp",
        "--legacy-privacy",
        "shared",
        "--timeout",
        "1",
        "--query",
        QUERIES[0],
    ]
    assert t6.main(argv) == 0
    report = json.loads(capsys.readouterr().out)
    (row,) = report["queries"]
    assert row["qmd_status"].startswith("unavailable:") and row["qmd"] == []
    assert row["builtin"]  # the builtin side still answers
