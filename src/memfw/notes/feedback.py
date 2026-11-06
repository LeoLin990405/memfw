"""Separate, privacy-bounded retrieval feedback store.

The memory change log remains the authoritative audit trail. This module stores
retrieval traces and explicitly returned client feedback in a separate SQLite
database. It defaults to storing identifiers/scores, not document bodies.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

FeedbackKind = Literal["citation", "correction", "task_outcome", "permission"]


@dataclass(frozen=True)
class CandidateTrace:
    path: str
    rank: int
    score: float
    section_id: int | None = None
    content_hash: str = ""
    source: str = ""
    content: str | None = None


@dataclass(frozen=True)
class RetrievalTrace:
    trace_id: str
    task_id: str
    query: str
    mode: str
    rewrites: tuple[str, ...] = ()
    candidates: tuple[CandidateTrace, ...] = ()
    model_versions: dict[str, str] = field(default_factory=dict)
    tenant: str = ""
    source: str = ""
    created_at: float = field(default_factory=time.time)
    include_content: bool = False


@dataclass(frozen=True)
class FeedbackEvent:
    trace_id: str
    task_id: str
    kind: FeedbackKind
    candidate_path: str = ""
    relevance: str = "unknown"
    correctness: str = "unknown"
    permission: str = "unknown"
    confidence: float = 0.0
    note: str = ""
    actor: str = ""
    source: str = ""
    created_at: float = field(default_factory=time.time)


class FeedbackStore:
    """SQLite-backed telemetry with minimal content retention by default."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _initialize(self) -> None:
        with sqlite3.connect(self.path) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS traces (
                    trace_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    query TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    rewrites TEXT NOT NULL,
                    candidates TEXT NOT NULL,
                    model_versions TEXT NOT NULL,
                    tenant TEXT NOT NULL,
                    source TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS feedback (
                    feedback_id TEXT PRIMARY KEY,
                    trace_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    candidate_path TEXT NOT NULL,
                    relevance TEXT NOT NULL,
                    correctness TEXT NOT NULL,
                    permission TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    note TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    source TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_feedback_trace ON feedback(trace_id);
                CREATE INDEX IF NOT EXISTS idx_feedback_task ON feedback(task_id);
                """
            )
            connection.commit()

    def record_trace(self, trace: RetrievalTrace) -> None:
        candidates = []
        for candidate in trace.candidates:
            payload: dict[str, Any] = {
                "path": candidate.path,
                "rank": candidate.rank,
                "score": candidate.score,
                "section_id": candidate.section_id,
                "content_hash": candidate.content_hash,
                "source": candidate.source,
            }
            if trace.include_content and candidate.content is not None:
                payload["content"] = candidate.content
            candidates.append(payload)
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "INSERT OR REPLACE INTO traces VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    trace.trace_id,
                    trace.task_id,
                    trace.query,
                    trace.mode,
                    json.dumps(list(trace.rewrites), ensure_ascii=False),
                    json.dumps(candidates, ensure_ascii=False),
                    json.dumps(trace.model_versions, ensure_ascii=False),
                    trace.tenant,
                    trace.source,
                    trace.created_at,
                ),
            )
            connection.commit()

    def add_feedback(self, event: FeedbackEvent) -> str:
        feedback_id = str(uuid.uuid4())
        with sqlite3.connect(self.path) as connection:
            exists = connection.execute(
                "SELECT 1 FROM traces WHERE trace_id=?", (event.trace_id,)
            ).fetchone()
            if exists is None:
                raise ValueError("feedback references an unknown trace")
            connection.execute(
                "INSERT INTO feedback VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    feedback_id,
                    event.trace_id,
                    event.task_id,
                    event.kind,
                    event.candidate_path,
                    event.relevance,
                    event.correctness,
                    event.permission,
                    event.confidence,
                    event.note,
                    event.actor,
                    event.source,
                    event.created_at,
                ),
            )
            connection.commit()
        return feedback_id

    def traces(self, *, task_id: str | None = None) -> list[dict[str, Any]]:
        with sqlite3.connect(self.path) as connection:
            if task_id is None:
                rows = connection.execute("SELECT * FROM traces").fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM traces WHERE task_id=?", (task_id,)
                ).fetchall()
        return [
            {
                "trace_id": row[0],
                "task_id": row[1],
                "query": row[2],
                "mode": row[3],
                "rewrites": json.loads(row[4]),
                "candidates": json.loads(row[5]),
                "model_versions": json.loads(row[6]),
                "tenant": row[7],
                "source": row[8],
                "created_at": row[9],
            }
            for row in rows
        ]

    def feedback(self, *, trace_id: str | None = None) -> list[dict[str, Any]]:
        with sqlite3.connect(self.path) as connection:
            if trace_id is None:
                rows = connection.execute("SELECT * FROM feedback").fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM feedback WHERE trace_id=?", (trace_id,)
                ).fetchall()
        return [
            {
                "feedback_id": row[0],
                "trace_id": row[1],
                "task_id": row[2],
                "kind": row[3],
                "candidate_path": row[4],
                "relevance": row[5],
                "correctness": row[6],
                "permission": row[7],
                "confidence": row[8],
                "note": row[9],
                "actor": row[10],
                "source": row[11],
                "created_at": row[12],
            }
            for row in rows
        ]


def query_digest(query: str) -> str:
    """Stable digest for de-duplicating queries without storing them separately."""
    return hashlib.sha256(query.encode("utf-8")).hexdigest()


__all__ = [
    "CandidateTrace",
    "FeedbackEvent",
    "FeedbackKind",
    "FeedbackStore",
    "RetrievalTrace",
    "query_digest",
]
