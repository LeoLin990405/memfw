"""Adapters for third-party open-source memory frameworks used in the Step G comparison.

Every adapter implements the small ``MemorySystem`` protocol used by
``eval/stepG_runner.run_system``: add one LoCoMo turn, search top-k memories, export
memories, report memory count/storage/token usage. The adapter only swaps the memory
layer; the answerer, embedder, judge and LoCoMo protocol stay identical across arms.

The framework packages are optional dependencies and are imported lazily so the main
Step G pipeline and its unit tests keep working without them.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any


def _text_of(value: Any) -> str:
    """Normalise a framework-specific memory object to plain text."""
    content = getattr(value, "content", value)
    if isinstance(content, str):
        return content
    inner = getattr(content, "content", None)
    if isinstance(inner, str):
        return inner
    dump = getattr(content, "model_dump", None)
    if callable(dump):
        data = dump()
        if isinstance(data, dict):
            for key in ("content", "text", "memory"):
                if isinstance(data.get(key), str):
                    return str(data[key])
        return str(data)
    return str(content)


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
    temporary.replace(path)


class LangMemSystem:
    """langchain-ai/langmem memory manager (create_memory_manager + LangGraph store)."""

    name = "langmem"
    user_id = "stepg-locomo-public"

    def __init__(self, root: Path, config: dict[str, Any]) -> None:
        from langchain_ollama import ChatOllama, OllamaEmbeddings
        from langgraph.store.memory import InMemoryStore
        from langmem import create_memory_manager
        from pydantic import Field

        class CountingChatOllama(ChatOllama):
            usage: dict[str, int] = Field(
                default_factory=lambda: {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0}
            )

            def _generate(
                self, messages: Any, stop: Any = None, run_manager: Any = None, **kwargs: Any
            ) -> Any:
                result = super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
                for generation in result.generations:
                    message = getattr(generation, "message", None)
                    usage = getattr(message, "usage_metadata", None) or {}
                    self.usage["calls"] += 1
                    self.usage["prompt_tokens"] += int(usage.get("input_tokens") or 0)
                    self.usage["completion_tokens"] += int(usage.get("output_tokens") or 0)
                return result

        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._items: dict[str, dict[str, Any]] = {}
        self._text_index: dict[str, str] = {}
        self._items_path = root / "memories.jsonl"
        if self._items_path.exists():
            for line in self._items_path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    row = json.loads(line)
                    self._items[str(row["memory_id"])] = row
        llm = config["llm"]
        embedding = config["embedding"]
        self._model = CountingChatOllama(
            model=llm["model"],
            base_url=llm["base_url"],
            temperature=float(llm.get("temperature", 0.0)),
            num_ctx=int(llm.get("num_ctx", 8192)),
            num_predict=int(llm.get("framework_max_tokens", 768)),
        )
        self._embedder = OllamaEmbeddings(model=embedding["model"], base_url=embedding["base_url"])
        self._store = InMemoryStore(
            index={
                "embed": self._embedder,
                "dims": int(embedding["dims"]),
                "embed_fields": ["text"],
            }
        )
        self._manager = create_memory_manager(
            self._model, enable_updates=False, enable_deletes=False
        )
        for memory_id, item in self._items.items():
            self._text_index[self._normalise(item["memory"])] = memory_id
            self._store.put(
                ("memories", self.user_id),
                memory_id,
                {"text": item["memory"], "dia_ids": list(item.get("dia_ids") or [])},
            )

    @staticmethod
    def _content(turn: dict[str, str]) -> str:
        return f"[{turn['created_at'][:10]}] {turn['speaker']}: {turn['text']}"

    @staticmethod
    def _normalise(text: str) -> str:
        return " ".join(text.split()).lower()

    def add(self, turn: dict[str, str]) -> None:
        from langmem.knowledge.extraction import Memory

        recent = list(self._items.values())[-5:]
        existing = [(row["memory_id"], Memory(content=row["memory"])) for row in recent]
        result = self._manager.invoke(
            {"messages": [("user", self._content(turn))], "existing": existing}
        )
        for extracted in result:
            text = _text_of(extracted)
            key = self._normalise(text)
            existing_id = self._text_index.get(key)
            if existing_id is not None:
                row = self._items[existing_id]
                if turn["dia_id"] not in row["dia_ids"]:
                    row["dia_ids"].append(turn["dia_id"])
                self._store.put(
                    ("memories", self.user_id),
                    existing_id,
                    {"text": row["memory"], "dia_ids": list(row["dia_ids"])},
                )
                continue
            memory_id = str(getattr(extracted, "id", None) or uuid.uuid4())
            row = {
                "memory_id": memory_id,
                "memory": text,
                "dia_ids": [turn["dia_id"]],
                "metadata": {"source": "langmem", "created_at": turn["created_at"][:10]},
                "attributed_to": None,
            }
            self._items[memory_id] = row
            self._text_index[key] = memory_id
            self._store.put(
                ("memories", self.user_id),
                memory_id,
                {"text": text, "dia_ids": [turn["dia_id"]]},
            )
        _write_jsonl(self._items_path, list(self._items.values()))

    def search(self, query: str, *, top_k: int) -> list[dict[str, Any]]:
        items = self._store.search(("memories", self.user_id), query=query, limit=top_k)
        return [
            {
                "memory_id": item.key,
                "memory": str(item.value.get("text") or ""),
                "dia_ids": [item.value.get("dia_id")] if item.value.get("dia_id") else [],
                "score": float(getattr(item, "score", 0.0) or 0.0),
            }
            for item in items
        ]

    def export_memories(self) -> list[dict[str, Any]]:
        return list(self._items.values())

    def memory_count(self) -> int:
        return len(self._items)

    def storage_bytes(self) -> int:
        return sum(path.stat().st_size for path in self.root.rglob("*") if path.is_file())

    def token_usage(self) -> dict[str, int]:
        return dict(self._model.usage)


FRAMEWORK_ADAPTERS = {
    "langmem": LangMemSystem,
}


def make_framework_system(arm: str, root: Path, config: dict[str, Any]) -> Any:
    """Create a framework adapter by arm name (lazy imports happen inside the adapter)."""
    adapter = FRAMEWORK_ADAPTERS.get(arm)
    if adapter is None:
        raise ValueError(f"unknown framework arm: {arm!r} (known: {sorted(FRAMEWORK_ADAPTERS)})")
    return adapter(root, config)


def framework_arm_names() -> list[str]:
    return sorted(FRAMEWORK_ADAPTERS)


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


class MemOSSystem:
    """MemTensor MemOS (PyPI ``MemoryOS``) in its general-text memory configuration.

    MemOS' tree-text memory needs an external graph database (Neo4j/PolarDB/Postgres),
    which is not available in this local-only setup. ``general_text`` stores each turn
    verbatim in a Qdrant collection and retrieves it with the shared embedder, so this
    arm measures MemOS' storage/retrieval layer rather than its LLM graph extraction.
    That difference is disclosed in the comparison report.
    """

    name = "memoryos"
    user_id = "stepg-locomo-public"

    def __init__(self, root: Path, config: dict[str, Any]) -> None:
        import os

        os.environ["MEMOS_DIR"] = str(root / "memos_home")
        os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost,::1")
        os.environ.setdefault("no_proxy", "127.0.0.1,localhost,::1")
        from memos.configs.mem_cube import GeneralMemCubeConfig
        from memos.configs.mem_os import MOSConfig
        from memos.mem_cube.general import GeneralMemCube
        from memos.mem_os.main import MOS

        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        llm = config["llm"]
        embedding = config["embedding"]
        ollama_llm = {
            "backend": "ollama",
            "config": {
                "model_name_or_path": llm["model"],
                "temperature": float(llm.get("temperature", 0.0)),
                "max_tokens": int(llm.get("framework_max_tokens", 768)),
                "api_base": llm["base_url"],
                "enable_thinking": False,
            },
        }
        ollama_emb = {
            "backend": "ollama",
            "config": {
                "model_name_or_path": embedding["model"],
                "api_base": embedding["base_url"],
            },
        }
        chunker = {
            "backend": "sentence",
            "config": {
                "tokenizer_or_token_counter": "gpt2",
                "chunk_size": 512,
                "chunk_overlap": 128,
                "min_sentences_per_chunk": 1,
            },
        }
        mos_cfg = {
            "user_id": self.user_id,
            "chat_model": ollama_llm,
            "mem_reader": {
                "backend": "simple_struct",
                "config": {"llm": ollama_llm, "embedder": ollama_emb, "chunker": chunker},
            },
            "max_turns_window": 20,
            "top_k": int(config["retrieval"]["top_k"]),
            "enable_textual_memory": True,
            "enable_activation_memory": False,
            "enable_parametric_memory": False,
            "enable_preference_memory": False,
            "enable_mem_scheduler": False,
        }
        cube_id = "stepg-cube"
        cube_cfg = GeneralMemCubeConfig(
            user_id=self.user_id,
            cube_id=cube_id,
            text_mem={
                "backend": "general_text",
                "config": {
                    "extractor_llm": ollama_llm,
                    "embedder": ollama_emb,
                    "vector_db": {
                        "backend": "qdrant",
                        "config": {
                            "collection_name": "stepg_memos",
                            "path": str(root / "qdrant"),
                            "vector_dimension": int(embedding["dims"]),
                            "distance_metric": "cosine",
                        },
                    },
                },
            },
            act_mem={"backend": "uninitialized", "config": {}},
            para_mem={"backend": "uninitialized", "config": {}},
            pref_mem={"backend": "uninitialized", "config": {}},
        )
        self._mos = MOS(MOSConfig(**mos_cfg))
        try:
            self._mos.create_user(self.user_id)
        except Exception:  # noqa: BLE001 - already-created user is fine
            pass
        self._cube = GeneralMemCube(cube_cfg)
        self._mos.register_mem_cube(self._cube, mem_cube_id=cube_id, user_id=self.user_id)
        self._cube_id = cube_id
        self._usage = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0}
        self._num_ctx = int(llm.get("num_ctx", 8192))
        self._patch_models()

    def _patch_models(self) -> None:
        def patch(model: Any) -> None:
            client = getattr(model, "client", None)
            if client is None or not hasattr(client, "chat"):
                return
            raw_chat = client.chat

            def chat(**params: Any) -> Any:
                options = dict(params.pop("options", None) or {})
                options["num_ctx"] = self._num_ctx
                response = raw_chat(**params, options=options)
                self._usage["calls"] += 1
                self._usage["prompt_tokens"] += int(getattr(response, "prompt_eval_count", 0) or 0)
                self._usage["completion_tokens"] += int(getattr(response, "eval_count", 0) or 0)
                return response

            client.chat = chat

        for name in ("chat_llm",):
            patch(getattr(self._mos, name, None))
        reader = getattr(self._mos, "mem_reader", None)
        for name in ("llm", "general_llm", "preference_extractor_llm"):
            patch(getattr(reader, name, None) if reader is not None else None)
        text_mem = getattr(self._cube, "text_mem", None)
        for name in ("extractor_llm", "general_llm"):
            patch(getattr(text_mem, name, None) if text_mem is not None else None)

    @staticmethod
    def _content(turn: dict[str, str]) -> str:
        return f"[{turn['created_at'][:10]}] {turn['speaker']}: {turn['text']}"

    @staticmethod
    def _normalise(text: str) -> str:
        return " ".join(text.split()).lower()

    def add(self, turn: dict[str, str]) -> None:
        self._mos.add(
            memory_content=self._content(turn),
            mem_cube_id=self._cube_id,
            user_id=self.user_id,
            session_id=turn["dia_id"],
        )

    def _walk_items(self, payload: Any) -> list[Any]:
        items: list[Any] = []
        for block in _field(payload, "text_mem", []) or []:
            for item in _field(block, "memories", []) or []:
                items.append(item)
        return items

    def _to_row(self, item: Any) -> dict[str, Any]:
        metadata = _field(item, "metadata", {}) or {}
        session_id = _field(metadata, "session_id", None)
        return {
            "memory_id": str(_field(item, "id", "")),
            "memory": str(_field(item, "memory", "")),
            "dia_ids": [str(session_id)] if session_id else [],
            "metadata": {"session_id": session_id, "source": "memoryos"},
            "attributed_to": None,
        }

    def search(self, query: str, *, top_k: int) -> list[dict[str, Any]]:
        result = self._mos.search(query, user_id=self.user_id, top_k=top_k, mode="fast")
        rows = [self._to_row(item) for item in self._walk_items(result)]
        for row in rows:
            row["score"] = None
        return rows

    def export_memories(self) -> list[dict[str, Any]]:
        result = self._mos.get_all(mem_cube_id=self._cube_id, user_id=self.user_id)
        return [self._to_row(item) for item in self._walk_items(result)]

    def memory_count(self) -> int:
        return len(self.export_memories())

    def storage_bytes(self) -> int:
        return sum(path.stat().st_size for path in self.root.rglob("*") if path.is_file())

    def token_usage(self) -> dict[str, int]:
        return dict(self._usage)


FRAMEWORK_ADAPTERS = {
    "langmem": LangMemSystem,
    "memoryos": MemOSSystem,
}
