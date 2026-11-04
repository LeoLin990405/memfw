#!/usr/bin/env python3
"""Offline local-input evaluation for a documented LoCoMo-shaped JSON adapter.

Accepted shape: {"conversation": {"session_N": [turns] or {"turns": [...]},
"qa": [{"question": ..., "evidence": ["D1:1", ...]}]}. Evidence values
must match turn dia_id values. No downloads, APIs, or generated answers are used.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import shutil
import sys
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from itertools import count
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast

from memfw.core.repository import Repository
from memfw.curation.engine import enforce_budget
from memfw.index import MemoryIndex
from memfw.policies import PolicyName
from memfw.stats.keyness import tokenize

_EVIDENCE_ID = re.compile(r"(?<![A-Za-z0-9])D\s*:?\s*(\d+)\s*:\s*(\d+)(?!\s*:\s*\d)", re.IGNORECASE)


def _evidence_parts(value: Any) -> tuple[list[str], int]:
    """Split local LoCoMo evidence strings and count malformed fragments."""
    if isinstance(value, list):
        raw_parts = [
            part
            for item in value
            for part in (item.split(";") if isinstance(item, str) else [item])
        ]
    elif isinstance(value, str):
        raw_parts = value.split(";")
    else:
        return [], 1
    valid: list[str] = []
    invalid = 0
    for raw in raw_parts:
        if not isinstance(raw, str):
            invalid += 1
            continue
        fragment = raw.strip().strip(" ,")
        if not fragment:
            continue
        matches = list(_EVIDENCE_ID.finditer(fragment))
        residue = _EVIDENCE_ID.sub("", fragment).strip(" ,;|")
        if not matches:
            invalid += 1
            continue
        valid.extend(f"D{int(match.group(1))}:{int(match.group(2))}" for match in matches)
        if residue:
            invalid += 1
    return valid, invalid


def adapt_with_stats(
    payload: dict[str, Any] | list[Any],
) -> tuple[list[dict[str, str]], list[dict[str, Any]], dict[str, Any]]:
    """Adapt a LoCoMo object or top-level list and retain invalid-reference counts."""
    if isinstance(payload, list):
        combined_turns: list[dict[str, str]] = []
        combined_questions: list[dict[str, Any]] = []
        aggregate: dict[str, Any] = {
            "samples": len(payload),
            "invalid_evidence_fragments": 0,
            "unknown_evidence_references": 0,
            "category_counts": {str(category): 0 for category in range(1, 6)},
            "category_evidence_counts": {
                str(category): {
                    "questions": 0,
                    "valid_evidence_questions": 0,
                    "invalid_fragments": 0,
                    "unknown_references": 0,
                }
                for category in range(1, 6)
            },
            "questions_without_valid_evidence": 0,
            "timestamp_fallback_turns": 0,
        }
        for sample_index, sample in enumerate(payload):
            if not isinstance(sample, dict):
                raise ValueError("top-level sample list entries must be objects")
            sample_turns, sample_questions, sample_stats = adapt_with_stats(sample)
            for key in (
                "invalid_evidence_fragments",
                "unknown_evidence_references",
                "questions_without_valid_evidence",
                "timestamp_fallback_turns",
            ):
                aggregate[key] += sample_stats[key]
            for category, count in sample_stats["category_counts"].items():
                aggregate["category_counts"][category] += count
            for category, values in sample_stats["category_evidence_counts"].items():
                for name, amount in values.items():
                    aggregate["category_evidence_counts"][category][name] += amount
            prefix = f"sample{sample_index}/"
            remap = {turn["dia_id"]: prefix + turn["dia_id"] for turn in sample_turns}
            combined_turns.extend(
                {**turn, "dia_id": remap[turn["dia_id"]]} for turn in sample_turns
            )
            for row in sample_questions:
                combined_questions.append(
                    {**row, "evidence": [remap[evidence] for evidence in row["evidence"]]}
                )
        return combined_turns, combined_questions, aggregate
    conversation = payload.get("conversation")
    questions = payload.get("qa")
    if not isinstance(conversation, dict) or not isinstance(questions, list):
        raise ValueError("expected conversation.session_N plus top-level qa list")
    turns: list[dict[str, str]] = []
    timestamp_fallback_turns = 0
    for session_name, session in conversation.items():
        if re.fullmatch(r"session_\d+", session_name) is None:
            continue
        raw_turns = session.get("turns") if isinstance(session, dict) else session
        if not isinstance(raw_turns, list):
            raise ValueError(f"{session_name} must be a turn list or contain a turns list")
        session_time_raw = conversation.get(f"{session_name}_date_time")
        session_time: datetime | None = None
        if isinstance(session_time_raw, str):
            try:
                session_time = datetime.strptime(session_time_raw, "%I:%M %p on %d %B, %Y").replace(
                    tzinfo=UTC
                )
            except ValueError:
                session_time = None
        for turn_ordinal, turn in enumerate(raw_turns):
            if not isinstance(turn, dict):
                raise ValueError("each conversation turn must be an object")
            dia_id, text, speaker = turn.get("dia_id"), turn.get("text"), turn.get("speaker")
            if not isinstance(dia_id, str) or not isinstance(text, str):
                raise ValueError("each turn requires string dia_id and text")
            if session_time is None:
                created_at = datetime(2023, 1, 1, tzinfo=UTC) + timedelta(seconds=len(turns))
                timestamp_fallback_turns += 1
            else:
                created_at = session_time + timedelta(seconds=turn_ordinal)
            turns.append(
                {
                    "dia_id": dia_id,
                    "text": text,
                    "speaker": str(speaker or "unknown"),
                    "created_at": created_at.isoformat(),
                }
            )
    stats: dict[str, Any] = {
        "samples": 1,
        "invalid_evidence_fragments": 0,
        "unknown_evidence_references": 0,
        "category_counts": {str(category): 0 for category in range(1, 6)},
        "category_evidence_counts": {
            str(category): {
                "questions": 0,
                "valid_evidence_questions": 0,
                "invalid_fragments": 0,
                "unknown_references": 0,
            }
            for category in range(1, 6)
        },
        "questions_without_valid_evidence": 0,
        "timestamp_fallback_turns": timestamp_fallback_turns,
    }
    for row in questions:
        if not isinstance(row, dict) or not isinstance(row.get("question"), str):
            raise ValueError("each qa row requires a string question")
        evidence, malformed = _evidence_parts(row.get("evidence", []))
        evidence = list(dict.fromkeys(evidence))
        stats["invalid_evidence_fragments"] += malformed
        category = row.get("category")
        if isinstance(category, int) and 1 <= category <= 5:
            stats["category_counts"][str(category)] += 1
            category_stats = stats["category_evidence_counts"][str(category)]
            category_stats["questions"] += 1
            category_stats["invalid_fragments"] += malformed
        else:
            category_stats = None
        row["evidence"] = evidence
    ids = {row["dia_id"] for row in turns}
    normalized = [re.sub(r"[^A-Za-z0-9_-]", "-", value) for value in ids]
    if len(normalized) != len(set(normalized)):
        raise ValueError("turn dia_id values collide after repository-safe ID normalization")
    for row in questions:
        category = row.get("category")
        category_stats = (
            stats["category_evidence_counts"].get(str(category))
            if isinstance(category, int) and 1 <= category <= 5
            else None
        )
        known = [evidence_id for evidence_id in row["evidence"] if evidence_id in ids]
        stats["unknown_evidence_references"] += len(row["evidence"]) - len(known)
        if category_stats is not None:
            category_stats["unknown_references"] += len(row["evidence"]) - len(known)
        row["evidence"] = known
        if not known:
            stats["questions_without_valid_evidence"] += 1
        elif category_stats is not None:
            category_stats["valid_evidence_questions"] += 1
    return turns, questions, stats


def adapt(payload: dict[str, Any] | list[Any]) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    """Strict compatibility wrapper; evaluators should use adapt_with_stats."""
    turns, questions, stats = adapt_with_stats(payload)
    if stats["unknown_evidence_references"]:
        raise ValueError("qa evidence references an unknown dia_id")
    return turns, questions


def _fit_budget(ids: list[str], bodies: dict[str, str], budget: int) -> list[str]:
    selected: list[str] = []
    used = 0
    for identifier in ids:
        cost = len(tokenize(bodies[identifier]))
        if used + cost <= budget:
            selected.append(identifier)
            used += cost
    return selected


def _lexical_order(question: str, turns: list[dict[str, str]]) -> list[str]:
    query_terms = Counter(tokenize(question))
    scored: list[tuple[int, int, str]] = []
    for ordinal, turn in enumerate(turns):
        overlap = sum((query_terms & Counter(tokenize(turn["text"]))).values())
        if overlap:
            scored.append((-overlap, ordinal, turn["dia_id"]))
    return [identifier for _, _, identifier in sorted(scored)]


def _bm25_order(
    question: str, turns: list[dict[str, str]], *, k1: float = 1.2, b: float = 0.75
) -> list[str]:
    """Deterministic BM25 ranking over the retained turn corpus."""
    documents = {
        turn["dia_id"]: Counter(
            tokenize(f"speaker {turn.get('speaker', 'unknown')} {turn['text']}")
        )
        for turn in turns
    }
    if not documents:
        return []
    lengths = {key: sum(counts.values()) for key, counts in documents.items()}
    average = sum(lengths.values()) / len(lengths) or 1.0
    df: Counter[str] = Counter(term for counts in documents.values() for term in counts)
    query = tuple(dict.fromkeys(tokenize(question)))
    scored: list[tuple[float, str]] = []
    n = len(documents)
    for identifier, counts in documents.items():
        score = 0.0
        for term in query:
            frequency = counts[term]
            if not frequency:
                continue
            idf = max(0.0, math.log(1.0 + (n - df[term] + 0.5) / (df[term] + 0.5)))
            denominator = frequency + k1 * (1 - b + b * lengths[identifier] / average)
            score += idf * frequency * (k1 + 1) / denominator
        if score > 0:
            scored.append((score, identifier))
    return [identifier for _, identifier in sorted(scored, key=lambda item: (-item[0], item[1]))]


class OfflineMem0StyleStore:
    """Small in-memory CRUD and lexical-retrieval reference; not official Mem0."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, str]] = {}

    def add(self, identifier: str, text: str, speaker: str = "unknown") -> None:
        self.rows[identifier] = {"text": text, "speaker": speaker}

    def update(self, identifier: str, text: str) -> None:
        if identifier not in self.rows:
            raise KeyError(identifier)
        self.rows[identifier]["text"] = text

    def delete(self, identifier: str) -> None:
        self.rows.pop(identifier, None)

    def retrieve(self, query: str) -> list[str]:
        active_turns = [
            {
                "dia_id": identifier,
                "text": f"speaker {value['speaker']} {value['text']}",
            }
            for identifier, value in self.rows.items()
        ]
        return _lexical_order(query, active_turns)


