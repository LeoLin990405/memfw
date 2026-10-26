"""Step G formal runner: memfw vs Mem0 on a fixed public LoCoMo subset.

This module contains no model-specific assumptions beyond a small transport
abstraction. Two transports are implemented:

* ``ollama``: native ``/api/chat`` and ``/api/embed`` (loopback only).
* ``openai``: any OpenAI-compatible chat endpoint (loopback only), used for
  example with ``mlx_lm.server``.

Everything that is written to disk is deterministic given the input files and
model responses; no private data is read. Only the public LoCoMo corpus named
in ``config.yaml`` is used.
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import math
import os
import re
import statistics
import sys
import time
import urllib.request
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

# Loopback model endpoints must bypass any macOS system proxy picked up by httpx
# (mem0's Ollama client). Keep this before any model client is constructed.
for _proxy_var in ("NO_PROXY", "no_proxy"):
    _existing = os.environ.get(_proxy_var, "")
    _loopback = "127.0.0.1,localhost,::1"
    os.environ[_proxy_var] = f"{_existing},{_loopback}" if _existing else _loopback

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import eval_locomo  # noqa: E402  (repository script, reused for the public LoCoMo adapter)
import yaml  # noqa: E402

from memfw.core.repository import Repository  # noqa: E402
from memfw.stats.keyness import tokenize  # noqa: E402

SCHEMA_VERSION = "stepg-question-v1"
CONFIG_SCHEMA_VERSION = "stepg-config-v1"
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
REQUEST_TIMEOUT_SECONDS = 300.0
ANSWER_PROMPT = (
    "Answer the question using only the memories below. Reply with a short phrase. "
    "If the memories do not contain the answer, reply exactly: unknown.\n\n"
    "Memories:\n{memories}\n\nQuestion: {question}\nAnswer:"
)
ANSWER_UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def require_loopback(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in LOOPBACK_HOSTS:
        raise ValueError(f"model endpoint must be loopback HTTP, got {url!r}")
    return url.rstrip("/")


def _post_json(
    url: str, payload: dict[str, Any], *, timeout: float = REQUEST_TIMEOUT_SECONDS
) -> Any:
    last: Exception | None = None
    for _attempt in range(3):
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
                return json.loads(response.read().decode("utf-8"))
        except (TimeoutError, OSError) as error:  # pragma: no cover - network path
            last = error
            time.sleep(1.0)
    assert last is not None
    raise last


def _get_json(url: str, *, timeout: float = 30.0) -> Any:
    with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310
        return json.loads(response.read().decode("utf-8"))


def percentile(values: Sequence[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    low = math.floor(position)
    high = math.ceil(position)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def latency_summary(values: Sequence[float]) -> dict[str, Any]:
    if not values:
        return {"count": 0, "mean_seconds": None, "p50_seconds": None, "p95_seconds": None}
    return {
        "count": len(values),
        "mean_seconds": statistics.fmean(values),
        "p50_seconds": percentile(values, 0.50),
        "p95_seconds": percentile(values, 0.95),
        "total_seconds": math.fsum(values),
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
    overlap = 0
    remaining = list(truth)
    for token in pred:
        if token in remaining:
            remaining.remove(token)
            overlap += 1
    if overlap == 0:
        return 0.0
    precision = overlap / len(pred)
    recall = overlap / len(truth)
    return 2 * precision * recall / (precision + recall)


def answer_score(prediction: str, gold: str | None, category: int) -> float:
    """LoCoMo-style: token F1 for categories 1-4, abstention credit for category 5."""
    if category == 5:
        lowered = prediction.lower()
        return float("unknown" in lowered or "not mentioned" in lowered)
    return token_f1(prediction, gold or "")


def fit_context(items: Sequence[dict[str, Any]], token_budget: int) -> list[dict[str, Any]]:
    """Greedy whole-item token fit; mirrors the repository's eval_locomo helper."""
    selected: list[dict[str, Any]] = []
    used = 0
    for item in items:
        cost = len(tokenize(str(item["memory"])))
        if used + cost <= token_budget:
            selected.append(item)
            used += cost
    return selected


def directory_bytes(root: Path) -> int:
    return sum(path.stat().st_size for path in root.rglob("*") if path.is_file())


# ---------------------------------------------------------------------------
# transports
# ---------------------------------------------------------------------------


class ChatModel(Protocol):
    def chat(self, prompt: str, *, max_tokens: int | None = None) -> tuple[str, dict[str, int]]: ...


class Embedder(Protocol):
    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class OllamaChat:
    def __init__(
        self, base_url: str, model: str, *, temperature: float, seed: int, num_ctx: int
    ) -> None:
        self.base = require_loopback(base_url)
        self.model = model
        self.temperature = temperature
        self.seed = seed
        self.num_ctx = num_ctx

    def chat(self, prompt: str, *, max_tokens: int | None = None) -> tuple[str, dict[str, int]]:
        result = _post_json(
            f"{self.base}/api/chat",
            {
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
                "think": False,
                "options": {
                    "temperature": self.temperature,
                    "seed": self.seed,
                    "num_ctx": self.num_ctx,
                    "num_predict": max_tokens or 64,
                },
            },
        )
        text = str(result["message"]["content"]).strip()
        usage = {
            "prompt_tokens": int(result.get("prompt_eval_count") or 0),
            "completion_tokens": int(result.get("eval_count") or 0),
        }
        return text, usage


