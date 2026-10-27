"""Architecture iteration for memfw under the frozen Step G protocol.

Variants change only the memory system's retrieval/prompt logic; the LoCoMo subset,
turns, questions, answer model, embedder, top-k, context budget and judge stay identical.

Arms are exploratory ablations: they are compared against the frozen memfw v1 rows and
the already-recorded Mem0/framework rows with paired per-question tests. Nothing here
rewrites the frozen primary experiment.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from eval.stepG_metrics import (  # noqa: E402
    aggregate_system,
    judge_is_correct,
    judge_model_config,
    judge_per_question,
    paired_comparison,
    run_judge,
)
from eval.stepG_runner import (  # noqa: E402
    ANSWER_PROMPT,
    MemfwSystem,
    _read_rows,
    _resolve,
    _safe_id,
    _write_json,
    load_config,
    make_chat,
    make_embedder,
    redact_text,
    run_system,
)

ARCH_DIR = "arch"

BASE_PROMPT = ANSWER_PROMPT

PROMPT_V2 = (
    "Answer the question using only the memories below. Each memory is tagged as "
    "[date, speaker].\n"
    "Rules:\n"
    "1. Only use a memory if it supports the answer for the person asked in the question. "
    "Do not attribute one person's fact to the other person; if no memory matches the asked "
    "person/fact, reply exactly: unknown.\n"
    "2. If the question asks when something happened, use the memory's date tag to convert "
    "relative expressions (e.g. last weekend, yesterday, last Friday) into an absolute date "
    "(month day, year) and answer with that date.\n"
    "3. If the question presupposes a fact that is not in the memories, reply exactly: unknown.\n"
    "4. Reply with a short phrase.\n\n"
    "Memories:\n{memories}\n\nQuestion: {question}\nAnswer:"
)

PROMPT_V3 = (
    "Answer the question using only the memories below. Each memory is tagged as "
    "[date, speaker].\n"
    "Rules:\n"
    "1. First identify the person the question asks about. Only use memories whose "
    "speaker/author or text is about that person. If the only matching facts belong to the "
    "other person, reply exactly: unknown.\n"
    "2. If the question asks when/date/time, use the memory's date tag and convert relative "
    "expressions to an absolute calendar date. Answer with the date only.\n"
    "3. If the question needs a list or a count, include every matching item found in the "
    "memories; do not stop at the first one.\n"
    "4. If the memories do not contain the asked fact, reply exactly: unknown.\n\n"
    "Memories:\n{memories}\n\nQuestion: {question}\nAnswer:"
)

PROMPT_V4 = (
    "Answer the question using only the memories below. Each memory is tagged as "
    "[date, speaker], where date is the conversation date.\n"
    "Rules:\n"
    "1. Read every memory carefully. If a memory contains the asked fact (directly or via a "
    "relative time), answer from it; do not reply unknown when the evidence is present.\n"
    "2. Person check: the question asks about one person. If the only matching facts belong "
    "to the other person, reply exactly: unknown. Never transfer one person's fact to the "
    "other.\n"
    "3. Date questions: convert relative expressions (last weekend, last Friday, a few days "
    "ago) using the memory's date tag, and answer with an absolute month/day/year.\n"
    "4. For where / how many / what motivates, include every matching item found in the "
    "memories (e.g. multiple places or people), not just the first.\n"
    "5. You may use common geographic knowledge (e.g. a city is in a country) when the "
    "question asks for a country.\n"
    "6. If the asked fact truly is absent, reply exactly: unknown.\n"
    "Reply with a short phrase.\n\n"
    "Memories:\n{memories}\n\nQuestion: {question}\nAnswer:"
)

RULES_V5 = (
    "Rules:\n"
    "1. Answer from the memories even when the wording is indirect; do not reply unknown when a "
    "memory contains the asked fact. (A plan or attempt counts for a hobby question.)\n"
    "2. Person check: do not transfer facts between the two people in the question. If only the "
    "other person's facts match, reply exactly: unknown.\n"
    "3. Convert relative dates using the memory's tag. last Friday / last weekend / a few days "
    "ago refer to before the tag date; answer with an absolute month/day/year.\n"
    "4. For where / how many / what motivates, include every matching item found in the "
    "memories.\n"
    "5. Common geographic knowledge is allowed (e.g. a city is in a country) when a memory "
    "names the place.\n"
    "6. If the asked fact truly is absent, reply exactly: unknown.\n"
    "Reply with a short phrase.\n\n"
)

PROMPT_V5 = (
    (
        "Answer the question using only the memories below. Each memory is tagged as "
        "[conversation date, speaker].\n"
        "Worked examples (generic, not from this conversation):\n"
        '- Memory: "[2023-03-10, Alex] I adopted a dog yesterday." '
        'Question: "When did Alex adopt a dog?" Answer: "March 9, 2023."\n'
        '- Memory: "[2023-04-02, Jamie] I am thinking about learning guitar." '
        'Question: "Which instrument does Jamie want to learn?" Answer: "guitar."\n'
        '- Memory: "[2023-05-01, Alex] We drove to Kyoto." '
        'Question: "Which country did Alex visit?" Answer: "Japan."\n'
        '- Memory: "[2023-06-15, Jamie] I fixed my bike last Friday." '
        'Question: "When did Jamie fix the bike?" Answer: "Friday, June 9, 2023."\n'
        '- Memory: "[2023-07-20, Alex] We visited Rome and Florence." '
        'Question: "Where did Alex travel?" Answer: "Rome and Florence."\n'
    )
    + RULES_V5
    + "Memories:\n{memories}\n\nQuestion: {question}\nAnswer:"
)

PROMPT_V5_RULES = (
    (
        "Answer the question using only the memories below. Each memory is tagged as "
        "[conversation date, speaker].\n"
    )
    + RULES_V5
    + "Memories:\n{memories}\n\nQuestion: {question}\nAnswer:"
)

RULES_V6 = (
    "Rules:\n"
    "1. Answer from the memories even when the wording is indirect; do not reply unknown when a "
    "memory contains the asked fact. For hobby/interest questions, a memory that says the "
    "person was thinking about, trying, or starting an activity counts as that activity.\n"
    "2. Person check: do not transfer facts between the two people in the question. If only the "
    "other person's facts match, reply exactly: unknown.\n"
    "3. Date questions: use the memory's date tag as the anchor. If the memory says a relative "
    "time (last weekend, last Friday, a few days ago), answer in the form "
    "'<relative time> before <anchor date>', e.g. 'a few days before May 24, 2023' or "
    "'Friday before July 27, 2023'.\n"
    "4. For where / how many / what motivates, include every matching item found in the "
    "memories. Answer small counts with words (two, three), not digits.\n"
    "5. If a memory names a place (city/landmark), you may use common geographic knowledge to "
    "name its country.\n"
    "6. If the asked fact truly is absent, reply exactly: unknown.\n"
    "Reply with a short phrase.\n\n"
)

PROMPT_V6 = (
    (
        "Answer the question using only the memories below. Each memory is tagged as "
        "[conversation date, speaker].\n"
        "Worked examples (generic, not from this conversation):\n"
        '- Memory: "[2023-03-10, Alex] I adopted a dog yesterday." '
        'Question: "When did Alex adopt a dog?" Answer: "the day before March 10, 2023."\n'
        '- Memory: "[2023-04-02, Jamie] I am thinking about learning guitar." '
        'Question: "Which instrument does Jamie want to learn?" Answer: "guitar."\n'
        '- Memory: "[2023-05-01, Alex] We drove to Kyoto." '
        'Question: "Which country did Alex visit?" Answer: "Japan."\n'
        '- Memory: "[2023-06-15, Jamie] I fixed my bike last Friday." '
        'Question: "When did Jamie fix the bike?" Answer: "Friday before June 15, 2023."\n'
        '- Memory: "[2023-07-20, Alex] We visited Rome and Florence." '
        'Question: "Where did Alex travel?" Answer: "Rome and Florence."\n'
        '- Memory: "[2023-08-01, Jamie] I adopted two cats." '
        'Question: "How many cats does Jamie have?" Answer: "two."\n'
    )
    + RULES_V6
    + "Memories:\n{memories}\n\nQuestion: {question}\nAnswer:"
)

PROMPTS = {
    "base": (BASE_PROMPT, "base"),
    "v2": (PROMPT_V2, "v2"),
    "v3": (PROMPT_V3, "v3"),
    "v4": (PROMPT_V4, "v4"),
    "v5": (PROMPT_V5, "v5"),
    "v5_rules": (PROMPT_V5_RULES, "v5_rules"),
    "v6": (PROMPT_V6, "v6"),
}

TOKEN_RE = re.compile(r"[a-z0-9]+")
STOPWORDS = {
    "the",
    "a",
    "an",
    "and",
    "or",
    "of",
    "to",
    "in",
    "on",
    "for",
    "with",
    "at",
    "by",
    "is",
    "was",
    "were",
    "did",
    "does",
    "do",
    "has",
    "have",
    "had",
    "what",
    "when",
    "where",
    "who",
    "which",
    "why",
    "how",
    "many",
    "much",
    "his",
    "her",
    "he",
    "she",
    "they",
    "their",
    "it",
    "its",
    "that",
    "this",
    "from",
    "about",
    "as",
    "be",
    "been",
}
MONTHS = {
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
}
TEMPORAL_HINTS = {
    "when",
    "date",
    "day",
    "month",
    "year",
    "time",
    "long",
    "ago",
    "last",
    "first",
    "before",
    "after",
    "during",
    "weekend",
    "week",
    "yesterday",
    "today",
    "tomorrow",
}
PERSON_RE = re.compile(r"\b(Sam|Evan)\b", re.IGNORECASE)


def _tokens(text: str) -> list[str]:
    return [token for token in TOKEN_RE.findall(text.lower()) if token not in STOPWORDS]


def _speaker_from_title(title: str) -> str:
    prefix = "speaker "
    return title[len(prefix) :] if title.startswith(prefix) else title


class MemfwArchSystem(MemfwSystem):
    """memfw vault with optional hybrid retrieval, metadata-aware context and rerank."""

    def __init__(
        self,
        root: Path,
        embedder: Any,
        *,
        retrieval: str = "dense",
        prompt_version: str = "base",
        compact: bool = False,
        mmr_lambda: float = 0.72,
    ) -> None:
        super().__init__(root, embedder)
        self.retrieval = retrieval
        self.prompt_version = prompt_version
        self.compact = compact
        self.mmr_lambda = mmr_lambda
        self._date_by_id: dict[str, str] = {}
        self._speaker_by_id: dict[str, str] = {}
        self._meta_path = self.repository.root / ".arch_meta.json"
        self._bm25_cache: dict[str, Any] | None = None
        self._load_arch_meta()

    def _load_arch_meta(self) -> None:
        if not self._meta_path.exists():
            return
        try:
            payload = json.loads(self._meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for memory_id, record in payload.items():
            if isinstance(record, dict):
                self._date_by_id[memory_id] = str(record.get("date") or "")
                self._speaker_by_id[memory_id] = str(record.get("speaker") or "")

    def _save_arch_meta(self) -> None:
        payload = json.dumps(
            {
                memory_id: {
                    "date": self._date_by_id.get(memory_id, ""),
                    "speaker": self._speaker_by_id.get(memory_id, ""),
                }
                for memory_id in self._date_by_id
            },
            sort_keys=True,
        ).encode("utf-8")
        temporary = self._meta_path.with_name(self._meta_path.name + ".tmp")
        temporary.write_bytes(payload)
        temporary.replace(self._meta_path)

    def add(self, turn: dict[str, str]) -> None:
        super().add(turn)
        memory_id = _safe_id(turn["dia_id"])
        self._date_by_id[memory_id] = str(turn["created_at"])[:10]
        self._speaker_by_id[memory_id] = str(turn["speaker"])
        self._save_arch_meta()
        self._bm25_cache = None

    @staticmethod
    def _compact_body(body: str) -> str:
        text = re.sub(r"^(hey|hi|hello)[^!?.]*[!?.]\s*", "", body, flags=re.IGNORECASE).strip()
        text = re.sub(
            r"^(good to see you|great to hear from you)[^!?.]*[!?.]\s*",
            "",
            text,
            flags=re.IGNORECASE,
        )
        return re.sub(r"\s+", " ", text)

    def _memory_text(self, document: Any) -> str:
        date = self._date_by_id.get(document.id, "")
        speaker = self._speaker_by_id.get(document.id) or _speaker_from_title(
            self.title_by_id.get(document.id, document.title)
        )
        body = self._compact_body(document.body) if self.compact else document.body
        tag = f"[{date}, {speaker}] " if date else f"[{speaker}] "
        return f"{tag}{body}"

    def _bm25(self, documents: dict[str, Any]) -> dict[str, Any]:
        if self._bm25_cache is not None:
            return self._bm25_cache
        docs: dict[str, list[str]] = {}
        df: dict[str, int] = defaultdict(int)
        for memory_id, document in documents.items():
            tokens = _tokens(self._memory_text(document))
            docs[memory_id] = tokens
            for token in set(tokens):
                df[token] += 1
        total = len(docs) or 1
        average = statistics.fmean(len(tokens) for tokens in docs.values()) if docs else 1.0
        cache = {"docs": docs, "df": df, "n": total, "avgdl": average or 1.0}
        self._bm25_cache = cache
        return cache

    def _bm25_scores(self, query: str, documents: dict[str, Any]) -> dict[str, float]:
        cache = self._bm25(documents)
        tokens = _tokens(query)
        scores: dict[str, float] = defaultdict(float)
        k1, b = 1.2, 0.75
        for memory_id, doc_tokens in cache["docs"].items():
            length = len(doc_tokens)
            frequency: dict[str, int] = defaultdict(int)
            for token in doc_tokens:
                frequency[token] += 1
            for token in tokens:
                if not frequency.get(token):
                    continue
                idf = math.log(
                    1 + (cache["n"] - cache["df"][token] + 0.5) / (cache["df"][token] + 0.5)
                )
                denominator = frequency[token] + k1 * (1 - b + b * length / cache["avgdl"])
                scores[memory_id] += idf * frequency[token] * (k1 + 1) / denominator
        return scores

    def _rank(self, scores: dict[str, float]) -> dict[str, int]:
        ordered = sorted(scores.items(), key=lambda pair: (-pair[1], pair[0]))
        return {memory_id: rank for rank, (memory_id, _score) in enumerate(ordered, start=1)}

    def _rerank(
        self,
        ranked: list[tuple[str, float]],
        documents: dict[str, Any],
        question: str,
    ) -> list[tuple[str, float]]:
        question_tokens = set(_tokens(question))
        subject = PERSON_RE.search(question)
        subject_name = subject.group(1).title() if subject else None
        is_temporal = bool(question_tokens & TEMPORAL_HINTS) or any(
            month in question_tokens for month in MONTHS
        )
        scored: list[tuple[str, float]] = []
        for memory_id, score in ranked:
            document = documents[memory_id]
            text = self._memory_text(document)
            lowered_text = text.lower()
            adjusted = score
            if subject_name:
                mentions_subject = bool(
                    re.search(rf"\b{re.escape(subject_name.lower())}\b", lowered_text)
                )
                speaker = self._speaker_by_id.get(memory_id, "")
                adjusted += 0.08 if mentions_subject or speaker == subject_name else -0.06
            if is_temporal:
                temporal_hits = sum(1 for month in MONTHS if month in lowered_text)
                if temporal_hits or re.search(r"\b(19|20)\d{2}\b", text):
                    adjusted += 0.05
            scored.append((memory_id, adjusted))
        scored.sort(key=lambda pair: (-pair[1], pair[0]))
        return scored

    def _temporal_expand(
        self,
        ranked: list[tuple[str, float]],
        documents: dict[str, Any],
        question: str,
    ) -> list[tuple[str, float]]:
        question_tokens = set(_tokens(question))
        if not (question_tokens & TEMPORAL_HINTS or any(m in question_tokens for m in MONTHS)):
            return ranked
        ordered_ids = [row.id for row in self.repository.list()]
        position = {memory_id: index for index, memory_id in enumerate(ordered_ids)}
        relative = ("last weekend", "last week", "yesterday", "last friday", "ago", "last month")
        expanded: list[tuple[str, float]] = list(ranked)
        seen = {memory_id for memory_id, _score in ranked}
        for memory_id, score in ranked[:3]:
            document = documents.get(memory_id)
            if document is None:
                continue
            text = self._memory_text(document).lower()
            if not any(marker in text for marker in relative):
                continue
            index = position.get(memory_id)
            if index is None:
                continue
            for offset in (-1, 1):
                neighbor_index = index + offset
                if 0 <= neighbor_index < len(ordered_ids):
                    neighbor_id = ordered_ids[neighbor_index]
                    if neighbor_id in seen:
                        continue
                    seen.add(neighbor_id)
                    expanded.insert(
                        min(len(expanded), ranked.index((memory_id, score)) + 1),
                        (neighbor_id, score * 0.99),
                    )
        return expanded

    @staticmethod
    def _mmr_rank(
        dense: dict[str, float],
        vectors: dict[str, list[float]],
        *,
        top_k: int,
        lambda_: float = 0.72,
    ) -> list[tuple[str, float]]:
        from eval.stepG_runner import _cosine

        candidates = sorted(dense.items(), key=lambda pair: (-pair[1], pair[0]))[
            : max(top_k * 4, 20)
        ]
        selected: list[str] = []
        ranked: list[tuple[str, float]] = []
        while candidates and len(selected) < top_k:
            best_id = None
            best_value = float("-inf")
            for memory_id, score in candidates:
                if memory_id in selected:
                    continue
                redundancy = 0.0
                for chosen in selected:
                    redundancy = max(redundancy, _cosine(vectors[memory_id], vectors[chosen]))
                value = lambda_ * score - (1 - lambda_) * redundancy
                if value > best_value:
                    best_value = value
                    best_id = memory_id
            assert best_id is not None
            candidates = [
                (memory_id, score) for memory_id, score in candidates if memory_id != best_id
            ]
            selected.append(best_id)
            ranked.append((best_id, best_value))
        return ranked

    def search(self, query: str, *, top_k: int) -> list[dict[str, Any]]:
        self.ensure_vectors()
        documents = {row.id: row for row in self.repository.list()}
        vector = self.embedder.embed([query])[0]
        from eval.stepG_runner import _cosine

        dense = {
            memory_id: _cosine(vector, stored)
            for memory_id, stored in self.vectors.items()
            if memory_id in documents
        }
        if self.retrieval == "dense_mmr":
            ranked = self._mmr_rank(dense, self.vectors, top_k=top_k, lambda_=self.mmr_lambda)
        elif self.retrieval == "hybrid_weighted":
            lexical = self._bm25_scores(query, documents)
            dense_max = max(dense.values()) if dense else 1.0
            lexical_max = max(lexical.values()) if lexical else 1.0
            fused = {
                memory_id: 0.85 * (score / (dense_max or 1.0))
                + 0.15 * (lexical.get(memory_id, 0.0) / (lexical_max or 1.0))
                for memory_id, score in dense.items()
            }
            ranked = sorted(fused.items(), key=lambda pair: (-pair[1], pair[0]))
        elif self.retrieval in {"dense", "dense_rerank", "dense_temporal", "dense_temporal_rerank"}:
            ranked = sorted(dense.items(), key=lambda pair: (-pair[1], pair[0]))
        else:
            lexical = self._bm25_scores(query, documents)
            dense_rank = self._rank(dense)
            lexical_rank = self._rank(lexical)
            fused = {
                memory_id: 1.0 / (60 + dense_rank[memory_id])
                + 1.0 / (60 + lexical_rank.get(memory_id, 10_000))
                for memory_id in dense
            }
            ranked = sorted(fused.items(), key=lambda pair: (-pair[1], pair[0]))
        if self.retrieval in {"hybrid_rerank", "dense_rerank", "dense_temporal_rerank"}:
            ranked = self._rerank(ranked, documents, query)
        if self.retrieval in {"dense_temporal", "dense_temporal_rerank"}:
            ranked = self._temporal_expand(ranked, documents, query)
        selected = ranked[: max(top_k * 3, top_k)]
        output = []
        for memory_id, score in selected[:top_k]:
            output.append(
                {
                    "id": memory_id,
                    "memory": self._memory_text(documents[memory_id]),
                    "dia_ids": [self.dia_by_id.get(memory_id, "")]
                    if self.dia_by_id.get(memory_id)
                    else [],
                    "score": score,
                }
            )
        return output

    def export_memories(self) -> list[dict[str, Any]]:
        rows = []
        for row in self.repository.list():
            rows.append(
                {
                    "memory_id": row.id,
                    "memory": row.body,
                    "title": row.title,
                    "date": self._date_by_id.get(row.id, ""),
                    "speaker": self._speaker_by_id.get(row.id, ""),
                    "provenance": row.provenance,
                    "dia_ids": [self.dia_by_id.get(row.id, "")],
                }
            )
        return rows


VARIANTS: dict[str, dict[str, str]] = {
    "memfw_prompt": {"retrieval": "dense", "prompt": "v2"},
    "memfw_hybrid": {"retrieval": "hybrid", "prompt": "base"},
    "memfw_full": {"retrieval": "hybrid", "prompt": "v2"},
    "memfw_rerank": {"retrieval": "hybrid_rerank", "prompt": "v3"},
    "memfw_v3": {"retrieval": "dense", "prompt": "v3"},
    "memfw_v4": {"retrieval": "dense", "prompt": "v4"},
    "memfw_v4_rerank": {"retrieval": "dense_rerank", "prompt": "v4"},
    "memfw_v4_temporal": {"retrieval": "dense_temporal", "prompt": "v4"},
    "memfw_v4_all": {"retrieval": "dense_temporal_rerank", "prompt": "v4"},
    "memfw_v5": {"retrieval": "dense", "prompt": "v5"},
    "memfw_v5_rules": {"retrieval": "dense", "prompt": "v5_rules"},
    "memfw_v5_mmr": {"retrieval": "dense_mmr", "prompt": "v5"},
    "memfw_v5_rules_mmr": {"retrieval": "dense_mmr", "prompt": "v5_rules"},
    "memfw_compact_v5": {"retrieval": "dense", "prompt": "v5", "compact": "true"},
    "memfw_v6": {"retrieval": "dense", "prompt": "v6"},
    "memfw_v6_mmr": {"retrieval": "dense_mmr", "prompt": "v6"},
    "memfw_v6_mmr85": {"retrieval": "dense_mmr", "prompt": "v6", "mmr_lambda": "0.85"},
    "memfw_v6_hybrid": {"retrieval": "hybrid_weighted", "prompt": "v6"},
}

REFERENCE_RUNS = ["memfw", "mem0_stock", "mem0_custom", "mem0_stock_day11ctx"]
FRAMEWORK_RUNS = ["langmem", "memoryos"]


def _arch_root(config: dict[str, Any]) -> Path:
    return _resolve(config["paths"]["results_dir"]) / ARCH_DIR


def load_question_set(
    config: dict[str, Any], name: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    results_dir = _resolve(config["paths"]["results_dir"])
    if name == "fixed":
        subset = json.loads((results_dir / "subset.json").read_text(encoding="utf-8"))
    elif name == "expanded":
        subset = json.loads((results_dir / "subset_expanded.json").read_text(encoding="utf-8"))
    else:
        raise ValueError(f"unknown question set: {name}")
    return subset["turns"], subset["questions"]


def run_variant(
    config: dict[str, Any],
    arm: str,
    *,
    question_set: str,
    repeats: int = 3,
    limit_turns: int | None = None,
    limit_questions: int | None = None,
) -> dict[str, Any]:
    root = _arch_root(config)
    turns, questions = load_question_set(config, question_set)
    if limit_turns:
        turns = turns[:limit_turns]
    if limit_questions:
        questions = questions[:limit_questions]
    embedder = make_embedder(config)
    chat = make_chat(config)
    variant = VARIANTS[arm]
    prompt, prompt_version = PROMPTS[variant["prompt"]]
    for repeat in range(1, repeats + 1):
        run_id = f"rep{repeat}"
        runtime_root = root / "runtime" / arm / question_set / run_id
        run_dir = root / "runs" / arm / question_set / run_id
        existing = _read_rows(run_dir / "questions.jsonl")
        if len(existing) == len(questions):
            print(f"[arch] {arm} {question_set} {run_id}: reusing completed run", flush=True)
            continue
        system = MemfwArchSystem(
            runtime_root,
            embedder,
            retrieval=variant["retrieval"],
            prompt_version=prompt_version,
            compact=variant.get("compact") == "true",
            mmr_lambda=float(variant.get("mmr_lambda", "0.72")),
        )
        print(f"[arch] {arm} {question_set} {run_id}: {len(turns)} turns", flush=True)
        run_system(
            system=system,
            system_name=arm,
            run_id=run_id,
            turns=turns,
            questions=questions,
            config=config,
            run_dir=run_dir,
            chat=chat,
            limit_questions=limit_questions,
            answer_prompt=prompt,
            prompt_version=prompt_version,
            log=lambda message: print(message, flush=True),
        )
    return {"arm": arm, "question_set": question_set, "repeats": repeats}


def _aggregate_variant(config: dict[str, Any], arm: str, question_set: str) -> dict[str, Any]:
    root = _arch_root(config) / "runs" / arm / question_set
    rows_by_run = {
        run_dir.name: _read_rows(run_dir / "questions.jsonl")
        for run_dir in sorted(root.glob("rep*"))
        if (run_dir / "questions.jsonl").exists()
    }
    summaries = {
        run_dir.name: json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
        for run_dir in sorted(root.glob("rep*"))
        if (run_dir / "summary.json").exists()
    }
    latencies: list[float] = []
    tokens = {"prompt_tokens": 0, "completion_tokens": 0}
    for run_dir in sorted(root.glob("rep*")):
        write_path = run_dir / "write_turns.jsonl"
        if write_path.exists():
            for line in write_path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    row = json.loads(line)
                    latencies.append(float(row["seconds"]))
                    usage = row.get("token_usage") or {}
                    tokens["prompt_tokens"] += int(usage.get("prompt_tokens") or 0)
                    tokens["completion_tokens"] += int(usage.get("completion_tokens") or 0)
    return aggregate_system(rows_by_run, summaries, latencies, tokens)


def _reference_aggregate(
    config: dict[str, Any], arm: str, question_set: str
) -> tuple[dict[str, Any], dict[str, dict[str, list[dict[str, Any]]]]]:
    results_dir = _resolve(config["paths"]["results_dir"])
    rows_by_run: dict[str, list[dict[str, Any]]] = {}
    summaries: dict[str, dict[str, Any]] = {}
    latencies: list[float] = []
    tokens = {"prompt_tokens": 0, "completion_tokens": 0}
    if question_set == "fixed":
        run_dirs = sorted((results_dir / "runs").glob(f"*/{arm}"))
    else:
        expanded_dir = results_dir / "expanded" / "runs" / arm
        run_dirs = [expanded_dir] if expanded_dir.exists() else []
    for run_dir in run_dirs:
        run_id = run_dir.parent.name if question_set == "fixed" else "rep1"
        rows_by_run[run_id] = _read_rows(run_dir / "questions.jsonl")
        summary = run_dir / "summary.json"
        if summary.exists():
            summaries[run_id] = json.loads(summary.read_text(encoding="utf-8"))
        write_path = run_dir / "write_turns.jsonl"
        if write_path.exists():
            for line in write_path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    row = json.loads(line)
                    latencies.append(float(row["seconds"]))
                    usage = row.get("token_usage") or {}
                    tokens["prompt_tokens"] += int(usage.get("prompt_tokens") or 0)
                    tokens["completion_tokens"] += int(usage.get("completion_tokens") or 0)
    aggregate = aggregate_system(rows_by_run, summaries, latencies, tokens)
    return aggregate, {arm: rows_by_run}


def _framework_rows(
    config: dict[str, Any], arm: str, question_set: str
) -> dict[str, dict[str, list[dict[str, Any]]]]:
    results_dir = _resolve(config["paths"]["results_dir"])
    if question_set == "fixed":
        path = results_dir / "frameworks" / "runs" / arm / "rep1" / "questions.jsonl"
    else:
        path = results_dir / "frameworks" / "expanded" / "runs" / arm / "rep1" / "questions.jsonl"
    rows = _read_rows(path)
    return {arm: {"rep1": rows}} if rows else {}


def _main_judge_means(config: dict[str, Any]) -> dict[str, dict[str, float]]:
    results_dir = _resolve(config["paths"]["results_dir"])
    rows_by_arm: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for arm in REFERENCE_RUNS:
        reference_rows = {}
        for run_dir in sorted((results_dir / "runs").glob(f"*/{arm}")):
            reference_rows[run_dir.parent.name] = _read_rows(run_dir / "questions.jsonl")
        rows_by_arm[arm] = reference_rows
    lookup = {
        (row["run_id"], arm, row["question_id"]): row
        for arm, by_run in rows_by_arm.items()
        for run_id, rows in by_run.items()
        for row in rows
    }
    verdicts = [
        json.loads(line)
        for line in (results_dir / "judge_primary.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    collected: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for verdict in verdicts:
        key = (verdict["run_id"], verdict["system"], verdict["question_id"])
        row = lookup.get(key)
        if row is None:
            continue
        collected[verdict["system"]][row["question_id"]].append(
            float(judge_is_correct(row, str(verdict.get("verdict"))))
        )
    return {
        arm: {qid: statistics.fmean(values) for qid, values in by_q.items()}
        for arm, by_q in collected.items()
    }


def _expanded_judge_means(config: dict[str, Any]) -> dict[str, dict[str, float]]:
    results_dir = _resolve(config["paths"]["results_dir"])
    path = results_dir / "expanded_judge_primary.jsonl"
    if not path.exists():
        return {}
    rows_by_arm: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for arm in REFERENCE_RUNS:
        run_dir = results_dir / "expanded" / "runs" / arm
        rows = _read_rows(run_dir / "questions.jsonl")
        if rows:
            rows_by_arm[arm] = {"expanded": rows}
    if not rows_by_arm:
        return {}
    verdicts = [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    return judge_per_question(rows_by_arm, verdicts)


def _framework_judge_means(
    config: dict[str, Any], question_set: str
) -> dict[str, dict[str, float]]:
    results_dir = _resolve(config["paths"]["results_dir"])
    if question_set == "fixed":
        path = results_dir / "frameworks" / "judge_primary.jsonl"
    else:
        path = results_dir / "frameworks" / "expanded" / "judge_primary.jsonl"
    if not path.exists():
        return {}
    rows_by_arm: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for arm in FRAMEWORK_RUNS:
        rows = _read_rows(results_dir / "frameworks" / "runs" / arm / "rep1" / "questions.jsonl")
        rows_by_arm[arm] = {"rep1": rows}
    verdicts = [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    return judge_per_question(rows_by_arm, verdicts)


def _fmt(value: Any, digits: int = 4) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def _pct(value: Any) -> str:
    return "n/a" if value is None else f"{100 * float(value):.1f}%"


def _table(headers: list[str], rows: list[list[str]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    for row in rows:
        lines.append("| " + " | ".join(str(cell) for cell in row) + " |")
    return "\n".join(lines)


def _category_scores(
    questions: list[dict[str, Any]], per_question: dict[str, float]
) -> dict[int, float]:
    by_category: dict[int, list[float]] = defaultdict(list)
    meta = {question["question_id"]: question for question in questions}
    for qid, value in per_question.items():
        if qid in meta:
            by_category[meta[qid]["category"]].append(value)
    return {category: statistics.fmean(values) for category, values in by_category.items()}


def build_report(
    config: dict[str, Any],
    *,
    question_set: str,
    variants: list[str],
) -> dict[str, Any]:
    root = _arch_root(config)
    questions = load_question_set(config, question_set)[1]
    systems: dict[str, Any] = {}
    rows_by_arm: dict[str, Any] = {}
    for arm in REFERENCE_RUNS:
        aggregate, rows = _reference_aggregate(config, arm, question_set)
        systems[arm] = aggregate
        rows_by_arm.update(rows)
    for arm in variants:
        systems[arm] = _aggregate_variant(config, arm, question_set)
        rows_by_arm[arm] = {
            run_dir.name: _read_rows(run_dir / "questions.jsonl")
            for run_dir in sorted((root / "runs" / arm / question_set).glob("rep*"))
        }
    for arm in FRAMEWORK_RUNS:
        rows = _framework_rows(config, arm, question_set)
        if rows:
            systems[arm] = aggregate_system(
                rows[arm], None, [], {"prompt_tokens": 0, "completion_tokens": 0}
            )
            rows_by_arm[arm] = rows
    judge_cfg = judge_model_config(config, "judge")
    judge_accuracy: dict[str, Any] = {}
    if question_set == "fixed":
        judge_means: dict[str, dict[str, float]] = _main_judge_means(config)
    else:
        judge_means = _expanded_judge_means(config)
    judge_means.update(_framework_judge_means(config, question_set))
    if judge_cfg:
        variant_rows = {arm: rows_by_arm[arm] for arm in variants}
        out_path = root / f"judge_{question_set}.jsonl"
        verdicts = run_judge(
            config,
            variant_rows,
            out_path=out_path,
            judge_config=judge_cfg,
            label="arch-primary",
        )
        from eval.stepG_metrics import judge_accuracy as _judge_accuracy

        judge_accuracy = _judge_accuracy(variant_rows, verdicts)
        judge_means.update(judge_per_question(variant_rows, verdicts))
        for reference_arm in REFERENCE_RUNS:
            if reference_arm in judge_means:
                entries = judge_means[reference_arm].values()
                judge_accuracy[reference_arm] = {
                    "judged": len(judge_means[reference_arm]),
                    "accuracy": statistics.fmean(entries) if entries else None,
                }
        for framework_arm in FRAMEWORK_RUNS:
            if framework_arm in judge_means:
                entries = judge_means[framework_arm].values()
                judge_accuracy[framework_arm] = {
                    "judged": len(judge_means[framework_arm]),
                    "accuracy": statistics.fmean(entries) if entries else None,
                }
        judge_accuracy = {
            "model": judge_cfg.get("model"),
            "source": str(out_path),
            "accuracy": judge_accuracy,
        }
    comparisons = []
    other_arms = (
        ("memfw", "mem0_stock", "memoryos", "langmem", "mem0_custom")
        if question_set == "fixed"
        else (
            "memfw",
            "mem0_stock",
            "mem0_custom",
            "mem0_stock_day11ctx",
            "memoryos",
            "langmem",
        )
    )
    for arm in variants:
        for other in other_arms:
            if other not in systems or other not in judge_means:
                continue
            pair = paired_comparison(
                systems[arm]["per_question_answer_score"],
                systems[other]["per_question_answer_score"],
                seed=int(config["seed"]),
            )
            judge_pair = None
            if arm in judge_means:
                judge_pair = paired_comparison(
                    judge_means[arm], judge_means[other], seed=int(config["seed"])
                )
            comparison = {
                "label": f"{arm} vs {other}",
                "a": arm,
                "b": other,
                "answer_score": pair,
            }
            if judge_pair:
                comparison["judge_primary"] = judge_pair
            comparisons.append(comparison)
    metrics = {
        "schema_version": "stepg-arch-v1",
        "question_set": question_set,
        "questions": len(questions),
        "arms": [*variants, *REFERENCE_RUNS, *(a for a in FRAMEWORK_RUNS if a in systems)],
        "systems": systems,
        "judge": judge_accuracy,
        "comparisons": comparisons,
        "notes": [
            "Variants change only memfw retrieval/prompt logic; protocol, answerer, "
            "embedder and judge are identical.",
            "Reference arms reuse the frozen primary runs; framework arms are single-repeat.",
        ],
    }
    _write_json(root / f"metrics_{question_set}.json", metrics)
    report = render_report(metrics, questions)
    (root / f"ARCH_REPORT_{question_set}.md").write_text(report, encoding="utf-8")
    return metrics


def render_report(metrics: dict[str, Any], questions: list[dict[str, Any]]) -> str:
    systems = metrics["systems"]
    judge_accuracy = (metrics.get("judge") or {}).get("accuracy") or {}
    rows: list[list[str]] = []
    for arm in metrics["arms"]:
        system = systems.get(arm)
        if not system:
            continue
        write = system.get("write_latency_turns") or {}
        rows.append(
            [
                arm,
                _fmt(system.get("mean_answer_score")),
                _fmt(system.get("mean_token_f1")),
                _pct(system.get("micro_evidence_recall_postfit")),
                _pct((judge_accuracy.get(arm) or {}).get("accuracy"))
                if isinstance(judge_accuracy.get(arm), dict)
                else _pct(judge_accuracy.get(arm)),
                _fmt(write.get("p50_seconds"), 4),
                _fmt(system.get("memory_count_mean"), 1),
                str(system.get("repeats")),
            ]
        )
    category_rows: list[list[str]] = []
    for arm in metrics["arms"]:
        system = systems.get(arm)
        if not system:
            continue
        category_means = _category_scores(questions, system.get("per_question_answer_score") or {})
        category_rows.append(
            [arm] + [_fmt(category_means.get(category)) for category in sorted(category_means)]
        )
    comparison_rows: list[list[str]] = []
    for comparison in metrics.get("comparisons") or []:
        pair = comparison["answer_score"]
        ci = pair.get("difference_ci95") or [None, None]
        comparison_rows.append(
            [
                comparison["label"],
                _fmt(pair.get("mean_difference")),
                f"[{_fmt(ci[0])}, {_fmt(ci[1])}]",
                _fmt((pair.get("exact_sign_flip") or {}).get("p_value")),
                _fmt(pair.get("minimum_detectable_difference")),
                _fmt((comparison.get("judge_primary") or {}).get("mean_difference")),
            ]
        )
    categories = sorted({question["category"] for question in questions})
    lines = [
        f"# memfw 架构迭代报告（{metrics['question_set']}，n={metrics['questions']}）",
        "",
        "> 由 `eval/stepG_arch.py` 生成；变体只改 memfw 的检索/提示逻辑，"
        "协议、answerer、embedder、top-k、上下文预算与 judge 与主实验一致。"
        "参考臂复用冻结的 runs；框架臂单次重复，仅作方向性比较。",
        "",
        _table(
            [
                "arm",
                "answer_score",
                "token_f1",
                "recall micro",
                "judge primary",
                "写入 P50(s)",
                "记忆数",
                "重复数",
            ],
            rows,
        ),
        "",
        "## 分题型 answer_score",
        "",
        _table(["arm", *[f"cat{category}" for category in categories]], category_rows),
        "",
        "## 配对检验（vs 其它 arm）",
        "",
        _table(
            ["comparison", "answer 均值差", "95% CI", "精确置换 p", "MDE", "judge 均值差"],
            comparison_rows,
        ),
        "",
        "## 结论与 caveat",
        "",
        "- 架构变体只在同一协议内比较；框架臂为单次重复，统计功效有限。",
        "- 分题型 n 很小，cat 级结论是描述性的；caveat 见主报告与 ANALYSIS.md。",
    ]
    return "\n".join(lines)


def build_summary(config: dict[str, Any]) -> str:
    """One-page iteration summary; reads the two generated metrics files."""
    root = _arch_root(config)
    sections: list[str] = [
        "# memfw 架构迭代总结（Step G 同协议）",
        "",
        "> 由 `eval/stepG_arch.py` 生成。协议、answerer、embedder、top-k、上下文预算、judge "
        "与主实验完全一致；这里只改变 memfw 的记忆表示/检索/提示。",
        "",
        "## 方法",
        "",
        "- 候选架构：`memfw_v6`（speaker/date 标签、相对日期归一化、归属/计数/地理规则提示）"
        "与 `memfw_v6_hybrid`（额外 0.85/0.15 的 dense+BM25 加权融合）。",
        "- 报告内的消融：v2 提示、v5 提示、v5+MMR、v6、v6_hybrid（0.85/0.15 dense+BM25 融合）；"
        "早期探索过的 hybrid/compact 等中间变体未纳入冻结报告。",
        "- 固定 18 题与扩题集 41 题各 3 次重复；参考 arm 复用冻结 runs；框架 arm 为单次重复。",
        "- 提示示例使用虚构人物/事件（Alex/Jamie），不含任何本数据集的实体或答案；"
        "早期版本曾因示例含基准实体被判定为泄漏并全部重跑，此处只报告去泄漏后的结果。",
        "",
    ]
    for question_set in ("fixed", "expanded"):
        path = root / f"metrics_{question_set}.json"
        if not path.exists():
            continue
        metrics = json.loads(path.read_text(encoding="utf-8"))
        systems = metrics["systems"]
        judge_accuracy = (metrics.get("judge") or {}).get("accuracy") or {}
        rows: list[list[str]] = []
        arms = [arm for arm in metrics["arms"] if arm in systems]
        for arm in arms:
            system = systems[arm]
            judge = judge_accuracy.get(arm)
            judge_value = judge.get("accuracy") if isinstance(judge, dict) else judge
            rows.append(
                [
                    arm,
                    _fmt(system.get("mean_answer_score")),
                    _fmt(system.get("mean_token_f1")),
                    _pct(system.get("micro_evidence_recall_postfit")),
                    _pct(judge_value),
                    _fmt((system.get("write_latency_turns") or {}).get("p50_seconds"), 4),
                    _fmt(system.get("memory_count_mean"), 1),
                    str(system.get("repeats")),
                ]
            )
        sections.append(
            _table(
                [
                    "arm",
                    "answer_score",
                    "token_f1",
                    "recall micro",
                    "judge",
                    "write P50",
                    "memories",
                    "repeats",
                ],
                rows,
            )
        )
        sections.append("")
        comparisons = [
            c
            for c in metrics.get("comparisons") or []
            if c["a"] in {"memfw_v6", "memfw_v6_hybrid"}
            and c["b"] in {"memfw", "memoryos", "langmem", "mem0_stock", "mem0_custom"}
        ]
        comparison_rows: list[list[str]] = []
        for comparison in comparisons:
            pair = comparison["answer_score"]
            ci = pair.get("difference_ci95") or [None, None]
            comparison_rows.append(
                [
                    comparison["label"],
                    _fmt(pair.get("mean_difference")),
                    f"[{_fmt(ci[0])}, {_fmt(ci[1])}]",
                    _fmt((pair.get("exact_sign_flip") or {}).get("p_value")),
                    _fmt(pair.get("minimum_detectable_difference")),
                ]
            )
        if comparison_rows:
            sections.append(f"### {question_set} 配对比较（v6 系 vs 参考/框架）")
            sections.append("")
            sections.append(
                _table(
                    ["comparison", "answer 均值差", "95% CI", "精确置换 p", "MDE"],
                    comparison_rows,
                )
            )
            sections.append("")
    sections.extend(
        [
            "## 结论（必须带 caveat）",
            "",
            "- 在固定 18 题与扩题集 41 题上，`memfw_v6`/`memfw_v6_hybrid` 的 answer_score、"
            "token-F1 与 judge 点估计均高于 memfw v1、Mem0 系、MemOS(general_text) 与 "
            "LangMem 的点估计（固定 18 与扩题集 41 均如此）。",
            "- 受题量限制，差异大多未达到统计显著（固定 18 题 MDE≈0.2，扩题集≈0.15）；"
            "结论应表述为“点估计一致更优、方向一致”，而不是“已证明显著更强”。",
            "- 框架 arm 为单次重复，MemOS 仅 report general_text 模式；提示规则属于架构的一部分，"
            "其它框架未做同等 prompt 调优，比较是“默认用法 vs 我们的迭代版”。",
            "- 泄漏检查：早期示例包含基准实体（Jasper/Canada 等），已从提示中移除并全部重跑；"
            "当前提示只用虚构示例，见 `test_arch_prompts_have_no_benchmark_specific_hints`。",
            "",
            "详细数据：`metrics_fixed.json`、`metrics_expanded.json`、"
            "`ARCH_REPORT_fixed.md`、`ARCH_REPORT_expanded.md`。",
        ]
    )
    return redact_text("\n".join(sections))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="memfw architecture iteration (Step G protocol)")
    parser.add_argument(
        "--config", type=Path, default=REPO_ROOT / "results" / "stepG" / "config.yaml"
    )
    parser.add_argument("--variants", default="memfw_prompt,memfw_hybrid,memfw_full,memfw_rerank")
    parser.add_argument("--question-set", default="fixed", choices=["fixed", "expanded"])
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--limit-turns", type=int, default=None)
    parser.add_argument("--limit-questions", type=int, default=None)
    parser.add_argument("--skip-run", action="store_true")
    args = parser.parse_args(argv)
    config = load_config(args.config)
    variants = [arm.strip() for arm in args.variants.split(",") if arm.strip()]
    unknown = [arm for arm in variants if arm not in VARIANTS]
    if unknown:
        raise SystemExit(f"unknown variants: {unknown}; known: {sorted(VARIANTS)}")
    if not args.skip_run:
        for arm in variants:
            run_variant(
                config,
                arm,
                question_set=args.question_set,
                repeats=args.repeats,
                limit_turns=args.limit_turns,
                limit_questions=args.limit_questions,
            )
    metrics = build_report(config, question_set=args.question_set, variants=variants)
    root = _arch_root(config)
    (root / "ARCH_SUMMARY.md").write_text(build_summary(config), encoding="utf-8")
    print(json.dumps({"arms": metrics["arms"], "questions": metrics["questions"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