def _retrieval_order(
    index: MemoryIndex, question: str, index_to_dia: dict[str, str], limit: int
) -> list[str]:
    """Read-only retrieval. It does not create MEMORY_ACCESS events."""
    query_terms = tokenize(question)
    term_hits: Counter[str] = Counter()
    for term in dict.fromkeys(query_terms):
        if len(term) >= 2:
            term_hits.update(
                identifier for identifier, _ in index.search(term, limit=max(10, limit))
            )
    return [
        index_to_dia[key]
        for key, _ in sorted(term_hits.items(), key=lambda pair: (-pair[1], pair[0]))
        if key in index_to_dia
    ]


def _score_phase(
    questions: list[dict[str, Any]],
    context_bodies: dict[str, dict[str, str]],
    token_budget: int,
    orders: dict[str, list[list[str]]],
) -> dict[str, Any]:
    totals = {name: [0, 0, 0] for name in orders}
    categories = {name: {str(category): [0, 0, 0] for category in range(1, 6)} for name in orders}
    question_count = sum(bool(row["evidence"]) for row in questions)
    for question_index, row in enumerate(questions):
        evidence = set(row["evidence"])
        if not evidence:
            continue
        for name, system_orders in orders.items():
            selected = set(
                _fit_budget(system_orders[question_index], context_bodies[name], token_budget)
            )
            totals[name][0] += len(selected & evidence)
            totals[name][1] += len(evidence)
            totals[name][2] += bool(selected & evidence)
            category = row.get("category")
            if isinstance(category, int) and 1 <= category <= 5:
                values = categories[name][str(category)]
                values[0] += len(selected & evidence)
                values[1] += len(evidence)
                values[2] += bool(selected & evidence)
    result: dict[str, Any] = {
        name: {
            "evidence_recall": values[0] / values[1] if values[1] else 0.0,
            "evidence_retrieved": values[0],
            "evidence_total": values[1],
            "question_hit_fraction": values[2] / question_count if question_count else 0.0,
            "questions_with_evidence_hit": values[2],
            "questions_with_evidence_total": question_count,
        }
        for name, values in totals.items()
    }
    for name, category_totals in categories.items():
        result[name]["by_category"] = {
            category: {
                "evidence_recall": counts[0] / counts[1] if counts[1] else None,
                "evidence_retrieved": counts[0],
                "evidence_total": counts[1],
                "questions_with_evidence_hit": counts[2],
                "questions_with_valid_evidence": sum(
                    1
                    for row in questions
                    if str(row.get("category")) == category and bool(row["evidence"])
                ),
            }
            for category, counts in sorted(category_totals.items())
        }
    return result