class OpenAICompatibleChat:
    def __init__(self, base_url: str, model: str, *, temperature: float, seed: int) -> None:
        self.base = require_loopback(base_url).rstrip("/")
        if not self.base.endswith("/v1"):
            self.base += "/v1"
        self.model = model
        self.temperature = temperature
        self.seed = seed

    def chat(self, prompt: str, *, max_tokens: int | None = None) -> tuple[str, dict[str, int]]:
        result = _post_json(
            f"{self.base}/chat/completions",
            {
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": self.temperature,
                "max_tokens": max_tokens or 64,
                "seed": self.seed,
                "stream": False,
            },
        )
        choice = result["choices"][0]["message"]["content"]
        usage = result.get("usage") or {}
        return str(choice).strip(), {
            "prompt_tokens": int(usage.get("prompt_tokens") or 0),
            "completion_tokens": int(usage.get("completion_tokens") or 0),
        }


class OllamaEmbedder:
    def __init__(self, base_url: str, model: str) -> None:
        self.base = require_loopback(base_url)
        self.model = model

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        result = _post_json(f"{self.base}/api/embed", {"model": self.model, "input": list(texts)})
        return [list(map(float, row)) for row in result["embeddings"]]


def make_chat(config: dict[str, Any]) -> ChatModel:
    llm = config["llm"]
    transport = llm["transport"]
    if transport == "ollama":
        return OllamaChat(
            llm["base_url"],
            llm["model"],
            temperature=float(llm.get("temperature", 0.0)),
            seed=int(config["seed"]),
            num_ctx=int(llm.get("num_ctx", 8192)),
        )
    if transport == "openai":
        return OpenAICompatibleChat(
            llm["base_url"],
            llm["model"],
            temperature=float(llm.get("temperature", 0.0)),
            seed=int(config["seed"]),
        )
    raise ValueError(f"unsupported llm.transport: {transport!r}")


def make_embedder(config: dict[str, Any]) -> Embedder:
    embedding = config["embedding"]
    if embedding["transport"] != "ollama":
        raise ValueError(f"unsupported embedding.transport: {embedding['transport']!r}")
    return OllamaEmbedder(embedding["base_url"], embedding["model"])


def check_endpoints(config: dict[str, Any], *, embedder: Embedder | None = None) -> dict[str, Any]:
    """Fail fast when the configured loopback model stack is not reachable."""
    report: dict[str, Any] = {}
    llm = config["llm"]
    base = require_loopback(llm["base_url"])
    if llm["transport"] == "ollama":
        tags = _get_json(f"{base}/api/tags")
        names = {row.get("name") for row in tags.get("models", [])}
        wanted = {llm["model"], f"{llm['model']}:latest", llm["model"].removesuffix(":latest")}
        report["llm_models"] = sorted(name for name in names if name)
        if not names & wanted:
            raise RuntimeError(f"LLM model {llm['model']!r} not available at {base}/api/tags")
    else:
        models = _get_json(f"{base}/v1/models")
        identifiers = [str(row.get("id")) for row in models.get("data", [])]
        report["llm_models"] = identifiers
    embedding = config["embedding"]
    embed_base = require_loopback(embedding["base_url"])
    tags = _get_json(f"{embed_base}/api/tags")
    embedding_names = {str(row.get("name")) for row in tags.get("models", [])}
    report["embedding_models"] = sorted(name for name in embedding_names if name)
    if embedder is None:
        embedder = make_embedder(config)
    probe = embedder.embed(["stepg endpoint probe"])
    report["embedding_probe_dims"] = len(probe[0])
    chat = make_chat(config)
    text, usage = chat.chat("Reply with the single word: ok", max_tokens=8)
    report["chat_probe"] = text
    report["chat_probe_usage"] = usage
    report["ok"] = True
    return report


# ---------------------------------------------------------------------------
# memory systems
# ---------------------------------------------------------------------------


def _safe_id(dia_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "-", dia_id)


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    norm = math.sqrt(sum(a * a for a in left)) * math.sqrt(sum(b * b for b in right))
    return dot / norm if norm else 0.0


class MemorySystem(Protocol):
    name: str

    def add(self, turn: dict[str, str]) -> None: ...

    def search(self, query: str, *, top_k: int) -> list[dict[str, Any]]: ...

    def export_memories(self) -> list[dict[str, Any]]: ...

    def memory_count(self) -> int: ...

    def storage_bytes(self) -> int: ...

    def token_usage(self) -> dict[str, int]: ...


