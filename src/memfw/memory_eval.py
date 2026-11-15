"""Local five-class memory evaluation scaffold.

Questions are generated from the vault; they are candidates for the human/AI
annotation pipeline, not human-labelled ground truth. Metrics cover stale-fact
recall for updated memories, as_of accuracy, and refusal correctness.
"""

from __future__ import annotations

import json
import random
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

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
    "CAUSAL_FIXTURE_DIR",
    "CausalMaterial",
    "CausalPromotionReport",
    "CausalRetrievalResult",
    "CausalWriteResult",
    "MemoryEvalReport",
    "MemoryQuestion",
    "QuestionKind",
    "compose_answer",
    "evaluate_questions",
    "generate_questions",
    "load_causal_materials",
    "run_causal_promotion_eval",
]


# --- causal promotion experiment ---------------------------------------------
#
# Kept in this module rather than a new ``memfw.eval`` package: the repository
# already has a top-level ``eval/`` report directory, so a second ``eval`` name
# inside the package would shadow it in confusing ways, and this module is where
# the existing evaluation scaffolding (questions, reports) lives. The experiment
# uses two layers:
#
# * write layer — each fixture material is written through the real
#   ``Repository.add`` path with ``causal=verified`` and the shared gate either
#   accepts or rejects it. A false accept is a pseudo-causal promotion.
# * retrieval layer — correctly labelled documents are searched with the local
#   FTS retriever (no model, no network) and a deterministic answer template
#   checks that a ``hypothesis`` is never rendered as a conclusion.
#
# The fixtures are synthetic; see ``tests/fixtures/causal_eval/``.

CAUSAL_FIXTURE_DIR = (
    Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "causal_eval"
)

_HYPOTHESIS_LEVELS = frozenset({"hypothesis", "association"})


@dataclass(frozen=True)
class CausalMaterial:
    """One synthetic material: what is attempted, and what should be stored."""

    name: str
    expected_verified: bool
    query: str
    attempt: dict[str, Any]
    retrieval: dict[str, Any]
    note: str = ""
    group: str = ""
    expected_block: str = ""
    """Which rule should refuse the write: identification, evidence, or empty."""


@dataclass(frozen=True)
class CausalWriteResult:
    name: str
    expected_verified: bool
    accepted: bool
    resulting_causal: str | None
    error: str | None

    @property
    def false_accept(self) -> bool:
        return self.accepted and not self.expected_verified

    @property
    def false_reject(self) -> bool:
        return not self.accepted and self.expected_verified

    @property
    def spurious_promotion(self) -> bool:
        return self.accepted and self.resulting_causal == "verified" and not self.expected_verified


@dataclass(frozen=True)
class CausalRetrievalResult:
    name: str
    query: str
    expected_label: str
    labels: dict[str, str]
    missing_labels: tuple[str, ...]
    mismatched_labels: tuple[str, ...]
    hypothesis_as_conclusion: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "expected_label": self.expected_label,
            "labels": dict(self.labels),
            "missing_labels": list(self.missing_labels),
            "mismatched_labels": list(self.mismatched_labels),
            "hypothesis_as_conclusion": self.hypothesis_as_conclusion,
        }


@dataclass
class CausalPromotionReport:
    write: list[CausalWriteResult]
    retrieval: list[CausalRetrievalResult]

    @property
    def materials(self) -> int:
        return len(self.write)

    @property
    def false_accepts(self) -> int:
        return sum(1 for item in self.write if item.false_accept)

    @property
    def false_rejects(self) -> int:
        return sum(1 for item in self.write if item.false_reject)

    @property
    def spurious_promotions(self) -> int:
        return sum(1 for item in self.write if item.spurious_promotion)

    @property
    def missing_labels(self) -> int:
        return sum(len(item.missing_labels) for item in self.retrieval)

    @property
    def hypothesis_as_conclusion(self) -> int:
        return sum(item.hypothesis_as_conclusion for item in self.retrieval)

    def as_dict(self) -> dict[str, Any]:
        return {
            "materials": self.materials,
            "spurious_promotions": self.spurious_promotions,
            "false_accepts": self.false_accepts,
            "false_rejects": self.false_rejects,
            "write_layer": {
                "attempts": self.materials,
                "accepted": sum(1 for item in self.write if item.accepted),
                "rejected": sum(1 for item in self.write if not item.accepted),
                "false_accepts": self.false_accepts,
                "false_rejects": self.false_rejects,
                "spurious_promotions": self.spurious_promotions,
                "materials": [
                    {
                        "name": item.name,
                        "expected_verified": item.expected_verified,
                        "accepted": item.accepted,
                        "resulting_causal": item.resulting_causal,
                        "error": item.error,
                    }
                    for item in self.write
                ],
            },
            "retrieval_layer": {
                "queries": len(self.retrieval),
                "missing_labels": self.missing_labels,
                "hypothesis_as_conclusion": self.hypothesis_as_conclusion,
                "materials": [item.as_dict() for item in self.retrieval],
            },
        }


