"""Event log forward compatibility (docs/attachments.md, "Unknown event kinds").

A kind this version does not know is read only when the event carries
``details.projection == "none"``; without it the log is refused as in 0.3.0.
``tests/fixtures/event_forward_compat/newer-log`` is a vault whose log a
hypothetical newer memfw extended (``tests/harness/forward_compat.py``).
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from memfw.cli import app
from memfw.core.errors import IntegrityError
from memfw.core.models import (
    MEMORY_EVENT_KINDS,
    NON_MEMORY_EVENT_KINDS,
    Event,
    EventKind,
    ForeignEventKind,
    foreign_event_kind_counts,
)
from memfw.core.repository import Repository
from memfw.log import event_digest
from tests.harness.forward_compat import MARKER, append_raw_events

FIXTURE = Path(__file__).parent / "fixtures" / "event_forward_compat" / "newer-log"
FOREIGN_IDS = ["f" * 31 + "1", "f" * 31 + "2", "f" * 31 + "3"]


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()


def _newer_vault(tmp_path: Path) -> Path:
    root = tmp_path / "newer-log"
    shutil.copytree(FIXTURE, root)
    return root


def _plain_vault(tmp_path: Path) -> Repository:
    repository = Repository(tmp_path / "plain")
    repository.init()
    repository.add("Plain", "A plain memory.", memory_id="m-plain")
    return repository


def _cli(*args: str) -> Any:
    return CliRunner().invoke(app, list(args))


def _lines(root: Path) -> list[bytes]:
    return (root / "events.jsonl").read_bytes().splitlines()


# --- unmarked unknown kinds: refused exactly as before ------------------------------------


@pytest.mark.parametrize(
    "details",
    [
        {},
        {"projection": "memory"},
        {"projection": None},
        {"projection": "None"},
        {"Projection": "none"},
    ],
)
def test_unmarked_unknown_kind_is_refused(tmp_path: Path, details: dict[str, Any]) -> None:
    repository = _plain_vault(tmp_path)
    append_raw_events(
        repository.root,
        [{"event_id": "e" * 32, "kind": "proxy_retire", "memory_id": "p-1", "details": details}],
    )
    with pytest.raises(IntegrityError, match="input_value='proxy_retire'"):
        Repository(repository.root).events()
    for command in (("log", "verify"), ("log", "replay"), ("fsck",)):
        result = _cli(*command, "--root", str(repository.root))
        assert result.exit_code != 0, command
        assert "input_value='proxy_retire'" in f"{result.output}{result.exception}", command
    result = _cli("add", "--title", "t", "--body", "b", "--root", str(repository.root))
    assert result.exit_code != 0


def test_unmarked_unknown_kind_error_is_the_plain_enum_error() -> None:
    line = {
        "event_id": "e",
        "kind": "attach_future",
        "memory_id": "m",
        "timestamp": "2026-11-17T00:00:00Z",
        "actor": "a",
        "event_hash": "0" * 64,
    }
    with pytest.raises(Exception) as caught:
        Event.model_validate(line)
    text = str(caught.value)
    assert "1 validation error for Event" in text
    assert "kind\n  Input should be 'create'" in text
    assert "input_value='attach_future'" in text
    assert "ForeignEventKind" not in text


@pytest.mark.parametrize("kind", ["Proxy", "x y", "", "a" * 65, "1abc", "proxy/retire"])
def test_marker_does_not_admit_a_malformed_kind_name(kind: str) -> None:
    with pytest.raises(Exception, match="kind"):
        Event.model_validate(
            {
                "event_id": "e",
                "kind": kind,
                "memory_id": "m",
                "timestamp": "2026-11-17T00:00:00Z",
                "actor": "a",
                "details": dict(MARKER),
                "event_hash": "0" * 64,
            }
        )


def test_marker_on_a_known_kind_changes_nothing() -> None:
    event = Event.model_validate(
        {
            "event_id": "e",
            "kind": "create",
            "memory_id": "m",
            "timestamp": "2026-11-17T00:00:00Z",
            "actor": "a",
            "details": dict(MARKER),
            "event_hash": "0" * 64,
        }
    )
    assert event.kind is EventKind.CREATE and not event.is_foreign_kind
    assert event.kind in MEMORY_EVENT_KINDS


def test_known_kinds_keep_their_python_and_json_form() -> None:
    event = Event.model_validate(
        {
            "event_id": "e",
            "kind": "attach",
            "memory_id": "m",
            "timestamp": "2026-11-17T00:00:00Z",
            "actor": "a",
            "event_hash": "0" * 64,
        }
    )
    assert event.model_dump()["kind"] is EventKind.ATTACH
    assert event.model_dump(mode="json")["kind"] == "attach"
    assert '"kind":"attach"' in event.model_dump_json()


# --- marked unknown kinds: a log a newer version wrote ------------------------------------


def test_fixture_log_from_a_newer_version_reads_byte_for_byte(tmp_path: Path) -> None:
    root = _newer_vault(tmp_path)
    events = Repository(root).events()
    assert [line.decode() for line in _lines(root)] == [e.model_dump_json() for e in events]
    foreign = [event for event in events if event.is_foreign_kind]
    assert [event.event_id for event in foreign] == FOREIGN_IDS
    assert all(isinstance(event.kind, ForeignEventKind) for event in foreign)
    assert all(event_digest(event) == event.event_hash for event in events)
    assert foreign[1].kind == "memory.annotate" and foreign[1].kind.value == "memory.annotate"
    assert foreign[1].details["note"] == {"labels": ["a", "b"], "weight": 0.5}
    assert all(event.kind not in NON_MEMORY_EVENT_KINDS for event in foreign)
    assert foreign_event_kind_counts(events) == {"memory.annotate": 1, "proxy_retire": 2}


def test_replay_is_unaffected_by_marked_unknown_events(tmp_path: Path) -> None:
    root = _newer_vault(tmp_path)
    current = hashlib.sha256((root / "memories" / "m-plain.md").read_bytes()).hexdigest()
    assert Repository(root).replay() == {"m-plain": current}
    result = _cli("log", "replay", "--root", str(root))
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {"m-plain": current}


def test_every_command_that_reads_the_log_works(tmp_path: Path) -> None:
    root = _newer_vault(tmp_path)
    r = ("--root", str(root))

    def ok(*args: str) -> str:
        result = _cli(*args)
        assert result.exit_code == 0, (args, result.output, result.exception)
        return str(result.output)

    verify = json.loads(ok("log", "verify", *r))
    assert verify["valid"] is True and verify["event_count"] == 5
    assert verify["unknown_non_memory_event_count"] == 3
    assert verify["unknown_non_memory_event_kinds"] == {"memory.annotate": 1, "proxy_retire": 2}
    issues = json.loads(ok("fsck", *r))
    assert [row["kind"] for row in issues] == ["unknown_non_memory_events"] * 2
    assert "2 event(s) of kind proxy_retire" in issues[1]["message"]
    assert "m-plain" in ok("list", *r)
    assert "edited" in ok("show", "m-plain", *r)
    ok("index", "rebuild", *r)
    ok("stats", *r)
    ok("recover", "--dry-run", *r)
    ok("log", "compact", *r)
    added = ok("add", "--title", "New", "--body", "Written after a newer log.", *r).strip()
    ok("update", "m-plain", "--body", "Edited again.", *r)
    ok("delete", added, *r)
    # The log still verifies, and our own writes chained after the foreign events.
    repository = Repository(root)
    events = repository.events()
    assert [event.event_id for event in events if event.is_foreign_kind] == FOREIGN_IDS
    assert _lines(root)[:5] == _lines(FIXTURE)
    assert json.loads(ok("fsck", *r))[0]["kind"] == "unknown_non_memory_events"


def test_compaction_round_trip_is_byte_identical(tmp_path: Path) -> None:
    root = _newer_vault(tmp_path)
    repository = Repository(root)
    before = [event.model_dump_json() for event in repository.events()]
    repository.compact_log(keep_recent=1)
    archived, _ = repository._log_segments()
    assert {event.event_id for event in archived if event.is_foreign_kind} == set(FOREIGN_IDS[:2])
    assert all(event.is_compacted_summary for event in archived)
    assert [event.model_dump_json() for event in Repository(root).events()] == before
    # Re-compacting re-summarizes the archived summaries; still lossless.
    repository.add("Second", "Another memory.")
    repository.compact_log(keep_recent=1)
    reread = Repository(root).events()
    assert [event.model_dump_json() for event in reread[: len(before)]] == before
    assert all(event_digest(event) == event.event_hash for event in reread)
    rows = _summary_rows(root)
    foreign_rows = [row for row in rows if row["event"]["event_id"] in FOREIGN_IDS]
    assert len(foreign_rows) == 3 and all(row["lossless"] for row in foreign_rows)
    assert [row["event_line"] for row in foreign_rows] == [
        line.decode() for line in _lines(FIXTURE) if json.loads(line)["event_id"] in FOREIGN_IDS
    ]
    assert not any("event_line" in row for row in rows if row not in foreign_rows)
    verify = json.loads(_cli("log", "verify", "--root", str(root)).output)
    assert verify["unknown_non_memory_event_count"] == 3
    current = hashlib.sha256((root / "memories" / "m-plain.md").read_bytes()).hexdigest()
    assert Repository(root).replay()["m-plain"] == current


def _summary_rows(root: Path) -> list[dict[str, Any]]:
    import io
    import tarfile

    import zstandard

    repository = Repository(root)
    catalog = json.loads(repository.log_catalog_path.read_text(encoding="utf-8"))
    container = repository.log_segments_dir / catalog["container"]["name"]
    tar_bytes = zstandard.ZstdDecompressor().decompress(container.read_bytes())
    rows: list[dict[str, Any]] = []
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:") as archive:
        for name in sorted(archive.getnames()):
            if name.endswith("summaries.jsonl"):
                member = archive.extractfile(name)
                assert member is not None
                rows.extend(json.loads(line) for line in member.read().splitlines())
    return rows


def test_rollback_refuses_a_marked_unknown_event(tmp_path: Path) -> None:
    root = _newer_vault(tmp_path)
    repository = Repository(root)
    for event_id in FOREIGN_IDS:
        with pytest.raises(IntegrityError, match="rollback acts only on memory events"):
            repository.rollback(event_id)
        result = _cli("rollback", event_id, "--dry-run", "--root", str(root))
        assert result.exit_code == 0
        assert json.loads(result.output) == {"event_id": event_id, "available": False}
        assert _cli("rollback", event_id, "--apply", "--root", str(root)).exit_code != 0
    assert len(repository.events()) == 5


def test_rollback_of_a_memory_event_ignores_foreign_hashes(tmp_path: Path) -> None:
    # The foreign ``memory.annotate`` names m-plain with an after_hash that is not a
    # memory version; it sits between the create and the update. Counting it would
    # push the create version out of the rollback retention window.
    root = _newer_vault(tmp_path)
    repository = Repository(root)
    update = next(event for event in repository.events() if event.kind == EventKind.UPDATE)
    restored = repository.rollback(update.event_id)
    assert restored.id == "m-plain"
    assert "A plain memory." in (root / "memories" / "m-plain.md").read_text(encoding="utf-8")
    assert "edited" not in (root / "memories" / "m-plain.md").read_text(encoding="utf-8")


def test_a_vault_without_foreign_events_reports_as_before(tmp_path: Path) -> None:
    repository = _plain_vault(tmp_path)
    verify = json.loads(_cli("log", "verify", "--root", str(repository.root)).output)
    assert not any(key.startswith("unknown_") for key in verify)
    assert repository.fsck() == []


_BASE_EVENT: dict[str, Any] = {
    "event_id": "e",
    "memory_id": "m",
    "timestamp": "2026-11-17T00:00:00Z",
    "actor": "a",
    "event_hash": "0" * 64,
}


@pytest.mark.parametrize("name", ["create", "attach", "Bad Name", ""])
def test_a_foreign_kind_instance_must_be_an_unknown_well_formed_name(name: str) -> None:
    with pytest.raises(ValueError, match="not a foreign event kind"):
        Event(**{**_BASE_EVENT, "kind": ForeignEventKind(name), "details": dict(MARKER)})


def test_a_foreign_kind_instance_must_carry_the_marker() -> None:
    with pytest.raises(ValueError, match="not marked"):
        Event(**{**_BASE_EVENT, "kind": ForeignEventKind("proxy_retire")})
    event = Event(**{**_BASE_EVENT, "kind": ForeignEventKind("proxy_retire"), "details": MARKER})
    assert event.is_foreign_kind and event.kind not in MEMORY_EVENT_KINDS


def test_a_foreign_kind_reserves_its_memory_id(tmp_path: Path) -> None:
    repository = Repository(_newer_vault(tmp_path))
    with pytest.raises(IntegrityError, match="already used"):
        repository.add("Clash", "Same id as a proxy.", memory_id="p-0001")


def test_a_foreign_summary_row_without_its_event_line_is_refused(tmp_path: Path) -> None:
    from memfw.log.store import _canonical_json, _parse_segment_summaries, _summarize_events

    events = Repository(_newer_vault(tmp_path)).events()[:2]
    rows = [json.loads(row) for row in _summarize_events(events, "0" * 64)[0]]
    assert "event_line" in rows[1]
    previous = "0" * 64
    for row in rows:
        row.pop("event_line", None)
        row["previous_summary_hash"] = previous
        payload = {key: value for key, value in row.items() if key != "summary_hash"}
        row["summary_hash"] = previous = hashlib.sha256(_canonical_json(payload)).hexdigest()
    raw = b"".join(json.dumps(row).encode() + b"\n" for row in rows)
    with pytest.raises(IntegrityError, match="lacks its event line"):
        _parse_segment_summaries(raw, "0" * 64, "0" * 64)