class MemfwSystem:
    """memfw vault with verbatim writes and cosine retrieval over the shared embedder.

    This mirrors the Day 11 protocol: memfw stores the public LoCoMo turn verbatim
    (source/provenance recorded), and retrieval uses the same embedding model as
    the Mem0 baseline so ranking quality is comparable.
    """

    name = "memfw"

    def __init__(self, root: Path, embedder: Embedder) -> None:
        self.root = root
        self.embedder = embedder
        self.repository: Any = Repository(root / "vault")
        self._sequence = itertools.count()
        self.repository.id_factory = lambda: f"stepg-event-{next(self._sequence):09d}"
        self.repository.clock = lambda: datetime(2023, 1, 1, tzinfo=UTC)
        self.repository.init()
        self.vectors: dict[str, list[float]] = {}
        self.dia_by_id: dict[str, str] = {}
        self.title_by_id: dict[str, str] = {}
        self._dia_map_path = self.repository.root / ".dia_map.json"
        self._load_dia_map()

    def _load_dia_map(self) -> None:
        if not self._dia_map_path.exists():
            return
        try:
            payload = json.loads(self._dia_map_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for memory_id, record in payload.items():
            if isinstance(record, dict):
                self.dia_by_id[memory_id] = str(record.get("dia_id") or "")
                self.title_by_id[memory_id] = str(record.get("title") or "")

    def _save_dia_map(self) -> None:
        payload = json.dumps(
            {
                memory_id: {
                    "dia_id": self.dia_by_id.get(memory_id, ""),
                    "title": self.title_by_id.get(memory_id, ""),
                }
                for memory_id in self.dia_by_id
            },
            sort_keys=True,
        ).encode("utf-8")
        temporary = self._dia_map_path.with_name(self._dia_map_path.name + ".tmp")
        temporary.write_bytes(payload)
        os.replace(temporary, self._dia_map_path)

    def hydrate_from_export(self, memories: Sequence[dict[str, Any]]) -> None:
        """Fill the memory-id -> dia_id/title maps from a run's memories.json export."""
        for item in memories:
            memory_id = str(item.get("memory_id") or "")
            dia_ids = item.get("dia_ids") or []
            if not memory_id or not dia_ids:
                continue
            self.dia_by_id[memory_id] = str(dia_ids[0])
            if item.get("title"):
                self.title_by_id[memory_id] = str(item["title"])

    def ensure_vectors(self) -> None:
        """Rebuild retrieval vectors for a resumed/expanded run from stored Markdown."""
        if self.vectors:
            return
        for document in self.repository.list():
            title = self.title_by_id.get(document.id) or f"speaker {document.title}"
            self.title_by_id[document.id] = title
            self.vectors[document.id] = self.embedder.embed([f"{title} {document.body}"])[0]

    def add(self, turn: dict[str, str]) -> None:
        timestamp = datetime.fromisoformat(turn["created_at"]).astimezone(UTC)
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
        self.title_by_id[memory_id] = title
        self._save_dia_map()

    def search(self, query: str, *, top_k: int) -> list[dict[str, Any]]:
        self.ensure_vectors()
        vector = self.embedder.embed([query])[0]
        documents = {row.id: row for row in self.repository.list()}
        scored = sorted(
            (
                (_cosine(vector, stored), key)
                for key, stored in self.vectors.items()
                if key in documents
            ),
            key=lambda pair: (-pair[0], pair[1]),
        )
        return [
            {
                "id": key,
                "memory": f"{documents[key].title} {documents[key].body}",
                "dia_ids": [self.dia_by_id.get(key, "")] if self.dia_by_id.get(key) else [],
                "score": score,
            }
            for score, key in scored[:top_k]
        ]

    def export_memories(self) -> list[dict[str, Any]]:
        return [
            {
                "memory_id": row.id,
                "memory": row.body,
                "title": row.title,
                "provenance": row.provenance,
                "dia_ids": [self.dia_by_id.get(row.id, "")],
            }
            for row in self.repository.list()
        ]

    def memory_count(self) -> int:
        return len(self.repository.list())

    def storage_bytes(self) -> int:
        return directory_bytes(self.repository.root)

    def token_usage(self) -> dict[str, int]:
        return {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0}


def mem0_add_kwargs(
    turn: dict[str, str], user_id: str, custom_instructions: str | None
) -> dict[str, Any]:
    """Build Mem0 add() kwargs; custom instructions are only used by the custom arm."""
    kwargs: dict[str, Any] = {
        "user_id": user_id,
        "metadata": {"dia_id": turn["dia_id"]},
        "infer": True,
    }
    if custom_instructions:
        kwargs["prompt"] = custom_instructions
    return kwargs


class Mem0System:
    """Thin wrapper over open-source mem0.Memory using only the local model stack."""

    name = "mem0"
    user_id = "stepg-locomo-public"

    def __init__(
        self, root: Path, config: dict[str, Any], *, custom_instructions: bool = True
    ) -> None:
        os.environ["MEM0_TELEMETRY"] = "False"
        from mem0 import Memory

        llm = config["llm"]
        embedding = config["embedding"]
        if llm["transport"] == "ollama":
            llm_config: dict[str, Any] = {
                "provider": "ollama",
                "config": {
                    "model": llm["model"],
                    "temperature": float(llm.get("temperature", 0.0)),
                    "ollama_base_url": require_loopback(llm["base_url"]),
                },
            }
        elif llm["transport"] == "openai":
            llm_config = {
                "provider": "openai",
                "config": {
                    "model": llm["model"],
                    "temperature": float(llm.get("temperature", 0.0)),
                    "max_tokens": int(llm.get("mem0_max_tokens", 512)),
                    "openai_base_url": require_loopback(llm["base_url"]).rstrip("/") + "/v1",
                    "api_key": "local-no-key",
                },
            }
        else:  # pragma: no cover - validated earlier
            raise ValueError(f"unsupported llm.transport: {llm['transport']!r}")
        mem0_config = {
            "llm": llm_config,
            "embedder": {
                "provider": "ollama",
                "config": {
                    "model": embedding["model"],
                    "ollama_base_url": require_loopback(embedding["base_url"]),
                    "embedding_dims": int(embedding["dims"]),
                },
            },
            "vector_store": {
                "provider": "qdrant",
                "config": {
                    "collection_name": str(config["runs"]["mem0"]["collection_name"]),
                    "path": str(root / "qdrant"),
                    "embedding_model_dims": int(embedding["dims"]),
                    "on_disk": True,
                },
            },
            "history_db_path": str(root / "history.db"),
        }
        self.root = root
        self._num_ctx = int(llm.get("num_ctx", 8192))
        instructions = str((config.get("mem0") or {}).get("custom_instructions") or "").strip()
        self._custom_instructions = instructions if custom_instructions and instructions else None
        self.memory: Any = Memory.from_config(mem0_config)
        self._usage = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0}
        self._instrument_llm(llm["transport"])

    def _instrument_llm(self, transport: str) -> None:
        if transport == "ollama":
            client = self.memory.llm.client
            raw_chat = client.chat

            def chat(**params: Any) -> Any:
                options = dict(params.pop("options", None) or {})
                options["num_ctx"] = self._num_ctx
                # qwen3.5 emits reasoning traces unless think=False; Day 11 used the same flag.
                response = raw_chat(**params, options=options, think=False)
                prompt = getattr(response, "prompt_eval_count", 0) or 0
                completion = getattr(response, "eval_count", 0) or 0
                self._usage["calls"] += 1
                self._usage["prompt_tokens"] += int(prompt)
                self._usage["completion_tokens"] += int(completion)
                return response

            client.chat = chat
        else:
            completions = self.memory.llm.client.chat.completions
            raw_create = completions.create

            def create(**params: Any) -> Any:
                response = raw_create(**params)
                usage = getattr(response, "usage", None)
                self._usage["calls"] += 1
                self._usage["prompt_tokens"] += int(getattr(usage, "prompt_tokens", 0) or 0)
                self._usage["completion_tokens"] += int(getattr(usage, "completion_tokens", 0) or 0)
                return response

            completions.create = create

    def add(self, turn: dict[str, str]) -> None:
        content = f"[{turn['created_at'][:10]}] {turn['speaker']}: {turn['text']}"
        last_error: Exception | None = None
        for _attempt in range(2):
            try:
                kwargs: dict[str, Any] = self._add_kwargs(turn, content)
                self.memory.add(
                    [{"role": "user", "content": content}],
                    **kwargs,
                )
                return
            except Exception as error:  # noqa: BLE001 - one bounded retry, then fail
                last_error = error
        assert last_error is not None
        raise last_error

    def _add_kwargs(self, turn: dict[str, str], content: str) -> dict[str, Any]:
        return mem0_add_kwargs(turn, self.user_id, self._custom_instructions)

    def search(self, query: str, *, top_k: int) -> list[dict[str, Any]]:
        result = self.memory.search(
            query, top_k=top_k, filters={"user_id": self.user_id}, threshold=0.0
        )
        rows: list[dict[str, Any]] = []
        for item in result.get("results", []):
            metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
            dia_id = metadata.get("dia_id") if isinstance(metadata, dict) else None
            rows.append(
                {
                    **item,
                    "dia_ids": [dia_id] if isinstance(dia_id, str) else [],
                }
            )
        return rows

    def export_memories(self) -> list[dict[str, Any]]:
        result = self.memory.get_all(filters={"user_id": self.user_id}, top_k=100000)
        rows: list[dict[str, Any]] = []
        for item in result.get("results", []):
            metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
            dia_id = metadata.get("dia_id") if isinstance(metadata, dict) else None
            rows.append(
                {
                    "memory_id": item.get("id"),
                    "memory": item.get("memory"),
                    "metadata": metadata or {},
                    "dia_ids": [dia_id] if isinstance(dia_id, str) else [],
                    "attributed_to": item.get("attributed_to") or item.get("role"),
                }
            )
        return rows

    def memory_count(self) -> int:
        return len(self.export_memories())

    def storage_bytes(self) -> int:
        return directory_bytes(self.root)

    def token_usage(self) -> dict[str, int]:
        return dict(self._usage)


