"""Index staleness check for the interfaces (moved from the MCP server, A3a)."""

from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from pathlib import Path

from memfw.core.repository import Repository
from memfw.index import MemoryIndex


def _indexed_ids(path: Path) -> set[str] | None:
    """Memory ids in the index, or None when it cannot be read."""

    try:
        with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as db:
            return {str(row[0]) for row in db.execute("SELECT id FROM memory_rows")}
    except (OSError, sqlite3.Error):
        return None


def ensure_search_index(repository: Repository) -> None:
    """Rebuild the disposable memory index when it is stale.

    Stale means: missing, built by an earlier schema generation (an index
    written by memfw 0.1.x has no ``memory_temporal`` projection and would
    answer every FTS query with nothing), older than a Markdown file, or
    holding a different set of memories than ``memories/`` once that directory
    changed after the index was built. The last check catches a file copied
    in with its old mtime kept (``rsync -a``, Syncthing, ``cp -p``) and a file
    deleted outside memfw: neither leaves a surviving file newer than the
    index. A file that cannot be parsed is skipped by the rebuild, so it only
    triggers one rebuild per directory change, never one per search.
    """

    path = repository.index_path
    stale = not path.is_file() or not MemoryIndex(path).schema_is_current()
    if not stale and repository.memories_dir.is_dir():
        indexed_at = path.stat().st_mtime
        stems: set[str] = set()
        with os.scandir(repository.memories_dir) as entries:
            for item in entries:
                if not item.name.endswith(".md"):
                    continue
                stems.add(item.name[: -len(".md")])
                if item.stat(follow_symlinks=False).st_mtime > indexed_at:
                    stale = True
                    break
        if not stale and repository.memories_dir.stat().st_mtime > indexed_at:
            stale = _indexed_ids(path) != stems
    if stale:
        repository.init()
        MemoryIndex(path).rebuild(repository)


__all__ = ["ensure_search_index"]