def load_causal_materials(directory: Path | str | None = None) -> list[CausalMaterial]:
    """Read the synthetic fixtures. Unknown keys are rejected by :class:`dict` use."""

    base = Path(directory) if directory is not None else CAUSAL_FIXTURE_DIR
    if not base.is_dir():
        raise FileNotFoundError(f"causal eval fixtures not found: {base}")
    materials: list[CausalMaterial] = []
    for path in sorted(base.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        materials.append(
            CausalMaterial(
                name=str(payload["name"]),
                expected_verified=bool(payload["expected_verified"]),
                query=str(payload.get("query") or "does it cause that"),
                attempt=dict(payload["attempt"]),
                retrieval=dict(payload["retrieval"]),
                note=str(payload.get("note") or ""),
                group=str(payload.get("group") or ""),
                expected_block=str(payload.get("expected_block") or ""),
            )
        )
    if not materials:
        raise FileNotFoundError(f"no causal eval fixtures in {base}")
    return materials


def _write_layer(materials: list[CausalMaterial], workdir: Path) -> list[CausalWriteResult]:
    from .core.errors import IntegrityError
    from .core.repository import Repository

    results: list[CausalWriteResult] = []
    for index, material in enumerate(materials):
        root = workdir / f"write-{index}-{material.name}"
        repository = Repository(root)
        repository.init()
        accepted = False
        resulting: str | None = None
        error: str | None = None
        try:
            document = repository.add(**material.attempt, memory_type="claim")
            accepted = True
            resulting = document.causal
        except (IntegrityError, ValueError, TypeError) as exc:
            error = str(exc)
        results.append(
            CausalWriteResult(
                name=material.name,
                expected_verified=material.expected_verified,
                accepted=accepted,
                resulting_causal=resulting,
                error=error,
            )
        )
    return results


def compose_answer(question: str, rows: list[dict[str, Any]]) -> tuple[str, int]:
    """A deterministic answer template that never states a hypothesis as fact.

    The template has one conclusion section, fed only by rows whose label is
    ``verified``. Everything else is reported under an explicit hedge. The
    returned count re-reads that conclusion section from the labels and is the
    number of hypothesis/association rows it contains: a correct template
    always returns 0, and a template edited to quote a hypothesis as a
    conclusion makes it positive.
    """

    levels = {str(row.get("id")): str(row.get("causal") or "none") for row in rows}
    conclusion = [identifier for identifier, level in levels.items() if level == "verified"]
    hedged = [identifier for identifier, level in levels.items() if level != "verified"]
    lines = [f"Question: {question}"]
    if conclusion:
        lines.append(
            "Verified causal claims (identification checked): " + "; ".join(conclusion)
        )
    if hedged:
        hedged_levels = sorted({levels[identifier] for identifier in hedged})
        lines.append(
            "Not causal conclusions (" + ", ".join(hedged_levels) + "): " + "; ".join(hedged)
        )
    hypothesis_as_conclusion = sum(
        1 for identifier in conclusion if levels[identifier] in _HYPOTHESIS_LEVELS
    )
    return "\n".join(lines), hypothesis_as_conclusion


def _retrieval_layer(
    materials: list[CausalMaterial], workdir: Path
) -> list[CausalRetrievalResult]:
    from .core.repository import Repository
    from .memory_hybrid import MemoryHybridRetriever

    results: list[CausalRetrievalResult] = []
    root = workdir / "retrieval"
    repository = Repository(root)
    repository.init()
    ids_by_material: dict[str, str] = {}
    for material in materials:
        retrieval = dict(material.retrieval)
        title = str(retrieval.pop("title", f"{material.name} claim"))
        default_body = f"{material.name}: {material.note or 'synthetic causal material'}"
        body = str(retrieval.pop("body", default_body))
        document = repository.add(title, body, memory_type="claim", **retrieval)
        ids_by_material[material.name] = document.id
    retriever = MemoryHybridRetriever(repository)
    for material in materials:
        result = retriever.search(material.query, limit=10, mode="fts")
        by_id = {str(item["id"]): item for item in result["results"]}
        expected_id = ids_by_material[material.name]
        labels: dict[str, str] = {}
        missing: list[str] = []
        mismatched: list[str] = []
        expected_label = str(material.retrieval.get("causal") or "none")
        for identifier, item in by_id.items():
            if "causal" not in item or item.get("causal") in (None, ""):
                missing.append(identifier)
                continue
            labels[identifier] = str(item["causal"])
        if expected_id in labels and labels[expected_id] != expected_label:
            mismatched.append(expected_id)
        rows = [
            {"id": identifier, "causal": item.get("causal")} for identifier, item in by_id.items()
        ]
        answer, hypothesis_as_conclusion = compose_answer(material.query, rows)
        if not answer:
            raise AssertionError("answer template produced no text")
        results.append(
            CausalRetrievalResult(
                name=material.name,
                query=material.query,
                expected_label=expected_label,
                labels=labels,
                missing_labels=tuple(missing),
                mismatched_labels=tuple(mismatched),
                hypothesis_as_conclusion=hypothesis_as_conclusion,
            )
        )
    return results


def run_causal_promotion_eval(
    fixtures: Path | str | None = None,
    *,
    workdir: Path | str | None = None,
) -> CausalPromotionReport:
    """Run the pseudo-causality experiment over the synthetic fixtures.

    ``workdir`` is a scratch directory (a temporary one by default). Nothing is
    written outside it, no network is used, and no model is called.
    """

    materials = load_causal_materials(fixtures)
    if workdir is not None:
        base = Path(workdir)
        base.mkdir(parents=True, exist_ok=True)
        return CausalPromotionReport(
            write=_write_layer(materials, base),
            retrieval=_retrieval_layer(materials, base),
        )
    with tempfile.TemporaryDirectory(prefix="memfw-causal-eval-") as scratch:
        base = Path(scratch)
        return CausalPromotionReport(
            write=_write_layer(materials, base),
            retrieval=_retrieval_layer(materials, base),
        )
