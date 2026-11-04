"""Explicit capture state machine and human-reviewed candidate staging."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol, cast

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..agents import JudgeAgent
from ..core.context import RepositoryHost
from ..core.models import CaptureState, EventKind
from ..core.repository import Repository

Source = Literal["human", "rule", "model"]
Action = Literal["signal", "content", "forget", "remember", "sleep", "auto"]
Decision = Literal["keep", "discard", "anchor"]

TRANSITIONS: dict[CaptureState, frozenset[CaptureState]] = {
    CaptureState.DORMANT: frozenset({CaptureState.STANDBY, CaptureState.FULL}),
    CaptureState.STANDBY: frozenset({CaptureState.DORMANT, CaptureState.FULL}),
    CaptureState.FULL: frozenset({CaptureState.STANDBY, CaptureState.DORMANT}),
}


class TimedSignal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    timestamp: datetime
    source: Source
    action: Action = "signal"
    intensity: float = Field(default=0.0, ge=0.0, le=1.0)
    text: str | None = None
    source_id: str | None = None

    @field_validator("timestamp")
    @classmethod
    def timestamp_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("capture timestamps must include a timezone")
        return value.astimezone(UTC)


class CaptureCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    candidate_id: str
    session_id: str
    sequence: int
    timestamp: datetime
    source: Source
    source_id: str | None = None
    text: str
    decision: Decision | None = None
    actor: str | None = None

    @field_validator("timestamp")
    @classmethod
    def timestamp_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("capture timestamps must include a timezone")
        return value.astimezone(UTC)

    @property
    def approved(self) -> bool:
        return self.decision in {"keep", "anchor"}


def _read_stage_records(path: Path) -> list[tuple[CaptureCandidate, bytes]]:
    pack_path = path.with_name("capture-candidates.jsonl.zst")
    raw_exists, pack_exists = path.exists(), pack_path.exists()
    if raw_exists and pack_exists:
        raw = path.read_bytes()
        packed = _decompress_stage_pack(pack_path.read_bytes())
        if raw != packed:
            raise ValueError("raw and packed capture staging files disagree; recover explicitly")
    elif raw_exists:
        raw = path.read_bytes()
    elif pack_exists:
        raw = _decompress_stage_pack(pack_path.read_bytes())
    else:
        return []
    if raw and not raw.endswith(b"\n"):
        raise ValueError("staged capture JSONL must end with LF")
    previous = "0" * 64
    result: list[tuple[CaptureCandidate, bytes]] = []
    for number, line in enumerate(raw.splitlines(), 1):
        try:
            record = json.loads(line)
            payload = {key: record[key] for key in ("previous_hash", "content", "content_hash")}
            expected = hashlib.sha256(
                json.dumps(
                    payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ).encode()
            ).hexdigest()
            if record["previous_hash"] != previous or record["record_hash"] != expected:
                raise ValueError("broken candidate hash chain")
            content = record["content"].encode("utf-8")
            if hashlib.sha256(content).hexdigest() != record["content_hash"]:
                raise ValueError("candidate content hash mismatch")
            pieces = content.decode("utf-8").split("\n---\n", 1)
            if len(pieces) != 2 or not pieces[0].startswith("---\n"):
                raise ValueError("invalid candidate frontmatter")
            metadata = yaml.safe_load(pieces[0][4:])
            if not isinstance(metadata, dict):
                raise ValueError("invalid candidate metadata")
            candidate = CaptureCandidate.model_validate({**metadata, "text": pieces[1]})
            result.append((candidate, content))
            previous = expected
        except Exception as exc:
            raise ValueError(
                f"staged capture JSONL integrity failure at line {number}: {exc}"
            ) from exc
    return result


def _decompress_stage_pack(packed: bytes) -> bytes:
    import zstandard

    try:
        return zstandard.ZstdDecompressor().decompress(packed)
    except zstandard.ZstdError as exc:
        raise ValueError("invalid compressed capture staging pack") from exc


def _ensure_raw_stage_locked(repository: RepositoryHost, path: Path) -> None:
    """Restore a verified packed WAL before a mutation, then remove the pack."""
    pack_path = path.with_name("capture-candidates.jsonl.zst")
    if not pack_path.exists():
        return
    packed_raw = _decompress_stage_pack(pack_path.read_bytes())
    if path.exists() and path.read_bytes() != packed_raw:
        raise ValueError("raw and packed capture staging files disagree; recover explicitly")
    if not path.exists():
        repository._atomic_write(path, packed_raw)
        _read_stage_records(path)
    pack_path.unlink()


def pack_capture_candidates(repository: Repository) -> dict[str, int | str]:
    """Pack the complete candidate WAL after a session; the compressed copy replaces raw WAL."""
    import zstandard

    path = repository.root / ".staging" / "capture-candidates.jsonl"
    pack_path = path.with_name("capture-candidates.jsonl.zst")
    with repository._locked():
        if not path.exists() and not pack_path.exists():
            return {"raw_bytes": 0, "packed_bytes": 0, "records": 0}
        _recover_stage_intent(repository, path)
        records = _read_stage_records(path)
        event_hashes = {
            str(
                event.details.get("candidate_id", event.details.get("proposal_id"))
            ): event.details.get("source_hash")
            for event in repository.events()
            if event.kind == EventKind.CAPTURE_RECORD
        }
        for candidate, content in records:
            if event_hashes.get(candidate.candidate_id) != hashlib.sha256(content).hexdigest():
                raise ValueError(
                    f"capture candidate lacks a matching durable event: {candidate.candidate_id}"
                )
        raw = path.read_bytes() if path.exists() else _decompress_stage_pack(pack_path.read_bytes())
        packed = zstandard.ZstdCompressor(level=8, threads=0, write_checksum=True).compress(raw)
        if _decompress_stage_pack(packed) != raw:
            raise ValueError("capture staging pack failed byte-for-byte verification")
        repository._atomic_write(pack_path, packed)
        if _decompress_stage_pack(pack_path.read_bytes()) != raw:
            raise ValueError("committed capture staging pack does not match source WAL")
        path.unlink(missing_ok=True)
        return {"raw_bytes": len(raw), "packed_bytes": len(packed), "records": len(records)}


def _rewrite_stage(
    path: Path,
    records: list[tuple[CaptureCandidate, bytes]],
    repository: Repository | None = None,
) -> None:
    previous = "0" * 64
    lines: list[bytes] = []
    for _candidate, content in records:
        content_hash = hashlib.sha256(content).hexdigest()
        payload = {
            "previous_hash": previous,
            "content": content.decode("utf-8"),
            "content_hash": content_hash,
        }
        record_hash = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        lines.append(
            (
                json.dumps(
                    {**payload, "record_hash": record_hash},
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode()
        )
        previous = record_hash
    data = b"".join(lines)
    path.parent.mkdir(parents=True, exist_ok=True)
    if repository is not None:
        repository._atomic_write(path, data)
    else:
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_bytes(data)
        temporary.replace(path)


def _recover_stage_intent(repository: RepositoryHost, path: Path) -> bool:
    intent_path = path.with_name("capture-candidates.pending.json")
    if not intent_path.exists():
        return False
    _ensure_raw_stage_locked(repository, path)
    try:
        intent = json.loads(intent_path.read_text(encoding="utf-8"))
        candidate = CaptureCandidate.model_validate(intent["candidate"])
        content = str(intent["content"]).encode("utf-8")
        base_size = int(intent["base_size"])
        content_hash = hashlib.sha256(content).hexdigest()
        if content_hash != intent["content_hash"]:
            raise ValueError("intent content hash mismatch")
        current = path.read_bytes() if path.exists() else b""
        if len(current) < base_size or (base_size and not current[:base_size].endswith(b"\n")):
            raise ValueError("stage prefix differs from pending intent")
        prefix = current[:base_size]
        if hashlib.sha256(prefix).hexdigest() != intent["prefix_hash"]:
            raise ValueError("stage prefix hash mismatch")
        tail = current[base_size:]
        if tail:
            try:
                _read_stage_records(path)
                records = _read_stage_records(path)
            except ValueError:
                # Only bytes after the exact recorded prefix belong to this
                # single in-flight append and may be truncated/retried.
                repository._atomic_write(path, prefix)
                records = _read_stage_records(path)
            present = next(
                (raw for item, raw in records if item.candidate_id == candidate.candidate_id),
                None,
            )
            if present is not None and present != content:
                raise ValueError("pending candidate ID has different staged content")
            if present is None:
                if len(records) != prefix.count(b"\n"):
                    raise ValueError("unexpected records after pending append prefix")
                _append_stage_record(path, candidate, content)
            elif not records or records[-1][0].candidate_id != candidate.candidate_id:
                raise ValueError("pending candidate append is not the last staged record")
        else:
            _append_stage_record(path, candidate, content)
        source_hash = content_hash
        existing = next(
            (
                event
                for event in reversed(repository.events())
                if event.kind == EventKind.CAPTURE_RECORD
                and event.details.get("candidate_id") == candidate.candidate_id
            ),
            None,
        )
        if existing is None:
            repository.capture_event(
                EventKind.CAPTURE_RECORD,
                actor="mechanism",
                timestamp=candidate.timestamp,
                details={
                    "candidate_id": candidate.candidate_id,
                    "proposal_id": candidate.candidate_id,
                    "source": candidate.source,
                    "session_id": candidate.session_id,
                    "source_hash": source_hash,
                },
            )
        elif existing.details.get("source_hash") != source_hash:
            raise ValueError("pending stage intent conflicts with committed capture event")
        intent_path.unlink()
        return True
    except Exception as exc:
        raise ValueError(f"Unable to recover pending capture stage append: {exc}") from exc


def _append_stage_record(path: Path, candidate: CaptureCandidate, content: bytes) -> None:
    """Durably append one authenticated record; resolved decisions use atomic rewrite."""
    _read_stage_records(path)
    previous = "0" * 64
    if path.exists() and path.stat().st_size:
        record = json.loads(path.read_bytes().splitlines()[-1])
        previous = str(record["record_hash"])
    content_hash = hashlib.sha256(content).hexdigest()
    payload = {
        "previous_hash": previous,
        "content": content.decode("utf-8"),
        "content_hash": content_hash,
    }
    record_hash = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    line = (
        json.dumps(
            {**payload, "record_hash": record_hash},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    try:
        os.write(fd, line)
        os.fsync(fd)
    finally:
        os.close(fd)


class CaptureController(Protocol):
    def current_state(self) -> CaptureState: ...

    def record_state(self, state: CaptureState, *, actor: str) -> None: ...


@dataclass(frozen=True)
class CaptureEvent:
    kind: EventKind
    timestamp: datetime
    source: Source
    intensity: float
    state: CaptureState


class CaptureSession:
    """Time-driven capture session with an in-RAM standby ring and staged full candidates."""

    def __init__(
        self,
        *,
        repository: Repository | None = None,
        session_id: str = "capture-session",
        initial_state: CaptureState = CaptureState.DORMANT,
        high_threshold: float = 0.7,
        low_threshold: float = 0.3,
        standby_seconds: float = 30.0,
        dormant_seconds: float = 120.0,
        ring_seconds: float = 30.0,
        load_staged: bool = True,
    ) -> None:
        if not (0 <= low_threshold < high_threshold <= 1):
            raise ValueError("thresholds must satisfy 0 <= low < high <= 1")
        if min(standby_seconds, dormant_seconds, ring_seconds) < 0:
            raise ValueError("capture durations must be non-negative")
        if not session_id or (
            session_id != "*" and not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", session_id)
        ):
            raise ValueError("session_id must be a short safe identifier")
        self.repository = repository
        self.session_id = session_id
        self.state = initial_state
        self.high_threshold = high_threshold
        self.low_threshold = low_threshold
        self.standby_seconds = standby_seconds
        self.dormant_seconds = dormant_seconds
        self.ring_seconds = ring_seconds
        self._ring: list[TimedSignal] = []
        self._items: dict[str, CaptureCandidate] = {}
        self._stage_hashes: dict[str, str] = {}
        self._decided_ids: set[str] = set()
        self._sequence = 0
        self._human_lock: Literal["full", "dormant"] | None = None
        self._low_since: datetime | None = None
        self._standby_since: datetime | None = None
        self._last_timestamp: datetime | None = None
        self._last_event_timestamp: datetime | None = None
        self.events: list[CaptureEvent] = []
        self._staging = self._staging_path()
        if self.repository is not None:
            for event in self.repository.events():
                candidate_id = event.details.get("candidate_id")
                if candidate_id is None:
                    candidate_id = event.details.get("proposal_id")
                decision = event.details.get("decision")
                if (
                    event.kind == EventKind.HUMAN_DECISION
                    and event.actor == "human"
                    and isinstance(candidate_id, str)
                    and decision in {"keep", "discard", "anchor"}
                ):
                    self._decided_ids.add(candidate_id)
        if load_staged:
            self._load_staged()

    @classmethod
    def from_repository(cls, repository: Repository, *, session_id: str = "*") -> CaptureSession:
        """Load staged candidates for review; this does not restore an active session."""
        return cls(repository=repository, session_id=session_id)

    def _staging_path(self) -> Path | None:
        if self.repository is None:
            return None
        return self.repository.root / ".staging" / "capture-candidates.jsonl"

    def _load_staged(self) -> None:
        if self._staging is None:
            return
        if self.repository is not None:
            with self.repository._locked():
                _recover_stage_intent(self.repository, self._staging)
        recorded_hashes: dict[str, str] = {}
        decided_ids: set[str] = set()
        if self.repository is not None:
            for event in self.repository.events():
                candidate_id = event.details.get("candidate_id")
                if candidate_id is None:
                    candidate_id = event.details.get("proposal_id")
                source_hash = event.details.get("source_hash")
                if (
                    event.kind == EventKind.CAPTURE_RECORD
                    and isinstance(candidate_id, str)
                    and isinstance(source_hash, str)
                ):
                    recorded_hashes[candidate_id] = source_hash
                decision = event.details.get("decision")
                if (
                    event.kind == EventKind.HUMAN_DECISION
                    and event.actor == "human"
                    and isinstance(candidate_id, str)
                    and decision in {"keep", "discard", "anchor"}
                ):
                    decided_ids.add(candidate_id)
        records = _read_stage_records(self._staging)
        unlogged = {candidate.candidate_id for candidate, _ in records} - set(recorded_hashes)
        if unlogged:
            raise ValueError(
                f"staged capture candidate lacks a durable intent: {sorted(unlogged)[0]}"
            )
        file_ids = {candidate.candidate_id for candidate, _ in records}
        missing = set(recorded_hashes) - file_ids - decided_ids
        if missing:
            raise ValueError(f"staged capture candidate is missing: {sorted(missing)[0]}")
        for candidate, raw in records:
            digest = hashlib.sha256(raw).hexdigest()
            candidate_id = candidate.candidate_id
            if self.repository is not None:
                expected_hash = recorded_hashes.get(candidate_id)
                if expected_hash is None or digest != expected_hash:
                    raise ValueError(
                        f"staged capture candidate integrity check failed: {candidate_id}"
                    )
                self._stage_hashes[candidate_id] = expected_hash
            if self.session_id == "*" or candidate.session_id == self.session_id:
                self._items[candidate_id] = candidate
                self._sequence = max(self._sequence, candidate.sequence + 1)
        if self.repository is not None:
            decisions: dict[str, Decision] = {}
            for event in self.repository.events():
                candidate_id = event.details.get("candidate_id")
                if candidate_id is None:
                    candidate_id = event.details.get("proposal_id")
                decision = event.details.get("decision")
                if (
                    event.kind == EventKind.HUMAN_DECISION
                    and event.actor == "human"
                    and isinstance(candidate_id, str)
                    and decision in {"keep", "discard", "anchor"}
                ):
                    decisions[candidate_id] = cast(Decision, decision)
            for candidate_id, decision in decisions.items():
                if candidate_id in self._items:
                    self._items[candidate_id] = self._items[candidate_id].model_copy(
                        update={"decision": decision, "actor": "human"}
                    )
            if decided_ids:
                with self.repository._locked():
                    _rewrite_stage(
                        self._staging,
                        [
                            (candidate, raw)
                            for candidate, raw in records
                            if candidate.candidate_id not in decided_ids
                        ],
                        self.repository,
                    )

    def current_state(self) -> CaptureState:
        return self.state

    def record_state(
        self,
        state: CaptureState,
        *,
        actor: str,
        timestamp: datetime | None = None,
    ) -> None:
        if actor not in {"human", "mechanism", "rule", "model"}:
            raise ValueError("capture state actor must be human, mechanism, rule, or model")
        if self._human_lock is not None and actor != "human":
            raise PermissionError("human capture lock blocks automatic state changes")
        event_time = timestamp or self._last_timestamp or datetime(2000, 1, 1, tzinfo=UTC)
        source: Source = cast(Source, actor) if actor in {"human", "rule", "model"} else "rule"
        self._transition(
            state,
            actor=actor,
            timestamp=event_time,
            source=source,
            intensity=0.0,
        )
        if actor == "human" and state == CaptureState.FULL:
            self._human_lock = "full"
        elif actor == "human" and state == CaptureState.DORMANT:
            self._human_lock = "dormant"

    def _record(self, event: CaptureEvent, *, actor: str) -> None:
        self.events.append(event)
        if self.repository is not None:
            elapsed = 0.0
            if self._last_event_timestamp is not None:
                elapsed = max(0.0, (event.timestamp - self._last_event_timestamp).total_seconds())
            self._last_event_timestamp = event.timestamp
            self.repository.capture_event(
                event.kind,
                actor="human" if actor == "human" else "mechanism",
                timestamp=event.timestamp,
                details={
                    "state": event.state.value,
                    "source": event.source,
                    "intensity": event.intensity,
                    "elapsed_seconds": elapsed,
                },
            )

    def _transition(
        self,
        state: CaptureState,
        *,
        actor: str,
        timestamp: datetime,
        source: Source,
        intensity: float,
    ) -> None:
        if state not in TRANSITIONS[self.state]:
            raise ValueError(f"illegal capture transition: {self.state.value} -> {state.value}")
        prior = self.state
        self.state = state
        if state == CaptureState.STANDBY:
            self._standby_since = timestamp
        elif state == CaptureState.DORMANT:
            self._standby_since = None
            self._low_since = None
        elif state == CaptureState.FULL:
            self._low_since = None
        kind = (
            EventKind.CAPTURE_WAKE
            if state == CaptureState.FULL
            else (
                EventKind.CAPTURE_SLEEP
                if state == CaptureState.DORMANT
                else EventKind.CAPTURE_STATE_CHANGE
            )
        )
        self._record(CaptureEvent(kind, timestamp, source, intensity, state), actor=actor)
        if prior != state and kind != EventKind.CAPTURE_STATE_CHANGE:
            self._record(
                CaptureEvent(EventKind.CAPTURE_STATE_CHANGE, timestamp, source, intensity, state),
                actor=actor,
            )

    def feed(self, signal: TimedSignal) -> CaptureState:
        timestamp = signal.timestamp
        if self._last_timestamp is not None and timestamp < self._last_timestamp:
            raise ValueError("capture stream timestamps must be non-decreasing")
        self._advance_time(timestamp)
        self._last_timestamp = timestamp
        self._prune_ring(timestamp)

        if signal.action == "forget" and signal.source == "human":
            self._human_lock = "dormant"
            self._ring.clear()
            if self.state != CaptureState.DORMANT:
                self._transition(
                    CaptureState.DORMANT,
                    actor="human",
                    timestamp=timestamp,
                    source="human",
                    intensity=signal.intensity,
                )
            return self.state
        if signal.action == "remember" and signal.source == "human":
            self._human_lock = "full"
            if self.state != CaptureState.FULL:
                self._transition(
                    CaptureState.FULL,
                    actor="human",
                    timestamp=timestamp,
                    source="human",
                    intensity=1.0,
                )
            self._backfill(timestamp)
            if signal.text:
                self._stage_candidate(signal, text=signal.text)
            return self.state
        if signal.action == "auto" and signal.source == "human":
            self._human_lock = None
            return self.state
        if self._human_lock is not None and signal.source != "human":
            if signal.action == "content" and signal.text and self._human_lock == "full":
                self._stage_candidate(signal, text=signal.text)
            return self.state
        if signal.action == "sleep":
            if signal.source == "human":
                self._human_lock = "dormant"
                self._ring.clear()
            if self.state != CaptureState.DORMANT:
                self._transition(
                    CaptureState.DORMANT,
                    actor=signal.source,
                    timestamp=timestamp,
                    source=signal.source,
                    intensity=signal.intensity,
                )
            return self.state

        if signal.action == "content" and signal.text:
            if self.state == CaptureState.FULL:
                self._stage_candidate(signal, text=signal.text)
            elif self.state == CaptureState.STANDBY and signal.intensity < self.high_threshold:
                self._ring.append(signal)

        if self._human_lock is not None:
            return self.state

        if signal.intensity >= self.high_threshold:
            self._low_since = None
            if self.state != CaptureState.FULL:
                self._transition(
                    CaptureState.FULL,
                    actor=signal.source,
                    timestamp=timestamp,
                    source=signal.source,
                    intensity=signal.intensity,
                )
                self._backfill(timestamp)
                if signal.action == "content" and signal.text:
                    self._stage_candidate(signal, text=signal.text)
            return self.state

        if signal.intensity <= self.low_threshold:
            if self._low_since is None:
                self._low_since = timestamp
            low_elapsed = (timestamp - self._low_since).total_seconds()
            if self.state == CaptureState.FULL and low_elapsed >= self.standby_seconds:
                self._transition(
                    CaptureState.STANDBY,
                    actor=signal.source,
                    timestamp=timestamp,
                    source=signal.source,
                    intensity=signal.intensity,
                )
            elif self.state == CaptureState.STANDBY and self._standby_since is not None:
                standby_elapsed = (timestamp - self._standby_since).total_seconds()
                if low_elapsed >= self.standby_seconds and standby_elapsed >= self.dormant_seconds:
                    self._transition(
                        CaptureState.DORMANT,
                        actor=signal.source,
                        timestamp=timestamp,
                        source=signal.source,
                        intensity=signal.intensity,
                    )
        else:
            self._low_since = None
        return self.state

    def feed_model_text(self, timestamp: datetime, text: str, judge: JudgeAgent) -> CaptureState:
        """Use the selected judge's intensity score as an explicit model signal."""
        return self.feed(
            TimedSignal(
                timestamp=timestamp,
                source="model",
                action="content",
                intensity=judge.wake_intensity(text),
                text=text,
            )
        )

    def advance_to(self, timestamp: datetime) -> CaptureState:
        """Advance timers through an explicit stream boundary without sleeping."""
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("capture timestamps must include a timezone")
        timestamp = timestamp.astimezone(UTC)
        if self._last_timestamp is not None and timestamp < self._last_timestamp:
            raise ValueError("capture stream timestamps must be non-decreasing")
        self._advance_time(timestamp)
        self._last_timestamp = timestamp
        self._prune_ring(timestamp)
        return self.state

    def _advance_time(self, now: datetime) -> None:
        """Apply sustained-low timers at their exact boundary before the next input."""
        if self._human_lock is not None or self._low_since is None:
            return
        if self.state == CaptureState.FULL:
            standby_at = self._low_since.timestamp() + self.standby_seconds
            if standby_at <= now.timestamp():
                self._transition(
                    CaptureState.STANDBY,
                    actor="rule",
                    timestamp=datetime.fromtimestamp(standby_at, tz=UTC),
                    source="rule",
                    intensity=self.low_threshold,
                )
        if self.state == CaptureState.STANDBY and self._standby_since is not None:
            dormant_at = self._standby_since.timestamp() + self.dormant_seconds
            if dormant_at <= now.timestamp():
                self._transition(
                    CaptureState.DORMANT,
                    actor="rule",
                    timestamp=datetime.fromtimestamp(dormant_at, tz=UTC),
                    source="rule",
                    intensity=self.low_threshold,
                )

    def _prune_ring(self, now: datetime) -> None:
        boundary = now.timestamp() - self.ring_seconds
        self._ring = [item for item in self._ring if item.timestamp.timestamp() >= boundary]

    def _backfill(self, now: datetime) -> None:
        self._prune_ring(now)
        for signal in self._ring:
            if signal.text:
                self._stage_candidate(signal, text=signal.text)
        self._ring.clear()

    def _stage_candidate(self, signal: TimedSignal, *, text: str) -> CaptureCandidate:
        sequence = self._sequence
        self._sequence += 1
        identity = (
            f"{self.session_id}\n{sequence}\n{signal.timestamp.isoformat()}\n"
            f"{signal.source}\n{signal.source_id or ''}\n{text}"
        )
        digest = hashlib.sha256(identity.encode()).hexdigest()
        candidate_id = digest[:24]
        candidate = CaptureCandidate(
            candidate_id=candidate_id,
            session_id=self.session_id,
            sequence=sequence,
            timestamp=signal.timestamp,
            source=signal.source,
            source_id=signal.source_id,
            text=text,
        )
        if candidate_id in self._decided_ids:
            return candidate
        self._items[candidate_id] = candidate
        if self._staging is not None:
            self._write_candidate(candidate)
        return candidate

    def _write_candidate(self, candidate: CaptureCandidate) -> None:
        if self._staging is None:
            return
        metadata = candidate.model_dump(mode="json", exclude={"text"})
        frontmatter = yaml.safe_dump(metadata, allow_unicode=True, sort_keys=True).rstrip()
        content = f"---\n{frontmatter}\n---\n{candidate.text}"
        path = self._staging
        if self.repository is None:
            path.parent.mkdir(parents=True, exist_ok=True)
            records = _read_stage_records(path)
            _rewrite_stage(path, [*records, (candidate, content.encode("utf-8"))])
            return
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        with self.repository._locked():
            _ensure_raw_stage_locked(self.repository, path)
            path.parent.mkdir(parents=True, exist_ok=True)
            records = _read_stage_records(path)
            if any(item.candidate_id == candidate.candidate_id for item, _ in records):
                raise ValueError(f"duplicate staged capture candidate: {candidate.candidate_id}")
            intent_path = path.with_name("capture-candidates.pending.json")
            prefix = path.read_bytes() if path.exists() else b""
            self.repository._atomic_write(
                intent_path,
                json.dumps(
                    {
                        "candidate": candidate.model_dump(mode="json"),
                        "content": content,
                        "content_hash": content_hash,
                        "base_size": len(prefix),
                        "prefix_hash": hashlib.sha256(prefix).hexdigest(),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode(),
            )
            _append_stage_record(path, candidate, content.encode("utf-8"))
            self.repository.capture_event(
                EventKind.CAPTURE_RECORD,
                actor="mechanism",
                timestamp=self._last_timestamp or candidate.timestamp,
                details={
                    "candidate_id": candidate.candidate_id,
                    "proposal_id": candidate.candidate_id,
                    "source": candidate.source,
                    "session_id": candidate.session_id,
                    "source_hash": content_hash,
                },
            )
            intent_path.unlink(missing_ok=True)
        self._stage_hashes[candidate.candidate_id] = content_hash

    def items(self, *, include_decided: bool = False) -> list[CaptureCandidate]:
        values = sorted(self._items.values(), key=lambda item: (item.timestamp, item.sequence))
        if include_decided:
            return values
        return [item for item in values if item.decision is None]

    def confirm_candidate(
        self, candidate_id: str, decision: Decision, *, actor: str = "human"
    ) -> CaptureCandidate:
        if actor != "human":
            raise PermissionError("only a human may confirm a capture candidate")
        if decision == "anchor" and actor != "human":
            raise PermissionError("anchor decisions require a human")
        if self.repository is None:
            return self._confirm_candidate_locked(candidate_id, decision, actor=actor)
        with self.repository._locked():
            if self._staging is not None:
                _ensure_raw_stage_locked(self.repository, self._staging)
            return self._confirm_candidate_locked(candidate_id, decision, actor=actor)

    def _confirm_candidate_locked(
        self, candidate_id: str, decision: Decision, *, actor: str
    ) -> CaptureCandidate:
        try:
            current = self._items[candidate_id]
        except KeyError as exc:
            raise KeyError(f"unknown staged candidate: {candidate_id}") from exc
        if current.decision is not None:
            raise ValueError("candidate already has a decision")
        if self.repository is not None:
            for event in self.repository.events():
                recorded_id = event.details.get("candidate_id")
                if recorded_id is None:
                    recorded_id = event.details.get("proposal_id")
                if (
                    event.kind == EventKind.HUMAN_DECISION
                    and event.actor == "human"
                    and recorded_id == candidate_id
                    and event.details.get("decision") in {"keep", "discard", "anchor"}
                ):
                    raise ValueError("candidate already has a persisted decision")
        staged_path: Path | None = None
        if self._staging is not None and self.repository is not None:
            staged_path = self._staging
            records = _read_stage_records(staged_path)
            found = next((raw for item, raw in records if item.candidate_id == candidate_id), None)
            if found is None:
                raise ValueError("staged candidate is missing")
            current_hash = hashlib.sha256(found).hexdigest()
            if current_hash != self._stage_hashes.get(candidate_id):
                raise ValueError("staged candidate changed after capture; refusing confirmation")
        updated = current.model_copy(update={"decision": decision, "actor": actor})
        if self.repository is not None:
            self.repository.capture_event(
                EventKind.HUMAN_DECISION,
                actor="human",
                details={
                    "candidate_id": candidate_id,
                    "proposal_id": candidate_id,
                    "decision": decision,
                    "source_hash": self._stage_hashes.get(
                        candidate_id, hashlib.sha256(current.text.encode("utf-8")).hexdigest()
                    ),
                },
            )
            self._decided_ids.add(candidate_id)
        if staged_path is not None and self.repository is not None:
            with self.repository._locked():
                _rewrite_stage(
                    staged_path,
                    [(item, raw) for item, raw in records if item.candidate_id != candidate_id],
                    self.repository,
                )
        self._items[candidate_id] = updated
        return updated


__all__ = [
    "Action",
    "CaptureCandidate",
    "CaptureController",
    "CaptureEvent",
    "CaptureSession",
    "Decision",
    "Source",
    "TRANSITIONS",
    "TimedSignal",
    "pack_capture_candidates",
]
