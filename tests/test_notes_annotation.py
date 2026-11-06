from __future__ import annotations

from pathlib import Path

import pytest

from memfw.notes import GoldenAnnotator, NotesIndex, load_cases


def test_annotator_appends_validates_and_resumes(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    (root / "a.md").write_text("# A\n\ncorpus\n", encoding="utf-8")
    (root / "b.md").write_text("# B\n\nother\n", encoding="utf-8")
    index_path = tmp_path / "index.sqlite"
    index = NotesIndex(index_path)
    index.index(root)
    out = tmp_path / "golden.yml"
    session = GoldenAnnotator(index_path, out, annotator="tester", annotated_at="2026-11-06")
    entry = session.add("corpus question", ["a.md"], kind="answerable", notes="A answers it")
    assert entry.query_id == "h001"
    assert out.exists()
    with pytest.raises(ValueError):
        session.add("corpus question", ["b.md"])
    with pytest.raises(ValueError):
        session.add("unknown path", ["missing.md"])
    with pytest.raises(ValueError):
        session.add("no answer", [], kind="no_answer")
    session.add("no answer", [], kind="no_answer", notes="searched vault")
    resumed = GoldenAnnotator(index_path, out, annotator="tester")
    assert [item.query_id for item in resumed.entries] == ["h001", "h002"]
    cases = load_cases(out)
    assert [case.query_id for case in cases] == ["h001", "h002"]
    assert cases[1].answerable is False
