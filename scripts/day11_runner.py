"""Real local runner for the Day 11 memfw vs Mem0 comparison on public LoCoMo.

All model calls go to a loopback Ollama endpoint. Nothing here reads private
memfw-data vaults; the only input is the public LoCoMo JSON named in the manifest.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import random
import re
import statistics
import time
import traceback
import urllib.request
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

import eval_locomo
from day7_bootstrap import paired_stratified_bootstrap

from memfw.core.repository import Repository
from memfw.curation.engine import enforce_budget
from memfw.policies import PolicyName
from memfw.stats.keyness import tokenize

RETENTION_RATIOS = (1.0, 0.05)
MEMFW_POLICY = "salience"
SYSTEMS = ("mem0", "memfw")
AUDIT_SAMPLE_SIZE = 30
REQUEST_TIMEOUT_SECONDS = 180.0
NUM_CTX = 8192
BOOTSTRAP_REPEATS = 2000
CATEGORY_LABELS = {
    "1": "multi_hop",
    "2": "temporal",
    "3": "open_domain",
    "4": "single_hop",
    "5": "adversarial",
}
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
ANSWER_PROMPT = (
    "Answer the question using only the memories below. Reply with a short phrase. "
    "If the memories do not contain the answer, reply exactly: unknown.\n\n"
    "Memories:\n{memories}\n\nQuestion: {question}\nAnswer:"
)
DERIVED_LABEL_KEYS = ("derived", "inferred", "is_inference", "source_type", "provenance_kind")


class Embedder(Protocol):
    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class AnswerModel(Protocol):
    def answer(self, question: str, contexts: Sequence[str]) -> str: ...


class Mem0Like(Protocol):
    def add(self, content: str, *, dia_id: str) -> list[dict[str, Any]]: ...

    def search(self, query: str, *, top_k: int) -> list[dict[str, Any]]: ...

    def get_all(self) -> list[dict[str, Any]]: ...

    def delete(self, memory_id: str) -> None: ...

    def storage_bytes(self) -> int: ...


def require_loopback(url: str) -> str:
    host = urlparse(url).hostname
    if host not in LOOPBACK_HOSTS:
        raise ValueError(f"only loopback model endpoints are allowed, got {host!r}")
    return url.rstrip("/")


def _post_json(url: str, payload: dict[str, Any], timeout: float | None = None) -> Any:
    last: Exception | None = None
    for _attempt in range(3):
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(  # noqa: S310
                request, timeout=timeout or REQUEST_TIMEOUT_SECONDS
            ) as response:
                return json.loads(response.read().decode("utf-8"))
        except (TimeoutError, OSError) as error:
            last = error
    assert last is not None
    raise last


def ollama_digests(base_url: str, models: Sequence[str]) -> dict[str, str | None]:
    base = require_loopback(base_url)
    with urllib.request.urlopen(f"{base}/api/tags", timeout=30) as response:  # noqa: S310
        tags = json.loads(response.read().decode("utf-8"))
    found = {row.get("name"): row.get("digest") for row in tags.get("models", [])}
    return {model: found.get(model) or found.get(f"{model}:latest") for model in models}


class OllamaEmbedder:
    def __init__(self, base_url: str, model: str) -> None:
        self.base = require_loopback(base_url)
        self.model = model

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        result = _post_json(f"{self.base}/api/embed", {"model": self.model, "input": list(texts)})
        return [list(map(float, row)) for row in result["embeddings"]]


class OllamaAnswerModel:
    def __init__(self, base_url: str, model: str) -> None:
        self.base = require_loopback(base_url)
        self.model = model

    def answer(self, question: str, contexts: Sequence[str]) -> str:
        memories = "\n".join(f"- {text}" for text in contexts) or "- (none)"
        result = _post_json(
            f"{self.base}/api/chat",
            {
                "model": self.model,
                "messages": [
                    {
                        "role": "user",
                        "content": ANSWER_PROMPT.format(memories=memories, question=question),
                    }
                ],
                "stream": False,
                "think": False,
                "options": {
                    "temperature": 0.0,
                    "num_predict": 64,
                    "seed": 20261021,
                    "num_ctx": NUM_CTX,
                },
            },
        )
        return str(result["message"]["content"]).strip()


def mem0_config(
    root: Path, base_url: str, answer_model: str, embedding_model: str
) -> dict[str, Any]:
    base = require_loopback(base_url)
    return {
        "llm": {
            "provider": "ollama",
            "config": {"model": answer_model, "temperature": 0.0, "ollama_base_url": base},
        },
        "embedder": {
            "provider": "ollama",
            "config": {"model": embedding_model, "ollama_base_url": base, "embedding_dims": 768},
        },
        "vector_store": {
            "provider": "qdrant",
            "config": {
                "collection_name": "day11_locomo",
                "path": str(root / "qdrant"),
                "embedding_model_dims": 768,
                "on_disk": True,
            },
        },
        "history_db_path": str(root / "history.db"),
    }


def directory_bytes(root: Path) -> int:
    return sum(path.stat().st_size for path in root.rglob("*") if path.is_file())


class Mem0Adapter:
    """Thin wrapper over open-source mem0.Memory with a fully local stack."""

    user_id = "locomo-public"

    def __init__(self, root: Path, base_url: str, answer_model: str, embedding_model: str) -> None:
        os.environ["MEM0_TELEMETRY"] = "False"  # no outbound telemetry
        from mem0 import Memory

        self.root = root
        self.config = mem0_config(root, base_url, answer_model, embedding_model)
        self.memory: Any = Memory.from_config(self.config)
        from ollama import Client

        base = require_loopback(base_url)
        # Bounded timeouts so a stalled local request fails loudly instead of hanging.
        self.memory.llm.client = Client(host=base, timeout=REQUEST_TIMEOUT_SECONDS)
        self.memory.embedding_model.client = Client(host=base, timeout=REQUEST_TIMEOUT_SECONDS)
        raw_chat = self.memory.llm.client.chat

        def chat(**params: Any) -> Any:
            # mem0ai 2.2.1 exposes no Ollama "think" option; disable reasoning traces and
            # cap the context window so Mem0 extraction uses the same qwen3.5 settings as
            # the answer step.
            options = dict(params.pop("options", None) or {})
            options["num_ctx"] = NUM_CTX
            return raw_chat(**params, options=options, think=False)

        self.memory.llm.client.chat = chat
        self.retries = 0

    def add(self, content: str, *, dia_id: str) -> list[dict[str, Any]]:
        for attempt in range(2):
            try:
                result = self.memory.add(
                    [{"role": "user", "content": content}],
                    user_id=self.user_id,
                    metadata={"dia_id": dia_id},
                    infer=True,
                )
                return list(result.get("results", []))
            except Exception:
                if attempt == 1:
                    raise
                self.retries += 1
        return []

    def search(self, query: str, *, top_k: int) -> list[dict[str, Any]]:
        result = self.memory.search(
            query, top_k=top_k, filters={"user_id": self.user_id}, threshold=0.0
        )
        return list(result.get("results", []))

    def get_all(self) -> list[dict[str, Any]]:
        result = self.memory.get_all(filters={"user_id": self.user_id}, top_k=100000)
        return list(result.get("results", []))

    def delete(self, memory_id: str) -> None:
        self.memory.delete(memory_id)

    def storage_bytes(self) -> int:
        return directory_bytes(self.root)


def _safe_id(dia_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "-", dia_id)


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    norm = math.sqrt(sum(a * a for a in left)) * math.sqrt(sum(b * b for b in right))
    return dot / norm if norm else 0.0


class MemfwSystem:
    """memfw vault with verbatim writes; retrieval uses the shared embedder for alignment."""

    def __init__(self, root: Path, embedder: Embedder) -> None:
        self.root = root
        self.embedder = embedder
        self.repository: Any = Repository(root / "vault")
        sequence = iter(range(10**9))
        self.repository.id_factory = lambda: f"day11-event-{next(sequence):09d}"
        self.repository.clock = lambda: datetime(2023, 1, 1, tzinfo=UTC)
        self.repository.init()
        self.vectors: dict[str, list[float]] = {}
        self.dia_by_id: dict[str, str] = {}
        self.latest: datetime = datetime(2023, 1, 1, tzinfo=UTC)

    def add(self, turn: dict[str, str]) -> None:
        timestamp = datetime.fromisoformat(turn["created_at"]).astimezone(UTC)
        self.latest = max(self.latest, timestamp)
        self.repository.clock = lambda: timestamp
        memory_id = _safe_id(turn["dia_id"])
        title = f"speaker {turn['speaker']}"
        self.repository.add(
            title,
            turn["text"],
            memory_id=memory_id,
            source="public-locomo",
            provenance="verbatim-public-locomo-turn",
        )
        self.vectors[memory_id] = self.embedder.embed([f"{title} {turn['text']}"])[0]
        self.dia_by_id[memory_id] = turn["dia_id"]

    def documents(self) -> list[Any]:
        return list(self.repository.list())

    def enforce(self, budget_bytes: int) -> dict[str, Any]:
        now = self.latest + timedelta(days=2)
        self.repository.clock = lambda: now
        config = self.repository.read_config()
        config.update(
            {
                "budget_mode": "body",
                "budget_scope": "all",
                "max_bytes": budget_bytes,
                "forget_policy": MEMFW_POLICY,
                "forget_action": "hard",
                "auto_enforce_on_write": False,
            }
        )
        config.pop("max_count", None)
        self.repository.configure(config)
        self.repository.clock = lambda: now
        result = enforce_budget(
            self.repository,
            now=now,
            policy_override=PolicyName(MEMFW_POLICY),
            dry_run=False,
            actor="mechanism",
        )
        pending = list(result.pending_proposal_ids)
        approved = 0
        if pending:
            from memfw.curation.review import apply_approved

            proposals = {
                row.proposal_id: row for row in self.repository.list_proposals(status="pending")
            }
            claimed: set[str] = set()
            chosen: list[str] = []
            for proposal_id in sorted(pending):
                targets = set(proposals[proposal_id].target_ids)
                if not targets & claimed:
                    chosen.append(proposal_id)
                    claimed.update(targets)
            for proposal_id in chosen:
                # Same simulated reviewer as the Day 7/10 evaluations; in real use a human
                # must approve each forgetting proposal.
                self.repository.simulate_proposal_decision(proposal_id, decision="approve")
                apply_approved(
                    self.repository,
                    proposal_id,
                    actor="simulated_approver",
                    enforce_fallback=False,
                )
            approved = len(chosen)
        fallback = False
        if not result.satisfied_after_apply:
            fallback = True
            enforce_budget(
                self.repository,
                now=now,
                policy_override=PolicyName.FIFO,
                dry_run=False,
                actor="mechanism",
            )
        retained = {row.id for row in self.documents()}
        self.vectors = {key: value for key, value in self.vectors.items() if key in retained}
        return {
            "forgetting_proposals_pending": len(pending),
            "forgetting_proposals_approved_by_simulated_reviewer": approved,
            "fifo_fallback_used": fallback,
        }

    def search(self, query: str, *, top_k: int) -> list[dict[str, Any]]:
        vector = self.embedder.embed([query])[0]
        bodies = {row.id: f"{row.title} {row.body}" for row in self.documents()}
        scored = sorted(
            (
                (_cosine(vector, stored), key)
                for key, stored in self.vectors.items()
                if key in bodies
            ),
            key=lambda pair: (-pair[0], pair[1]),
        )
        return [
            {"id": key, "memory": bodies[key], "dia_ids": [self.dia_by_id[key]], "score": score}
            for score, key in scored[:top_k]
        ]

    def storage_bytes(self) -> int:
        # Vault files plus float32 vectors held for the aligned embedding retrieval.
        return directory_bytes(self.repository.root) + 4 * sum(
            len(v) for v in self.vectors.values()
        )


def mem0_dia_ids(item: dict[str, Any]) -> list[str]:
    metadata = item.get("metadata") or {}
    value = metadata.get("dia_id") if isinstance(metadata, dict) else None
    return [value] if isinstance(value, str) else []


def fit_context(items: Sequence[dict[str, Any]], token_budget: int) -> list[dict[str, Any]]:
    """Same greedy whole-item token fit as eval_locomo._fit_budget."""
    selected: list[dict[str, Any]] = []
    used = 0
    for item in items:
        cost = len(tokenize(str(item["memory"])))
        if used + cost <= token_budget:
            selected.append(item)
            used += cost
    return selected


def percentile(values: Sequence[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    low = math.floor(position)
    high = math.ceil(position)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def latency_summary(values: Sequence[float]) -> dict[str, Any]:
    return {
        "count": len(values),
        "p50_seconds": percentile(values, 0.50),
        "p95_seconds": percentile(values, 0.95),
        "mean_seconds": statistics.fmean(values) if values else None,
    }


_ARTICLES = re.compile(r"\b(a|an|the|and)\b")
_PUNCT = re.compile(r"[^\w\s]")


def normalize_answer(text: str) -> list[str]:
    lowered = _PUNCT.sub(" ", text.lower())
    return _ARTICLES.sub(" ", lowered).split()


def token_f1(prediction: str, gold: str) -> float:
    pred = normalize_answer(prediction)
    truth = normalize_answer(gold)
    if not pred or not truth:
        return float(pred == truth)
    common: dict[str, int] = {}
    for token in pred:
        if token in truth:
            common[token] = min(pred.count(token), truth.count(token))
    overlap = sum(common.values())
    if overlap == 0:
        return 0.0
    precision = overlap / len(pred)
    recall = overlap / len(truth)
    return 2 * precision * recall / (precision + recall)


def answer_score(prediction: str, gold: str | None, category: int) -> float:
    """LoCoMo-style: token F1 for categories 1-4; abstention credit for adversarial (5)."""
    if category == 5:
        lowered = prediction.lower()
        return float("unknown" in lowered or "not mentioned" in lowered)
    return token_f1(prediction, gold or "")


def bootstrap_mean_ci(
    values: Sequence[float], *, repeats: int = BOOTSTRAP_REPEATS, seed: int = 20261021
) -> list[float | None]:
    if not values:
        return [None, None]
    rng = random.Random(seed)
    means = sorted(statistics.fmean(rng.choice(values) for _ in values) for _ in range(repeats))
    return [percentile(means, 0.025), percentile(means, 0.975)]


def paired_bootstrap_p(
    diffs: Sequence[float], *, repeats: int = BOOTSTRAP_REPEATS, seed: int = 20261021
) -> float:
    if all(value == 0 for value in diffs):
        return 1.0
    rng = random.Random(seed)
    means = [statistics.fmean(rng.choice(diffs) for _ in diffs) for _ in range(repeats)]
    low = (sum(m <= 0 for m in means) + 1) / (repeats + 1)
    high = (sum(m >= 0 for m in means) + 1) / (repeats + 1)
    return min(1.0, 2 * min(low, high))


def holm(pvalues: dict[str, float]) -> dict[str, float]:
    ordered = sorted(pvalues.items(), key=lambda pair: pair[1])
    adjusted: dict[str, float] = {}
    running = 0.0
    total = len(ordered)
    for rank, (key, value) in enumerate(ordered):
        running = max(running, min(1.0, (total - rank) * value))
        adjusted[key] = running
    return adjusted


def minimum_detectable_difference(diffs: Sequence[float]) -> float | None:
    """Approximate paired MDE at alpha=0.05 two-sided, power 0.8: 2.8 * sd / sqrt(n)."""
    if len(diffs) < 2:
        return None
    return 2.8 * statistics.stdev(diffs) / math.sqrt(len(diffs))


_WORD = re.compile(r"[a-z0-9']+")


def _norm_text(text: str) -> str:
    return " ".join(_WORD.findall(text.lower()))


def audit_memory(
    memory_text: str,
    source_text: str | None,
    speaker: str | None,
    metadata: dict[str, Any],
    attributed_to: str | None,
) -> dict[str, Any]:
    """Rule-based provenance judgement for one stored memory (public data only)."""
    norm_memory = _norm_text(memory_text)
    if speaker:
        norm_memory = re.sub(rf"^(speaker )?{re.escape(speaker.lower())} ", "", norm_memory)
    if source_text is None:
        return {"label": "missing_source_link", "flagged": False, "novel_numbers": False}
    norm_source = _norm_text(source_text)
    verbatim = bool(norm_memory) and norm_memory in norm_source
    source_words = set(norm_source.split()) | set(_norm_text(speaker or "").split())
    memory_words = norm_memory.split()
    novel = [word for word in memory_words if word not in source_words]
    novel_numbers = any(any(ch.isdigit() for ch in word) for word in novel)
    labelled = any(key in metadata for key in DERIVED_LABEL_KEYS)
    if verbatim:
        label = "verbatim_source_text"
    elif labelled:
        label = "derived_labelled"
    elif attributed_to == "user":
        label = "derived_attributed_to_user_unlabelled"
    else:
        label = "derived_unlabelled"
    return {
        "label": label,
        "flagged": label == "derived_attributed_to_user_unlabelled",
        "novel_token_fraction": len(novel) / len(memory_words) if memory_words else 0.0,
        "novel_numbers": novel_numbers,
    }


AUDIT_RULES = [
    "R0: memory without a resolvable source turn id -> missing_source_link.",
    "R1: lower-cased, punctuation-stripped memory text (after removing a leading "
    "speaker prefix) is a contiguous substring of its source turn -> verbatim_source_text.",
    "R2: otherwise derived. If metadata carries an explicit derived/inferred label "
    f"(keys {list(DERIVED_LABEL_KEYS)}) -> derived_labelled.",
    "R3: derived, unlabelled, and the system records it as attributed_to='user' -> "
    "derived_attributed_to_user_unlabelled (FLAGGED: inferred text stored as the user's).",
    "R4: derived, unlabelled, no user attribution -> derived_unlabelled.",
    "Auxiliary: novel_numbers = memory contains digit-bearing tokens absent from the source "
    "turn (e.g., dates resolved by the extractor).",
]


def summarize_audit(judgements: Sequence[dict[str, Any]]) -> dict[str, Any]:
    labels: dict[str, int] = {}
    for row in judgements:
        labels[row["label"]] = labels.get(row["label"], 0) + 1
    return {
        "sample_size": len(judgements),
        "label_counts": dict(sorted(labels.items())),
        "flagged_count": sum(bool(row["flagged"]) for row in judgements),
        "novel_number_count": sum(bool(row["novel_numbers"]) for row in judgements),
    }


def run_comparison(
    *,
    turns: Sequence[dict[str, str]],
    questions: Sequence[dict[str, Any]],
    mem0: Mem0Like,
    memfw: MemfwSystem,
    answerer: AnswerModel,
    top_k: int,
    context_token_budget: int,
    seed: int,
    log: Callable[[str], None] = lambda _message: None,
) -> dict[str, Any]:
    """Run ingest, retention, retrieval, answering and audits; returns raw measurements."""
    turn_by_id = {turn["dia_id"]: turn for turn in turns}
    raw_bytes = sum(len(turn["text"].encode("utf-8")) for turn in turns)
    write_latency: dict[str, list[float]] = {name: [] for name in SYSTEMS}
    mem0_events: dict[str, int] = {}
    mem0_add_errors: list[dict[str, str]] = []
    started = time.perf_counter()
    for index, turn in enumerate(turns):
        content = f"[{turn['created_at'][:10]}] {turn['speaker']}: {turn['text']}"
        t0 = time.perf_counter()
        try:
            events = mem0.add(content, dia_id=turn["dia_id"])
        except Exception as error:  # noqa: BLE001 - recorded honestly, run continues
            events = []
            mem0_add_errors.append({"dia_id": turn["dia_id"], "error": type(error).__name__})
        write_latency["mem0"].append(time.perf_counter() - t0)
        for event in events:
            kind = str(event.get("event", "UNKNOWN"))
            mem0_events[kind] = mem0_events.get(kind, 0) + 1
        t0 = time.perf_counter()
        memfw.add(turn)
        write_latency["memfw"].append(time.perf_counter() - t0)
        if (index + 1) % 10 == 0:
            log(f"ingested {index + 1}/{len(turns)} turns ({time.perf_counter() - started:.0f}s)")
    ingest_seconds = time.perf_counter() - started

    # Provenance audit on the full (100%) stores.
    rng = random.Random(seed)
    mem0_all = mem0.get_all()
    mem0_sample = rng.sample(mem0_all, min(AUDIT_SAMPLE_SIZE, len(mem0_all)))
    audit: dict[str, list[dict[str, Any]]] = {"mem0": [], "memfw": []}
    for item in mem0_sample:
        ids = mem0_dia_ids(item)
        source = turn_by_id.get(ids[0]) if ids else None
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        audit["mem0"].append(
            audit_memory(
                str(item.get("memory", "")),
                source["text"] if source else None,
                source["speaker"] if source else None,
                metadata or {},
                item.get("attributed_to") or item.get("role"),
            )
        )
    documents = memfw.documents()
    for document in rng.sample(documents, min(AUDIT_SAMPLE_SIZE, len(documents))):
        source = turn_by_id.get(memfw.dia_by_id.get(document.id, ""))
        audit["memfw"].append(
            audit_memory(
                str(document.body),
                source["text"] if source else None,
                None,
                {},
                None,
            )
        )

    conditions: dict[str, dict[str, Any]] = {name: {} for name in SYSTEMS}
    per_question: dict[str, list[dict[str, Any]]] = {}
    for ratio in RETENTION_RATIOS:
        key = f"retention_{round(ratio * 100)}pct"
        budget_bytes = max(1, math.floor(raw_bytes * ratio))
        retention_meta: dict[str, Any] = {"mem0": {}, "memfw": {}}
        if ratio < 1.0:
            retention_meta["memfw"] = memfw.enforce(budget_bytes)
            stored = sorted(mem0.get_all(), key=lambda row: str(row.get("created_at", "")))
            used = sum(len(str(row.get("memory", "")).encode("utf-8")) for row in stored)
            evicted = 0
            while stored and used > budget_bytes:
                oldest = stored.pop(0)
                used -= len(str(oldest.get("memory", "")).encode("utf-8"))
                mem0.delete(str(oldest["id"]))
                evicted += 1
            retention_meta["mem0"] = {"fifo_evicted_memories": evicted}
        mem0_retained = mem0.get_all()
        memfw_docs = memfw.documents()
        retained = {
            "mem0": (
                len(mem0_retained),
                sum(len(str(r.get("memory", "")).encode("utf-8")) for r in mem0_retained),
            ),
            "memfw": (len(memfw_docs), sum(len(d.body.encode("utf-8")) for d in memfw_docs)),
        }
        storage = {"mem0": mem0.storage_bytes(), "memfw": memfw.storage_bytes()}
        search_latency: dict[str, list[float]] = {name: [] for name in SYSTEMS}
        rows: list[dict[str, Any]] = []
        for question in questions:
            evidence = set(question["evidence"])
            row: dict[str, Any] = {
                "qa_index": question["qa_index"],
                "category": question["category"],
                "evidence_total": len(evidence),
                "sample_id": "day11",
                "hits": {},
                "answer_score": {},
                "retrieved_turn_ids": {},
            }
            for name in SYSTEMS:
                t0 = time.perf_counter()
                if name == "mem0":
                    found = [
                        {**item, "dia_ids": mem0_dia_ids(item)}
                        for item in mem0.search(question["question"], top_k=top_k)
                    ]
                else:
                    found = memfw.search(question["question"], top_k=top_k)
                search_latency[name].append(time.perf_counter() - t0)
                selected = fit_context(found, context_token_budget)
                retrieved = {dia for item in selected for dia in item["dia_ids"]}
                row["hits"][name] = len(retrieved & evidence)
                row["retrieved_turn_ids"][name] = sorted(retrieved)
                prediction = answerer.answer(
                    question["question"], [str(item["memory"]) for item in selected]
                )
                row["answer_score"][name] = answer_score(
                    prediction, question.get("answer"), int(question["category"])
                )
            rows.append(row)
        per_question[key] = rows
        evidence_total = sum(row["evidence_total"] for row in rows)
        for name in SYSTEMS:
            by_category: dict[str, Any] = {}
            for category in sorted({str(row["category"]) for row in rows}):
                subset = [row for row in rows if str(row["category"]) == category]
                total = sum(row["evidence_total"] for row in subset)
                by_category[category] = {
                    "label": CATEGORY_LABELS.get(category, category),
                    "questions": len(subset),
                    "evidence_recall": sum(r["hits"][name] for r in subset) / total,
                    "answer_score_mean": statistics.fmean(r["answer_score"][name] for r in subset),
                }
            scores = [row["answer_score"][name] for row in rows]
            conditions[name][key] = {
                "retention_ratio": ratio,
                "retention_budget_bytes": budget_bytes,
                "evidence_recall": sum(row["hits"][name] for row in rows) / evidence_total,
                "evidence_retrieved": sum(row["hits"][name] for row in rows),
                "evidence_total": evidence_total,
                "answer_score_mean": statistics.fmean(scores),
                "answer_score_bootstrap_95_ci": bootstrap_mean_ci(scores),
                "by_category": by_category,
                "write_latency": latency_summary(write_latency[name]),
                "search_latency": latency_summary(search_latency[name]),
                "retained_memory_count": retained[name][0],
                "retained_memory_text_bytes": retained[name][1],
                "storage_bytes_on_disk": storage[name],
                "retention_detail": retention_meta[name],
            }
        log(f"finished condition {key}")

    statistics_out: dict[str, Any] = {"recall": {}, "answer_score": {}}
    raw_recall_p: dict[str, float] = {}
    raw_answer_p: dict[str, float] = {}
    for key, rows in per_question.items():
        boot = paired_stratified_bootstrap(
            rows, strategies=["memfw", "mem0"], repeats=BOOTSTRAP_REPEATS, seed=seed
        )
        overall = boot["scopes"]["overall"]
        pair = overall["pairwise"]["memfw__minus__mem0"]
        raw_recall_p[key] = float(pair["two_sided_bootstrap_p_unadjusted"])
        recall_diffs = [
            (row["hits"]["memfw"] - row["hits"]["mem0"]) / row["evidence_total"] for row in rows
        ]
        statistics_out["recall"][key] = {
            "difference_memfw_minus_mem0": pair["difference_in_micro_recall"],
            "paired_bootstrap_95_ci": pair["paired_bootstrap_95_ci"],
            "p_unadjusted": pair["two_sided_bootstrap_p_unadjusted"],
            "system_cis": {
                name: overall["systems"][name]["paired_bootstrap_95_ci"] for name in SYSTEMS
            },
            "approx_minimum_detectable_difference": minimum_detectable_difference(recall_diffs),
        }
        diffs = [row["answer_score"]["memfw"] - row["answer_score"]["mem0"] for row in rows]
        raw_answer_p[key] = paired_bootstrap_p(diffs)
        statistics_out["answer_score"][key] = {
            "difference_memfw_minus_mem0": statistics.fmean(diffs),
            "paired_bootstrap_95_ci": bootstrap_mean_ci(diffs),
            "p_unadjusted": raw_answer_p[key],
            "approx_minimum_detectable_difference": minimum_detectable_difference(diffs),
        }
    for family, raw in (("recall", raw_recall_p), ("answer_score", raw_answer_p)):
        for key, value in holm(raw).items():
            statistics_out[family][key]["p_holm"] = value
            statistics_out[family][key]["significant_at_0_05"] = value < 0.05
    return {
        "ingest_seconds": ingest_seconds,
        "raw_turn_bytes": raw_bytes,
        "mem0_add_events": mem0_events,
        "mem0_add_errors": mem0_add_errors,
        "mem0_add_retries": int(getattr(mem0, "retries", 0)),
        "conditions": conditions,
        "statistics": statistics_out,
        "provenance_audit": {
            "rules": AUDIT_RULES,
            "systems": {name: summarize_audit(audit[name]) for name in SYSTEMS},
        },
        "per_question": per_question,
    }


def load_questions(
    data_path: Path, manifest: dict[str, Any]
) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    """Return the manifest's 100 turns and 18 questions with gold answers attached."""
    samples = json.loads(data_path.read_text(encoding="utf-8"))
    payload = samples[manifest["source"]["sample_index"]]
    turns, questions, _ = eval_locomo.adapt_with_stats(payload)
    window = set(manifest["scope"]["input_turn_ids"])
    input_turns = [turn for turn in turns if turn["dia_id"] in window]
    raw_by_text = {
        str(row.get("question")): row for row in payload.get("qa", []) if isinstance(row, dict)
    }
    selected: list[dict[str, Any]] = []
    for item in manifest["questions"]:
        question = questions[item["qa_index"]]
        text = question["question"]
        if hashlib.sha256(text.encode("utf-8")).hexdigest() != item["question_sha256"]:
            raise ValueError(f"manifest question hash mismatch at qa_index {item['qa_index']}")
        raw = raw_by_text.get(text, {})
        answer = raw.get("answer", raw.get("adversarial_answer"))
        selected.append(
            {
                "qa_index": item["qa_index"],
                "category": item["category"],
                "question": text,
                "evidence": list(item["evidence_turn_ids"]),
                "answer": None if answer is None else str(answer),
            }
        )
    return input_turns, selected


