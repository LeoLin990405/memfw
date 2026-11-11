"""Shared types for ``memfw dream``.

A dream reads the vault and the transcript index and produces *candidates*:
proposed changes a human reviews. It never writes the authoritative vault.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from memfw.core.models import MemoryDocument

Operation = Literal["verify", "curate", "distill", "promote"]
"""① verify, ② curate (duplicates, conflicts, stale), ③ distill, ④ promote/demote."""

Action = Literal[
    # ① verify
    "confirm",
    "correct",
    "mark-disputed",
    # ② curate
    "merge",
    "supersede",
    "mark-stale",
    # ③ / ④ (proposal-only)
    "distill",
    "demote",
]

Polarity = Literal["support", "contradict", "neutral"]

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True)
class EvidenceRef:
    """A pointer into the transcript index: opaque ref only, never a path or text."""

    ref: str
    polarity: Polarity
    session_id: str = ""
    timestamp: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Candidate:
    """One proposed change. ``changes`` maps memory field names to proposed values."""

    operation: Operation
    action: Action
    memory_ids: list[str]
    reason: str
    oid: str = ""
    evidence: list[EvidenceRef] = field(default_factory=list)
    changes: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "operation": self.operation,
            "action": self.action,
            "memory_ids": list(self.memory_ids),
            "oid": self.oid,
            "reason": self.reason,
            "evidence": [item.as_dict() for item in self.evidence],
            "changes": dict(self.changes),
        }


def concept_key(document: MemoryDocument) -> str:
    """The concept a memory's candidates are filed under.

    The first ``about`` (else ``applies_to``) oid; a memory not linked to any
    concept gets its own key, so it still has exactly one candidate file.
    """

    for value in (*document.about, *document.applies_to):
        if value:
            return value
    return f"memory-{document.id}"


def candidate_filename(oid: str) -> str:
    """A safe, stable file name for one concept: ``<oid>.md``."""

    cleaned = _UNSAFE.sub("_", oid).strip("._") or "unnamed"
    return f"{cleaned[:120]}.md"
