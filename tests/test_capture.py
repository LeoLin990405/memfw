from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from memfw.capture import (
    Action,
    CaptureCandidate,
    CaptureSession,
    Source,
    TimedSignal,
    _append_stage_record,
    _read_stage_records,
    pack_capture_candidates,
)
from memfw.capture.cli import app
from memfw.core.models import CaptureState, EventKind
from memfw.core.repository import Repository


def instant(seconds: int) -> datetime:
    return datetime.fromtimestamp(seconds, tz=UTC)


def signal(
    seconds: int,
    intensity: float,
    *,
    source: Source = "rule",
    action: Action = "signal",
    text: str | None = None,
) -> TimedSignal:
    return TimedSignal(
        timestamp=instant(seconds), source=source, intensity=intensity, action=action, text=text
    )


def test_illegal_self_transition_raises() -> None:
    session = CaptureSession()
    with pytest.raises(ValueError, match="illegal capture transition"):
        session.record_state(CaptureState.DORMANT, actor="human")


def test_human_override_blocks_models_until_human_auto() -> None:
    session = CaptureSession()
    session.feed(signal(0, 1.0, source="human", action="remember"))
    assert session.current_state() == CaptureState.FULL
    session.feed(signal(1, 0.0, source="model"))
    session.feed(signal(2, 0.0, source="rule", action="sleep"))
    session.advance_to(instant(1000))
    assert session.current_state() == CaptureState.FULL

    session.feed(signal(1001, 0.0, source="human", action="forget"))
    assert session.current_state() == CaptureState.DORMANT
    session.feed(signal(1002, 1.0, source="model"))
    session.feed(signal(1003, 0.0, source="model", action="sleep"))
    session.advance_to(instant(2000))
    assert session.current_state() == CaptureState.DORMANT

    session.feed(signal(2001, 0.0, source="human", action="auto"))
    session.feed(signal(2002, 1.0, source="model"))
    assert session.current_state() == CaptureState.FULL


def test_human_remember_keeps_collecting_content_and_calls_judge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from memfw.agents import ClaudeJudge

    session = CaptureSession()
    session.feed(signal(0, 1.0, source="human", action="remember"))
    judge = ClaudeJudge()
    calls: list[str] = []

    def wake(text: str) -> float:
        calls.append(text)
        return 0.0

    monkeypatch.setattr(judge, "wake_intensity", wake)
    session.feed_model_text(instant(1), "capture this note", judge)
    assert calls == ["capture this note"]
    assert session.current_state() == CaptureState.FULL
    assert [item.text for item in session.items()] == ["capture this note"]


def test_human_forget_clears_standby_ring_before_later_remember() -> None:
    session = CaptureSession(standby_seconds=5, dormant_seconds=100)
    session.feed(signal(0, 0.9))
    session.feed(signal(1, 0.1))
    session.advance_to(instant(6))
    session.feed(signal(7, 0.1, action="content", text="must be forgotten"))
    session.feed(signal(8, 0.0, source="human", action="forget"))
    session.feed(signal(9, 1.0, source="human", action="remember"))
    assert session.items() == []


def test_standby_wake_backfills_triggering_content_once() -> None:
    session = CaptureSession(standby_seconds=5, dormant_seconds=100)
    session.feed(signal(0, 0.9))
    session.feed(signal(1, 0.1))
    session.advance_to(instant(6))
    session.feed(signal(7, 0.9, action="content", text="wake and capture once"))
    assert [item.text for item in session.items()] == ["wake and capture once"]


def test_human_sleep_clears_ring_and_locks_dormant() -> None:
    session = CaptureSession(standby_seconds=5, dormant_seconds=100)
    session.feed(signal(0, 0.9))
    session.feed(signal(1, 0.1))
    session.advance_to(instant(6))
    session.feed(signal(7, 0.1, action="content", text="pre-sleep buffer"))
    session.feed(signal(8, 0.0, source="human", action="sleep"))
    session.feed(signal(9, 1.0, source="model"))
    assert session.items() == []
    assert session.current_state() == CaptureState.DORMANT


