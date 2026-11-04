"""Append-only, hash-chained JSONL event log helpers."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ..core.errors import IntegrityError
from ..core.models import Event

GENESIS = "0" * 64


def event_digest(event: Event) -> str:
    payload = event.model_dump(mode="json", exclude={"event_hash"})
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def read_events(
    path: Path, *, verify_head: bool = True, expected_prev: str = GENESIS
) -> list[Event]:
    """Read and validate canonical JSONL, hash chain, and committed head."""
    if not path.exists():
        return []
    result: list[Event] = []
    chain_prev = expected_prev
    raw = path.read_bytes()
    if raw and not raw.endswith(b"\n"):
        raise IntegrityError("Event log must end with LF")
    if b"\r" in raw:
        raise IntegrityError("Event log must use canonical LF line endings")
    raw_lines = raw[:-1].split(b"\n") if raw else []
    for number, raw_line in enumerate(raw_lines, start=1):
        if not raw_line:
            raise IntegrityError(f"Blank event line at line {number}")
        try:
            line = raw_line.decode("utf-8")
            event = Event.model_validate_json(line)
        except Exception as exc:
            raise IntegrityError(f"Invalid event at line {number}: {exc}") from exc
        canonical = event.model_dump_json()
        if line != canonical:
            raise IntegrityError(f"Non-canonical event at line {number}")
        if event.prev_event_hash != chain_prev:
            raise IntegrityError(f"Broken event hash chain at line {number}")
        if event_digest(event) != event.event_hash:
            raise IntegrityError(f"Event hash mismatch at line {number}")
        chain_prev = event.event_hash
        result.append(event)
    state_path = path.with_name("events.state.json")
    head_path = path.with_name("events.head")
    if verify_head:
        if state_path.exists():
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
                head = state["last_hash"]
            except (OSError, ValueError, TypeError, KeyError) as exc:
                raise IntegrityError(f"Event state is invalid: {exc}") from exc
            if head_path.exists() and head_path.read_text(encoding="ascii").strip() != head:
                raise IntegrityError("Legacy event head conflicts with combined event state")
            legacy_cursor = path.with_name("events.cursor.json")
            if legacy_cursor.exists():
                try:
                    old = json.loads(legacy_cursor.read_text(encoding="utf-8"))
                    if any(
                        old.get(key) != state.get(key)
                        for key in ("catalog_hash", "offset", "event_count", "last_hash")
                    ):
                        raise IntegrityError("Legacy cursor conflicts with combined event state")
                except (OSError, ValueError, TypeError) as exc:
                    raise IntegrityError(f"Legacy event cursor is invalid: {exc}") from exc
        elif head_path.exists():
            head = head_path.read_text(encoding="ascii").strip()
        else:
            head = None
        if result and head != result[-1].event_hash:
            raise IntegrityError("Event head is missing or does not match the log")
        elif not result and head is not None and head != expected_prev:
            raise IntegrityError("Empty event log head does not match its archived anchor")
    return result
