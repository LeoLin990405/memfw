"""Capture stream replay command. Mount ``app`` as ``memfw capture``."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, cast

import typer
from pydantic import BaseModel, ConfigDict, ValidationError

from ..core.models import CaptureState
from ..core.repository import Repository
from . import CaptureEvent, CaptureSession, TimedSignal

app = typer.Typer(help="Replay capture state streams.")


@app.callback()
def capture_root() -> None:
    """Simulate capture streams and review staged candidates."""


class _StreamEnd(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    type: str = "end"
    timestamp: datetime


def _object_from_json(line: str, line_number: int) -> dict[str, object]:
    value: object = json.loads(line)
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"line {line_number}: expected a JSON object")
    return cast(dict[str, object], value)


def _read_stream(path: Path) -> tuple[list[TimedSignal], datetime, str]:
    raw_bytes = path.read_bytes()
    text = raw_bytes.decode("utf-8")
    signals: list[TimedSignal] = []
    end_time: datetime | None = None
    end_seen = False
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        if end_seen:
            raise ValueError(f"line {line_number}: stream end must be the final record")
        try:
            record = _object_from_json(line, line_number)
            if record.get("type") == "end":
                marker = _StreamEnd.model_validate(record)
                end_time = marker.timestamp
                end_seen = True
                continue
            signal = TimedSignal.model_validate(record)
        except (ValueError, ValidationError) as exc:
            raise ValueError(f"line {line_number}: {exc}") from exc
        signals.append(signal)
    if end_time is None:
        raise ValueError('stream must end with an explicit {"type":"end","timestamp":...} record')
    if not signals:
        raise ValueError("stream must contain at least one signal")
    if end_time.tzinfo is None or end_time.utcoffset() is None:
        raise ValueError("stream end timestamp must include a timezone")
    end_time = end_time.astimezone(UTC)
    if end_time < signals[-1].timestamp:
        raise ValueError("stream end timestamp must not precede the last signal")
    for previous, current in zip(signals, signals[1:], strict=False):
        if current.timestamp < previous.timestamp:
            raise ValueError("capture stream timestamps must be non-decreasing")
    return signals, end_time, hashlib.sha256(raw_bytes).hexdigest()


def _timeline(events: list[CaptureEvent], start: datetime, end: datetime) -> dict[str, object]:
    ordered = sorted(enumerate(events), key=lambda pair: (pair[1].timestamp, pair[0]))
    state = CaptureState.DORMANT
    cursor = start
    full_seconds = 0.0
    transitions: list[dict[str, object]] = []
    for _, event in ordered:
        if event.timestamp < start or event.timestamp > end:
            continue
        elapsed = max(0.0, (event.timestamp - cursor).total_seconds())
        if state == CaptureState.FULL:
            full_seconds += elapsed
        cursor = event.timestamp
        state = event.state
        transitions.append(
            {
                "timestamp": event.timestamp.isoformat(),
                "event": event.kind.value,
                "source": event.source,
                "intensity": event.intensity,
                "state": event.state.value,
            }
        )
    tail = max(0.0, (end - cursor).total_seconds())
    if state == CaptureState.FULL:
        full_seconds += tail
    total = max(0.0, (end - start).total_seconds())
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "total_seconds": total,
        "full_seconds": full_seconds,
        "duty_cycle": (full_seconds / total) if total else 0.0,
        "timeline": transitions,
    }


def _capture_config(repository: Repository | None) -> dict[str, float]:
    defaults = {
        "high_threshold": 0.7,
        "low_threshold": 0.3,
        "standby_seconds": 30.0,
        "dormant_seconds": 120.0,
        "ring_seconds": 30.0,
    }
    if repository is None or not repository.config_path.exists():
        return defaults
    raw_config = repository.read_config().get("capture", {})
    if not isinstance(raw_config, dict):
        raise ValueError("vault capture configuration must be a mapping")
    for key in defaults:
        value = raw_config.get(key, defaults[key])
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"capture.{key} must be numeric")
        defaults[key] = float(value)
    if not 0 <= defaults["low_threshold"] < defaults["high_threshold"] <= 1:
        raise ValueError("capture thresholds must satisfy 0 <= low < high <= 1")
    if min(defaults["standby_seconds"], defaults["dormant_seconds"], defaults["ring_seconds"]) < 0:
        raise ValueError("capture durations must be non-negative")
    return defaults


@app.command("simulate")
def simulate(
    stream: Annotated[
        Path, typer.Option(..., "--stream", exists=True, file_okay=True, dir_okay=False)
    ],
    vault: Annotated[Path | None, typer.Option("--vault")] = None,
    session_id: Annotated[str | None, typer.Option("--session-id")] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Do not write events or staged content.")
    ] = False,
) -> None:
    """Replay JSONL signals and print exact state timeline and full-capture duty cycle."""
    try:
        signals, end_time, stream_hash = _read_stream(stream)
        repository: Repository | None = None
        config_repository = Repository(vault) if vault is not None else None
        capture_config = _capture_config(config_repository)
        if not dry_run:
            if config_repository is None:
                raise ValueError("--vault is required unless --dry-run is set")
            # Reuse the root CLI's injected seed when present. Otherwise derive event
            # IDs from the stream and current event count so replay is still stable.
            from .. import cli as root_cli

            if root_cli._ID_SEED is not None:
                id_factory = root_cli._id_factory(config_repository.root)
            else:
                event_path = config_repository.events_path
                sequence = (
                    len([line for line in event_path.read_bytes().splitlines() if line])
                    if event_path.exists()
                    else 0
                )

                def id_factory() -> str:
                    nonlocal sequence
                    sequence += 1
                    return hashlib.sha256(f"{stream_hash}:{sequence}".encode()).hexdigest()[:32]

            repository = Repository(
                config_repository.root,
                clock=lambda: root_cli._NOW or datetime(2000, 1, 1, tzinfo=UTC),
                id_factory=id_factory,
            )
            repository.init()
        session_id_value = session_id or f"sim-{stream_hash[:16]}"
        session = CaptureSession(
            repository=repository,
            session_id=session_id_value,
            load_staged=False,
            high_threshold=capture_config["high_threshold"],
            low_threshold=capture_config["low_threshold"],
            standby_seconds=capture_config["standby_seconds"],
            dormant_seconds=capture_config["dormant_seconds"],
            ring_seconds=capture_config["ring_seconds"],
        )
        for signal in signals:
            session.feed(signal)
        session.advance_to(end_time)
        result = _timeline(session.events, signals[0].timestamp, end_time)
        result["session_id"] = session.session_id
        result["stream_hash"] = stream_hash
        result["candidate_ids"] = [candidate.candidate_id for candidate in session.items()]
        result["dry_run"] = dry_run
        typer.echo(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    except (OSError, UnicodeDecodeError, ValueError, ValidationError) as exc:
        raise typer.BadParameter(str(exc)) from exc


__all__ = ["app", "simulate"]
