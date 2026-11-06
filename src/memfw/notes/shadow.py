"""Read-only comparison of two index generations before any cutover."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from .evaluation import EvalCase, EvalReport, evaluate_retriever
from .index import NotesIndex
from .retrieval import Mode, NotesRetriever, RetrievalConfig


@dataclass(frozen=True)
class ShadowRow:
    query_id: str
    baseline_rank: int | None
    candidate_rank: int | None


@dataclass
class ShadowReport:
    baseline: EvalReport
    candidate: EvalReport
    rows: list[ShadowRow]

    def as_dict(self, k: int = 10) -> dict[str, Any]:
        return {
            "baseline": self.baseline.as_dict(k),
            "candidate": self.candidate.as_dict(k),
            "rows": [
                {
                    "query_id": row.query_id,
                    "baseline_rank": row.baseline_rank,
                    "candidate_rank": row.candidate_rank,
                }
                for row in self.rows
            ],
            "delta": {
                key: round(self.candidate.metric(k)[key] - self.baseline.metric(k)[key], 4)
                for key in ("recall_at_k", "mrr", "ndcg_at_k")
            },
        }


def compare_indexes(
    baseline: NotesIndex,
    candidate: NotesIndex,
    cases: Sequence[EvalCase],
    *,
    mode: Mode = "hybrid",
    candidates: int = 50,
    limit: int = 10,
) -> ShadowReport:
    """Evaluate two indexes on identical cases. No index is modified or switched."""
    baseline_report = evaluate_retriever(
        NotesRetriever(
            baseline,
            config=RetrievalConfig(mode=mode, candidates=candidates, final_k=limit),
        ),
        cases,
        name="baseline",
        limit=limit,
    )
    candidate_report = evaluate_retriever(
        NotesRetriever(
            candidate,
            config=RetrievalConfig(mode=mode, candidates=candidates, final_k=limit),
        ),
        cases,
        name="candidate",
        limit=limit,
    )
    rows = [
        ShadowRow(
            query_id=base.query_id,
            baseline_rank=base.rank,
            candidate_rank=cand.rank,
        )
        for base, cand in zip(baseline_report.rows, candidate_report.rows, strict=True)
    ]
    return ShadowReport(baseline=baseline_report, candidate=candidate_report, rows=rows)


__all__ = ["ShadowReport", "ShadowRow", "compare_indexes"]
