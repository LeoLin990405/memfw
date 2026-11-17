"""Probes for ``byte_identical``: each runs inside the tree under comparison.

Use only memfw APIs present in every tree you compare against (here: the
``Repository`` constructor's ``clock``/``id_factory`` callables, ``add``,
``MemoryIndex.rebuild`` and the MCP ``Gateway`` JSON-RPC entry point).
"""

from __future__ import annotations

import json
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

T0 = datetime(2026, 1, 1, tzinfo=UTC)

#: Small fixed vault: (memory id, title, body, source, tags).
VAULT: tuple[tuple[str, str, str, str, tuple[str, ...]], ...] = (
    ("m-backup", "Homelab backup", "nightly homelab backup to the NAS", "manual", ()),
    ("m-cron", "Cron pause", "pause cron before the disk migration", "manual", ()),
    ("m-notes", "Backup notes", "backup notes and restore drills", "nas", ()),
    ("m-release", "Release", "release notes for memfw", "manual", ()),
    ("m-zh", "迁移计划", "迁移之前先暂停定时任务，再做备份", "manual", ()),
    ("m-priv", "Private backup", "private backup keys", "manual", ("sensitivity:private",)),
)


def _vault(root: Path) -> Path:
    from memfw.core.repository import Repository
    from memfw.index import MemoryIndex

    counter = iter(range(1, 1_000_000))
    repository = Repository(root, clock=lambda: T0, id_factory=lambda: f"id-{next(counter):06d}")
    repository.init()
    for memory_id, title, body, source, tags in VAULT:
        repository.add(title, body, memory_id=memory_id, source=source, tags=list(tags))
    MemoryIndex(repository.index_path).rebuild(repository)
    return repository.root


def mcp_memory_search(payload: dict[str, Any], workdir: Path) -> str:
    """One MCP ``tools/call memory_search`` on a fresh fixture vault.

    Returns the JSON-RPC responses, pretty-printed, with the tool's JSON text
    result decoded in place (so a diff points at the changed field).
    """

    from memfw.mcp.server import Gateway

    root = _vault(workdir / "vault")
    gateway = Gateway(root, tenant="", entity_enabled=False, allowed_sources=())
    sent: list[dict[str, Any]] = []
    gateway.respond = sent.append  # type: ignore[method-assign]
    gateway.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "memory_search", "arguments": payload},
        }
    )
    for item in sent:  # the tool result is JSON inside a string: unpack it for readable diffs
        for block in item.get("result", {}).get("content", []):
            try:
                block["text"] = json.loads(block["text"])
            except (TypeError, ValueError):
                pass
    text = "\n".join(
        json.dumps(item, ensure_ascii=False, sort_keys=True, indent=1) for item in sent
    )
    return text.replace(str(root), "<vault>").replace(str(workdir), "<workdir>")


def loaded_from(payload: dict[str, Any], workdir: Path) -> str:
    """The ``src`` directory ``memfw`` was imported from (harness self-test)."""

    import memfw

    return str(Path(memfw.__file__).resolve().parents[1])


#: Concept vault for ``mcp_memory_search_stable``: (id, title, body, about).
CONCEPT_VAULT: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    ("m-port-a", "Port A", "the service port is 8080", ("concept:service-port",)),
    ("m-port-b", "Port B", "the service port is 9090", ("concept:service-port",)),
    (
        "m-runbook",
        "Port runbook",
        "service port runbook and backup notes",
        ("concept:service-port",),
    ),
    ("m-legacy", "Port legacy", "legacy service port notes", ()),
)