def test_hysteresis_timeout_boundaries_are_exact_and_explicit() -> None:
    session = CaptureSession(standby_seconds=5, dormant_seconds=10)
    session.feed(signal(0, 0.9))
    session.feed(signal(1, 0.1))
    session.advance_to(instant(6))
    assert session.current_state() == CaptureState.STANDBY
    assert session.events[-1].timestamp == instant(6)
    session.advance_to(instant(16))
    assert session.current_state() == CaptureState.DORMANT
    assert session.events[-1].timestamp == instant(16)


def test_ring_buffer_backfills_only_in_boundary_and_preserves_order() -> None:
    session = CaptureSession(standby_seconds=5, dormant_seconds=100, ring_seconds=10)
    session.feed(signal(0, 0.9))
    session.feed(signal(1, 0.1))
    session.advance_to(instant(6))
    assert session.current_state() == CaptureState.STANDBY
    session.feed(signal(19, 0.1, action="content", text="outside boundary"))
    session.feed(signal(20, 0.1, action="content", text="inside boundary"))
    session.feed(signal(30, 0.9))
    items = session.items()
    assert [item.text for item in items] == ["inside boundary"]
    assert items[0].timestamp == instant(20)


def test_reverse_timestamps_rejected_without_mutating_state() -> None:
    session = CaptureSession()
    session.feed(signal(10, 0.9))
    with pytest.raises(ValueError, match="non-decreasing"):
        session.feed(signal(9, 0.0))
    assert session.current_state() == CaptureState.FULL


def test_candidate_stage_is_persisted_hash_checked_and_removed_after_confirm(
    tmp_path: Path,
) -> None:
    repository = Repository(tmp_path / "vault")
    repository.init()
    session = CaptureSession(repository=repository, session_id="capture-test")
    session.feed(
        TimedSignal(
            timestamp=instant(0),
            source="model",
            action="content",
            intensity=0.9,
            source_id="synthetic-memory-7",
            text="staged capture text",
        )
    )
    assert len(session.items()) == 1
    stage_path = tmp_path / "vault" / ".staging" / "capture-candidates.jsonl"
    reloaded = CaptureSession.from_repository(repository, session_id="capture-test")
    concurrent = CaptureSession.from_repository(repository, session_id="capture-test")
    recovered = reloaded.items()[0]
    assert recovered.text == "staged capture text"
    assert recovered.source_id == "synthetic-memory-7"

    original_stage = stage_path.read_bytes()
    stage_path.write_bytes(original_stage.replace(b"staged capture text", b"tampered capture text"))
    with pytest.raises(ValueError, match="integrity"):
        CaptureSession.from_repository(repository, session_id="capture-test")

    stage_path.write_bytes(original_stage)
    with pytest.raises(PermissionError, match="only a human"):
        reloaded.confirm_candidate(recovered.candidate_id, "keep", actor="agent:mlx")
    confirmed = reloaded.confirm_candidate(recovered.candidate_id, "keep")
    assert confirmed.approved
    assert not stage_path.read_text(encoding="utf-8").strip()
    assert reloaded.items() == []
    with pytest.raises(ValueError, match="persisted decision"):
        concurrent.confirm_candidate(recovered.candidate_id, "discard")


def test_capture_pack_reopens_confirms_and_unpack_before_append(tmp_path: Path) -> None:
    repository = Repository(tmp_path / "packed-vault")
    repository.init()
    session = CaptureSession(repository=repository, session_id="pack-test")
    session.feed(
        TimedSignal(
            timestamp=instant(0),
            source="model",
            action="content",
            intensity=0.9,
            source_id="source-1",
            text="packed candidate content",
        )
    )
    stage = repository.root / ".staging" / "capture-candidates.jsonl"
    pack = stage.with_name("capture-candidates.jsonl.zst")
    result = pack_capture_candidates(repository)
    assert result["records"] == 1
    assert isinstance(result["packed_bytes"], int)
    assert isinstance(result["raw_bytes"], int)
    assert result["packed_bytes"] < result["raw_bytes"]
    assert not stage.exists()
    assert pack.exists()

    read_only = CaptureSession.from_repository(repository, session_id="pack-test")
    assert len(read_only.items()) == 1
    assert pack.exists()
    assert not stage.exists()
    repository.recover_pending()
    assert pack.exists()
    assert not stage.exists()

    appended = CaptureSession(repository=repository, session_id="pack-test-2")
    appended.feed(
        TimedSignal(
            timestamp=instant(1),
            source="model",
            action="content",
            intensity=0.9,
            source_id="source-2",
            text="new content after unpack",
        )
    )
    assert stage.exists()
    assert not pack.exists()
    assert len(CaptureSession.from_repository(repository, session_id="*").items()) == 2

    loaded = CaptureSession.from_repository(repository, session_id="pack-test")
    candidate = loaded.items()[0]
    loaded.confirm_candidate(candidate.candidate_id, "keep")
    assert not pack.exists()
    assert [item.source_id for item, _ in _read_stage_records(stage)] == ["source-2"]