def failure_record(stage: str, error: BaseException) -> dict[str, Any]:
    return {
        "stage": stage,
        "error_type": type(error).__name__,
        "error_message": str(error)[:500],
        "traceback_tail": traceback.format_exc().splitlines()[-5:],
    }


def _fmt(value: Any, digits: int = 3) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def _pct(value: Any) -> str:
    return "n/a" if value is None else f"{100 * float(value):.1f}%"


def _ci(values: Any, percent: bool = True) -> str:
    if not values or values[0] is None:
        return "n/a"
    render = _pct if percent else _fmt
    return f"[{render(values[0])}, {render(values[1])}]"


def build_run_report(
    *,
    manifest: dict[str, Any],
    settings: dict[str, Any],
    environment: dict[str, Any],
    command: str,
    measurements: dict[str, Any] | None,
    failure: dict[str, Any] | None,
    runtime_seconds: float,
) -> dict[str, Any]:
    completed = measurements is not None and failure is None
    return {
        "schema_version": "day11-baseline-report-v2",
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "status": "completed" if completed else "failed",
        "execution_performed": measurements is not None,
        "command": command,
        "runtime_seconds": runtime_seconds,
        "settings": settings,
        "environment": environment,
        "human_confirmation": {
            "memfw": (
                "Explicit add stores the user's verbatim text; staged full-capture candidates "
                "require keep/discard via `memfw capture confirm`. Forgetting can be routed "
                "through approval proposals, but in this benchmark budget enforcement ran as "
                "the mechanism actor and deleted directly (see retention_detail for the "
                "proposal count), so no human step was exercised."
            ),
            "mem0": (
                "add(infer=True) lets the LLM extract, ADD, UPDATE or DELETE memories "
                "automatically; mem0ai 2.2.1 OSS has no built-in human confirmation step."
            ),
        },
        "failure": failure,
        "manifest": manifest,
        "results": measurements,
    }


