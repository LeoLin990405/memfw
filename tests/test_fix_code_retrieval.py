"""Code index: repo-relative ignore list, per-chunk embeddings, repo-root recent files."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from memfw.code_retrieval import CodeIndex, index_code


def test_repo_under_build_directory_is_indexed(tmp_path: Path) -> None:
    repo = tmp_path / "build" / "myrepo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "m.py").write_text("def foo():\n    return 1\n", encoding="utf-8")
    (repo / "build").mkdir()
    (repo / "build" / "gen.py").write_text("def gen():\n    return 2\n", encoding="utf-8")
    counts = index_code(repo, tmp_path / "db.sqlite")
    assert counts["files"] == 1  # pkg/m.py; the repo's own build/ stays ignored
    paths = {row["path"] for row in CodeIndex(tmp_path / "db.sqlite")._rows()}
    assert paths == {"pkg/m.py"}


class _Embedder:
    dimension = 2

    def embed(self, texts: list[str], *, role: str = "document") -> list[list[float]]:
        del role
        out = []
        for text in texts:
            if "alpha" in text and "beta" not in text:
                out.append([1.0, 0.0])
            elif "beta" in text and "alpha" not in text:
                out.append([0.0, 1.0])
            else:
                out.append([0.5, 0.5])
        return out


def test_duplicate_symbols_keep_their_own_embeddings(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "d.py").write_text(
        "def dup():\n    return 'alpha'\n\n\ndef dup():\n    return 'beta'\n", encoding="utf-8"
    )
    db = tmp_path / "db.sqlite"
    index_code(repo, db, embedder=_Embedder())
    with sqlite3.connect(db) as connection:
        rows = connection.execute(
            "SELECT start_line, embedding FROM chunks WHERE symbol='dup' ORDER BY start_line"
        ).fetchall()
    assert [json.loads(row[1]) for row in rows] == [[1.0, 0.0], [0.0, 1.0]]


def test_recent_files_resolve_against_repo_root(tmp_path: Path) -> None:
    repo = tmp_path / "repo2"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "m.py").write_text("def foo():\n    return 1\n", encoding="utf-8")
    (repo / "pkg" / "cli.py").write_text("def main():\n    foo()\n", encoding="utf-8")
    db = tmp_path / "cache" / "code.sqlite"
    index_code(repo, db)
    payload = CodeIndex(db).repo_map()
    assert set(payload["recent_files"]) == {"pkg/m.py", "pkg/cli.py"}
