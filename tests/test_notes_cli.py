from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from memfw.cli import app

runner = CliRunner()


def test_notes_cli_index_search_status(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "note.md").write_text("# Corpus\n\nKWIC concordance corpus.\n", encoding="utf-8")
    db = tmp_path / "notes.sqlite"
    indexed = runner.invoke(app, ["notes", "index", "--root", str(vault), "--db", str(db)])
    assert indexed.exit_code == 0, indexed.stdout
    payload = json.loads(indexed.stdout)
    assert payload["counts"]["updated"] == 1
    searched = runner.invoke(app, ["notes", "search", "corpus", "--db", str(db)])
    assert searched.exit_code == 0, searched.stdout
    payload = json.loads(searched.stdout)
    hits = payload["results"]
    assert hits and hits[0]["path"] == "note.md"
    assert payload["mode"] in {"hybrid", "fts", "vector"}
    status = runner.invoke(app, ["notes", "status", "--db", str(db)])
    assert status.exit_code == 0, status.stdout
    assert json.loads(status.stdout)["notes"] == 1
