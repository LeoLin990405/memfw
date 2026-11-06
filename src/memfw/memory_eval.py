"""Local five-class memory evaluation scaffold.

Questions are generated from the vault; they are candidates for the human/AI
annotation pipeline, not human-labelled ground truth. Metrics cover stale-fact
recall for updated memories, as_of accuracy, and refusal correctness.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from .core.models import MemoryDocument
from .core.repository import Repository
from .memory_hybrid import MemoryHybridRetriever

QuestionKind = Literal["single_hop", "multi_hop", "temporal", "update", "refusal"]


@dataclass(frozen=True)
class MemoryQuestion:
    question_id: str
    query: str
    kind: QuestionKind
    expected_ids: tuple[str, ...] = ()
    as_of: datetime | None = None
    stale_ids: tuple[str, ...] = ()


@dataclass
class MemoryEvalReport:
    queries: int
    recall_at_k: float
    mrr: float
    ndcg_at_k: float
    stale_fact_rate: float
    as_of_accuracy: float
    refusal_accuracy: float

    def as_dict(self) -> dict[str, float]:
        return {
            "queries": float(self.queries),
            "recall_at_k": self.recall_at_k,
            "mrr": self.mrr,
            "ndcg_at_k": self.ndcg_at_k,
            "stale_fact_rate": self.stale_fact_rate,
            "as_of_accuracy": self.as_of_accuracy,
            "refusal_accuracy": self.refusal_accuracy,
        }


def _first_phrase(document: MemoryDocument) -> str:
    return " ".join(document.body.split()[:12]) or document.title


def generate_questions(
    repository: Repository,
    *,
    per_class: int = 20,
    seed: int = 17,
) -> list[MemoryQuestion]:
    docs = repository.list()
    current = [doc for doc in docs if doc.valid_to is None]
    historical = [doc for doc in docs if doc.valid_to is not None]
    rng = random.Random(seed)
    output: list[MemoryQuestion] = []
    for doc in current[:per_class]:
        output.append(
            MemoryQuestion(
                f"single-{doc.id}",
                f"{doc.title} {_first_phrase(doc)}",
                "single_hop",
                (doc.id,),
            )
        )
    tag_groups: dict[str, list[MemoryDocument]] = {}
    for doc in current:
        if doc.tags:
            tag_groups.setdefault(doc.tags[0], []).append(doc)
    pairs = [group for group in tag_groups.values() if len(group) >= 2]
    for group in pairs[:per_class]:
        left, right = group[0], group[1]
        output.append(
            MemoryQuestion(
                f"multi-{left.id}-{right.id}",
                f"{left.title} {right.title}",
                "multi_hop",
                (left.id, right.id),
            )
        )
    if len([item for item in output if item.kind == "multi_hop"]) < per_class:
        for index, left in enumerate(current):
            if len([item for item in output if item.kind == "multi_hop"]) >= per_class:
                break
            for right in current[index + 1 :]:
                left_tokens = set(left.title.split())
                right_tokens = set(right.title.split())
                if left.source == right.source or left_tokens & right_tokens:
                    output.append(
                        MemoryQuestion(
                            f"multi-{left.id}-{right.id}",
                            f"{left.title} {right.title}",
                            "multi_hop",
                            (left.id, right.id),
                        )
                    )
                    break
    for doc in historical[:per_class]:
        candidates = [
            other for other in current if other.id == doc.superseded_by or other.title == doc.title
        ]
        if doc.valid_to is None:
            continue
        midpoint = (doc.valid_from or doc.created_at) + (
            doc.valid_to - (doc.valid_from or doc.created_at)
        ) / 2
        output.append(
            MemoryQuestion(
                f"temporal-{doc.id}",
                doc.title,
                "temporal",
                (doc.id,),
                as_of=midpoint,
            )
        )
        if candidates:
            output.append(
                MemoryQuestion(
                    f"update-{doc.id}",
                    doc.title,
                    "update",
                    (candidates[0].id,),
                    stale_ids=(doc.id,),
                )
            )
    no_answer_tokens = ["火星", "moon base", "quantum refrigerator", "虚构项目"]
    for index in range(per_class):
        output.append(
            MemoryQuestion(
                f"refusal-{index}",
                f"{rng.choice(no_answer_tokens)} {index}",
                "refusal",
            )
        )
    return output


def evaluate_questions(
    retriever: MemoryHybridRetriever,
    questions: list[MemoryQuestion],
    *,
    limit: int = 10,
) -> MemoryEvalReport:
    answerable = [item for item in questions if item.kind != "refusal" and item.expected_ids]
    hits = 0
    reciprocal = 0.0
    ndcg = 0.0
    stale_hits = 0
    stale_total = 0
    as_of_total = 0
    as_of_hits = 0
    refusal_total = 0
    refusal_hits = 0
    for question in questions:
        result = retriever.search(
            question.query,
            limit=limit,
            mode="hybrid",
            as_of=question.as_of.isoformat() if question.as_of else None,
        )
        ids = {item["id"] for item in result["results"]}
        if question.kind == "refusal":
            refusal_total += 1
            if not ids:
                refusal_hits += 1
            continue
        if question.as_of is not None:
            as_of_total += 1
            if set(question.expected_ids) & ids:
                as_of_hits += 1
        ranked = [item["id"] for item in result["results"]]
        if set(question.expected_ids) & ids:
            hits += 1
        for rank, identifier in enumerate(ranked, start=1):
            if identifier in question.expected_ids:
                reciprocal += 1.0 / rank
                if rank <= limit:
                    ndcg += 1.0 / __import__("math").log2(rank + 1)
                break
        if question.stale_ids:
            stale_total += 1
            if set(question.stale_ids) & ids:
                stale_hits += 1
    denominator = max(1, len(answerable))
    return MemoryEvalReport(
        queries=len(questions),
        recall_at_k=hits / denominator,
        mrr=reciprocal / denominator,
        ndcg_at_k=ndcg / denominator,
        stale_fact_rate=stale_hits / max(1, stale_total),
        as_of_accuracy=as_of_hits / max(1, as_of_total),
        refusal_accuracy=refusal_hits / max(1, refusal_total),
    )


__all__ = [
    "MemoryEvalReport",
    "MemoryQuestion",
    "QuestionKind",
    "evaluate_questions",
    "generate_questions",
]
