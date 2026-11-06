from __future__ import annotations

from pathlib import Path

from memfw.notes import EvalCase, NotesIndex, compare_indexes


def test_shadow_compares_identical_indexes(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    (root / "a.md").write_text("# A\n\ncorpus\n", encoding="utf-8")
    baseline = NotesIndex(tmp_path / "baseline.sqlite")
    candidate = NotesIndex(tmp_path / "candidate.sqlite")
    baseline.index(root)
    candidate.index(root)
    cases = [EvalCase("q1", "corpus", ("a.md",), "content")]
    report = compare_indexes(baseline, candidate, cases, mode="fts", limit=3)
    data = report.as_dict(3)
    assert data["baseline"]["metrics"]["recall_at_k"] == 1.0
    assert data["candidate"]["metrics"]["recall_at_k"] == 1.0
    assert data["delta"]["recall_at_k"] == 0.0
    assert data["rows"][0]["baseline_rank"] == 1