def _retention_budget_bytes(turns: list[dict[str, str]], ratio: float) -> tuple[int, int]:
    raw_bytes = sum(len(row["text"].encode("utf-8")) for row in turns)
    return raw_bytes, max(1, math.floor(raw_bytes * ratio))


def _event_id_factory(start: int = 0) -> Callable[[], str]:
    """Return unbounded deterministic IDs for records, proposals, and decisions."""
    sequence = count(start)
    return lambda: f"locomo-event-{next(sequence):08d}"


def _fixed_clock(value: datetime) -> Callable[[], datetime]:
    return lambda: value


def _fifo_fit_ids(
    turns: list[dict[str, str]], body_by_id: dict[str, str], budget_bytes: int
) -> list[str]:
    """Keep the newest whole-record suffix after FIFO eviction to the byte limit."""
    selected = [turn["dia_id"] for turn in turns]
    used = sum(len(body_by_id[identifier].encode("utf-8")) for identifier in selected)
    while selected and used > budget_bytes:
        evicted = selected.pop(0)
        used -= len(body_by_id[evicted].encode("utf-8"))
    return selected


def evaluate_file(
    path: Path,
    *,
    budget_tokens: int = 128,
    policies: tuple[str, ...] = ("fifo",),
    retention_ratios: tuple[float, ...] = (0.10, 0.25, 0.50),
    checkpoint_path: Path | None = None,
) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        sample_results: list[dict[str, Any]] = []
        for index, sample in enumerate(payload):
            sample_result = _evaluate_payload(
                path,
                sample,
                budget_tokens=budget_tokens,
                policies=policies,
                retention_ratios=retention_ratios,
                sample_index=index,
            )
            sample_results.append(sample_result)
            print(
                f"completed sample {index + 1}/{len(payload)} "
                f"({sample_result['sample_id'] or index}; "
                f"{sample_result['turn_count']} turns, {sample_result['question_count']} QA)",
                file=sys.stderr,
                flush=True,
            )
            if checkpoint_path is not None:
                _write_checkpoint(
                    checkpoint_path,
                    status="running",
                    expected_samples=len(payload),
                    sample_results=sample_results,
                )
        result = {
            "input": str(path),
            "sample_count": len(payload),
            "sample_results": sample_results,
        }
        if checkpoint_path is not None:
            _write_checkpoint(
                checkpoint_path,
                status="complete",
                expected_samples=len(payload),
                sample_results=sample_results,
            )
        return result
    return _evaluate_payload(
        path,
        payload,
        budget_tokens=budget_tokens,
        policies=policies,
        retention_ratios=retention_ratios,
        sample_index=None,
    )