def _zero_timings(value: Any) -> Any:
    """``total_ms`` and ``latency_ms`` differ per run: set them to 0."""

    if isinstance(value, dict):
        return {
            key: 0 if key in {"total_ms", "latency_ms"} else _zero_timings(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_zero_timings(item) for item in value]
    return value


def mcp_memory_search_stable(payload: dict[str, Any], workdir: Path) -> str:
    """``memory_search`` on the fixture vault plus concept memories, timings zeroed.

    ``payload``: ``arguments`` (the tool arguments), ``open_question`` (add an
    unresolved open question about ``concept:service-port`` listing
    ``m-port-a`` and ``m-legacy`` in its conflicts) and ``embedder`` (a
    ``HashEmbeddingProvider``). Uses only APIs present since U1.
    """

    from memfw.core.repository import Repository
    from memfw.index import MemoryIndex
    from memfw.mcp.server import Gateway
    from memfw.notes import HashEmbeddingProvider

    counter = iter(range(1, 1_000_000))
    repository = Repository(
        workdir / "vault", clock=lambda: T0, id_factory=lambda: f"id-{next(counter):06d}"
    )
    repository.init()
    for memory_id, title, body, source, tags in VAULT:
        repository.add(title, body, memory_id=memory_id, source=source, tags=list(tags))
    for memory_id, title, body, about in CONCEPT_VAULT:
        repository.add(title, body, memory_id=memory_id, about=list(about))
    if payload.get("open_question"):
        repository.add(
            "Service port unknown",
            "The memories disagree on the service port.",
            memory_id="q-port",
            memory_type="open-question",
            about=["concept:service-port"],
            open_question={
                "question": "Which service port is used?",
                "conflicts": ["m-port-a", "m-legacy"],
            },
        )
    MemoryIndex(repository.index_path).rebuild(repository)
    root = repository.root
    embedder = HashEmbeddingProvider(dimension=64) if payload.get("embedder") else None
    gateway = Gateway(root, tenant="", entity_enabled=False, allowed_sources=(), embedder=embedder)
    sent: list[dict[str, Any]] = []
    gateway.respond = sent.append  # type: ignore[method-assign]
    gateway.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "memory_search", "arguments": payload["arguments"]},
        }
    )
    for item in sent:
        for block in item.get("result", {}).get("content", []):
            try:
                block["text"] = _zero_timings(json.loads(block["text"]))
            except (TypeError, ValueError):
                pass
    text = "\n".join(
        json.dumps(item, ensure_ascii=False, sort_keys=True, indent=1) for item in sent
    )
    return text.replace(str(root), "<vault>").replace(str(workdir), "<workdir>")


#: Transcript lines for ``mcp_all_tools`` (``history_sources: {logs: ...}``).
HISTORY_LINES: tuple[dict[str, str], ...] = (
    {"session_id": "s1", "ts": "2026-02-01T00:00:00Z", "message": "Tune the greenhouse fan curve"},
    {"session_id": "s1", "ts": "2026-02-01T00:00:01Z", "message": "Fan curve tuned for backup"},
    {"session_id": "s2", "ts": "2026-02-02T00:00:00Z", "message": "Restore drill for the NAS"},
)


def _tool(name: str, arguments: Any = None, *, omit: bool = False) -> dict[str, Any]:
    params: dict[str, Any] = {"name": name}
    if not omit:
        params["arguments"] = {} if arguments is None else arguments
    return {"jsonrpc": "2.0", "id": name, "method": "tools/call", "params": params}


