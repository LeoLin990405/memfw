"""A stand-in for a newer memfw writer, for the event forward-compatibility tests.

It appends canonical, hash-chained event lines straight to ``events.jsonl``
(without this version's ``Event`` model, as a newer memfw would) and commits
the head in ``events.state.json``. ``build_newer_vault`` makes the vault that
is checked in under ``tests/fixtures/event_forward_compat/newer-log``::

    python -m tests.harness.forward_compat tests/fixtures/event_forward_compat/newer-log
"""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

GENESIS = "0" * 64
NOW = datetime(2026, 11, 17, tzinfo=UTC)
# Field order of the canonical event line (``Event.model_dump_json``).
_FIELDS = (
    "event_id",
    "kind",
    "memory_id",
    "timestamp",
    "actor",
    "before_hash",
    "after_hash",
    "version_ref",
    "details",
    "prev_event_hash",
    "event_hash",
)
MARKER = {"projection": "none"}


def _digest(event: dict[str, Any]) -> str:
    payload = {key: value for key, value in event.items() if key != "event_hash"}
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def append_raw_events(root: Path, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Append events (``event_id``, ``kind``, ``memory_id`` and optional fields)."""
    log = root / "events.jsonl"
    raw = log.read_bytes() if log.exists() else b""
    lines = raw[:-1].split(b"\n") if raw else []
    previous = json.loads(lines[-1])["event_hash"] if lines else GENESIS
    state_path = root / "events.state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    written: list[dict[str, Any]] = []
    with log.open("ab") as stream:
        for partial in events:
            event: dict[str, Any] = {
                "event_id": partial["event_id"],
                "kind": partial["kind"],
                "memory_id": partial["memory_id"],
                "timestamp": partial.get("timestamp", "2026-11-17T00:00:00Z"),
                "actor": partial.get("actor", "mechanism"),
                "before_hash": partial.get("before_hash"),
                "after_hash": partial.get("after_hash"),
                "version_ref": partial.get("version_ref", partial.get("after_hash")),
                "details": partial.get("details", {}),
                "prev_event_hash": previous,
            }
            event["event_hash"] = _digest(event)
            ordered = {key: event[key] for key in _FIELDS}
            stream.write(
                json.dumps(ordered, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
            )
            previous = event["event_hash"]
            written.append(ordered)
    state.update(
        {
            "event_count": int(state["event_count"]) + len(events),
            "last_hash": previous,
            "offset": log.stat().st_size,
        }
    )
    state_path.write_text(
        json.dumps(state, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8"
    )
    (root / "events.cursor.json").unlink(missing_ok=True)
    return written


def build_newer_vault(root: Path) -> Path:
    """A vault this version made, plus events of kinds only a "v0.5" writer knows.

    * ``proxy_retire`` about a proxy id (no memory), twice;
    * ``memory.annotate`` naming ``m-plain`` with an ``after_hash`` that is not
      a memory version, between two real versions of ``m-plain``.

    All carry ``details.projection == "none"``.
    """
    from memfw.core.repository import Repository

    counter = iter(range(1, 1000))
    repository = Repository(
        root,
        clock=lambda: NOW,
        id_factory=lambda: hashlib.sha256(f"fc:{next(counter)}".encode()).hexdigest()[:32],
    )
    repository.init()
    repository.add("Plain", "A plain memory.", memory_id="m-plain")
    append_raw_events(
        root,
        [
            {
                "event_id": "f" * 31 + "1",
                "kind": "proxy_retire",
                "memory_id": "p-0001",
                "details": {**MARKER, "storage": "embedded", "reason": "superseded"},
            },
            {
                "event_id": "f" * 31 + "2",
                "kind": "memory.annotate",
                "memory_id": "m-plain",
                "after_hash": "ab" * 32,
                "details": {**MARKER, "note": {"labels": ["a", "b"], "weight": 0.5}},
            },
        ],
    )
    Repository(root, clock=lambda: NOW).update("m-plain", body="A plain memory, edited.")
    append_raw_events(
        root,
        [
            {
                "event_id": "f" * 31 + "3",
                "kind": "proxy_retire",
                "memory_id": "p-0002",
                "details": {**MARKER},
            }
        ],
    )
    (root / ".memfw.lock").unlink(missing_ok=True)
    return root


if __name__ == "__main__":
    build_newer_vault(Path(sys.argv[1]))
