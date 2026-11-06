from __future__ import annotations

from pathlib import Path

import pytest

from memfw.notes import (
    GoldenAnnotator,
    NotesIndex,
    is_finish,
    load_cases,
    normalize_kind,
    path_search_term,
    path_selection,
)


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


def test_voice_command_aliases() -> None:
    assert is_finish("结束")
    assert is_finish("done")
    assert normalize_kind("无答案") == "no_answer"
    assert normalize_kind("中文") == "chinese"
    assert normalize_kind("") == "answerable"
    assert path_search_term("搜索 Korsgaard") == "Korsgaard"
    assert path_search_term("?corpus") == "corpus"
    assert path_selection("选择 2") == 2
    assert path_selection("第3个") == 3