def all_tool_messages() -> list[Any]:
    """Every MCP method and tool, with argument variants and error paths.

    ``"$HISTORY_REF"`` is replaced by the first ref a ``history_search`` returned.
    """

    search = [
        {"query": "backup", "mode": mode}
        for mode in ("fts", "hybrid", "vector", "multihop", "two_stage")
    ]
    search += [
        {"query": "backup"},
        {"query": "backup", "mode": "two_stage", "policy": "strict"},
        {"query": "backup", "mode": "two_stage", "policy": ""},
        {"query": "backup", "mode": "fts", "labels": True},
        {"query": "service port", "mode": "fts", "labels": True},
        {"query": "service port", "mode": "hybrid", "labels": True},
        {"query": "service port", "mode": "multihop", "rounds": 2},
        {"query": "service port", "mode": "two_stage"},
        {"query": "backup", "mode": "fts", "source": "nas", "limit": 1},
        {"query": "backup", "mode": "fts", "scope": "homelab"},
        {"query": "backup", "mode": "fts", "include_history": True},
        {"query": "backup", "mode": "fts", "as_of": "2026-01-02T00:00:00+00:00"},
        {"query": "backup", "mode": "multihop", "as_of": "2026-01-02T00:00:00+00:00"},
        {"query": "backup", "mode": "two_stage", "as_of": "2026-01-02T00:00:00"},
        {"query": "backup", "mode": "hybrid", "limit": 0},
        {"query": "迁移", "mode": "fts"},
        {"query": "port", "mode": "vector", "labels": True},
        # Error paths.
        {"query": "  "},
        {"query": "backup", "policy": "strict"},
        {"query": "backup", "mode": "two_stage", "policy": "loose"},
        {"query": "backup", "mode": "two_stage", "policy": 3},
        {"query": "backup", "labels": "yes"},
        {"query": "backup", "include_history": 1},
        {"query": "backup", "scope": "x" * 513},
        {"query": "backup", "scope": 5},
        {"query": "backup", "mode": "multihop", "as_of": "2026-01-02T00:00:00"},
        {"query": "backup", "mode": "bogus"},
        {"query": "backup", "source": "other"},
        {"query": '"unbalanced', "mode": "two_stage"},
    ]
    messages: list[Any] = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "method": "initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "ping"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 4, "method": "nope"},
        {"jsonrpc": "2.0", "method": "nope"},
        {"jsonrpc": "2.0", "id": 5, "method": "ping", "params": []},
        {"jsonrpc": "2.0", "method": "ping", "params": "x"},
        {"jsonrpc": "2.0", "id": 6, "method": "ping", "params": None},
        "not an object",
        [],
        [{"jsonrpc": "2.0", "id": 7, "method": "ping"}, 5],
        _tool("no_such_tool"),
        _tool("memory_status", omit=True),
        _tool("memory_status", None),
        _tool("memory_status", [1]),
        _tool("memory_search", {"query": "backup", "base_url": "http://x.invalid"}),
        _tool("memory_search", {"query": "backup", "rerank-endpoint": "x"}),
    ]
    messages += [_tool("memory_search", arguments) for arguments in search]
    messages += [
        _tool("memory_get", {"memory_id": "m-backup"}),
        _tool("memory_get", {"memory_id": "m-port-a"}),
        _tool("memory_get", {"memory_id": "m-missing"}),
        _tool("memory_get", {}),
        _tool("memory_remember", {"content": "remember the backup window", "title": "Window"}),
        _tool(
            "memory_remember",
            {"content": "tagged", "title": "T", "tags": ["a"], "related": ["m-backup"]},
        ),
        _tool("memory_remember", {"content": "   "}),
        _tool("memory_remember", {"content": "x" * (64 * 1024 + 1)}),
        _tool("memory_related", {"memory_id": "m-backup"}),
        _tool("memory_related", {"memory_id": "m-backup", "limit": 0}),
        _tool("memory_related", {"memory_id": "m-missing"}),
        _tool("memory_context", {}),
        _tool("memory_context", {"limit_tokens": 40}),
        _tool("memory_context", {"v": 1}),
        _tool("memory_context", {"v": 2}),
        _tool("memory_context", {"v": 2, "policy": "strict", "limit_tokens": 200}),
        _tool("memory_context", {"v": 3}),
        _tool("memory_context", {"v": 2, "policy": "loose"}),
        _tool("memory_context", {"v": 1, "policy": "strict"}),
        _tool("memory_status", {}),
        # Off by default (C5): an unknown tool on both trees, nothing recorded.
        _tool("memory_used", {"ids": ["m-backup"], "session_ref": "s1"}),
        _tool("history_list", {}),
        _tool("history_list", {"source": "logs"}),
        _tool("history_list", {"session": "s1"}),
        _tool("history_list", {"source": "logs", "session": "s1", "limit": 1}),
        _tool("history_list", {"since": "2026-02-02", "until": "2026-02-03"}),
        _tool("history_list", {"source": "unknown"}),
        _tool("history_search", {"query": "fan curve"}),
        _tool("history_search", {"query": "fan curve", "source": "logs", "limit": 1}),
        _tool("history_search", {"query": "restore", "session": "s2"}),
        _tool("history_search", {"query": "fan", "source": "unknown"}),
        _tool("history_read", {"ref": "$HISTORY_REF"}),
        _tool("history_read", {"ref": "$HISTORY_REF", "before": 0, "after": 0, "max_bytes": 64}),
        _tool("history_read", {"ref": "bogus"}),
        _tool("history_read", {"ref": "unknown:s1:0"}),
        _tool("conversations_search", {"query": "uploader"}),
        _tool("conversations_search", {"query": "uploader", "limit": 1, "offset": 1}),
        _tool("conversations_search", {"query": "uploader", "app": "codex"}),
        _tool("conversations_search", {"query": "uploader", "since": "not-a-date"}),
        _tool("conversations_list", {}),
        _tool("conversations_list", {"app": "claude", "limit": 1}),
        _tool("conversations_get", {"session_id": "s-beta", "end": 2}),
        _tool("conversations_get", {"session_id": "s-alpha", "start": 1, "max_bytes": 4096}),
        _tool("conversations_get", {"session_id": "s-missing"}),
        _tool("conversations_get", {"session_id": "../x"}),
        _tool("conversations_handoff", {"session_id": "s-alpha", "for_agent": "grok"}),
        _tool("conversations_handoff", {"session_id": "s-alpha", "max_bytes": 4096}),
        _tool("conversations_handoff", {"session_id": "s-missing"}),
        _tool("dream_list", {}),
        _tool("dream_run", {}),
        _tool("dream_run", {"focus": "backup", "all": True, "limit": 5, "stale_days": 0}),
        _tool("dream_run", {"max_sessions": 2, "source": "logs", "min_events": 1}),
        _tool("dream_run", {"since": "2026-01-01", "until": "2026-03-01"}),
        _tool("dream_run", {"limit": -1}),
        _tool("dream_run", {"limit": 1001}),
        _tool("dream_run", {"max_sessions": 0}),
        _tool("dream_run", {"stale_days": -1}),
        _tool("dream_run", {"dry_run": "no"}),
        _tool("dream_run", {"dry_run": False}),
        _tool("dream_run", {"dry_run": False}),
        _tool("dream_list", {}),
        _tool("dream_list", {"limit": 1}),
        _tool("dream_list", {"limit": 0}),
        _tool("dream_list", {"limit": 201}),
    ]
    return messages


