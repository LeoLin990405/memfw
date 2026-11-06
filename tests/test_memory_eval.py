from __future__ import annotations

from pathlib import Path

from memfw.core.repository import Repository
from memfw.memory_eval import evaluate_questions, generate_questions
from memfw.memory_hybrid import MemoryHybridRetriever
from memfw.notes import HashEmbeddingProvider


def test_five_class_memory_eval_scaffold(tmp_path: Path) -> None:
    repository = Repository(tmp_path / "vault")
    repository.init()
    repository.add("Alpha", "alpha corpus", memory_id="a", tags=["shared"])
    repository.add("Beta", "beta corpus", memory_id="b", tags=["shared"])
    repository.add("Old rule", "old body", memory_id="old")
    repository.supersede("old", "New rule", "new body", memory_id_new="new")
    questions = generate_questions(repository, per_class=5)
    kinds = {item.kind for item in questions}
    assert {"single_hop", "multi_hop", "temporal", "update", "refusal"} <= kinds
    retriever = MemoryHybridRetriever(repository, embedder=HashEmbeddingProvider(8))
    retriever.rebuild()
    report = evaluate_questions(retriever, questions, limit=10)
    assert report.queries == len(questions)
    assert 0.0 <= report.as_of_accuracy <= 1.0
    assert report.refusal_accuracy >= 0.0