def render_run_markdown(report: dict[str, Any]) -> str:
    settings = report["settings"]
    env = report["environment"]
    manifest = report["manifest"]
    lines = [
        "# Day 11: memfw vs Mem0 local baseline on public LoCoMo",
        "",
        f"Status: **{report['status']}**. Runtime: {report['runtime_seconds'] / 60:.1f} min.",
        "",
    ]
    if report["failure"]:
        failure = report["failure"]
        lines += [
            "## Failure (recorded as-is; no simulated numbers)",
            "",
            f"- Stage: `{failure['stage']}`",
            f"- Error: `{failure['error_type']}`: {failure['error_message']}",
            "",
        ]
    lines += [
        "## Configuration",
        "",
        f"- Data: public LoCoMo `{manifest['source']['sample_id']}` "
        f"(SHA-256 `{manifest['source']['sha256'][:16]}…`), first "
        f"{manifest['scope']['input_turn_count']} turns, "
        f"{manifest['scope']['selected_question_count']} fixed questions, seed {manifest['seed']}.",
        f"- Shared answer/extraction model: `{settings['answer_model']}` "
        f"(digest `{_fmt(settings['answer_model_digest'])[:16]}`), Ollama loopback, think=false, "
        "temperature 0, "
        f"num_ctx {settings.get('ollama_num_ctx')}.",
        f"- Shared embedder: `{settings['embedding_model']}` "
        f"(digest `{_fmt(settings['embedding_model_digest'])[:16]}`).",
        f"- top-k {settings['top_k']}; context budget {settings['context_token_budget']} tokens "
        "(project tokenizer, greedy whole-item fit, same as eval_locomo).",
        f"- Retention budgets: {settings['retention_levels_percent']}% of raw turn UTF-8 bytes.",
        f"- memfw: verbatim writes, `{MEMFW_POLICY}` forgetting policy (the strongest arm in "
        "Day 10 — chosen after seeing Day 10, a possible selection bias), retrieval = cosine "
        "top-k over the shared embedder.",
        "- Mem0: `mem0ai` "
        f"{env.get('mem0ai_version')}, `Memory.from_config` with Ollama LLM + Ollama embedder "
        "+ on-disk Qdrant, `add(infer=True)` one turn per call, `search(threshold=0.0)`. "
        "Mem0 has no capacity-based forgetting, so the same byte budget is applied by FIFO "
        "deletion of its oldest stored memories (by created_at). Session date is prefixed "
        "to each turn because OSS `add` rejects the timestamp argument.",
        f"- Python {env.get('python_version')}, {env.get('platform')}; MEM0_TELEMETRY=False; "
        "no external API calls.",
        "",
        "Command:",
        "",
        "```sh",
        report["command"],
        "```",
        "",
    ]
    results = report["results"]
    if results:
        lines += [
            "## Results",
            "",
            "Evidence recall is the project's existing metric (share of gold evidence turn ids "
            "whose memories fit in the context budget). Answer score = LoCoMo-style token F1 "
            "(categories 1–4) / abstention credit (category 5) from the shared answer model.",
            "",
            "| System | Retention | Evidence recall [95% CI] | Answer score [95% CI] | "
            "Write p50/p95 (s) | Search p50/p95 (s) | Retained memories | Text bytes | "
            "On-disk bytes |",
            "|---|---:|---|---|---|---|---:|---:|---:|",
        ]
        stats = results["statistics"]
        for key in (f"retention_{round(r * 100)}pct" for r in RETENTION_RATIOS):
            for name in SYSTEMS:
                cell = results["conditions"][name][key]
                ci = stats["recall"][key]["system_cis"][name]
                lines.append(
                    f"| {name} | {round(cell['retention_ratio'] * 100)}% | "
                    f"{_pct(cell['evidence_recall'])} {_ci(ci)} | "
                    f"{_fmt(cell['answer_score_mean'])} "
                    f"{_ci(cell['answer_score_bootstrap_95_ci'], percent=False)} | "
                    f"{_fmt(cell['write_latency']['p50_seconds'], 3)}/"
                    f"{_fmt(cell['write_latency']['p95_seconds'], 3)} | "
                    f"{_fmt(cell['search_latency']['p50_seconds'], 3)}/"
                    f"{_fmt(cell['search_latency']['p95_seconds'], 3)} | "
                    f"{cell['retained_memory_count']} | {cell['retained_memory_text_bytes']} | "
                    f"{cell['storage_bytes_on_disk']} |"
                )
        lines += [
            "",
            "Write latency is per-turn ingest (Mem0: LLM extraction + update decision + "
            "embedding + store; memfw: vault write + one embedding) and is measured once, so it "
            "is the same for both retention rows.",
            "",
            "### Paired comparisons (memfw − Mem0), Holm-corrected across the two budgets",
            "",
            "| Metric | Retention | Difference [95% CI] | p (raw) | p (Holm) | "
            "Approx. MDE | Significant? |",
            "|---|---:|---|---:|---:|---:|---|",
        ]
        for family, label, percent in (
            ("recall", "Evidence recall", True),
            ("answer_score", "Answer score", False),
        ):
            for key, value in stats[family].items():
                render = _pct if percent else _fmt
                lines.append(
                    f"| {label} | {key.split('_')[1]} | "
                    f"{render(value['difference_memfw_minus_mem0'])} "
                    f"{_ci(value['paired_bootstrap_95_ci'], percent=percent)} | "
                    f"{_fmt(value['p_unadjusted'])} | {_fmt(value['p_holm'])} | "
                    f"{render(value['approx_minimum_detectable_difference'])} | "
                    f"{'yes' if value['significant_at_0_05'] else 'no'} |"
                )
        lines += [
            "",
            "With 18 questions from one conversation, statistical power is low: the approximate "
            "minimum detectable difference (paired, α=0.05, power 0.8) is shown per row; "
            "differences smaller than that cannot be distinguished from noise.",
            "",
            "### By category (exploratory, 2–4 questions each)",
            "",
            "| Category | Retention | mem0 recall | memfw recall | mem0 answer | memfw answer |",
            "|---|---:|---:|---:|---:|---:|",
        ]
        for key in (f"retention_{round(r * 100)}pct" for r in RETENTION_RATIOS):
            mem0_cats = results["conditions"]["mem0"][key]["by_category"]
            memfw_cats = results["conditions"]["memfw"][key]["by_category"]
            for category, row in mem0_cats.items():
                other = memfw_cats[category]
                lines.append(
                    f"| {category} {row['label']} (n={row['questions']}) | {key.split('_')[1]} | "
                    f"{_pct(row['evidence_recall'])} | {_pct(other['evidence_recall'])} | "
                    f"{_fmt(row['answer_score_mean'])} | {_fmt(other['answer_score_mean'])} |"
                )
        audit = results["provenance_audit"]
        lines += [
            "",
            "## Provenance audit (rule-based, public data only)",
            "",
            *[f"- {rule}" for rule in audit["rules"]],
            "",
            "| System | Sampled | Label counts | Flagged (R3) | Novel numbers |",
            "|---|---:|---|---:|---:|",
        ]
        for name in SYSTEMS:
            row = audit["systems"][name]
            counts = ", ".join(f"{k}={v}" for k, v in row["label_counts"].items())
            lines.append(
                f"| {name} | {row['sample_size']} | {counts} | {row['flagged_count']} | "
                f"{row['novel_number_count']} |"
            )
        lines += [
            "",
            "Reading this fairly: Mem0 memories are third-person summaries and Mem0 does not "
            "claim they are quotations; R3 counts that they are stored with "
            "`attributed_to=user` and no machine-readable marker separating extracted/inferred "
            "text from what the user actually said. The rule check is automatic, not a human "
            "judgement, and does not measure whether a summary is correct.",
            "",
            f"Mem0 add events: {results['mem0_add_events']}; add errors: "
            f"{len(results['mem0_add_errors'])}; retried adds: "
            f"{results.get('mem0_add_retries', 0)}. Ingest wall time: "
            f"{results['ingest_seconds'] / 60:.1f} min.",
            "",
        ]
    lines += [
        "## Human confirmation (design property)",
        "",
        f"- memfw: {report['human_confirmation']['memfw']}",
        f"- Mem0: {report['human_confirmation']['mem0']}",
        "",
        "## Limitations",
        "",
        "- One public conversation, first 100 turns, 18 questions: very low power; category "
        "rows are anecdotal.",
        "- The question set requires all evidence within the first 100 turns (selection bias).",
        "- memfw's retrieval here is an aligned embedding layer added for this comparison, "
        "not memfw's native FTS/BM25 retrieval; the comparison isolates storage/forgetting "
        "behaviour under a shared retriever.",
        "- Evidence recall for Mem0 credits a memory to the turn whose add() call created it "
        "(metadata dia_id); memories merged by UPDATE keep that single link, which can "
        "under- or over-credit Mem0.",
        "- Mem0 FIFO eviction is our addition (Mem0 has no budgeted forgetting). How many "
        "memories survive a byte budget depends on memory length (Mem0 summaries add resolved "
        "dates and names; see the text-bytes column).",
        "- Mem0's on-disk size does not shrink after deletes (Qdrant segments and the history "
        "database keep tombstones), so on-disk bytes at 5% are not a retained-size measure.",
        "- Category 5 abstention credit rewards answering 'unknown'; an empty or irrelevant "
        "context can therefore score well on adversarial questions.",
        "- A single 9B local model was used for extraction and answering; results with larger "
        "or hosted models (Mem0's default) may differ. Think mode was disabled for speed.",
        "- No human-review step was exercised for either system, so its cost is not measured.",
        "",
        "## Implications for the workshop paper",
        "",
        "- Report this as a small, local, same-model sanity comparison, not a leaderboard "
        "result. Only differences marked significant above may be stated as differences.",
        "- The provenance audit is the most distinctive qualitative contrast: verbatim storage "
        "plus explicit provenance (memfw) vs. LLM-extracted summaries attributed to the user "
        "(Mem0). Frame it as a design trade-off — Mem0's extraction compresses and may "
        "generalise better — not as a defect.",
        "- The 5% condition connects to Day 10: budget pressure is where forgetting policy "
        "matters; a larger, forgetting-sensitive benchmark is needed before any claim.",
        "",
    ]
    return "\n".join(lines)


