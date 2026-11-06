from __future__ import annotations

from pathlib import Path
from typing import Any

from memfw.notes.ai_annotation import JudgeVerdict, cross_annotate, write_annotation_files


class FakeJudge:
    def __init__(self, name: str, paths: tuple[str, ...], confidence: float = 0.9) -> None:
        self.name = name
        self.paths = paths
        self.confidence = confidence

    def judge(self, query: str, candidates: Any) -> JudgeVerdict:
        del query, candidates
        return JudgeVerdict(True, self.paths, self.confidence, self.name)


def test_cross_annotation_accepts_only_agreement_and_kappa_gate() -> None:
    items = [("q", [{"path": "a.md"}])]
    accepted = cross_annotate(
        items,
        FakeJudge("a", ("a.md",)),
        FakeJudge("b", ("a.md",)),
        human_kappa=0.80,
    )
    assert len(accepted.accepted) == 1
    assert accepted.accepted[0].judges == ("a", "b")
    assert not accepted.human_queue

    gated = cross_annotate(
        items,
        FakeJudge("a", ("a.md",)),
        FakeJudge("b", ("a.md",)),
        human_kappa=0.745,
    )
    assert not gated.accepted
    assert gated.human_queue

    disagree = cross_annotate(
        items,
        FakeJudge("a", ("a.md",)),
        FakeJudge("b", ("b.md",)),
        human_kappa=0.80,
    )
    assert not disagree.accepted
    assert disagree.disagreements == 1


def test_cross_annotation_writes_ai_and_human_queue(tmp_path: Path) -> None:
    result = cross_annotate(
        [("q", [{"path": "a.md"}])],
        FakeJudge("a", ("a.md",)),
        FakeJudge("b", ("a.md",)),
        human_kappa=0.80,
    )
    write_annotation_files(
        result,
        ai_path=tmp_path / "ai.yml",
        human_queue_path=tmp_path / "human.yml",
    )
    assert "ai-cross-model" in (tmp_path / "ai.yml").read_text(encoding="utf-8")
    assert "items:" in (tmp_path / "human.yml").read_text(encoding="utf-8")
