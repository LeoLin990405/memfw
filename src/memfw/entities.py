"""Rebuildable entity projection used as an optional third retrieval route."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable
from pathlib import Path

from .core.repository import Repository

_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{2,}|[\u4e00-\u9fff]{2,}")
_STOP = {
    "the",
    "and",
    "for",
    "with",
    "from",
    "this",
    "that",
    "into",
    "memory",
    "notes",
}


def extract_entities(text: str, dictionary: Iterable[str] = ()) -> set[str]:
    """Small local extractor: dictionary terms plus capitalized/acronym tokens."""
    found: set[str] = set()
    lowered = text.lower()
    for term in dictionary:
        if term and term.lower() in lowered:
            found.add(term)
    for match in _TOKEN_RE.finditer(text):
        token = match.group(0).strip()
        if token.lower() in _STOP or len(token) < 2:
            continue
        if token[0].isupper() or token.isupper() or any("\u4e00" <= c <= "\u9fff" for c in token):
            found.add(token)
    return found


def load_dictionary(path: Path | str | None) -> tuple[str, ...]:
    if path is None:
        return ()
    source = Path(path)
    if not source.exists():
        return ()
    return tuple(
        line.strip()
        for line in source.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )


class EntityIndex:
    def __init__(self, path: Path | str, *, dictionary: Iterable[str] = ()) -> None:
        self.path = Path(path)
        self.dictionary = tuple(dictionary)

    def rebuild(self, repository: Repository) -> int:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.unlink(missing_ok=True)
        with sqlite3.connect(temporary) as connection:
            connection.execute(
                "CREATE TABLE entities (entity TEXT NOT NULL, memory_id TEXT NOT NULL, "
                "PRIMARY KEY(entity, memory_id))"
            )
            count = 0
            for document in repository.list():
                text = " ".join((document.title, document.body, " ".join(document.tags)))
                for entity in sorted(extract_entities(text, self.dictionary)):
                    connection.execute(
                        "INSERT OR IGNORE INTO entities(entity, memory_id) VALUES (?,?)",
                        (entity.lower(), document.id),
                    )
                    count += 1
            connection.commit()
        temporary.replace(self.path)
        return count

    def search(self, query: str, *, limit: int = 50) -> list[str]:
        if not self.path.exists():
            return []
        entities = sorted(extract_entities(query, self.dictionary))
        if not entities:
            return []
        with sqlite3.connect(self.path) as connection:
            placeholders = ",".join("?" for _ in entities)
            rows = connection.execute(
                f"SELECT memory_id, COUNT(*) AS n FROM entities "
                f"WHERE entity IN ({placeholders}) GROUP BY memory_id "
                "ORDER BY n DESC, memory_id LIMIT ?",
                (*[item.lower() for item in entities], limit),
            ).fetchall()
        return [str(row[0]) for row in rows]


__all__ = ["EntityIndex", "extract_entities", "load_dictionary"]