def execute(
    *,
    data_path: Path,
    manifest: dict[str, Any],
    output_dir: Path,
    base_url: str,
    answer_model: str,
    embedding_model: str,
    top_k: int,
    context_token_budget: int,
    command: str,
    work_dir: Path,
    log: Callable[[str], None] = print,
    mem0_factory: Callable[[Path], Mem0Like] | None = None,
    embedder: Embedder | None = None,
    answerer: AnswerModel | None = None,
    digests: dict[str, str | None] | None = None,
) -> dict[str, Any]:
    import importlib.metadata
    import platform

    started = time.perf_counter()
    stage = "preflight"
    measurements: dict[str, Any] | None = None
    failure: dict[str, Any] | None = None
    try:
        version: str | None = importlib.metadata.version("mem0ai")
    except importlib.metadata.PackageNotFoundError:
        version = None
    environment = {
        "mem0ai_version": version,
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "ollama_url": base_url,
    }
    settings: dict[str, Any] = {
        "answer_model": answer_model,
        "answer_model_digest": None,
        "embedding_model": embedding_model,
        "embedding_model_digest": None,
        "top_k": top_k,
        "context_token_budget": context_token_budget,
        "retention_levels_percent": [round(r * 100) for r in RETENTION_RATIOS],
        "memfw_forgetting_policy": MEMFW_POLICY,
        "mem0_vector_store": "qdrant (local, on-disk)",
        "bootstrap_repeats": BOOTSTRAP_REPEATS,
        "ollama_num_ctx": NUM_CTX,
        "request_timeout_seconds": REQUEST_TIMEOUT_SECONDS,
        "seed": manifest["seed"],
    }
    try:
        require_loopback(base_url)
        found = (
            digests
            if digests is not None
            else ollama_digests(base_url, [answer_model, embedding_model])
        )
        settings["answer_model_digest"] = found.get(answer_model)
        settings["embedding_model_digest"] = found.get(embedding_model)
        stage = "load_data"
        turns, questions = load_questions(data_path, manifest)
        stage = "init_systems"
        work_dir.mkdir(parents=True, exist_ok=True)
        shared_embedder = embedder or OllamaEmbedder(base_url, embedding_model)
        mem0: Mem0Like = (
            mem0_factory(work_dir / "mem0")
            if mem0_factory
            else Mem0Adapter(work_dir / "mem0", base_url, answer_model, embedding_model)
        )
        memfw = MemfwSystem(work_dir / "memfw", shared_embedder)
        stage = "run"
        measurements = run_comparison(
            turns=turns,
            questions=questions,
            mem0=mem0,
            memfw=memfw,
            answerer=answerer or OllamaAnswerModel(base_url, answer_model),
            top_k=top_k,
            context_token_budget=context_token_budget,
            seed=manifest["seed"],
            log=log,
        )
    except Exception as error:  # noqa: BLE001 - failure is written to the report
        failure = failure_record(stage, error)
    report = build_run_report(
        manifest=manifest,
        settings=settings,
        environment=environment,
        command=command,
        measurements=measurements,
        failure=failure,
        runtime_seconds=time.perf_counter() - started,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "day11_baseline_comparison.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (output_dir / "day11_baseline_comparison.md").write_text(
        render_run_markdown(report), encoding="utf-8"
    )
    return report
