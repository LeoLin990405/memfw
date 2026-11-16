"""A supersede writes a new version of the same memory, in the same scope.

Before this fix ``Repository.supersede`` built the replacement with ``add``
defaults: a memory superseded in tenant ``acme`` came back in the default
tenant, visible to untenanted callers and invisible to its owner, and it also
lost its layer, tier, type, about/applies_to, anchor, group_key and related.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from memfw.cli import app
from memfw.core.epistemic import EXPERIENCE_DISCLAIMER
from memfw.core.errors import IntegrityError
from memfw.core.models import Layer, Tier
from memfw.core.repository import Repository
from memfw.mcp.server import Gateway

runner = CliRunner()


def _vault(tmp_path: Path) -> Repository:
    repository = Repository(tmp_path / "vault")
    repository.init()
    return repository


def _scoped(repository: Repository, **extra: object) -> str:
    peer = repository.add("peer", "A peer note.", tenant="acme")
    old = repository.add(
        "quarry pump schedule",
        "The quarry pump runs at dawn.",
        tenant="acme",
        layer=Layer.CONSOLIDATED,
        tier=Tier.COLD,
        memory_type="note",
        related=[peer.id],
        about=["entity:quarry-pump"],
        applies_to=["site:north"],
        group_key="pumps",
        manually_marked=True,
        source="ops-log",
        tags=["ops", "pump"],
        evidence=["log:2025-01-01"],
        **extra,  # type: ignore[arg-type]
    )
    repository.update(
        old.id,
        actor="human",
        status="confirmed",
        summary="pump at dawn",
        strength=0.9,
        reference_count=7,
        last_access_interval_days=2.0,
        valid_from=datetime(2024, 1, 1, tzinfo=UTC),
    )
    return old.id


def test_replacement_keeps_tenant_and_scope_fields(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    old_id = _scoped(repository)
    old = repository.get(old_id)

    new = repository.supersede(old_id, "quarry pump schedule v2", "The quarry pump runs at noon.")
    stored = repository.get(new.id)

    assert stored.model_dump() == new.model_dump()
    assert new.tenant == "acme"
    assert new.layer == Layer.CONSOLIDATED
    assert new.tier == Tier.COLD
    assert new.type == "note"
    assert new.related == old.related
    assert new.about == ["entity:quarry-pump"]
    assert new.applies_to == ["site:north"]
    assert new.group_key == "pumps"
    assert new.manually_marked is True
    assert new.source == "ops-log"
    assert new.tags == ["ops", "pump"]
    assert new.evidence == ["log:2025-01-01"]
    assert new.provenance == f"supersedes:{old_id}"


def test_replacement_starts_fresh_bookkeeping(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    old_id = _scoped(repository)

    new = repository.supersede(old_id, "v2", "The quarry pump runs at noon.")
    old = repository.get(old_id)

    assert new.id != old_id
    assert new.version == 1
    assert new.status == "unconfirmed"
    assert new.summary == ""
    assert new.strength == 0.5
    assert new.reference_count == 0
    assert new.last_access_interval_days is None
    assert new.valid_from is None  # validity starts at created_at, the handoff
    assert new.valid_to is None
    assert new.superseded_by is None
    assert new.is_stub is False
    assert new.archive_ref is None and new.archived_hash is None
    assert new.created_at == new.updated_at == new.recorded_at
    assert old.valid_to == new.created_at
    assert old.superseded_by == new.id
    assert old.tenant == "acme"


def test_anchored_memory_keeps_anchor_and_stays_hot(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    old = repository.add(
        "anchor", "Pinned fact.", tenant="acme", layer=Layer.ANCHOR, anchor="ops:pinned"
    )
    new = repository.supersede(old.id, "anchor v2", "Pinned fact, revised.")
    assert new.layer == Layer.ANCHOR
    assert new.anchor == "ops:pinned"
    assert new.tier == Tier.HOT
    assert new.tenant == "acme"


def test_overrides_replace_inherited_fields_but_never_tenant(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    old_id = _scoped(repository)
    new = repository.supersede(
        old_id,
        "v2",
        "New body.",
        tags=["v2"],
        overrides={"tier": Tier.HOT, "about": ["entity:other"]},
    )
    assert new.tier == Tier.HOT
    assert new.about == ["entity:other"]
    assert new.tags == ["v2"]
    assert new.tenant == "acme"
    assert new.layer == Layer.CONSOLIDATED

    other = repository.add("other", "Body.", tenant="acme")
    with pytest.raises(IntegrityError):
        repository.supersede(other.id, "v2", "Body 2.", overrides={"tenant": ""})
    assert repository.get(other.id).valid_to is None


def test_replacement_is_invisible_to_another_tenant_over_mcp(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    old = repository.add("pump", "The zanzibarite pump runs at dawn.", tenant="acme")
    new = repository.supersede(old.id, "pump v2", "The zanzibarite pump runs at noon.")

    owner = Gateway(repository.root, tenant="acme")
    hits = owner.search("zanzibarite", limit=10, mode="fts")["results"]
    assert new.id in {item["id"] for item in hits}
    assert owner.get(new.id)["body"].endswith("noon.")

    for tenant in ("", "other"):
        stranger = Gateway(repository.root, tenant=tenant)
        hits = stranger.search("zanzibarite", limit=10, mode="fts", include_history=True)
        assert not {item["id"] for item in hits["results"]} & {old.id, new.id}
        with pytest.raises(ValueError):
            stranger.get(new.id)


def test_cli_supersede_keeps_tenant(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    old_id = _scoped(repository)
    result = runner.invoke(
        app,
        [
            "supersede",
            old_id,
            "--title",
            "v2",
            "--body",
            "The quarry pump runs at noon.",
            "--root",
            str(repository.root),
            "--apply",
        ],
    )
    assert result.exit_code == 0, result.output
    new_id = json.loads(result.output)["new_memory_id"]
    new = repository.get(new_id)
    assert new.tenant == "acme"
    assert new.layer == Layer.CONSOLIDATED
    assert new.about == ["entity:quarry-pump"]
    assert repository.get(old_id).superseded_by == new_id


def test_experience_point_supersede_is_unchanged(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    body = EXPERIENCE_DISCLAIMER + "\n\nran the checklist"
    old = repository.add(
        "experience",
        body,
        memory_type="experience-point",
        epistemic="experience",
        experience={
            "task": "run the checklist",
            "conditions": ["fixture vault"],
            "actions": ["run the checklist"],
            "review": {"reviewer": "reviewer", "verdict": "accepted"},
        },
    )
    new = repository.supersede(old.id, "experience v2", body, memory_id_new="experience-v2")
    assert new.id == "experience-v2"
    assert new.type == "experience-point"
    assert new.epistemic == "experience"
    assert new.causal == "none"
    assert new.experience == old.experience
    assert new.tenant == ""


def test_uninvolved_files_are_byte_identical(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    old_id = _scoped(repository)
    bystander = repository.add("bystander", "Unrelated.", tenant="acme").id
    untenanted = repository.add("free", "Unrelated too.").id
    memories = repository.memories_dir
    before = {path.name: path.read_bytes() for path in memories.glob("*.md")}

    new = repository.supersede(old_id, "v2", "New body.")

    after = {path.name: path.read_bytes() for path in memories.glob("*.md")}
    assert set(after) == set(before) | {f"{new.id}.md"}
    changed = {name for name in before if before[name] != after[name]}
    assert changed == {f"{old_id}.md"}
    assert f"{bystander}.md" in before and f"{untenanted}.md" in before


def test_fsck_reports_a_replacement_in_another_tenant_without_rewriting(
    tmp_path: Path,
) -> None:
    repository = _vault(tmp_path)
    old = repository.add("pump", "Old body.", tenant="acme")
    # A vault written before the fix: the replacement landed in tenant "".
    legacy = repository.add("pump v2", "New body.")
    repository.update(old.id, actor="human", valid_to=legacy.created_at, superseded_by=legacy.id)
    legacy_path = repository._current_path(legacy.id)
    old_path = repository._current_path(old.id)
    legacy_bytes, old_bytes = legacy_path.read_bytes(), old_path.read_bytes()

    issues = repository.fsck()

    mismatches = [item for item in issues if item["kind"] == "supersede_scope_mismatch"]
    assert len(mismatches) == 1
    assert mismatches[0]["memory_id"] == legacy.id
    assert old.id in str(mismatches[0]["message"])
    assert legacy_path.read_bytes() == legacy_bytes
    assert old_path.read_bytes() == old_bytes


def test_fsck_is_quiet_for_a_fixed_supersede(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    old = repository.add("pump", "Old body.", tenant="acme")
    repository.supersede(old.id, "pump v2", "New body.")
    assert not [item for item in repository.fsck() if item["kind"] == "supersede_scope_mismatch"]


def test_merge_refuses_targets_from_another_tenant(tmp_path: Path) -> None:
    from memfw.curation.review import _validate_merge

    repository = _vault(tmp_path)
    keep = repository.add("keep", "Shared text.", tenant="acme")
    other = repository.add("dup", "Shared text.", tenant="globex")

    class _Proposal:
        output = {"representative_id": keep.id, "duplicate_ids": [other.id]}
        target_ids = (keep.id, other.id)

    with pytest.raises(IntegrityError, match="tenant"):
        _validate_merge(repository, _Proposal())  # type: ignore[arg-type]
