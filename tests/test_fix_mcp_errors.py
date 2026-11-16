"""Regression tests: MCP errors never carry absolute vault paths."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from memfw.core.models import Layer
from memfw.core.repository import Repository
from memfw.index import MemoryIndex
from memfw.mcp.server import Gateway
from memfw.related import RelatedIndex


def _scoped_vault(tmp_path: Path) -> Repository:
    repository = Repository(tmp_path / "vault")
    repository.init()
    repository.add(
        "alpha manual",
        "alpha corpus keyword manual note",
        memory_id="mem-alpha",
        tenant="tenant-a",
        source="manual",
        tags=["corpus"],
        layer=Layer.ANCHOR,
    )
    repository.add(
        "beta nas",
        "alpha corpus keyword nas secret",
        memory_id="mem-beta",
        tenant="tenant-a",
        source="nas",
        tags=["corpus"],
        layer=Layer.ANCHOR,
    )
    repository.add(
        "gamma other tenant",
        "alpha corpus keyword other tenant",
        memory_id="mem-gamma",
        tenant="tenant-b",
        source="manual",
        tags=["corpus"],
        layer=Layer.ANCHOR,
    )
    MemoryIndex(repository.index_path).rebuild(repository)
    RelatedIndex(repository.root / ".memfw-related.sqlite").rebuild_memory(repository)
    return repository


# --- S4: errors carrying absolute vault paths do not reach clients ---------


def _break_a_concept(root: Path) -> None:
    directory = root / "concepts" / "thing"
    directory.mkdir(parents=True)
    (directory / "x.md").write_text("---\ntitle: x\n---\nbody\n", encoding="utf-8")


@pytest.mark.parametrize("mode", ["fts", "multihop", "hybrid"])
def test_concept_errors_do_not_leak_absolute_paths(
    tmp_path: Path, mode: str, capsys: pytest.CaptureFixture[str]
) -> None:
    repository = _scoped_vault(tmp_path)
    _break_a_concept(repository.root)
    gateway = Gateway(repository.root, tenant="tenant-a")
    payload = gateway.search("corpus", limit=5, mode=mode)
    text = json.dumps(payload)
    assert str(tmp_path) not in text
    assert str(tmp_path.resolve()) not in text
    assert payload["errors"], "the broken concept is still reported"
    if mode == "fts":
        assert str(tmp_path.resolve().name) not in text


def test_tool_call_errors_do_not_leak_absolute_paths(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    gateway = Gateway(root)
    sent: list[dict[str, object]] = []

    def boom(_args: dict[str, object]) -> object:
        raise RuntimeError(f"cannot open {root.resolve()}/secret/thing.md")

    gateway.handlers = lambda: {"memory_status": boom}  # type: ignore[method-assign]
    gateway.respond = sent.append  # type: ignore[method-assign]
    gateway.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "memory_status", "arguments": {}},
        }
    )
    text = json.dumps(sent)
    assert sent and sent[0]["result"]["isError"] is True  # type: ignore[index]
    assert str(tmp_path) not in text and str(tmp_path.resolve()) not in text
    assert "secret/thing.md" in text
