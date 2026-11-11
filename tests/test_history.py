"""Tests for the read-only transcript layer (``memfw history``).

Every fixture is synthetic and written under ``tmp_path``. Secret-shaped
values are assembled at runtime so the source file itself holds none.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from memfw.cli import app
from memfw.core.repository import Repository
from memfw.history import (
    HistoryError,
    SourceRoot,
    TranscriptIndex,
    has_secret,
    redact,
    scan,
    sources_from_config,
    split_ref,
)

# Synthetic secret-shaped values, built so no literal secret lives in this file.
PEM_LABEL = "RSA " + "PRIVATE KEY"
PEM = (
    f"-----BEGIN {PEM_LABEL}-----\nMIIEsynthetic0000\nAAAAsynthetic1111\n-----END {PEM_LABEL}-----"
)
URL_CREDS = "postgres://" + "svc" + ":" + "pa55w0rdXYZ" + "@db.invalid:5432/app"
SPLIT_KV = "api_key" + ":\n    " + "Zq8" + "x" * 20 + "9w"
GH_TOKEN = "gh" + "p_" + "A1b2C3d4" * 5


def _line(**fields: Any) -> str:
    return json.dumps(fields) + "\n"


def _claude(session: str, uuid: str, text: str, ts: str, parent: str | None = None) -> str:
    role = "user" if uuid.endswith(("1", "3", "5")) else "assistant"
    return _line(
        type=role,
        sessionId=session,
        uuid=uuid,
        parentUuid=parent,
        timestamp=ts,
        message={"role": role, "content": [{"type": "text", "text": text}]},
    )


@pytest.fixture
def roots(tmp_path: Path) -> dict[str, Path]:
    agents = tmp_path / "agents"
    conductor = tmp_path / "conductor"
    (agents / "project").mkdir(parents=True)
    conductor.mkdir()
    (agents / "project" / "alpha.jsonl").write_text(
        _claude(
            "sess-alpha", "a1", "Calibrate the greenhouse humidity sensor", "2026-01-01T10:00:00Z"
        )
        + _claude(
            "sess-alpha",
            "a2",
            "Humidity sensor calibrated to 41 percent",
            "2026-01-01T10:01:00Z",
            "a1",
        )
        + _line(
            type="system",
            subtype="compact_boundary",
            sessionId="sess-alpha",
            uuid="a2b",
            timestamp="2026-01-01T10:02:00Z",
            content="Conversation compacted",
        )
        + _claude(
            "sess-alpha",
            "a3",
            "After compaction: check the irrigation valve",
            "2026-01-01T10:03:00Z",
            "a2b",
        )
        + _claude(
            "sess-alpha",
            "a4",
            "Valve torque is fine; nothing else to report",
            "2026-01-01T10:04:00Z",
            "a3",
        ),
        encoding="utf-8",
    )
    (agents / "rollout-beta.jsonl").write_text(
        _line(
            timestamp="2026-01-02T09:00:00Z",
            type="session_meta",
            payload={
                "id": "sess-beta",
                "forked_from_id": "sess-alpha",
                "timestamp": "2026-01-02T09:00:00Z",
            },
        )
        + _line(
            timestamp="2026-01-02T09:00:05Z",
            type="response_item",
            payload={
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "Plot the soil moisture trend"}],
            },
        )
        + _line(
            timestamp="2026-01-02T09:00:09Z",
            type="response_item",
            payload={
                "type": "function_call",
                "name": "shell",
                "arguments": '{"cmd": "plot moisture"}',
            },
        )
        + _line(timestamp="2026-01-02T09:01:00Z", type="compacted", payload={"message": "summary"})
        + _line(
            timestamp="2026-01-02T09:01:05Z",
            type="response_item",
            payload={
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Moisture trend plotted"}],
            },
        ),
        encoding="utf-8",
    )
    (conductor / "ticket-77.jsonl").write_text(
        _line(
            ticket_id="T-77",
            worker_id="worker-3",
            handoff_from="T-70",
            ts=1767261600,
            message="Worker picked up the pump firmware ticket",
        )
        + _line(
            ticket_id="T-77",
            worker_id="worker-3",
            ts=1767261660,
            message="Pump firmware flashed and verified",
        ),
        encoding="utf-8",
    )
    (conductor / "worker-3.log").write_text(
        "worker started on pump queue\nqueue drained\n", encoding="utf-8"
    )
    return {"agents": agents, "conductor": conductor}


def _index(tmp_path: Path, roots: dict[str, Path]) -> TranscriptIndex:
    sources = [SourceRoot.create(name, path) for name, path in roots.items()]
    return TranscriptIndex(tmp_path / "vault" / ".memfw-history.sqlite", sources)


# -- redaction ---------------------------------------------------------------------


def test_redact_pem_block() -> None:
    text = f"here is the key\n{PEM}\nthanks"
    assert has_secret(text)
    redacted = redact(text)
    assert "MIIEsynthetic" not in redacted and "BEGIN" not in redacted
    assert "[REDACTED:private_key]" in redacted
    assert redacted.startswith("here is the key") and redacted.endswith("thanks")


def test_redact_url_credentials() -> None:
    text = f"connect with {URL_CREDS} please"
    assert [finding.kind for finding in scan(text)] == ["url_credentials"]
    redacted = redact(text)
    assert "pa55w0rdXYZ" not in redacted and "svc:" not in redacted
    # The user:pass@host run also looks like an e-mail; the union of both is masked.
    assert redacted.startswith("connect with postgres://[REDACTED:credentials]")


def test_redact_key_value_split_across_lines() -> None:
    assert has_secret(SPLIT_KV)
    redacted = redact("config:\n  " + SPLIT_KV + "\n  region: north")
    assert "Zq8" not in redacted
    assert "api_key" in redacted and "region: north" in redacted
    # JSON-escaped newlines inside a raw JSONL line are also one pair.
    escaped = json.dumps({"text": SPLIT_KV})
    assert has_secret(escaped)
    assert "Zq8" not in redact(escaped)


def test_redact_tokens_and_personal_data() -> None:
    home = "/" + "home" + "/" + "someone" + "/project"
    text = f"token {GH_TOKEN} from ops@example.org at 192.0.2.10 in {home}"
    redacted = redact(text)
    assert GH_TOKEN not in redacted
    assert "ops@example.org" not in redacted and "192.0.2.10" not in redacted
    assert "someone" not in redacted and "/home/[REDACTED:user]/project" in redacted
    assert redact(redacted) == redacted


def test_scan_ignores_placeholders_and_token_counts() -> None:
    assert not has_secret('{"max_tokens": 4096, "input_tokens": 1200}')
    assert not has_secret("password: <redacted>")
    assert not has_secret("api_key = ${API_KEY}")
    assert not has_secret("plain prose about tokens and passwords")


# -- index, list, search, read ---------------------------------------------------------


def test_list_sessions_with_lineage(tmp_path: Path, roots: dict[str, Path]) -> None:
    index = _index(tmp_path, roots)
    counts = index.refresh()
    assert counts["files_indexed"] == 4
    sessions = {row["session_id"]: row for row in index.list()["sessions"]}
    assert set(sessions) == {"sess-alpha", "sess-beta", "T-77", "worker-3"}
    assert sessions["sess-alpha"]["windows"] == 2
    assert sessions["sess-beta"]["parent_session_id"] == "sess-alpha"
    assert sessions["sess-beta"]["windows"] == 2
    assert sessions["T-77"]["parent_session_id"] == "T-70"
    assert sessions["T-77"]["started_at"] == "2026-01-01T10:00:00Z"
    assert sessions["worker-3"]["entries"] == 2

    detail = index.list(session="sess-alpha")
    windows = {window["window_id"]: window for window in detail["windows"]}
    assert set(windows) == {"w0", "w1"}
    assert windows["w1"]["parent_window_id"] == "w0"
    entries = detail["entries"]
    assert [entry["entry_id"] for entry in entries] == ["a1", "a2", "a2b", "a3", "a4"]
    assert entries[1]["parent_entry_id"] == "a1"
    assert all(entry["timestamp"] for entry in entries)
    assert entries[0]["path"] == "project/alpha.jsonl"
    assert entries[0]["ref"].startswith("agents:") and entries[0]["ref"].endswith("@0")
    assert "alpha" not in entries[0]["ref"]
    assert index.list(source="conductor")["sessions"]
    assert {row["session_id"] for row in index.list(since="2026-01-02")["sessions"]} == {
        "sess-beta"
    }


def test_search_returns_refs(tmp_path: Path, roots: dict[str, Path]) -> None:
    index = _index(tmp_path, roots)
    index.refresh()
    hits = index.search("humidity sensor")["results"]
    assert hits and all(hit["session_id"] == "sess-alpha" for hit in hits)
    assert "humidity" in hits[0]["snippet"].lower()
    firmware = index.search("firmware", source="conductor")["results"]
    assert {hit["session_id"] for hit in firmware} == {"T-77"}
    assert index.search("firmware", source="agents")["results"] == []
    assert index.search("moisture", session="sess-beta")["results"]
    with pytest.raises(HistoryError):
        index.search("   ")


def test_read_window_is_redacted_and_capped(tmp_path: Path, roots: dict[str, Path]) -> None:
    index = _index(tmp_path, roots)
    index.refresh()
    ref = index.search("irrigation valve")["results"][0]["ref"]
    window = index.read(ref, before=1, after=1)
    assert [entry["entry_id"] for entry in window["entries"]] == ["a2b", "a3", "a4"]
    assert [entry["anchor"] for entry in window["entries"]] == [False, True, False]
    assert "irrigation valve" in window["entries"][1]["text"]
    assert window["window_id"] == "w1"
    capped = index.read(ref, before=5, after=5, max_bytes=256)
    assert capped["bytes"] <= 256
    assert capped["max_bytes"] == 256
    huge = index.read(ref, max_bytes=10**9)
    assert huge["max_bytes"] == 64 * 1024


def test_read_requires_a_stored_location(tmp_path: Path, roots: dict[str, Path]) -> None:
    index = _index(tmp_path, roots)
    index.refresh()
    with pytest.raises(HistoryError, match="no indexed entry"):
        index.read("agents:project/alpha.jsonl@7")
    with pytest.raises(HistoryError, match="unknown history source"):
        index.read("elsewhere:project/alpha.jsonl@0")
    with pytest.raises(HistoryError):
        split_ref("not-a-ref")


def test_read_truncates_long_entry(tmp_path: Path) -> None:
    root = tmp_path / "logs"
    root.mkdir()
    (root / "long.jsonl").write_text(
        _line(session_id="s-long", ts="2026-01-03T00:00:00Z", message="word " * 5000),
        encoding="utf-8",
    )
    index = TranscriptIndex(tmp_path / "h.sqlite", [SourceRoot.create("logs", root)])
    index.refresh()
    window = index.read("logs:long.jsonl@0", max_bytes=1000)
    assert window["truncated"] is True
    assert window["bytes"] <= 1000
    assert len(index.list(session="s-long")["entries"][0]["snippet"]) <= 400


def test_raw_lines_never_stored(tmp_path: Path, roots: dict[str, Path]) -> None:
    index = _index(tmp_path, roots)
    index.refresh()
    raw = index.db.read_bytes()
    assert b'"sessionId"' not in raw
    assert b"input_text" not in raw


# -- secret scan ---------------------------------------------------------------------


def test_entries_with_secrets_are_skipped_and_counted(tmp_path: Path) -> None:
    root = tmp_path / "logs"
    root.mkdir()
    (root / "mixed.jsonl").write_text(
        _line(session_id="s-mix", ts="2026-01-04T00:00:00Z", message="deploy notes, nothing secret")
        + _line(session_id="s-mix", ts="2026-01-04T00:00:01Z", message=f"use {URL_CREDS}")
        + _line(session_id="s-mix", ts="2026-01-04T00:00:02Z", message=PEM)
        + _line(
            session_id="s-mix",
            ts="2026-01-04T00:00:03Z",
            tool_input={"token": GH_TOKEN},
            message="ran a tool",
        )
        + _line(session_id="s-mix", ts="2026-01-04T00:00:04Z", message="deploy finished cleanly"),
        encoding="utf-8",
    )
    (root / "split.log").write_text(
        "worker config follows\nclient_secret =\n" + "Zq8" + "y" * 24 + "\nworker done\n",
        encoding="utf-8",
    )
    index = TranscriptIndex(tmp_path / "h.sqlite", [SourceRoot.create("logs", root)])
    counts = index.refresh()
    assert counts["skipped_secret"] == 4
    assert index.stats()["skipped_secret"] == 4
    entries = index.list(session="s-mix")["entries"]
    assert [entry["snippet"] for entry in entries] == [
        "deploy notes, nothing secret",
        "deploy finished cleanly",
    ]
    split_entries = [entry["snippet"] for entry in index.list(session="split")["entries"]]
    assert "worker config follows" in split_entries and "worker done" in split_entries
    assert not any("Zq8" in snippet for snippet in split_entries)
    stored = index.db.read_bytes()
    for needle in (b"pa55w0rd", b"MIIEsynthetic", GH_TOKEN.encode(), b"Zq8"):
        assert needle not in stored
    window = index.read(entries[0]["ref"], after=5)
    text = json.dumps(window)
    assert "pa55w0rd" not in text and "MIIEsynthetic" not in text and GH_TOKEN not in text


def test_read_refuses_changed_line(tmp_path: Path) -> None:
    root = tmp_path / "logs"
    root.mkdir()
    path = root / "s.jsonl"
    path.write_text(_line(session_id="s", message="hello world entry"), encoding="utf-8")
    index = TranscriptIndex(tmp_path / "h.sqlite", [SourceRoot.create("logs", root)])
    index.refresh()
    # Same bytes length, different content: the digest check refuses the read.
    original = path.read_bytes()
    path.write_bytes(original.replace(b"hello", b"jello"))
    with pytest.raises(HistoryError, match="changed since indexing"):
        index.read("logs:s.jsonl@0")


# -- path safety ---------------------------------------------------------------------


def test_symlinks_are_refused(tmp_path: Path, roots: dict[str, Path]) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "leak.jsonl").write_text(
        _line(session_id="leak", message="outside content must not be indexed"), encoding="utf-8"
    )
    os.symlink(outside / "leak.jsonl", roots["agents"] / "link.jsonl")
    os.symlink(outside, roots["agents"] / "linkdir")
    index = _index(tmp_path, roots)
    counts = index.refresh()
    assert counts["symlinks_refused"] == 2
    assert index.search("outside content")["results"] == []
    with pytest.raises(HistoryError, match="symlink"):
        index.read("agents:link.jsonl@0")
    with pytest.raises(HistoryError, match="symlink"):
        index.read("agents:linkdir/leak.jsonl@0")
    os.symlink(outside, tmp_path / "rootlink")
    with pytest.raises(HistoryError, match="symlink"):
        SourceRoot.create("linked", tmp_path / "rootlink")


def test_file_swapped_for_symlink_after_indexing(tmp_path: Path, roots: dict[str, Path]) -> None:
    index = _index(tmp_path, roots)
    index.refresh()
    target = roots["agents"] / "rollout-beta.jsonl"
    copy = tmp_path / "copy.jsonl"
    copy.write_bytes(target.read_bytes())
    target.unlink()
    os.symlink(copy, target)
    with pytest.raises(HistoryError, match="symlink"):
        index.read("agents:rollout-beta.jsonl@0")


@pytest.mark.parametrize(
    "ref",
    [
        "agents:../conductor/ticket-77.jsonl@0",
        "agents:project/../../conductor/ticket-77.jsonl@0",
        "agents:/etc/hosts@0",
        "agents:project\\alpha.jsonl@0",
        "agents:./project/alpha.jsonl@0",
    ],
)
def test_paths_outside_roots_are_refused(tmp_path: Path, roots: dict[str, Path], ref: str) -> None:
    index = _index(tmp_path, roots)
    index.refresh()
    with pytest.raises(HistoryError, match="outside the configured history roots"):
        index.read(ref)


def test_tampered_index_row_cannot_escape(tmp_path: Path, roots: dict[str, Path]) -> None:
    import sqlite3

    index = _index(tmp_path, roots)
    index.refresh()
    with sqlite3.connect(index.db) as connection:
        connection.execute(
            "UPDATE entries SET path = '../conductor/ticket-77.jsonl', offset = 0"
            " WHERE id = (SELECT MIN(id) FROM entries)"
        )
    with pytest.raises(HistoryError, match="outside"):
        index.read("agents:../conductor/ticket-77.jsonl@0")


# -- incremental re-index --------------------------------------------------------------


def test_incremental_reindex(tmp_path: Path, roots: dict[str, Path]) -> None:
    index = _index(tmp_path, roots)
    assert index.refresh()["files_indexed"] == 4
    second = index.refresh()
    assert second["files_indexed"] == 0 and second["files_unchanged"] == 4

    log = roots["conductor"] / "worker-3.log"
    with log.open("a", encoding="utf-8") as handle:
        handle.write("pump queue refilled\n")
    third = index.refresh()
    assert third["files_resumed"] == 1 and third["files_unchanged"] == 3
    assert third["entries_indexed"] == 1
    assert index.search("refilled")["results"]

    # Same size, new mtime: revisited (resumed, since its content still matches).
    ticket = roots["conductor"] / "ticket-77.jsonl"
    stat = ticket.stat()
    os.utime(ticket, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))
    touched = index.refresh()
    assert touched["files_resumed"] + touched["files_indexed"] == 1
    assert touched["entries_indexed"] == 0

    ticket.unlink()
    fourth = index.refresh()
    assert fourth["files_removed"] == 1
    assert index.search("firmware")["results"] == []
    assert "T-77" not in {row["session_id"] for row in index.list()["sessions"]}

    # A source dropped from the configuration is dropped from the index.
    only_agents = TranscriptIndex(index.db, [SourceRoot.create("agents", roots["agents"])])
    assert only_agents.refresh()["files_removed"] == 1
    assert {row["source"] for row in only_agents.list()["sessions"]} == {"agents"}


# -- configuration and CLI -------------------------------------------------------------


def test_sources_from_config(tmp_path: Path, roots: dict[str, Path]) -> None:
    assert sources_from_config({}) == []
    listed = sources_from_config(
        {"history_sources": [{"name": "agents", "path": str(roots["agents"])}]}
    )
    mapped = sources_from_config({"history_sources": {"agents": str(roots["agents"])}})
    assert listed == mapped
    with pytest.raises(HistoryError):
        sources_from_config({"history_sources": "nope"})
    with pytest.raises(HistoryError):
        sources_from_config({"history_sources": [{"name": "../x", "path": str(tmp_path)}]})
    with pytest.raises(HistoryError):
        sources_from_config({"history_sources": {"missing": str(tmp_path / "nope")}})


def _vault_with_sources(tmp_path: Path, roots: dict[str, Path]) -> Path:
    vault = tmp_path / "vault"
    repository = Repository(vault)
    repository.init()
    config = repository.read_config()
    config["history_sources"] = [{"name": name, "path": str(path)} for name, path in roots.items()]
    repository.configure(config)
    return vault


def test_cli_list_search_read(tmp_path: Path, roots: dict[str, Path]) -> None:
    vault = _vault_with_sources(tmp_path, roots)
    runner = CliRunner()
    indexed = runner.invoke(app, ["history", "index", "--root", str(vault)])
    assert indexed.exit_code == 0, indexed.output
    assert json.loads(indexed.stdout)["counts"]["files_indexed"] == 4
    assert (vault / ".memfw-history.sqlite").is_file()

    listed = runner.invoke(app, ["history", "list", "--root", str(vault)])
    assert listed.exit_code == 0, listed.output
    assert len(json.loads(listed.stdout)["sessions"]) == 4

    found = runner.invoke(app, ["history", "search", "irrigation", "--root", str(vault)])
    assert found.exit_code == 0, found.output
    ref = json.loads(found.stdout)["results"][0]["ref"]

    read = runner.invoke(app, ["history", "read", ref, "--root", str(vault), "--after", "0"])
    assert read.exit_code == 0, read.output
    assert "irrigation valve" in json.loads(read.stdout)["entries"][-1]["text"]

    refused = runner.invoke(
        app, ["history", "read", "agents:../conductor/ticket-77.jsonl@0", "--root", str(vault)]
    )
    assert refused.exit_code == 2
    assert "outside" in refused.stderr


def test_cli_source_option_and_missing_sources(tmp_path: Path, roots: dict[str, Path]) -> None:
    runner = CliRunner()
    db = tmp_path / "h.sqlite"
    result = runner.invoke(
        app,
        [
            "history",
            "search",
            "firmware",
            "--source",
            f"tickets={roots['conductor']}",
            "--db",
            str(db),
        ],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["results"][0]["source"] == "tickets"
    empty = tmp_path / "empty-vault"
    Repository(empty).init()
    missing = runner.invoke(app, ["history", "list", "--root", str(empty)])
    assert missing.exit_code == 2
    assert "no history sources" in missing.stderr
