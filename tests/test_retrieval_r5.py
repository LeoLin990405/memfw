"""R5: delete requests and soft delete. Hard delete stays a plan."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from memfw.core.repository import Repository
from memfw.index import MemoryIndex
from memfw.mcp.server import Gateway
from memfw.retrieval.forget import apply_soft_delete, hard_delete_plan, projection_residue
from scripts.memfw_readonly_service import ServiceError, forget_request

NOW = datetime(2026, 4, 1, tzinfo=UTC)


def _repo(tmp_path: Path) -> Repository:
    repository = Repository(tmp_path / "vault")
    repository.init()
    return repository


def test_forget_request_does_not_erase_the_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MEMFW_TENANT", "alpha")
    repository = _repo(tmp_path)
    repository.add("Title", "body text", memory_id="mem-a", tenant="alpha")
    path = repository.root / "memories" / "mem-a.md"
    before = path.read_bytes()
    result = forget_request(repository.root, {"proposal_id": "del-1", "memory_id": "mem-a"})
    assert result["deleted"] is False
    assert result["authority"] is False
    assert result["kind"] == "delete-request"
    assert result["file_present"] is True
    assert path.read_bytes() == before


def test_client_tenant_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEMFW_TENANT", "alpha")
    root = _repo(tmp_path).root
    with pytest.raises(ServiceError) as caught:
        forget_request(root, {"proposal_id": "del-2", "memory_id": "mem-a", "tenant": "beta"})
    assert caught.value.status == 400
    assert caught.value.message == "tenant is server-assigned"


def test_soft_delete_leaves_no_retrieval_residue_and_no_cross_tenant_leak(tmp_path: Path) -> None:
    repository = _repo(tmp_path)
    repository.add("Alpha", "alpha parking bulletin", memory_id="alpha-doc", tenant="alpha")
    repository.add("Beta", "beta parking bulletin", memory_id="beta-doc", tenant="beta")
    MemoryIndex(repository.index_path).rebuild(repository)
    with pytest.raises(ValueError, match="not visible"):
        apply_soft_delete(repository, "beta-doc", tenant="alpha", now=NOW)
    apply_soft_delete(repository, "alpha-doc", tenant="alpha", now=NOW)
    alpha = Gateway(repository.root, tenant="alpha", entity_enabled=False)
    beta = Gateway(repository.root, tenant="beta", entity_enabled=False)
    alpha_ids = {item["id"] for item in alpha.search("parking", limit=10, mode="fts")["results"]}
    beta_ids = {item["id"] for item in beta.search("parking", limit=10, mode="fts")["results"]}
    assert "alpha-doc" not in alpha_ids
    assert "beta-doc" not in alpha_ids
    assert beta_ids == {"beta-doc"}
    assert projection_residue(repository) == []


def test_a_missing_open_file_is_residue(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from memfw.cli import app

    repository = _repo(tmp_path)
    repository.add("Alpha", "alpha parking bulletin", memory_id="alpha-doc", tenant="alpha")
    MemoryIndex(repository.index_path).rebuild(repository)
    (repository.root / "memories" / "alpha-doc.md").unlink()
    assert projection_residue(repository) == ["alpha-doc"]
    result = CliRunner().invoke(app, ["projection-residue", "--root", str(repository.root)])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["residue"] == ["alpha-doc"]


def test_hard_delete_plan_is_not_wired() -> None:
    plan = hard_delete_plan("mem-a", content_hash="sha256:aa", previous_hash="sha256:bb")
    assert plan["implemented"] is False
    assert plan["keep"] == ["content_hash", "previous_hash"]
    assert plan["blank_body"] is True
    assert plan["tombstone_event"] is True
    assert plan["verify"] == "link_only"
    root = Path(__file__).resolve().parents[1]
    service = (root / "scripts" / "memfw_readonly_service.py").read_text(encoding="utf-8")
    assert "hard_delete_plan" not in service
    capture = (root / "src" / "memfw" / "capture" / "__init__.py").read_text(encoding="utf-8")
    assert 'hashlib.sha256(content).hexdigest() != record["content_hash"]' in capture
