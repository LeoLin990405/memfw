"""Interactive/iterative human golden-query annotation.

The annotation is intentionally local and file-backed. Voice input is provided
by the operating system's dictation (for example macOS fn-fn) into the terminal;
no audio leaves the machine and no speech-model dependency is added.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

ANNOTATION_KINDS = {
    "answerable",
    "chinese",
    "mixed",
    "title",
    "concept",
    "no_answer",
    "unspecified",
}


@dataclass(frozen=True)
class AnnotationEntry:
    query_id: str
    query: str
    expected_paths: tuple[str, ...]
    kind: str
    notes: str = ""


class GoldenAnnotator:
    """Append validated entries to a golden YAML file immediately."""

    def __init__(
        self,
        db: Path | str,
        out: Path | str,
        *,
        annotator: str,
        annotated_at: str | None = None,
    ) -> None:
        self.db = Path(db)
        self.out = Path(out)
        self.annotator = annotator
        self.annotated_at = annotated_at or dt.date.today().isoformat()
        self.entries: list[AnnotationEntry] = []
        self._known_paths = self._load_paths()
        if self.out.exists():
            self._load_existing()

    def _load_paths(self) -> set[str]:
        with sqlite3.connect(self.db) as connection:
            rows = connection.execute("SELECT path FROM notes").fetchall()
        return {str(row[0]) for row in rows}

    def _load_existing(self) -> None:
        payload = yaml.safe_load(self.out.read_text(encoding="utf-8"))
        rows = payload.get("queries", []) if isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            raise ValueError("existing golden file must contain queries")
        for row in rows:
            if not isinstance(row, dict):
                continue
            paths = row.get("expected_paths", row.get("expected_path", []))
            if isinstance(paths, str):
                paths = [paths]
            self.entries.append(
                AnnotationEntry(
                    query_id=str(row.get("id") or ""),
                    query=str(row.get("query") or ""),
                    expected_paths=tuple(str(item) for item in (paths or [])),
                    kind=str(row.get("kind") or "unspecified"),
                    notes=str(row.get("notes") or ""),
                )
            )

    @property
    def next_id(self) -> str:
        return f"h{len(self.entries) + 1:03d}"

    def suggest_paths(self, text: str, *, limit: int = 8) -> list[str]:
        needle = f"%{text.strip()}%"
        if not text.strip():
            return []
        with sqlite3.connect(self.db) as connection:
            rows = connection.execute(
                "SELECT path FROM notes WHERE path LIKE ? OR title LIKE ? ORDER BY path LIMIT ?",
                (needle, needle, limit),
            ).fetchall()
        return [str(row[0]) for row in rows]

    def add(
        self,
        query: str,
        expected_paths: list[str] | tuple[str, ...],
        *,
        kind: str = "answerable",
        notes: str = "",
    ) -> AnnotationEntry:
        query = query.strip()
        if not query:
            raise ValueError("query must not be empty")
        if any(entry.query == query for entry in self.entries):
            raise ValueError("query already exists in this golden file")
        if kind not in ANNOTATION_KINDS:
            raise ValueError(f"kind must be one of {sorted(ANNOTATION_KINDS)}")
        paths = tuple(path.strip() for path in expected_paths if path.strip())
        unknown = sorted(set(paths) - self._known_paths)
        if unknown:
            raise ValueError(f"unknown note path(s): {', '.join(unknown)}")
        if kind == "no_answer" and paths:
            raise ValueError("no_answer entries must not have expected paths")
        if kind != "no_answer" and not paths:
            raise ValueError("answerable entries require at least one expected path")
        if kind == "no_answer" and not notes.strip():
            raise ValueError("no_answer entries require a note explaining the search")
        entry = AnnotationEntry(
            query_id=self.next_id,
            query=query,
            expected_paths=paths,
            kind=kind,
            notes=notes.strip(),
        )
        self.entries.append(entry)
        self.save()
        return entry

    def save(self) -> None:
        self.out.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = {
            "annotator": self.annotator,
            "annotated_at": self.annotated_at,
            "guideline_version": "1",
            "queries": [
                {
                    "id": entry.query_id,
                    "query": entry.query,
                    "expected_paths": list(entry.expected_paths),
                    "kind": entry.kind,
                    "notes": entry.notes,
                }
                for entry in self.entries
            ],
        }
        temp = self.out.with_suffix(self.out.suffix + ".tmp")
        temp.write_text(
            yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        temp.replace(self.out)


__all__ = ["ANNOTATION_KINDS", "AnnotationEntry", "GoldenAnnotator"]
