"""MCP remember collisions, tenant isolation, and protocol guards."""

from __future__ import annotations

import io
import json
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from memfw.capture.review import read_candidate, validate_candidate_hash
from memfw.cli import app
from memfw.core.repository import Repository
from memfw.mcp.server import Gateway, main, serve
from memfw.related import RelatedIndex


def _vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    repository = Repository(root)
    repository.init()
    repository.add(title="greenhouse", body="The greenhouse sensor reports 18 degrees.")
    return root


def _candidate_files(root: Path) -> list[Path]:
    directory = root / "_candidates" / "remember"
    return sorted(path for path in directory.glob("*.md") if path.is_file())


def test_remember_same_title_same_second_keeps_both(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        time,
        "gmtime",
        lambda: time.struct_time((2026, 11, 8, 1, 2, 3, 6, 312, 0)),
    )
    root = _vault(tmp_path)
    gateway = Gateway(root)
    first = gateway.remember("alpha body", title="Same Title")
    second = gateway.remember("alpha body", title="Same Title")
    assert first["staged_candidate"] != second["staged_candidate"]
    assert not Path(first["staged_candidate"]).is_absolute()
    assert str(root.resolve()) not in json.dumps(first)
    files = _candidate_files(root)
    assert len(files) == 2
    assert {path.name.split("-")[0] for path in files} == {"20261108T010203Z"}
    texts = [path.read_text(encoding="utf-8") for path in files]
    assert all("alpha body" in text for text in texts)
    assert all(not path.name.startswith(".") for path in files)
    assert list((root / "_candidates" / "remember").glob(".*.tmp")) == []