def test_capture_pack_rejects_mismatched_dual_sources(tmp_path: Path) -> None:
    repository = Repository(tmp_path / "dual-vault")
    repository.init()
    session = CaptureSession(repository=repository, session_id="dual-test")
    session.feed(
        TimedSignal(
            timestamp=instant(0),
            source="model",
            action="content",
            intensity=0.9,
            source_id="source-2",
            text="candidate text",
        )
    )
    stage = repository.root / ".staging" / "capture-candidates.jsonl"
    pack_capture_candidates(repository)
    stage.write_bytes(b"different raw WAL\n")
    with pytest.raises(ValueError, match="disagree"):
        CaptureSession.from_repository(repository, session_id="dual-test")


def test_stage_intent_recovers_append_before_capture_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = Repository(tmp_path / "stage-recovery")
    repository.init()
    session = CaptureSession(repository=repository, session_id="capture-recovery")

    original_capture_event = repository.capture_event

    def fail_record(kind: EventKind, *args: Any, **kwargs: Any) -> Any:
        if kind == EventKind.CAPTURE_RECORD:
            raise OSError("simulated event append failure")
        return original_capture_event(kind, *args, **kwargs)

    monkeypatch.setattr(repository, "capture_event", fail_record)
    with pytest.raises(OSError, match="event append"):
        session.feed(
            TimedSignal(
                timestamp=instant(0),
                source="model",
                action="content",
                intensity=0.9,
                text="recover the durable stage append",
            )
        )
    monkeypatch.undo()

    cleaned = repository.recover_pending()
    assert ".staging/capture-candidates.jsonl" in cleaned
    recovered = CaptureSession.from_repository(repository, session_id="capture-recovery")
    assert [item.text for item in recovered.items()] == ["recover the durable stage append"]
    assert any(event.kind == EventKind.CAPTURE_RECORD for event in repository.events())