# Arms: memfw, stock Mem0, and the custom-extraction-prompt Mem0 arm used in the
# first Step G pass. "mem0" remains an alias for the custom arm for compatibility.
SYSTEMS = {
    "memfw": "memfw",
    "mem0": "mem0_custom",
    "mem0_custom": "mem0_custom",
    "mem0_stock": "mem0_stock",
}
ARM_LABELS = {
    "memfw": "memfw (verbatim + shared-embedding cosine retrieval)",
    "mem0_stock": "Mem0 stock (add(infer=True), default extraction prompt)",
    "mem0_custom": "Mem0 + custom extraction instructions (schema-compat arm)",
}


# ---------------------------------------------------------------------------
# config / subset
# ---------------------------------------------------------------------------


def _resolve(path_like: str | Path) -> Path:
    path = Path(path_like)
    return path if path.is_absolute() else REPO_ROOT / path


def _path_label(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(REPO_ROOT.resolve()))
    except ValueError:
        return str(path)


def load_config(path: Path) -> dict[str, Any]:
    raw = yaml.safe_load(_resolve(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("config.yaml must be a YAML mapping")
    if raw.get("schema_version") != CONFIG_SCHEMA_VERSION:
        raise ValueError(f"config schema_version must be {CONFIG_SCHEMA_VERSION!r}")
    for key in ("seed", "paths", "data", "llm", "embedding", "retrieval", "runs"):
        if key not in raw:
            raise ValueError(f"config missing required key: {key}")
    if raw["llm"].get("transport") not in {"ollama", "openai"}:
        raise ValueError("llm.transport must be ollama or openai")
    if raw["embedding"].get("transport") != "ollama":
        raise ValueError("embedding.transport must be ollama for this runner")
    if not raw["runs"].get("systems"):
        raise ValueError("runs.systems must list at least one system")
    for system in raw["runs"]["systems"]:
        if system not in SYSTEMS:
            raise ValueError(f"unknown system: {system!r}")
    return raw


def data_path(config: dict[str, Any]) -> Path:
    return _resolve(config["paths"]["data"])


def subset_path(config: dict[str, Any]) -> Path:
    return _resolve(config["paths"]["subset"])


def verify_data(config: dict[str, Any]) -> Path:
    path = data_path(config)
    if not path.exists():
        raise FileNotFoundError(
            f"public LoCoMo data missing at {path}; "
            "run `python eval/stepG_pipeline.py --stage data`"
        )
    digest = sha256_file(path)
    expected = str(config["data"]["sha256"])
    if digest != expected:
        raise ValueError(f"LoCoMo sha256 mismatch: expected {expected}, got {digest}")
    return path


def build_subset(config: dict[str, Any]) -> dict[str, Any]:
    """Reconstruct the fixed Day 11 subset from the public corpus + manifest."""
    manifest_path = _resolve(config["paths"]["subset_manifest"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    samples = json.loads(verify_data(config).read_text(encoding="utf-8"))
    payload = samples[manifest["source"]["sample_index"]]
    if payload.get("sample_id") != manifest["source"]["sample_id"]:
        raise ValueError(
            f"sample mismatch: manifest={manifest['source']['sample_id']!r} "
            f"data={payload.get('sample_id')!r}"
        )
    turns, questions, stats = eval_locomo.adapt_with_stats(payload)
    window = set(manifest["scope"]["input_turn_ids"])
    input_turns = [turn for turn in turns if turn["dia_id"] in window]
    if len(input_turns) != int(manifest["scope"]["input_turn_count"]):
        raise ValueError(
            f"turn count mismatch: expected {manifest['scope']['input_turn_count']}, "
            f"got {len(input_turns)}"
        )
    raw_by_text = {str(row.get("question")): row for row in payload.get("qa", [])}
    selected: list[dict[str, Any]] = []
    for item in manifest["questions"]:
        question = questions[item["qa_index"]]
        text = question["question"]
        if sha256_text(text) != item["question_sha256"]:
            raise ValueError(f"question hash mismatch at qa_index {item['qa_index']}")
        raw = raw_by_text.get(text, {})
        answer = raw.get("answer", raw.get("adversarial_answer"))
        selected.append(
            {
                "question_id": sha256_text(text),
                "qa_index": item["qa_index"],
                "category": int(item["category"]),
                "question": text,
                "answer": None if answer is None else str(answer),
                "evidence_turn_ids": list(item["evidence_turn_ids"]),
            }
        )
    if len(selected) != int(manifest["scope"]["selected_question_count"]):
        raise ValueError("question count mismatch against manifest")
    subset = {
        "schema_version": "stepg-subset-v1",
        "seed": int(config["seed"]),
        "selection": manifest.get("selection", {}),
        "source": {
            "sample_id": manifest["source"]["sample_id"],
            "sample_index": int(manifest["source"]["sample_index"]),
            "data_sha256": sha256_file(data_path(config)),
            "manifest_path": _path_label(manifest_path),
            "manifest_sha256": sha256_file(manifest_path),
        },
        "scope": {
            "input_turn_count": len(input_turns),
            "selected_question_count": len(selected),
            "turn_ids": [turn["dia_id"] for turn in input_turns],
            "question_counts_by_category": {
                str(category): sum(1 for row in selected if row["category"] == category)
                for category in range(1, 6)
            },
            "adapter_stats": stats,
        },
        "turns": input_turns,
        "questions": selected,
    }
    target = subset_path(config)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(subset, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return subset


def load_subset(config: dict[str, Any]) -> dict[str, Any]:
    path = subset_path(config)
    if not path.exists():
        return build_subset(config)
    subset: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    expected_data = sha256_file(data_path(config))
    source = subset.get("source", {})
    if source.get("data_sha256") != expected_data:
        raise ValueError("subset was built from different data; rebuild with `--stage subset`")
    manifest_path = _resolve(config["paths"]["subset_manifest"])
    if manifest_path.exists() and source.get("manifest_sha256") != sha256_file(manifest_path):
        raise ValueError("subset was built from a different manifest; rebuild with --stage subset")
    turns = subset["turns"]
    questions = subset["questions"]
    if len(turns) != int(config["runs"].get("turn_count", len(turns))):
        raise ValueError("subset turn count differs from config")
    for question in questions:
        if question.get("answer") is None:
            raise ValueError(
                f"subset question {question.get('qa_index')} has no gold answer; "
                "rebuild the subset from the validated manifest"
            )
    return subset


# ---------------------------------------------------------------------------
# run loop
# ---------------------------------------------------------------------------


def _read_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rows.append(json.loads(line))
    return rows


def _append_row(path: Path, row: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )


def run_system(
    *,
    system: MemorySystem,
    system_name: str,
    run_id: str,
    turns: Sequence[dict[str, str]],
    questions: Sequence[dict[str, Any]],
    config: dict[str, Any],
    run_dir: Path,
    chat: ChatModel,
    dry_run: bool = False,
    limit_questions: int | None = None,
    log: Callable[[str], None] = lambda _message: None,
) -> dict[str, Any]:
    """Ingest, retrieve, answer, and persist one (run_id, system) result set."""
    run_dir.mkdir(parents=True, exist_ok=True)
    rows_path = run_dir / "questions.jsonl"
    marker = run_dir / ".ingested"
    write_turns_path = run_dir / "write_turns.jsonl"
    ingest_seconds = 0.0
    add_errors: list[dict[str, str]] = []
    if not marker.exists():
        started = time.perf_counter()
        write_latencies: list[float] = []
        for index, turn in enumerate(turns):
            begin = time.perf_counter()
            usage_before = system.token_usage()
            try:
                system.add(turn)
            except Exception as error:  # noqa: BLE001 - recorded honestly, run continues
                add_errors.append({"dia_id": turn["dia_id"], "error": type(error).__name__})
                raise
            elapsed = time.perf_counter() - begin
            usage_after = system.token_usage()
            delta_usage = {
                key: int(usage_after.get(key, 0)) - int(usage_before.get(key, 0))
                for key in ("calls", "prompt_tokens", "completion_tokens")
            }
            write_latencies.append(elapsed)
            _append_row(
                write_turns_path,
                {"dia_id": turn["dia_id"], "seconds": elapsed, "token_usage": delta_usage},
            )
            if (index + 1) % 10 == 0:
                log(f"[{system_name}] ingested {index + 1}/{len(turns)} turns")
        ingest_seconds = time.perf_counter() - started
        marker.write_text(
            json.dumps(
                {
                    "schema_version": "stepg-ingest-v1",
                    "system": system_name,
                    "run_id": run_id,
                    "turn_count": len(turns),
                    "ingest_seconds": ingest_seconds,
                    "write_latency": latency_summary(write_latencies),
                    "fingerprint": {
                        "turn_ids_sha256": hashlib.sha256(
                            "\n".join(turn["dia_id"] for turn in turns).encode("utf-8")
                        ).hexdigest(),
                        "llm": config["llm"].get("model"),
                        "embedding": config["embedding"].get("model"),
                        "seed": int(config["seed"]),
                        "num_ctx": config["llm"].get("num_ctx"),
                    },
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
    else:
        marker_data = json.loads(marker.read_text(encoding="utf-8"))
        ingest_seconds = float(marker_data.get("ingest_seconds", 0.0))
        fingerprint = marker_data.get("fingerprint")
        expected_turn_hash = hashlib.sha256(
            "\n".join(turn["dia_id"] for turn in turns).encode("utf-8")
        ).hexdigest()
        expected_fingerprint = {
            "turn_ids_sha256": expected_turn_hash,
            "llm": config["llm"].get("model"),
            "embedding": config["embedding"].get("model"),
            "seed": int(config["seed"]),
            "num_ctx": config["llm"].get("num_ctx"),
        }
        if fingerprint is not None and fingerprint != expected_fingerprint:
            raise ValueError(
                f"{system_name}/{run_id}: ingest marker fingerprint does not match the "
                "configured model/subset; rerun with a clean runtime directory"
            )

    if not (run_dir / "memories.json").exists():
        _write_json(run_dir / "memories.json", system.export_memories())

    done = {row["question_id"] for row in _read_rows(rows_path)}
    selected_questions = list(questions)
    if dry_run:
        selected_questions = selected_questions[:2]
    if limit_questions is not None:
        selected_questions = selected_questions[:limit_questions]

    top_k = int(config["retrieval"]["top_k"])
    context_budget = int(config["retrieval"]["context_token_budget"])
    retrieval_latencies: list[float] = []
    answer_latencies: list[float] = []
    answer_usage = {"prompt_tokens": 0, "completion_tokens": 0}
    errors: list[dict[str, str]] = list(add_errors)
    for question in selected_questions:
        if question["question_id"] in done:
            continue
        begin = time.perf_counter()
        retrieved = system.search(question["question"], top_k=top_k)
        retrieval_seconds = time.perf_counter() - begin
        context = fit_context(retrieved, context_budget)
        memories_text = "\n".join(f"- {item['memory']}" for item in context) or "- (none)"
        prompt = ANSWER_PROMPT.format(memories=memories_text, question=question["question"])
        begin = time.perf_counter()
        try:
            answer, usage = chat.chat(prompt, max_tokens=int(config["llm"].get("max_tokens", 64)))
        except Exception as error:  # noqa: BLE001 - recorded per question
            answer, usage = "", {"prompt_tokens": 0, "completion_tokens": 0}
            errors.append({"question_id": question["question_id"], "error": type(error).__name__})
        answer_seconds = time.perf_counter() - begin
        retrieval_latencies.append(retrieval_seconds)
        answer_latencies.append(answer_seconds)
        answer_usage["prompt_tokens"] += usage.get("prompt_tokens", 0)
        answer_usage["completion_tokens"] += usage.get("completion_tokens", 0)
        score = answer_score(answer, question["answer"], question["category"])
        row = {
            "schema_version": SCHEMA_VERSION,
            "run_id": run_id,
            "system": system_name,
            "question_id": question["question_id"],
            "qa_index": question["qa_index"],
            "category": question["category"],
            "question": question["question"],
            "gold_answer": question["answer"],
            "evidence_turn_ids": question["evidence_turn_ids"],
            "retrieved": [
                {
                    "memory_id": item.get("id"),
                    "dia_ids": item.get("dia_ids", []),
                    "score": item.get("score"),
                    "memory": item.get("memory"),
                }
                for item in retrieved
            ],
            "context_memories": [item.get("memory") for item in context],
            "context_items": [
                {
                    "memory_id": item.get("id"),
                    "dia_ids": item.get("dia_ids", []),
                    "score": item.get("score"),
                    "memory": item.get("memory"),
                }
                for item in context
            ],
            "answer": answer,
            "answer_score": score,
            "token_f1": token_f1(answer, question["answer"] or ""),
            "retrieval_seconds": retrieval_seconds,
            "answer_seconds": answer_seconds,
            "answer_token_usage": usage,
            "ingest_seconds": ingest_seconds,
            "memory_count": system.memory_count(),
            "storage_bytes": system.storage_bytes(),
        }
        _append_row(rows_path, row)
        done.add(question["question_id"])
        log(f"[{system_name}] {len(done)}/{len(selected_questions)} questions (score={score:.3f})")

    existing_rows = _read_rows(rows_path)
    answer_usage = {"prompt_tokens": 0, "completion_tokens": 0}
    for row in existing_rows:
        usage = row.get("answer_token_usage") or {}
        answer_usage["prompt_tokens"] += int(usage.get("prompt_tokens") or 0)
        answer_usage["completion_tokens"] += int(usage.get("completion_tokens") or 0)
    summary = {
        "schema_version": "stepg-run-summary-v1",
        "system": system_name,
        "run_id": run_id,
        "models": {
            "llm": config["llm"]["model"],
            "llm_transport": config["llm"]["transport"],
            "embedding": config["embedding"]["model"],
        },
        "arm": SYSTEMS.get(system_name, system_name),
        "custom_instructions_used": bool(
            SYSTEMS.get(system_name) == "mem0_custom"
            and (config.get("mem0") or {}).get("custom_instructions")
        ),
        "seed": int(config["seed"]),
        "turn_count": len(turns),
        "ingest_seconds": ingest_seconds,
        "write_latency": (
            json.loads(marker.read_text(encoding="utf-8")).get("write_latency")
            if marker.exists()
            else None
        ),
        "question_rows": len(existing_rows),
        "retrieval_latency": latency_summary([row["retrieval_seconds"] for row in existing_rows]),
        "answer_latency": latency_summary([row["answer_seconds"] for row in existing_rows]),
        "mean_answer_score": statistics.fmean([row["answer_score"] for row in existing_rows])
        if existing_rows
        else None,
        "mean_token_f1": statistics.fmean([row["token_f1"] for row in existing_rows])
        if existing_rows
        else None,
        "answer_token_usage": answer_usage,
        "llm_token_usage": system.token_usage(),
        "memory_count": system.memory_count(),
        "storage_bytes": system.storage_bytes(),
        "errors": errors,
        "dry_run": dry_run,
    }
    _write_json(run_dir / "summary.json", summary)
    return summary


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _default_config() -> Path:
    return REPO_ROOT / "results" / "stepG" / "config.yaml"


def _run_dir(config: dict[str, Any], run_id: str, system_name: str) -> Path:
    return _resolve(config["paths"]["results_dir"]) / "runs" / run_id / system_name


def _runtime_dir(config: dict[str, Any], run_id: str, system_name: str) -> Path:
    return _resolve(config["paths"]["results_dir"]) / "runtime" / run_id / system_name


def _make_system(
    system_name: str, config: dict[str, Any], run_id: str, embedder: Embedder
) -> MemorySystem:
    root = _runtime_dir(config, run_id, system_name)
    arm = SYSTEMS.get(system_name)
    if arm is None:
        raise ValueError(f"unknown system: {system_name!r}")
    if arm == "memfw":
        return MemfwSystem(root, embedder)
    if arm == "mem0_custom":
        return Mem0System(root, config, custom_instructions=True)
    if arm == "mem0_stock":
        return Mem0System(root, config, custom_instructions=False)
    raise ValueError(f"unknown system: {system_name!r}")


def execute(
    config: dict[str, Any],
    *,
    run_ids: Sequence[str] | None = None,
    systems: Sequence[str] | None = None,
    dry_run: bool = False,
    limit_questions: int | None = None,
    limit_turns: int | None = None,
    log: Callable[[str], None] = print,
) -> dict[str, Any]:
    subset = load_subset(config)
    turns = list(subset["turns"])
    if limit_turns is not None:
        turns = turns[:limit_turns]
    questions = list(subset["questions"])
    if run_ids is None:
        repeats = int(config["runs"].get("repeats", 3))
        run_ids = [f"rep{index}" for index in range(1, repeats + 1)]
    if dry_run:
        run_ids = ["dry-run"]
    chosen_systems = list(systems or config["runs"]["systems"])
    embedder = make_embedder(config)
    chat = make_chat(config)
    results: dict[str, Any] = {"runs": [], "config_schema": config.get("schema_version")}
    for run_id in run_ids:
        for system_name in chosen_systems:
            log(f"=== run {run_id} / {system_name} ===")
            root = _runtime_dir(config, run_id, system_name)
            if not dry_run and root.exists():
                root = root  # resume existing runtime state in place
            system = _make_system(system_name, config, run_id, embedder)
            summary = run_system(
                system=system,
                system_name=system_name,
                run_id=run_id,
                turns=turns,
                questions=questions,
                config=config,
                run_dir=_run_dir(config, run_id, system_name),
                chat=chat,
                dry_run=dry_run,
                limit_questions=limit_questions,
                log=log,
            )
            results["runs"].append(summary)
    results_path = _resolve(config["paths"]["results_dir"]) / (
        "dry_run_summary.json" if dry_run else "run_manifest.json"
    )
    _write_json(results_path, results)
    return results


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Step G formal memfw vs Mem0 runner")
    parser.add_argument("--config", type=Path, default=_default_config())
    parser.add_argument("--stage", choices=["subset", "check", "run"], default="run")
    parser.add_argument("--run-ids", type=str, default=None, help="comma separated run ids")
    parser.add_argument("--systems", type=str, default=None, help="comma separated systems")
    parser.add_argument(
        "--dry-run", action="store_true", help="ingest full turns, answer only 2 questions"
    )
    parser.add_argument("--limit-questions", type=int, default=None)
    parser.add_argument("--limit-turns", type=int, default=None, help="debug only")
    args = parser.parse_args(argv)
    config = load_config(args.config)
    if args.stage == "subset":
        subset = build_subset(config)
        print(
            f"wrote {subset_path(config)} ({len(subset['turns'])} turns, "
            f"{len(subset['questions'])} questions)"
        )
        return 0
    if args.stage == "check":
        report = check_endpoints(config)
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0
    if not data_path(config).exists():
        print(
            f"public LoCoMo data missing at {data_path(config)}; "
            "run `python eval/stepG_pipeline.py --stage data` first",
            file=sys.stderr,
        )
        return 2
    os.environ.setdefault(
        "MEM0_DIR", str(_resolve(config["paths"]["results_dir"]) / "runtime" / "mem0_home")
    )
    run_ids = args.run_ids.split(",") if args.run_ids else None
    systems = args.systems.split(",") if args.systems else None
    execute(
        config,
        run_ids=run_ids,
        systems=systems,
        dry_run=args.dry_run,
        limit_questions=args.limit_questions,
        limit_turns=args.limit_turns,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