def test_remember_frontmatter_is_applicable(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    repository = Repository(root)
    repository.init()
    repository.add("Anchor", "cite sources", memory_id="m-anchor", tenant="tenant-a")
    gateway = Gateway(root, tenant="tenant-a")
    payload = gateway.remember(
        "A kept fact",
        title="Kept",
        tags=["kept"],
        related=["m-anchor"],
    )
    relative = payload["staged_candidate"]
    path = root / relative
    candidate = read_candidate(path)
    validate_candidate_hash(candidate)
    assert candidate.proposal_id
    assert candidate.content_hash
    assert candidate.content_hash.startswith("sha256:")
    assert candidate.tenant == "tenant-a"
    assert candidate.tags == ["kept"]
    assert candidate.related == ["m-anchor"]
    applied = CliRunner().invoke(
        app,
        ["candidate-apply", str(path), "--root", str(root), "--apply"],
    )
    assert applied.exit_code == 0, applied.output
    document = next(item for item in Repository(root).list() if item.title == "Kept")
    assert document.body == "A kept fact"
    assert document.tenant == "tenant-a"
    assert document.tags == ["kept"]
    assert document.related == ["m-anchor"]


def test_remember_rejects_content_over_64kib(tmp_path: Path) -> None:
    root = _vault(tmp_path)
    gateway = Gateway(root)
    gateway.remember("a" * (64 * 1024), title="max")
    with pytest.raises(ValueError, match="64 KiB"):
        gateway.remember("b" * (64 * 1024 + 1), title="over")
    files = _candidate_files(root)
    assert len(files) == 1
    assert "b" * 100 not in files[0].read_text(encoding="utf-8")


def test_read_tools_do_not_init_a_missing_root(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    missing = tmp_path / "missing"
    empty = tmp_path / "empty"
    empty.mkdir()
    for root in (missing, empty, tmp_path):
        gateway = Gateway(root)
        with pytest.raises(ValueError, match="vault root"):
            gateway.search("greenhouse")
        with pytest.raises(ValueError, match="vault root"):
            gateway.get("missing")
        with pytest.raises(ValueError, match="vault root"):
            gateway.related("missing")
        with pytest.raises(ValueError, match="vault root"):
            gateway.context()
        with pytest.raises(ValueError, match="vault root"):
            gateway.status()
        with pytest.raises(ValueError, match="vault root"):
            gateway.remember("do not create", title="nope")
    assert not missing.exists()
    assert list(empty.iterdir()) == []
    assert not (tmp_path / "vault.yaml").exists()
    assert not (tmp_path / "memories").exists()


def test_main_requires_a_root(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("MEMFW_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as caught:
        main([])
    assert caught.value.code == 2
    assert not (tmp_path / "vault.yaml").exists()


def test_status_search_and_related_hide_the_vault_root(tmp_path: Path) -> None:
    root = _vault(tmp_path)
    gateway = Gateway(root)
    status = gateway.status()
    found = gateway.search("greenhouse", limit=5, mode="fts")
    memory_id = found["results"][0]["id"]
    related = gateway.related(memory_id)
    context = gateway.context()
    blob = json.dumps({"status": status, "found": found, "related": related, "context": context})
    assert "root" not in status
    assert "root" not in found
    assert "root" not in related
    assert "root" not in context
    assert str(root.resolve()) not in blob


def test_related_refuses_other_tenant_source_and_neighbours(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "vault"
    repository = Repository(root)
    repository.init()
    repository.add("Mine", "alpha fact", memory_id="mine", tenant="tenant-a")
    repository.add("Peer", "alpha peer", memory_id="peer", tenant="tenant-a")
    repository.add("Other", "beta fact", memory_id="other", tenant="tenant-b")
    seen: dict[str, Any] = {}

    def fake_related(
        self: RelatedIndex,
        source: str,
        identifier: str,
        *,
        limit: int = 10,
        include_history: bool = False,
        tenant: str | None = None,
        scope: str | None = None,
    ) -> list[dict[str, Any]]:
        del self, include_history, scope
        seen["source"] = source
        seen["identifier"] = identifier
        seen["tenant"] = tenant
        seen["limit"] = limit
        return [
            {
                "source": "memory",
                "id": "peer",
                "title": "Peer",
                "path": "memories/peer.md",
                "score": 1.0,
                "methods": ["explicit"],
                "evidence": [],
            },
            {
                "source": "memory",
                "id": "other",
                "title": "Other",
                "path": "memories/other.md",
                "score": 3.0,
                "methods": ["lexical"],
                "evidence": [],
            },
            {
                "source": "memory",
                "id": "missing",
                "title": "Missing",
                "path": "memories/missing.md",
                "score": 0.2,
                "methods": ["lexical"],
                "evidence": [],
            },
        ]

    monkeypatch.setattr(RelatedIndex, "related", fake_related)
    gateway = Gateway(root, tenant="tenant-a")
    with pytest.raises(ValueError, match="not visible"):
        gateway.related("other")
    payload = gateway.related("mine", limit=10)
    assert seen == {
        "source": "memory",
        "identifier": "mine",
        "tenant": "tenant-a",
        "limit": 50,
    }
    assert [item["id"] for item in payload["related"]] == ["peer"]
    assert "root" not in payload
    assert str(root.resolve()) not in json.dumps(payload)


def test_related_real_index_does_not_leak_other_tenants(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    repository = Repository(root)
    repository.init()
    repository.add(
        "Alpha",
        "alpha body",
        memory_id="alpha",
        tenant="tenant-a",
        related=["peer"],
    )
    repository.add(
        "Peer",
        "peer body",
        memory_id="peer",
        tenant="tenant-a",
        related=["alpha"],
    )
    repository.add(
        "Beta",
        "beta body",
        memory_id="beta",
        tenant="tenant-b",
        related=["alpha"],
    )
    RelatedIndex(root / ".memfw-related.sqlite").rebuild_memory(repository)
    payload = Gateway(root, tenant="tenant-a").related("alpha")
    assert all(item["id"] != "beta" for item in payload["related"])


def test_related_mcp_hides_target_after_live_tenant_move(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    repository = Repository(root)
    repository.init()
    repository.add(
        "Subject",
        "shared corpus token",
        memory_id="z-subject",
        tenant="tenant-a",
        tags=["shared"],
    )
    repository.add(
        "Secret Title",
        "shared corpus token",
        memory_id="z-target",
        tenant="tenant-a",
        tags=["shared"],
    )
    RelatedIndex(root / ".memfw-related.sqlite").rebuild_memory(repository)
    path = root / "memories" / "z-target.md"
    text = path.read_text(encoding="utf-8")
    assert "tenant: tenant-a" in text
    path.write_text(text.replace("tenant: tenant-a", "tenant: tenant-b", 1), encoding="utf-8")
    payload = Gateway(root, tenant="tenant-a").related("z-subject")
    blob = json.dumps(payload)
    assert "z-target" not in blob
    assert "Secret Title" not in blob


def test_related_mcp_keeps_same_tenant_under_foreign_pressure(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    repository = Repository(root)
    repository.init()
    repository.add(
        "Z subject",
        "shared corpus token",
        memory_id="z-subject",
        tenant="tenant-a",
        tags=["shared"],
    )
    repository.add(
        "Z target",
        "shared corpus token",
        memory_id="z-target",
        tenant="tenant-a",
        tags=["shared"],
    )
    for index in range(21):
        repository.add(
            f"Foreign {index:02d}",
            "shared corpus token",
            memory_id=f"b-foreign-{index:02d}",
            tenant="tenant-b",
            tags=["shared"],
        )
    RelatedIndex(root / ".memfw-related.sqlite").rebuild_memory(repository)
    payload = Gateway(root, tenant="tenant-a").related("z-subject")
    assert [item["id"] for item in payload["related"]] == ["z-target"]


def test_protocol_array_list_params_and_invalid_json(tmp_path: Path, monkeypatch, capsys) -> None:
    root = _vault(tmp_path)
    lines = "\n".join(
        [
            json.dumps([{"jsonrpc": "2.0", "id": 1, "method": "ping"}]),
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": ["memory_status"],
                }
            ),
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": "memory_status", "arguments": []},
                }
            ),
            "[]",
            "null",
            "{",
        ]
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO(lines + "\n"))
    assert serve(root) == 0
    messages = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert messages[0]["id"] == 1
    assert messages[0]["result"] == {}
    assert [item["error"]["code"] for item in messages[1:]] == [
        -32602,
        -32602,
        -32600,
        -32600,
        -32700,
    ]
    assert not (root / "memories" / "new").exists()