def test_stage_intent_recovers_partial_jsonl_append(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = Repository(tmp_path / "stage-partial-recovery")
    repository.init()
    session = CaptureSession(repository=repository, session_id="capture-partial")
    from memfw import capture as capture_module

    append = capture_module._append_stage_record

    def append_then_tear(path: Path, candidate: CaptureCandidate, content: bytes) -> None:
        append(path, candidate, content)
        path.write_bytes(path.read_bytes()[:-7])
        raise OSError("simulated partial append")

    monkeypatch.setattr(capture_module, "_append_stage_record", append_then_tear)
    with pytest.raises(OSError, match="partial append"):
        session.feed(
            TimedSignal(
                timestamp=instant(0),
                source="model",
                action="content",
                intensity=0.9,
                text="recover partial JSONL write",
            )
        )
    monkeypatch.setattr(capture_module, "_append_stage_record", append)
    repository.recover_pending()
    recovered = CaptureSession.from_repository(repository, session_id="capture-partial")
    assert [item.text for item in recovered.items()] == ["recover partial JSONL write"]
    assert any(event.kind == EventKind.CAPTURE_RECORD for event in repository.events())


def test_stage_rejects_valid_but_unlogged_jsonl_record(tmp_path: Path) -> None:
    repository = Repository(tmp_path / "stage-unlogged")
    repository.init()
    stage_path = repository.root / ".staging" / "capture-candidates.jsonl"
    candidate = CaptureCandidate(
        candidate_id="unlogged-candidate",
        session_id="capture-unlogged",
        sequence=0,
        timestamp=instant(0),
        source="model",
        text="must have a committed event",
    )
    content = (
        b"---\n"
        b"candidate_id: unlogged-candidate\n"
        b"session_id: capture-unlogged\n"
        b"sequence: 0\n"
        b"timestamp: '1970-01-01T00:00:00+00:00'\n"
        b"source: model\n"
        b"---\n"
        b"must have a committed event"
    )
    _append_stage_record(stage_path, candidate, content)

    with pytest.raises(ValueError, match="lacks a durable intent"):
        CaptureSession.from_repository(repository)


def test_simulation_integrates_full_duration_and_requires_final_end(tmp_path: Path) -> None:
    stream = tmp_path / "stream.jsonl"
    records = [
        {
            "timestamp": "2026-01-01T00:00:00+00:00",
            "source": "human",
            "action": "remember",
            "intensity": 1.0,
        },
        {"timestamp": "2026-01-01T00:00:10+00:00", "source": "rule", "intensity": 0.0},
        {"type": "end", "timestamp": "2026-01-01T00:00:20+00:00"},
    ]
    stream.write_text("\n".join(json.dumps(record) for record in records), encoding="utf-8")
    result = CliRunner().invoke(app, ["simulate", "--stream", str(stream), "--dry-run"])
    assert result.exit_code == 0, result.output
    output = json.loads(result.stdout)
    assert output["full_seconds"] == 20.0
    assert output["total_seconds"] == 20.0
    assert output["duty_cycle"] == 1.0

    stream.write_text(json.dumps(records[0]), encoding="utf-8")
    result = CliRunner().invoke(app, ["simulate", "--stream", str(stream), "--dry-run"])
    assert result.exit_code != 0


def test_simulation_loads_capture_thresholds_from_vault_config(tmp_path: Path) -> None:
    repository = Repository(tmp_path / "vault")
    repository.init()
    config = repository.read_config()
    config["capture"] = {"high_threshold": 0.95, "low_threshold": 0.1}
    repository.configure(config)
    stream = tmp_path / "configured.jsonl"
    records = [
        {"timestamp": "2026-01-01T00:00:00+00:00", "source": "rule", "intensity": 0.9},
        {"type": "end", "timestamp": "2026-01-01T00:00:10+00:00"},
    ]
    stream.write_text("\n".join(json.dumps(record) for record in records), encoding="utf-8")
    result = CliRunner().invoke(
        app,
        ["simulate", "--stream", str(stream), "--vault", str(repository.root), "--dry-run"],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["full_seconds"] == 0.0


def test_mounted_capture_cli_writes_identical_vault_bytes_with_injected_flags(
    tmp_path: Path,
) -> None:
    from memfw.cli import app as main_app

    stream = tmp_path / "mounted.jsonl"
    records = [
        {
            "timestamp": "2026-01-01T00:00:00+00:00",
            "source": "human",
            "action": "remember",
            "intensity": 1.0,
            "text": "stable candidate",
        },
        {"type": "end", "timestamp": "2026-01-01T00:00:10+00:00"},
    ]
    stream.write_text("\n".join(json.dumps(record) for record in records), encoding="utf-8")
    roots = [tmp_path / "mounted-a", tmp_path / "mounted-b"]
    for root in roots:
        result = CliRunner().invoke(
            main_app,
            [
                "--now",
                "2026-01-01T00:00:00+00:00",
                "--id-seed",
                "capture-seed",
                "capture",
                "simulate",
                "--stream",
                str(stream),
                "--vault",
                str(root),
            ],
        )
        assert result.exit_code == 0, result.output
    snapshots = [
        {
            path.relative_to(root).as_posix(): path.read_bytes()
            for path in root.rglob("*")
            if path.is_file() and path.name != ".memfw.lock"
        }
        for root in roots
    ]
    assert snapshots[0] == snapshots[1]


def test_invalid_stream_is_rejected_before_non_dry_run_vault_initialization(
    tmp_path: Path,
) -> None:
    stream = tmp_path / "out-of-order.jsonl"
    records = [
        {
            "timestamp": "2026-01-01T00:00:05+00:00",
            "source": "human",
            "action": "remember",
            "intensity": 1.0,
            "text": "must not be staged",
        },
        {"timestamp": "2026-01-01T00:00:04+00:00", "source": "rule", "intensity": 0.0},
        {"type": "end", "timestamp": "2026-01-01T00:00:10+00:00"},
    ]
    stream.write_text("\n".join(json.dumps(record) for record in records), encoding="utf-8")
    vault = tmp_path / "should-not-be-created"

    result = CliRunner().invoke(app, ["simulate", "--stream", str(stream), "--vault", str(vault)])

    assert result.exit_code != 0
    assert "non-decreasing" in result.output
    assert not vault.exists()