def mcp_all_tools(payload: dict[str, Any], workdir: Path) -> str:
    """Every MCP method and tool (``all_tool_messages``) against one rich fixture vault.

    ``payload`` keys: ``app`` (``--app``), ``share`` (``conversation_share``
    enabled), ``optout`` (apps in ``conversation_optout.agents``), ``history``
    (configure ``history_sources``), ``embedder`` (``HashEmbeddingProvider``),
    ``tenant`` and ``allowed_sources``. The vault is ``VAULT`` plus
    ``CONCEPT_VAULT`` and an open question; the conversation store is
    ``tests.conversation_fixtures.standard_rows``. Random ids and the clock are
    fixed; only timing fields are masked (``total_ms`` and ``latency_ms`` zeroed,
    the wall-clock ``generated_at`` emptied).

    The fixture lives in one fixed directory under the system temp dir, not in
    ``workdir``: ``vault.yaml`` holds the store and transcript paths, and its
    hash is in the event log head that ``memory_status`` and dream ids show.
    Do not run two copies of this probe at once.
    """

    del workdir  # see the docstring: the fixture paths must not vary per run
    workdir = Path(tempfile.gettempdir()) / "memfw-probe-mcp-all-tools"
    shutil.rmtree(workdir, ignore_errors=True)
    workdir.mkdir()
    try:
        return _mcp_all_tools(payload, workdir)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _mask_timing(value: Any) -> Any:
    value = _zero_timings(value)
    if isinstance(value, dict):
        return {
            key: "" if key == "generated_at" else _mask_timing(item) for key, item in value.items()
        }
    if isinstance(value, list):
        return [_mask_timing(item) for item in value]
    return value


