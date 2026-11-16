"""The capture candidate WAL under ``.staging/``: record format, reads, crash recovery.

The vault kernel owns this file: ``Repository.recover_pending`` replays an
interrupted append from ``capture-candidates.pending.json`` before anything else
reads the stage, so the record model and the recovery live here, below
:mod:`memfw.capture` (which re-exports them under their old private names).
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, field_validator

from ..core.context import RepositoryHost
from ..core.models import EventKind

Source = Literal["human", "rule", "model"]
Decision = Literal["keep", "discard", "anchor"]


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


def read_stage_records(path: Path) -> list[tuple[CaptureCandidate, bytes]]:
    pack_path = path.with_name("capture-candidates.jsonl.zst")
    raw_exists, pack_exists = path.exists(), pack_path.exists()
    if raw_exists and pack_exists:
        raw = path.read_bytes()
        packed = decompress_stage_pack(pack_path.read_bytes())
        if raw != packed:
            raise ValueError("raw and packed capture staging files disagree; recover explicitly")
    elif raw_exists:
        raw = path.read_bytes()
    elif pack_exists:
        raw = decompress_stage_pack(pack_path.read_bytes())
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


def decompress_stage_pack(packed: bytes) -> bytes:
    import zstandard

    try:
        return zstandard.ZstdDecompressor().decompress(packed)
    except zstandard.ZstdError as exc:
        raise ValueError("invalid compressed capture staging pack") from exc


def ensure_raw_stage_locked(repository: RepositoryHost, path: Path) -> None:
    """Restore a verified packed WAL before a mutation, then remove the pack."""
    pack_path = path.with_name("capture-candidates.jsonl.zst")
    if not pack_path.exists():
        return
    packed_raw = decompress_stage_pack(pack_path.read_bytes())
    if path.exists() and path.read_bytes() != packed_raw:
        raise ValueError("raw and packed capture staging files disagree; recover explicitly")
    if not path.exists():
        repository._atomic_write(path, packed_raw)
        read_stage_records(path)
    pack_path.unlink()


def recover_stage_intent(repository: RepositoryHost, path: Path) -> bool:
    intent_path = path.with_name("capture-candidates.pending.json")
    if not intent_path.exists():
        return False
    ensure_raw_stage_locked(repository, path)
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
                read_stage_records(path)
                records = read_stage_records(path)
            except ValueError:
                # Only bytes after the exact recorded prefix belong to this
                # single in-flight append and may be truncated/retried.
                repository._atomic_write(path, prefix)
                records = read_stage_records(path)
            present = next(
                (raw for item, raw in records if item.candidate_id == candidate.candidate_id),
                None,
            )
            if present is not None and present != content:
                raise ValueError("pending candidate ID has different staged content")
            if present is None:
                if len(records) != prefix.count(b"\n"):
                    raise ValueError("unexpected records after pending append prefix")
                append_stage_record(path, candidate, content)
            elif not records or records[-1][0].candidate_id != candidate.candidate_id:
                raise ValueError("pending candidate append is not the last staged record")
        else:
            append_stage_record(path, candidate, content)
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


def append_stage_record(path: Path, candidate: CaptureCandidate, content: bytes) -> None:
    """Durably append one authenticated record; resolved decisions use atomic rewrite."""
    read_stage_records(path)
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
