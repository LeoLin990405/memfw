"""CLI commands fail with a clean message and a non-zero exit, never a traceback.

Each test pins a bad input the audit found: a command either applied it or
crashed with an uncaught exception. Every vault here lives under ``tmp_path``.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner, Result

from memfw.cli import app
from memfw.core.errors import IntegrityError
from memfw.core.models import EventKind
from memfw.core.repository import Repository

runner = CliRunner()


def _invoke(*args: str) -> Result:
    return runner.invoke(app, list(args))


def _assert_clean_failure(result: Result) -> None:
    """A non-zero exit raised through typer, not an uncaught exception."""
    assert result.exit_code != 0, result.output
    assert result.exception is None or isinstance(result.exception, SystemExit), repr(
        result.exception
    )
    assert "Traceback" not in result.output


def _tree(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


@pytest.fixture
def vault(tmp_path: Path) -> Repository:
    repository = Repository(tmp_path / "vault")
    repository.init()
    repository.add("Hot memory", "Body of the hot memory", memory_id="hot-memory")
    return repository


def _payload(memory_id: str, operation: str, changes: dict[str, Any]) -> str:
    return json.dumps({"memory_id": memory_id, "operation": operation, "changes": changes})


def _record(repository: Repository, changes: dict[str, Any] | None = None) -> tuple[str, str]:
    changes = changes if changes is not None else {"title": "Renamed"}
    payload = _payload("hot-memory", "update", changes)
    event = repository.record_proposal(
        memory_id="hot-memory",
        operation="update",
        changes=changes,
        rationale="rename",
        agent_id="agent",
        signature="sig",
    )
    return event.event_id, payload


# --- item 4: candidate proposal_id must be a non-blank string -----------------


def _candidate(root: Path, proposal_id: Any, *, related: list[str] | None = None) -> Path:
    body = "candidate body"
    path = root / "_candidates" / "remember" / "cand-1.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    frontmatter: dict[str, Any] = {
        "title": "cand-1",
        "proposal_id": proposal_id,
        "source": "conductor",
        "tenant": "",
        "tags": [],
        "related": related or [],
        "created_at": "2026-11-06T00:00:00Z",
        "authority": False,
        "content_hash": "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest(),
    }
    header = yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False)
    path.write_text(f"---\n{header}---\n\n{body}\n", encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("proposal_id", "error"),
    [
        ("", "missing_proposal_id"),
        ("   ", "missing_proposal_id"),
        (False, "invalid_proposal_id"),
        (0, "invalid_proposal_id"),
        (["p-1"], "invalid_proposal_id"),
    ],
)
@pytest.mark.parametrize("mode", ["--apply", "--dry-run"])
def test_candidate_apply_rejects_blank_or_non_string_proposal_id(
    vault: Repository, proposal_id: Any, error: str, mode: str
) -> None:
    candidate = _candidate(vault.root, proposal_id)
    before = len(vault.list())

    result = _invoke("candidate-apply", str(candidate), "--root", str(vault.root), mode)

    _assert_clean_failure(result)
    payload = json.loads(result.stdout)
    assert payload["error"] == error
    if error == "missing_proposal_id":
        assert "/stage" in payload["message"] and payload["title"]
    assert len(vault.list()) == before


@pytest.mark.parametrize("mode", ["--apply", "--dry-run"])
def test_candidate_apply_unknown_related_id_is_a_clean_error(vault: Repository, mode: str) -> None:
    candidate = _candidate(vault.root, "p-related", related=["no-such-memory"])
    before = len(vault.list())

    result = _invoke("candidate-apply", str(candidate), "--root", str(vault.root), mode)

    _assert_clean_failure(result)
    assert "no-such-memory" in result.output
    assert len(vault.list()) == before


# --- item 6: approve-proposal --dry-run checks the proposal and payload --------


def test_approve_proposal_dry_run_unknown_proposal_fails(vault: Repository) -> None:
    result = _invoke(
        "approve-proposal",
        "no-such-proposal",
        "--payload",
        _payload("hot-memory", "update", {"title": "x"}),
        "--root",
        str(vault.root),
        "--dry-run",
    )
    _assert_clean_failure(result)
    assert "payload_valid" not in result.output


def test_approve_proposal_dry_run_mismatched_payload_is_invalid(vault: Repository) -> None:
    proposal_id, _ = _record(vault)
    before = _tree(vault.root)

    result = _invoke(
        "approve-proposal",
        proposal_id,
        "--payload",
        _payload("hot-memory", "update", {"title": "something else"}),
        "--root",
        str(vault.root),
        "--dry-run",
    )

    _assert_clean_failure(result)
    assert _tree(vault.root) == before


def test_approve_proposal_dry_run_valid_payload_passes_without_writing(
    vault: Repository,
) -> None:
    proposal_id, payload = _record(vault)
    before = _tree(vault.root)

    result = _invoke(
        "approve-proposal",
        proposal_id,
        "--payload",
        payload,
        "--root",
        str(vault.root),
        "--dry-run",
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {"proposal_id": proposal_id, "payload_valid": True}
    assert _tree(vault.root) == before


def test_approve_proposal_dry_run_stale_proposal_fails(vault: Repository) -> None:
    proposal_id, payload = _record(vault)
    vault.update("hot-memory", body="changed after the proposal")

    result = _invoke(
        "approve-proposal",
        proposal_id,
        "--payload",
        payload,
        "--root",
        str(vault.root),
        "--dry-run",
    )

    _assert_clean_failure(result)


def test_approve_proposal_dry_run_already_decided_fails(vault: Repository) -> None:
    proposal_id, payload = _record(vault)
    vault.decide_proposal(proposal_id, payload=json.loads(payload))

    result = _invoke(
        "approve-proposal",
        proposal_id,
        "--payload",
        payload,
        "--root",
        str(vault.root),
        "--dry-run",
    )

    _assert_clean_failure(result)


# --- item 7: clean errors instead of tracebacks -------------------------------


@pytest.mark.parametrize("mode", ["--apply", "--dry-run"])
def test_supersede_unknown_memory_is_a_clean_error(vault: Repository, mode: str) -> None:
    result = _invoke(
        "supersede",
        "no-such-memory",
        "--title",
        "t",
        "--body",
        "b",
        "--root",
        str(vault.root),
        mode,
    )
    _assert_clean_failure(result)


@pytest.mark.parametrize("mode", ["--apply", "--dry-run"])
def test_layer_set_unknown_memory_is_a_clean_error(vault: Repository, mode: str) -> None:
    result = _invoke("layer-set", "no-such-memory", "anchor", "--root", str(vault.root), mode)
    _assert_clean_failure(result)


def test_cold_archive_apply_without_cold_memories_is_a_clean_error(vault: Repository) -> None:
    before = _tree(vault.root)
    result = _invoke("cold-archive", "arch-1", "--root", str(vault.root), "--apply")
    _assert_clean_failure(result)
    assert "cold" in result.output.lower()
    assert _tree(vault.root) == before


@pytest.mark.parametrize("payload", ["[]", "null", '"text"', "42"])
@pytest.mark.parametrize("mode", ["--apply", "--dry-run"])
def test_record_proposal_non_object_payload_is_a_clean_error(
    vault: Repository, payload: str, mode: str
) -> None:
    result = _invoke(
        "record-proposal",
        "hot-memory",
        "--operation",
        "update",
        "--rationale",
        "r",
        "--payload",
        payload,
        "--agent-id",
        "agent",
        "--signature",
        "sig",
        "--root",
        str(vault.root),
        mode,
    )
    _assert_clean_failure(result)


def test_record_proposal_unsupported_fields_is_a_clean_error(vault: Repository) -> None:
    before = len(vault.events())
    result = _invoke(
        "record-proposal",
        "hot-memory",
        "--operation",
        "update",
        "--rationale",
        "r",
        "--payload",
        _payload("hot-memory", "update", {"not_a_field": 1}),
        "--agent-id",
        "agent",
        "--signature",
        "sig",
        "--root",
        str(vault.root),
        "--apply",
    )
    _assert_clean_failure(result)
    assert "unsupported" in result.output.lower()
    assert len(vault.events()) == before


def test_record_proposal_non_object_changes_is_a_clean_error(vault: Repository) -> None:
    result = _invoke(
        "record-proposal",
        "hot-memory",
        "--operation",
        "update",
        "--rationale",
        "r",
        "--payload",
        json.dumps({"memory_id": "hot-memory", "operation": "update", "changes": ["title"]}),
        "--agent-id",
        "agent",
        "--signature",
        "sig",
        "--root",
        str(vault.root),
        "--apply",
    )
    _assert_clean_failure(result)


@pytest.mark.parametrize("payload", ["[]", "null", '"text"', "{not json"])
@pytest.mark.parametrize("mode", ["--apply", "--dry-run"])
def test_approve_proposal_non_object_payload_is_a_clean_error(
    vault: Repository, payload: str, mode: str
) -> None:
    proposal_id, _ = _record(vault)
    before = _tree(vault.root)
    result = _invoke(
        "approve-proposal", proposal_id, "--payload", payload, "--root", str(vault.root), mode
    )
    _assert_clean_failure(result)
    assert _tree(vault.root) == before


@pytest.mark.parametrize(
    "content", [None, "entries: [unclosed\n", "just a string\n", "entries:\n  - 7\n"]
)
def test_concepts_import_seed_missing_or_malformed_file_is_a_clean_error(
    tmp_path: Path, content: str | None
) -> None:
    seed = tmp_path / "seed.yaml"
    if content is not None:
        seed.write_text(content, encoding="utf-8")
    result = _invoke("concepts", "import-seed", "--seed", str(seed))
    _assert_clean_failure(result)


def test_concepts_import_seed_non_utf8_file_is_a_clean_error(tmp_path: Path) -> None:
    seed = tmp_path / "seed.yaml"
    seed.write_bytes(b"\xff\xfe entries")
    result = _invoke("concepts", "import-seed", "--seed", str(seed))
    _assert_clean_failure(result)


# --- item 9: unknown targets are refused before any event is appended ---------


@pytest.mark.parametrize("operation", ["update", "delete", "layer_change", "tier_change"])
def test_record_proposal_for_unknown_memory_appends_no_event(
    vault: Repository, operation: str
) -> None:
    from memfw.core.models import EventKind

    changes: dict[str, Any] = {} if operation == "delete" else {"title": "x"}
    before = len(vault.events())
    result = _invoke(
        "record-proposal",
        "no-such-memory",
        "--operation",
        operation,
        "--rationale",
        "r",
        "--payload",
        _payload("no-such-memory", operation, changes),
        "--agent-id",
        "agent",
        "--signature",
        "sig",
        "--root",
        str(vault.root),
        "--apply",
    )
    _assert_clean_failure(result)
    assert len(vault.events()) == before
    assert not any(event.kind == EventKind.AGENT_PROPOSAL for event in vault.events())


def test_record_proposal_dry_run_for_unknown_memory_fails(vault: Repository) -> None:
    result = _invoke(
        "record-proposal",
        "no-such-memory",
        "--operation",
        "update",
        "--rationale",
        "r",
        "--payload",
        _payload("no-such-memory", "update", {"title": "x"}),
        "--agent-id",
        "agent",
        "--signature",
        "sig",
        "--root",
        str(vault.root),
        "--dry-run",
    )
    _assert_clean_failure(result)


def test_repository_record_proposal_rejects_unknown_memory(vault: Repository) -> None:
    from memfw.core.errors import NotFoundError

    before = len(vault.events())
    with pytest.raises(NotFoundError):
        vault.record_proposal(
            memory_id="no-such-memory",
            operation="update",
            changes={"title": "x"},
            rationale="r",
            agent_id="agent",
            signature="sig",
        )
    assert len(vault.events()) == before


def test_rollback_dry_run_unknown_event_fails_like_the_real_run(vault: Repository) -> None:
    real = _invoke("rollback", "no-such-event", "--root", str(vault.root), "--apply")
    dry = _invoke("rollback", "no-such-event", "--root", str(vault.root), "--dry-run")
    _assert_clean_failure(real)
    _assert_clean_failure(dry)
    assert dry.exit_code == real.exit_code


def test_rollback_dry_run_known_event_still_reports_availability(vault: Repository) -> None:
    updated = vault.update("hot-memory", body="second body")
    del updated
    event = [item for item in vault.events() if item.memory_id == "hot-memory"][-1]
    result = _invoke("rollback", event.event_id, "--root", str(vault.root), "--dry-run")
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {"event_id": event.event_id, "available": True}


# --- review M3: one shared validator for approval and its dry run ------------


@pytest.mark.parametrize("changes", [{"layer": "bogus"}, {"tier": "bogus"}, {"title": 5}])
def test_record_proposal_refuses_changes_that_could_never_apply(
    vault: Repository, changes: dict[str, Any]
) -> None:
    events = len(vault.events())
    with pytest.raises(IntegrityError):
        vault.record_proposal(
            memory_id="hot-memory",
            operation="update",
            changes=changes,
            rationale="r",
            agent_id="agent",
            signature="sig",
        )
    assert len(vault.events()) == events


def _legacy_bogus_proposal(repository: Repository, changes: dict[str, Any]) -> tuple[str, str]:
    """An agent proposal appended before record-time validation existed."""
    path = repository._current_path("hot-memory")
    event = repository._append_event(
        EventKind.AGENT_PROPOSAL,
        "hot-memory",
        actor="agent:agent",
        before_hash=hashlib.sha256(path.read_bytes()).hexdigest(),
        details={
            "memory_id": "hot-memory",
            "operation": "update",
            "agent_id": "agent",
            "signature": "sig",
            "proposal_hash": "x",
            "target_hash": repository._payload_hash("hot-memory", "update", changes),
        },
    )
    return event.event_id, _payload("hot-memory", "update", changes)


@pytest.mark.parametrize("changes", [{"layer": "bogus"}, {"tier": "bogus"}, {"title": 5}])
def test_approve_proposal_dry_run_and_apply_agree_on_unappliable_changes(
    vault: Repository, changes: dict[str, Any]
) -> None:
    event_id, payload = _legacy_bogus_proposal(vault, changes)
    before = _tree(vault.root)
    root = str(vault.root)

    dry = _invoke("approve-proposal", event_id, "--payload", payload, "--root", root, "--dry-run")
    real = _invoke("approve-proposal", event_id, "--payload", payload, "--root", root, "--apply")

    _assert_clean_failure(dry)
    _assert_clean_failure(real)
    assert dry.exit_code == real.exit_code == 2
    assert "payload_valid" not in dry.output
    # No human decision was written for a proposal that cannot apply.
    assert not any(e.kind == EventKind.HUMAN_DECISION for e in vault.events())
    assert _tree(vault.root) == before


def test_decide_proposal_uses_the_shared_check(vault: Repository) -> None:
    event_id, payload = _record(vault)
    proposal, _digest, memory_id, operation, changes = vault.check_proposal_approval(
        event_id, json.loads(payload)
    )
    assert (proposal.event_id, memory_id, operation, changes) == (
        event_id,
        "hot-memory",
        "update",
        {"title": "Renamed"},
    )
    vault.decide_proposal(event_id, payload=json.loads(payload))
    assert vault.get("hot-memory").title == "Renamed"


# --- review L7: record-proposal --dry-run is exactly as strict as the real run


@pytest.mark.parametrize(
    "changes", [{"title": "Fine"}, {"nope": 1}, {"layer": "bogus"}, {"title": 5}]
)
def test_record_proposal_dry_run_matches_the_real_run(
    vault: Repository, changes: dict[str, Any]
) -> None:
    args = [
        "record-proposal",
        "hot-memory",
        "--operation",
        "update",
        "--rationale",
        "r",
        "--payload",
        _payload("hot-memory", "update", changes),
        "--agent-id",
        "agent",
        "--signature",
        "sig",
        "--root",
        str(vault.root),
    ]
    dry = _invoke(*args, "--dry-run")
    real = _invoke(*args, "--apply")
    assert dry.exit_code == real.exit_code, (dry.output, real.output)
    if real.exit_code:
        _assert_clean_failure(dry)
        _assert_clean_failure(real)


def test_record_proposal_non_utf8_memory_is_a_clean_error(vault: Repository) -> None:
    vault._current_path("hot-memory").write_bytes(b"---\ntitle: \xff\xfe\n---\n\nbody\n")
    args = [
        "record-proposal",
        "hot-memory",
        "--operation",
        "update",
        "--rationale",
        "r",
        "--payload",
        _payload("hot-memory", "update", {"title": "x"}),
        "--agent-id",
        "agent",
        "--signature",
        "sig",
        "--root",
        str(vault.root),
    ]
    dry = _invoke(*args, "--dry-run")
    real = _invoke(*args, "--apply")
    _assert_clean_failure(dry)
    _assert_clean_failure(real)
    assert dry.exit_code == real.exit_code == 2