def _write_checkpoint(
    path: Path,
    *,
    status: str,
    expected_samples: int,
    sample_results: list[dict[str, Any]],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(
            {
                "status": status,
                "completed_samples": len(sample_results),
                "expected_samples": expected_samples,
                "sample_results": sample_results,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _evaluate_payload(
    path: Path,
    payload: dict[str, Any],
    *,
    budget_tokens: int,
    policies: tuple[str, ...],
    retention_ratios: tuple[float, ...],
    sample_index: int | None,
) -> dict[str, Any]:
    turns, questions, adapter_stats = adapt_with_stats(payload)
    if not turns:
        raise ValueError("no session_N turns found")
    if not policies or any(
        policy not in {item.value for item in PolicyName} for policy in policies
    ):
        raise ValueError("policies must be selected from the five supported memory policies")
    if any(not 0 < ratio <= 1 for ratio in retention_ratios):
        raise ValueError("retention ratios must be in (0, 1]")
    baseline_before = OfflineMem0StyleStore()
    extracted: dict[str, str] = {}
    original_contexts: dict[str, str] = {}
    extracted_contexts: dict[str, str] = {}
    for row in turns:
        fact = row["text"].split(".", 1)[0].split("。", 1)[0].strip() or row["text"].strip()
        extracted[row["dia_id"]] = fact
        original_contexts[row["dia_id"]] = f"speaker {row['speaker']} {row['text']}"
        extracted_contexts[row["dia_id"]] = f"speaker {row['speaker']} {fact}"
        baseline_before.add(row["dia_id"], fact, row["speaker"])
        baseline_before.update(row["dia_id"], fact)
    before_offline = [baseline_before.retrieve(row["question"]) for row in questions]
    before_bm25 = [_bm25_order(row["question"], turns) for row in questions]
    original_bodies = {row["dia_id"]: row["text"] for row in turns}
    with TemporaryDirectory(prefix="memfw-locomo-sample-") as base_temp:
        base_repo = Repository(Path(base_temp) / "base-vault")
        base_repo.id_factory = _event_id_factory()
        base_repo.clock = _fixed_clock(datetime(2023, 1, 1, tzinfo=UTC))
        base_repo.init()
        created_times: dict[str, datetime] = {}
        for ordinal, turn in enumerate(turns):
            created_at = turn.get("created_at")
            timestamp = (
                datetime.fromisoformat(created_at).astimezone(UTC)
                if created_at
                else datetime(2023, 1, 1, tzinfo=UTC) + timedelta(seconds=ordinal)
            )
            created_times[turn["dia_id"]] = timestamp

            base_repo.clock = _fixed_clock(timestamp)
            base_repo.add(
                f"speaker {turn['speaker']}",
                turn["text"],
                memory_id=re.sub(r"[^A-Za-z0-9_-]", "-", turn["dia_id"]),
                source="local-locomo-adapter",
                provenance="local-fixture",
            )
        evaluation_now = max(created_times.values()) + timedelta(days=2)
        base_index = MemoryIndex(base_repo.index_path)
        base_index.rebuild(base_repo)
        index_to_dia = {
            re.sub(r"[^A-Za-z0-9_-]", "-", row["dia_id"]): row["dia_id"] for row in turns
        }
        before_memfw = [
            _retrieval_order(base_index, row["question"], index_to_dia, len(turns))
            for row in questions
        ]
        before_scores = _score_phase(
            questions,
            {
                "memfw": original_contexts,
                "bm25": original_contexts,
                "offline_mock_crud": extracted_contexts,
            },
            budget_tokens,
            {
                "memfw": before_memfw,
                "bm25": before_bm25,
                "offline_mock_crud": before_offline,
            },
        )
        base_event_count = len(base_repo.events())
        budget_results: dict[str, dict[str, dict[str, Any]]] = {}
        for ratio in retention_ratios:
            _, ratio_budget_bytes = _retention_budget_bytes(turns, ratio)
            bm25_kept = set(_fifo_fit_ids(turns, original_bodies, ratio_budget_bytes))
            bm25_turns = [row for row in turns if row["dia_id"] in bm25_kept]
            after_bm25 = [_bm25_order(row["question"], bm25_turns) for row in questions]
            budget_results[f"{ratio:.0%}"] = {
                policy: _evaluate_file_policy(
                    path,
                    turns,
                    questions,
                    adapter_stats,
                    base_vault=base_repo.root,
                    before_scores=before_scores,
                    after_bm25=after_bm25,
                    extracted_bodies=extracted,
                    context_bodies={
                        "memfw": original_contexts,
                        "bm25": original_contexts,
                        "offline_mock_crud": extracted_contexts,
                    },
                    evaluation_now=evaluation_now,
                    next_event_id=base_event_count,
                    budget_tokens=budget_tokens,
                    policy=policy,
                    retention_ratio=ratio,
                )
                for policy in policies
            }
    primary = budget_results[f"{retention_ratios[0]:.0%}"][policies[0]]
    return {
        **primary,
        "sample_index": sample_index,
        "sample_id": payload.get("sample_id")
        if isinstance(payload.get("sample_id"), str)
        else None,
        "primary_policy_alias": policies[0],
        "budget_results": budget_results,
        "policies_evaluated": list(policies),
        "retention_ratios": list(retention_ratios),
    }


def _evaluate_file_policy(
    path: Path,
    turns: list[dict[str, str]],
    questions: list[dict[str, Any]],
    adapter_stats: dict[str, Any],
    *,
    base_vault: Path,
    before_scores: dict[str, Any],
    after_bm25: list[list[str]],
    extracted_bodies: dict[str, str],
    context_bodies: dict[str, dict[str, str]],
    evaluation_now: datetime,
    next_event_id: int,
    budget_tokens: int,
    policy: str,
    retention_ratio: float,
) -> dict[str, Any]:
    raw_body_bytes, budget_bytes = _retention_budget_bytes(turns, retention_ratio)
    baseline = OfflineMem0StyleStore()
    extracted: dict[str, str] = {}
    for row in turns:
        # A deterministic, transparent mock extractor keeps one first-sentence
        # fact per turn. It is not an LLM or a claim about Mem0 extraction.
        fact = row["text"].split(".", 1)[0].split("。", 1)[0].strip() or row["text"].strip()
        extracted[row["dia_id"]] = fact
        baseline.add(row["dia_id"], fact, row["speaker"])
    for identifier, fact in extracted.items():
        baseline.update(identifier, fact)
    baseline_initial_body_bytes = sum(len(text.encode("utf-8")) for text in extracted.values())
    offline_kept = set(_fifo_fit_ids(turns, extracted_bodies, budget_bytes))
    for identifier in tuple(baseline.rows):
        if identifier not in offline_kept:
            baseline.delete(identifier)
    with TemporaryDirectory(prefix="memfw-locomo-") as temp:
        clone_root = Path(temp) / "vault"
        shutil.copytree(base_vault, clone_root)
        repository: Any = Repository(clone_root)
        repository.id_factory = _event_id_factory(next_event_id)
        repository.clock = _fixed_clock(evaluation_now)
        index = MemoryIndex(repository.index_path)
        index_to_dia = {
            re.sub(r"[^A-Za-z0-9_-]", "-", row["dia_id"]): row["dia_id"] for row in turns
        }
        before = before_scores

        config = repository.read_config()
        config.update(
            {
                "budget_mode": "body",
                "budget_scope": "all",
                "max_bytes": budget_bytes,
                "forget_policy": policy,
                "forget_action": "hard",
                "auto_enforce_on_write": False,
            }
        )
        config.pop("max_count", None)
        repository.configure(config)
        repository.clock = _fixed_clock(evaluation_now)
        budget_result = enforce_budget(
            repository,
            now=evaluation_now,
            policy_override=PolicyName(policy),
            dry_run=False,
            actor="mechanism",
        )
        simulated_reviews = 0
        if budget_result.pending_proposal_ids:
            simulator = getattr(repository, "simulate_proposal_decision", None)
            if simulator is None:
                raise RuntimeError("simulated proposal approval API is not available")
            proposals = {
                proposal.proposal_id: proposal
                for proposal in repository.list_proposals(status="pending")
            }
            selected: list[str] = []
            claimed_targets: set[str] = set()
            for proposal_id in sorted(budget_result.pending_proposal_ids):
                proposal = proposals[proposal_id]
                targets = set(proposal.target_ids)
                if not targets & claimed_targets:
                    selected.append(proposal_id)
                    claimed_targets.update(targets)
            for proposal_id in selected:
                simulator(proposal_id, decision="approve")
            simulated_reviews = len(selected)
            from memfw.curation.review import apply_approved

            for proposal_id in selected:
                apply_approved(
                    repository,
                    proposal_id,
                    actor="simulated_approver",
                    enforce_fallback=False,
                )
        if not budget_result.satisfied_after_apply:
            budget_result = enforce_budget(
                repository,
                now=evaluation_now,
                policy_override=PolicyName.FIFO,
                dry_run=False,
                actor="mechanism",
            )
        index.rebuild(repository)
        retained_ids = {row.id for row in repository.list()}
        retained_documents = repository.list()
        retained_body_bytes = sum(len(row.body.encode("utf-8")) for row in retained_documents)
        event_count = len(repository.events())
        after_memfw = [
            _retrieval_order(index, row["question"], index_to_dia, len(turns)) for row in questions
        ]
        after_memfw_contexts = dict(context_bodies["memfw"])
        for document in retained_documents:
            original_id = index_to_dia.get(document.id)
            if original_id is not None:
                after_memfw_contexts[original_id] = f"{document.title} {document.body}"
        after_offline = [baseline.retrieve(row["question"]) for row in questions]
        after_context_bodies = dict(context_bodies)
        after_context_bodies["memfw"] = after_memfw_contexts
        after = _score_phase(
            questions,
            after_context_bodies,
            budget_tokens,
            {"memfw": after_memfw, "bm25": after_bm25, "offline_mock_crud": after_offline},
        )
        access_event_count = sum(
            event.kind.value == "memory_access" for event in repository.events()
        )
    before["offline_mem0_style_crud"] = before["offline_mock_crud"]
    after["offline_mem0_style_crud"] = after["offline_mock_crud"]
    return {
        "input": str(path),
        "turn_count": len(turns),
        "question_count": len(questions),
        "adapter_counts": adapter_stats,
        "budget_tokens_per_query": budget_tokens,
        "retention_budget": {
            "type": "body_utf8_bytes",
            "ratio": retention_ratio,
            "value": budget_bytes,
            "initial_value": raw_body_bytes,
            "initial_count": len(turns),
            "access_events": 0,
            "evaluation_clock": "max(created_at)+2 days",
        },
        "memfw_retained_count": len(retained_ids),
        "memfw_retained_body_bytes": retained_body_bytes,
        "baseline_retained_count": len(baseline.rows),
        "baseline_retained_body_bytes": sum(
            len(row["text"].encode("utf-8")) for row in baseline.rows.values()
        ),
        "baseline_initial_body_bytes": baseline_initial_body_bytes,
        "baseline_budget_satisfied": sum(
            len(row["text"].encode("utf-8")) for row in baseline.rows.values()
        )
        <= budget_bytes,
        "event_count": event_count,
        "memory_access_event_count": access_event_count,
        "memfw_forget_policy": f"{policy} hard forget",
        "budget_satisfied": budget_result.satisfied_after_apply,
        "simulated_consolidation_reviews": simulated_reviews,
        "consolidation_review_note": (
            "Deterministic local harness approvals only; "
            "no person or live model reviewed proposals."
            if policy == "consolidate_then_forget"
            else "not applicable"
        ),
        "query_tokenizer": (
            "memfw.stats.keyness.tokenize; same normalized query terms across all systems"
        ),
        "evidence_mapping": (
            "exact original turn IDs; merged representative bodies are retained, but deleted "
            "source IDs are not credited through provenance alone"
        ),
        "before_budget": before,
        "after_budget": after,
        "offline_mem0_style_crud_description": (
            "Compatibility label for offline_mock_extraction_crud; not official Mem0"
        ),
        "offline_mock_extraction_crud_description": (
            "local add/update/delete store with a deterministic first-sentence mock extractor; "
            "lexical overlap retrieval and FIFO hard deletion at the same body-byte capacity; "
            "not official Mem0. "
            "Update is exercised for every extracted fact."
        ),
        "bm25_description": "local deterministic Okapi BM25 ranking; no external service",
        "answer_generation": "none; evidence retrieval only",
    }


def main() -> None:
    if "--day7" in sys.argv:
        sys.argv.remove("--day7")
        from day7_eval import main as day7_main

        day7_main()
        return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "inputs", nargs="*", type=Path, help="Local JSON fixture or conversation files"
    )
    parser.add_argument(
        "--data", type=Path, help="One local LoCoMo JSON file, including top-level list"
    )
    parser.add_argument("--budget-tokens", type=int, default=128)
    parser.add_argument(
        "--policies",
        nargs="+",
        choices=[policy.value for policy in PolicyName],
        default=[policy.value for policy in PolicyName],
        help="Retention policies to compare under the same UTF-8 body-byte budgets",
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--checkpoint", type=Path)
    args = parser.parse_args()
    if args.budget_tokens < 1:
        parser.error("--budget-tokens must be positive")
    inputs = [args.data] if args.data is not None else args.inputs
    if not inputs:
        parser.error("provide local input paths or --data PATH")
    result_rows = [
        evaluate_file(
            path,
            budget_tokens=args.budget_tokens,
            policies=tuple(args.policies),
            checkpoint_path=(
                args.checkpoint
                if len(inputs) == 1
                else args.checkpoint.with_name(
                    f"{args.checkpoint.stem}-{index}{args.checkpoint.suffix}"
                )
            )
            if args.checkpoint
            else None,
        )
        for index, path in enumerate(inputs)
    ]
    report = {
        "adapter": "official LoCoMo session_N/qa local adapter v2",
        "dataset_sources": [
            {
                "path": str(path),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "content_stored_in_report": False,
            }
            for path in inputs
        ],
        "retention_budget_definition": (
            "floor(ratio * sum(UTF-8 bytes of every original session turn text)); "
            "turns are retained whole, without partial truncation"
        ),
        "query_context_budget_tokens": args.budget_tokens,
        "evaluation_clock": "per-sample max(created_at)+2 days",
        "access_events": "none for any evaluated system; retrieval does not log MEMORY_ACCESS",
        "text_scope": (
            "session_N turn.text only, plus the same speaker metadata for all retrieval systems; "
            "no image understanding, downloads, model answers, or generated captions"
        ),
        "results": result_rows,
    }
    text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
        result_rows = cast(list[dict[str, Any]], report["results"])
        compact = [
            {
                "input": result["input"],
                "sample_count": result.get("sample_count", 1),
                "sample_indices": [row["sample_index"] for row in result.get("sample_results", [])],
                "sample_ids": [row.get("sample_id") for row in result.get("sample_results", [])],
                "turn_count": result.get("turn_count"),
                "question_count": result.get("question_count"),
            }
            for result in result_rows
        ]
        print(json.dumps({"output": str(args.output), "results": compact}, sort_keys=True))
    else:
        print(text, end="")


if __name__ == "__main__":
    main()
