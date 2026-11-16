"""U4: the corroboration projection (rebuildable, read-only on the vault)."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from memfw.cli import app
from memfw.conversations.policy import SharePolicy
from memfw.core.epistemic import EXPERIENCE_DISCLAIMER
from memfw.core.models import EventKind
from memfw.core.repository import Repository
from memfw.corroboration import (
    CLAIMS_REPORT_KIND,
    CorroborationError,
    collect,
    corroboration_for,
    parse_conv_ref,
    projection_path,
    rebuild,
)
from memfw.dream.claims import DreamClaimStager
from memfw.dream.model import Candidate, EvidenceRef
from memfw.dream.stage import stage
from memfw.ports.claims import ClaimEvidence, StagedClaim

T0 = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
SHARE: dict[str, Any] = {
    "conversation_share": {
        "enabled": True,
        "apps": {
            "cursor": {"readable": False},
            "grok": {"can_read": False},
            "kimi": {"can_read": ["claude"]},
        },
    },
    "conversation_optout": {"agents": ["stepfun"]},
}


def _vault(tmp_path: Path, share: dict[str, Any] | None = None) -> Repository:
    repository = Repository(tmp_path / "vault", clock=lambda: T0)
    repository.init()
    config = repository.read_config()
    config.update(SHARE if share is None else share)
    repository.configure(config)
    return repository


def _rules(repository: Repository) -> SharePolicy:
    return SharePolicy.from_config(repository.read_config())


def _claims(repository: Repository, batch: str, claim: str, *refs: tuple[str, str]) -> None:
    """Stage ``claim`` the way the conversation worker does, one turn per ref."""

    evidence = tuple(
        ClaimEvidence(ref=f"conv:{ref}", session_id=ref.split(":")[1].split("@")[0], timestamp=ts)
        for ref, ts in refs
    )
    DreamClaimStager().stage_claims(
        repository.root,
        batch,
        [StagedClaim(claim=claim, oid="conversation-x", evidence=evidence)],
        {"kind": CLAIMS_REPORT_KIND},
        created_at=T0.isoformat(),
    )


def _experience(repository: Repository, related: list[str], evidence: list[str], **kw: Any) -> str:
    document = repository.add(
        "Reviewed point",
        EXPERIENCE_DISCLAIMER + "\n\nran the synthetic checklist",
        memory_type="experience-point",
        epistemic="experience",
        related=related,
        evidence=evidence,
        experience={
            "task": "run the synthetic checklist",
            "conditions": ["fixture vault"],
            "actions": ["run the checklist"],
            "review": {"reviewer": "synthetic-reviewer", "verdict": "accepted"},
        },
        **kw,
    )
    return document.id


def _all(repository: Repository, memory_id: str, caller: str = "operator") -> dict[str, Any]:
    answer = corroboration_for(
        [memory_id],
        caller_app=caller,
        rules=_rules(repository),
        path=projection_path(repository.root),
    )
    return answer[memory_id].as_dict()


def test_parse_conv_ref() -> None:
    assert parse_conv_ref("conv:Claude:S1@20") == ("claude", "S1")
    assert parse_conv_ref("conv:codex:abc#turn3") == ("codex", "abc")
    assert parse_conv_ref("claude:s1@3") is None
    assert parse_conv_ref("conv:claude") is None
    assert parse_conv_ref("conv:claude:../x") is None


# --- independence ----------------------------------------------------------------


def test_same_app_same_session_counts_once(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    memory = repository.add(
        "Uploads retry three times",
        "body",
        evidence=["conv:claude:s1@1", "conv:claude:s1@9", "conv:Claude:S1#turn2", "pr:12"],
    )
    _claims(
        repository,
        "conv-a",
        "uploads retry three times.",
        ("claude:s1@40", "2026-02-01T10:00:00Z"),
        ("claude:s1@41", "2026-02-01T10:05:00Z"),
        ("codex:s2@3", "2026-02-02T09:00:00+00:00"),
    )
    rebuild(repository, _rules(repository))
    shown = _all(repository, memory.id)
    # S1 and s1 are different session ids (ids are case-sensitive); apps are not.
    assert shown["apps"] == ["claude", "codex"]
    assert shown["sessions"] == ["claude:S1", "claude:s1", "codex:s2"]
    assert shown["count"] == 3
    assert shown["first_seen"] == "2026-02-01T10:00:00.000000+00:00"
    assert shown["last_seen"] == T0.isoformat(timespec="microseconds")


def test_self_derived_mentions_are_excluded(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    memory = repository.add("Cache keys include tenant", "body")
    # Derived: an experience point that cites the memory itself in its evidence.
    _experience(repository, [memory.id], [f"memory:{memory.id}", "conv:claude:s3@1"])
    # Derived: a dream candidate that names the memory (verify output).
    stage(
        repository.root,
        "dream-verify",
        [
            Candidate(
                operation="verify",
                action="confirm",
                memory_ids=[memory.id],
                reason="Cache keys include tenant",
                evidence=[EvidenceRef(ref="conv:codex:s4@2", polarity="support")],
            )
        ],
        {"kind": "dream"},
        created_at=T0.isoformat(),
    )
    # Independent: an experience point that only relates to it.
    _experience(repository, [memory.id], ["conv:kimi:s9@4"])
    rebuild(repository, _rules(repository))
    shown = _all(repository, memory.id)
    assert shown["sessions"] == ["kimi:s9"]
    assert shown["count"] == 1


def test_mentions_after_injection_are_excluded(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    memory = repository.add("Deploys freeze on Fridays", "body")
    repository.capture_event(
        EventKind.MEMORY_ACCESS,
        actor="mechanism",
        details={"memory_id": memory.id, "session_id": "s5", "source": "codex"},
        timestamp=T0 + timedelta(hours=1),
    )
    _claims(
        repository,
        "conv-b",
        "Deploys freeze on Fridays",
        ("codex:s5@1", (T0 + timedelta(minutes=30)).isoformat()),  # before: counts
        ("codex:s5@9", (T0 + timedelta(hours=2)).isoformat()),  # after: restated
        ("codex:s6@1", (T0 + timedelta(hours=2)).isoformat()),  # other session
        ("claude:s5@1", (T0 + timedelta(hours=2)).isoformat()),  # other app
    )
    rebuild(repository, _rules(repository))
    data = collect(repository, _rules(repository))[memory.id]
    assert (
        data[("codex", "s5")]
        == ((T0 + timedelta(minutes=30)).isoformat(timespec="microseconds"),) * 2
    )
    assert sorted(data) == [("claude", "s5"), ("codex", "s5"), ("codex", "s6")]


def test_injection_without_app_excludes_the_whole_session(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    memory = repository.add("Deploys freeze on Fridays", "body")
    repository.capture_event(
        EventKind.MEMORY_ACCESS,
        actor="mechanism",
        details={"memory_id": memory.id, "session_id": "s5"},
        timestamp=T0,
    )
    _claims(repository, "conv-c", "Deploys freeze on Fridays", ("codex:s5@9", None))
    assert collect(repository, _rules(repository)) == {}


def test_generated_provenance_is_excluded(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    distilled = repository.add(
        "Distilled lesson", "body", provenance="dream-distilled", evidence=["conv:claude:s1@1"]
    )
    memory = repository.add("Retry budget is five", "body")
    _experience(repository, [memory.id], ["conv:codex:s2@1"], provenance="agent")
    # A dream candidate that is not conversation-worker output, matching by text.
    stage(
        repository.root,
        "dream-distill",
        [
            Candidate(
                operation="distill",
                action="distill",
                memory_ids=[],
                reason="Retry budget is five",
                evidence=[EvidenceRef(ref="conv:kimi:s3@2", polarity="support")],
            )
        ],
        {"kind": "dream"},
        created_at=T0.isoformat(),
    )
    rebuild(repository, _rules(repository))
    assert _all(repository, distilled.id)["count"] == 0
    assert _all(repository, memory.id)["count"] == 0


def test_contradicting_claim_evidence_is_not_a_mention(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    memory = repository.add("Retry budget is five", "body")
    stage(
        repository.root,
        "conv-d",
        [
            Candidate(
                operation="distill",
                action="distill",
                memory_ids=[],
                reason="Retry budget is five",
                evidence=[EvidenceRef(ref="conv:kimi:s3@2", polarity="contradict")],
            )
        ],
        {"kind": CLAIMS_REPORT_KIND},
        created_at=T0.isoformat(),
    )
    assert memory.id not in collect(repository, _rules(repository))


# --- share policy ------------------------------------------------------------------


def _shared(tmp_path: Path) -> tuple[Repository, str]:
    repository = _vault(tmp_path)
    memory = repository.add(
        "Shared fact",
        "body",
        evidence=[
            "conv:claude:c1@1",
            "conv:codex:x1@1",
            "conv:grok:g1@1",
            "conv:kimi:k1@1",
            "conv:cursor:hidden1@1",
            "conv:stepfun:out1@1",
        ],
    )
    rebuild(repository, _rules(repository))
    return repository, memory.id


def test_hidden_and_opted_out_apps_are_never_written(tmp_path: Path) -> None:
    repository, _memory_id = _shared(tmp_path)
    with sqlite3.connect(projection_path(repository.root)) as connection:
        apps = {row[0] for row in connection.execute("SELECT app FROM mentions")}
    assert apps == {"claude", "codex", "grok", "kimi"}
    raw = projection_path(repository.root).read_bytes()
    assert b"cursor" not in raw and b"hidden1" not in raw
    assert b"stepfun" not in raw and b"out1" not in raw


def test_caller_trimming(tmp_path: Path) -> None:
    repository, memory_id = _shared(tmp_path)
    operator = _all(repository, memory_id)
    assert operator["apps"] == ["claude", "codex", "grok", "kimi"]
    assert operator["count"] == 4
    grok = _all(repository, memory_id, "grok")
    assert grok == {
        "count": 1,
        "apps": ["grok"],
        "sessions": ["grok:g1"],
        "first_seen": T0.isoformat(timespec="microseconds"),
        "last_seen": T0.isoformat(timespec="microseconds"),
    }
    kimi = _all(repository, memory_id, "kimi")
    assert kimi["apps"] == ["claude", "kimi"] and kimi["count"] == 2
    # Hidden (readable: false) still may read others; its own were never written.
    assert _all(repository, memory_id, "cursor")["apps"] == ["claude", "codex", "grok", "kimi"]
    # An opted-out caller reads nothing, and its own were never written either.
    assert _all(repository, memory_id, "stepfun")["count"] == 0
    for caller in ("operator", "grok", "kimi", "codex", "cursor", "stepfun"):
        text = json.dumps(_all(repository, memory_id, caller))
        assert "cursor" not in text and "stepfun" not in text


def test_policy_tightened_after_rebuild_still_trims(tmp_path: Path) -> None:
    repository, memory_id = _shared(tmp_path)
    config = repository.read_config()
    config["conversation_share"]["apps"]["codex"] = {"readable": False}
    repository.configure(config)
    assert "codex" not in _all(repository, memory_id)["apps"]
    assert _all(repository, memory_id)["count"] == 3


def test_share_off_shows_only_own(tmp_path: Path) -> None:
    repository, memory_id = _shared(tmp_path)
    config = repository.read_config()
    config["conversation_share"]["enabled"] = False
    repository.configure(config)
    assert _all(repository, memory_id, "claude")["apps"] == ["claude"]
    assert _all(repository, memory_id)["count"] == 0


def test_unknown_or_missing_ids_are_empty(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    answer = corroboration_for(
        ["nope"],
        caller_app="claude",
        rules=_rules(repository),
        path=projection_path(repository.root),
    )
    assert answer["nope"].as_dict() == {
        "count": 0,
        "apps": [],
        "sessions": [],
        "first_seen": None,
        "last_seen": None,
    }


# --- determinism, schema, vault untouched ----------------------------------------


def _vault_bytes(repository: Repository) -> dict[str, bytes]:
    return {
        path.relative_to(repository.root).as_posix(): path.read_bytes()
        for path in sorted(repository.root.rglob("*"))
        if path.is_file() and path.name != projection_path(repository.root).name
    }


def test_rebuild_is_byte_identical_and_read_only(tmp_path: Path) -> None:
    repository, memory_id = _shared(tmp_path)
    _claims(repository, "conv-e", "Shared fact", ("codex:x2@1", "2026-01-01T00:00:00Z"))
    before = _vault_bytes(repository)
    path = projection_path(repository.root)
    rebuild(repository, _rules(repository))
    first = path.read_bytes()
    rebuild(repository, _rules(repository))
    assert path.read_bytes() == first
    path.unlink()
    rebuild(repository, _rules(repository))
    assert path.read_bytes() == first
    assert _vault_bytes(repository) == before
    assert not list(repository.root.glob(".corroboration*"))
    assert _all(repository, memory_id)["count"] == 5


def test_schema_has_no_text_columns(tmp_path: Path) -> None:
    repository, _memory_id = _shared(tmp_path)
    with sqlite3.connect(projection_path(repository.root)) as connection:
        tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master")]
        columns = {
            table: [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]
            for table in tables
            if not table.startswith("sqlite_")
        }
    assert columns == {
        "meta": ["key", "value"],
        "mentions": ["memory_id", "app", "session_id", "first_seen", "last_seen"],
    }
    # The body, titles and claims never reach the file.
    raw = projection_path(repository.root).read_bytes()
    assert b"Shared fact" not in raw and b"body" not in raw


def test_refuses_to_write_inside_memories(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    with pytest.raises(CorroborationError):
        rebuild(repository, _rules(repository), repository.memories_dir / "x.sqlite3")


# --- CLI ---------------------------------------------------------------------------


def test_cli_rebuild_and_show(tmp_path: Path) -> None:
    repository, memory_id = _shared(tmp_path)
    projection_path(repository.root).unlink()
    runner = CliRunner()
    root = str(repository.root)
    built = runner.invoke(app, ["corroboration", "rebuild", "--root", root])
    assert built.exit_code == 0, built.output
    assert json.loads(built.stdout) == {
        "path": "corroboration.sqlite3",
        "memories": 1,
        "mentions": 4,
    }
    shown = runner.invoke(
        app, ["corroboration", "show", memory_id, "--as-app", "grok", "--root", root]
    )
    assert shown.exit_code == 0, shown.output
    payload = json.loads(shown.stdout)
    assert payload["id"] == memory_id
    assert payload["corroboration"]["apps"] == ["grok"]
    assert payload["corroboration"]["count"] == 1
