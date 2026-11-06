"""Retrieval evaluation: ablations, ranking metrics and latency percentiles."""

from __future__ import annotations

import json
import math
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .index import NoteHit, NotesIndex
from .retrieval import NotesRetriever, RetrievalConfig


@dataclass(frozen=True)
class EvalCase:
    query_id: str
    query: str
    expected_paths: tuple[str, ...]
    kind: str = "unspecified"

    @property
    def answerable(self) -> bool:
        return bool(self.expected_paths)


@dataclass
class EvalRow:
    query_id: str
    kind: str
    rank: int | None
    latency_ms: float
    hits: list[str] = field(default_factory=list)
    mode: str = ""
    answerable: bool = True
    reranked: bool = False


@dataclass
class EvalReport:
    name: str
    rows: list[EvalRow]
    elapsed_s: float

    def metric(self, k: int = 10) -> dict[str, float]:
        answerable = [row for row in self.rows if row.answerable]
        unanswerable = [row for row in self.rows if not row.answerable]
        total = len(answerable)
        hits_at_k = sum(1 for row in answerable if row.rank is not None and row.rank <= k)
        reciprocal = sum(1.0 / row.rank for row in answerable if row.rank)
        ndcg = 0.0
        for row in answerable:
            if row.rank and row.rank <= k:
                ndcg += 1.0 / math.log2(row.rank + 1)
        latencies = sorted(row.latency_ms for row in self.rows)
        with_hits = sum(1 for row in unanswerable if row.hits)
        return {
            "queries": float(len(self.rows)),
            "answerable_queries": float(total),
            "recall_at_k": hits_at_k / total if total else 0.0,
            "mrr": reciprocal / total if total else 0.0,
            "ndcg_at_k": ndcg / total if total else 0.0,
            "unanswerable_queries": float(len(unanswerable)),
            "unanswerable_with_results": float(with_hits),
            "latency_p50_ms": _percentile(latencies, 0.50),
            "latency_p95_ms": _percentile(latencies, 0.95),
        }

    def by_kind(self, k: int = 10) -> dict[str, dict[str, float]]:
        output: dict[str, dict[str, float]] = {}
        for kind in sorted({row.kind for row in self.rows}):
            subset = EvalReport(self.name, [row for row in self.rows if row.kind == kind], 0.0)
            output[kind] = subset.metric(k)
        return output

    def as_dict(self, k: int = 10) -> dict[str, Any]:
        return {
            "name": self.name,
            "metrics": self.metric(k),
            "by_kind": self.by_kind(k),
            "elapsed_s": self.elapsed_s,
            "rows": [
                {
                    "query_id": row.query_id,
                    "kind": row.kind,
                    "rank": row.rank,
                    "latency_ms": round(row.latency_ms, 2),
                    "mode": row.mode,
                    "reranked": row.reranked,
                    "hits": row.hits,
                }
                for row in self.rows
            ],
        }


def _percentile(values: Sequence[float], fraction: float) -> float:
    if not values:
        return 0.0
    index = min(len(values) - 1, int(len(values) * fraction))
    return values[index]


def _rank(hits: Sequence[NoteHit], expected: Sequence[str]) -> int | None:
    for position, hit in enumerate(hits, start=1):
        if any(hit.path == path for path in expected):
            return position
    return None


def evaluate_retriever(
    retriever: NotesRetriever,
    cases: Sequence[EvalCase],
    *,
    name: str = "retriever",
    limit: int = 10,
) -> EvalReport:
    rows: list[EvalRow] = []
    started = time.perf_counter()
    for case in cases:
        t0 = time.perf_counter()
        result = retriever.search(case.query, limit=limit)
        latency = (time.perf_counter() - t0) * 1000.0
        paths = [hit.path for hit in result.hits]
        rows.append(
            EvalRow(
                query_id=case.query_id,
                kind=case.kind,
                rank=_rank(result.hits, case.expected_paths),
                latency_ms=latency,
                hits=paths,
                mode=result.mode_used,
                answerable=case.answerable,
                reranked=result.reranked,
            )
        )
    return EvalReport(name=name, rows=rows, elapsed_s=time.perf_counter() - started)


def evaluate_ablation(
    index: NotesIndex,
    cases: Sequence[EvalCase],
    *,
    limit: int = 10,
    candidates: int = 50,
) -> dict[str, EvalReport]:
    """Run the stage-A baselines over the same cases.

    Reranker and rewrite arms are intentionally absent until a provider and a
    labelled evaluation set exist.
    """

    reports: dict[str, EvalReport] = {}
    for mode in ("fts", "vector", "hybrid"):
        retriever = NotesRetriever(
            index, config=RetrievalConfig(mode=mode, candidates=candidates, final_k=limit)
        )
        reports[mode] = evaluate_retriever(retriever, cases, name=mode, limit=limit)
    return reports


def load_cases(path: Path | str) -> list[EvalCase]:
    """Load golden cases from JSONL, JSON, or YAML without inventing labels."""
    source = Path(path)
    if source.suffix in {".yaml", ".yml"}:
        payload = yaml.safe_load(source.read_text(encoding="utf-8"))
        rows = payload.get("queries", payload) if isinstance(payload, dict) else payload
    elif source.suffix == ".jsonl":
        rows = [
            json.loads(line)
            for line in source.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    elif source.suffix == ".json":
        payload = json.loads(source.read_text(encoding="utf-8"))
        rows = payload.get("queries", payload) if isinstance(payload, dict) else payload
    else:
        raise ValueError("golden file must be .jsonl, .json, or .yaml")
    if not isinstance(rows, list):
        raise ValueError("golden file must contain a list of queries")
    cases: list[EvalCase] = []
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        query_id = raw.get("id") or raw.get("query_id")
        query = raw.get("query")
        if not query_id or not isinstance(query, str) or not query.strip():
            continue
        raw_expected = raw.get("expected_paths", raw.get("expected_path", []))
        expected: tuple[str, ...]
        if isinstance(raw_expected, str):
            expected = (raw_expected,)
        elif isinstance(raw_expected, list):
            expected = tuple(str(item) for item in raw_expected if str(item))
        else:
            expected = ()
        cases.append(
            EvalCase(
                query_id=str(query_id),
                query=query,
                expected_paths=expected,
                kind=str(raw.get("kind") or "unspecified"),
            )
        )
    return cases


def cases_from_rows(rows: Iterable[dict[str, Any]]) -> list[EvalCase]:
    return [
        EvalCase(
            query_id=str(row["id"]),
            query=str(row["query"]),
            expected_paths=(str(row["expected_path"]),),
            kind=str(row.get("kind", "unspecified")),
        )
        for row in rows
    ]