def _mcp_all_tools(payload: dict[str, Any], workdir: Path) -> str:
    import time
    import uuid
    from unittest import mock

    from memfw.core.repository import Repository
    from memfw.index import MemoryIndex
    from memfw.mcp.server import Gateway
    from memfw.notes import HashEmbeddingProvider
    from tests.conversation_fixtures import standard_rows, write_store

    counter = iter(range(1, 1_000_000))
    repository = Repository(
        workdir / "vault", clock=lambda: T0, id_factory=lambda: f"id-{next(counter):06d}"
    )
    repository.init()
    for memory_id, title, body, source, tags in VAULT:
        repository.add(title, body, memory_id=memory_id, source=source, tags=list(tags))
    for memory_id, title, body, about in CONCEPT_VAULT:
        repository.add(title, body, memory_id=memory_id, about=list(about))
    repository.add(
        "Service port unknown",
        "The memories disagree on the service port.",
        memory_id="q-port",
        memory_type="open-question",
        about=["concept:service-port"],
        open_question={
            "question": "Which service port is used?",
            "conflicts": ["m-port-a", "m-legacy"],
        },
    )
    MemoryIndex(repository.index_path).rebuild(repository)
    store = write_store(workdir / "data" / "conversations.sqlite", standard_rows())
    config = repository.read_config()
    config["conversation_store"] = {"path": str(store)}
    if payload.get("share"):
        config["conversation_share"] = {"enabled": True}
    if payload.get("optout"):
        config["conversation_optout"] = {"agents": list(payload["optout"])}
    if payload.get("history"):
        logs = workdir / "logs"
        logs.mkdir()
        for session in ("s1", "s2"):
            lines = [line for line in HISTORY_LINES if line["session_id"] == session]
            (logs / f"{session}.jsonl").write_text(
                "".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8"
            )
        config["history_sources"] = {"logs": str(logs)}
    repository.configure(config)
    root = repository.root
    embedder = HashEmbeddingProvider(dimension=64) if payload.get("embedder") else None
    ids = iter(range(1, 1_000_000))
    import memfw.dream.stage

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> Any:
            return T0 if tz is not None else T0.replace(tzinfo=None)

    # Vaults the server opens use the default clock; staged dreams stamp the wall clock.
    with (
        mock.patch.dict(Repository.__init__.__kwdefaults__, {"clock": lambda: T0}),
        mock.patch.object(memfw.dream.stage, "datetime", _FrozenDatetime),
        mock.patch.object(uuid, "uuid4", lambda: uuid.UUID(int=next(ids))),
        mock.patch.object(
            time, "gmtime", lambda *_: time.struct_time((2026, 1, 1, 0, 0, 0, 3, 1, 0))
        ),
    ):
        gateway = Gateway(
            root,
            tenant=payload.get("tenant", ""),
            entity_enabled=False,
            allowed_sources=tuple(payload.get("allowed_sources", ())),
            embedder=embedder,
            app=payload.get("app", ""),
        )
        gateway.dream_clock = lambda: T0
        sent: list[Any] = []
        gateway.respond = sent.append  # type: ignore[method-assign]
        history_ref = "s1:none"
        for message in all_tool_messages():
            text = json.dumps(message).replace("$HISTORY_REF", history_ref)
            before = len(sent)
            gateway.handle(json.loads(text))
            for item in sent[before:]:
                for block in item.get("result", {}).get("content", []):
                    try:
                        block["text"] = _mask_timing(json.loads(block["text"]))
                    except (TypeError, ValueError):
                        continue
                    results = (
                        block["text"].get("results") if isinstance(block["text"], dict) else None
                    )
                    if (
                        item.get("id") == "history_search"
                        and history_ref == "s1:none"
                        and results
                        and isinstance(results[0], dict)
                        and results[0].get("ref")
                    ):
                        history_ref = str(results[0]["ref"])
    text = "\n".join(
        json.dumps(item, ensure_ascii=False, sort_keys=True, indent=1) for item in sent
    )
    return text.replace(str(root), "<vault>").replace(str(workdir), "<workdir>")


def dream_run(payload: dict[str, Any], workdir: Path) -> str:
    """One staged dream over a small vault; its result and staged files (U3).

    ``payload["memories"]`` is a list of ``[id, title, body, about, tenant]``;
    ``payload["tenant"]`` the dream's tenant. The server-clock ``recorded_at``
    of the staged files is masked, and the workdir path replaced.
    """

    from memfw.core.repository import Repository
    from memfw.dream import DreamOptions, run_dream

    repository = Repository(workdir / "vault", clock=lambda: T0)
    repository.init()
    for memory_id, title, body, about, tenant in payload["memories"]:
        repository.add(title, body, memory_id=memory_id, about=list(about), tenant=tenant)
    now = datetime(2026, 3, 1, tzinfo=UTC)
    result = run_dream(
        repository.root,
        now=now,
        options=DreamOptions(tenant=payload.get("tenant", ""), dream_id="dream-probe"),
    )
    staged = repository.root / str(result.staged)
    files = {}
    for path in sorted(staged.iterdir()):
        lines = path.read_text(encoding="utf-8").splitlines()
        files[path.name] = [line for line in lines if "recorded_at" not in line]
    text = json.dumps({"result": result.as_dict(), "files": files}, sort_keys=True, indent=1)
    return text.replace(str(workdir), "<workdir>")
