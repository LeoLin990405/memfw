from __future__ import annotations

import hashlib
import json
import tarfile
from datetime import UTC, datetime
from pathlib import Path

import pytest
import zstandard

from memfw.core.models import EventKind, Layer, Tier
from memfw.core.repository import Repository
from memfw.curation.engine import enforce_budget
from memfw.stats import measure_runtime

NOW = datetime(2026, 10, 18, tzinfo=UTC)


def make_repo(root: Path) -> Repository:
    sequence = iter(f"day3-event-{index}" for index in range(2000))
    repository = Repository(root, clock=lambda: NOW, id_factory=lambda: next(sequence))
    repository.init()
    return repository


def configure(repository: Repository, **values: object) -> None:
    config = repository.read_config()
    config.update(values)
    repository.configure(config)


def test_total_actual_disk_budget_hard_forgets_until_under_limit(tmp_path: Path) -> None:
    repository = make_repo(tmp_path / "total-budget")
    repository.add("Older", "x" * 6000, memory_id="older")
    repository.add("Newer", "y" * 6000, memory_id="newer")
    before = measure_runtime(repository, now=NOW).physical_disk_bytes
    configure(
        repository,
        budget_mode="total",
        max_bytes=before - 2500,
        forget_policy="fifo",
        forget_action="hard",
        budget_scope="all",
    )
    pre_enforcement_bytes = measure_runtime(repository, now=NOW).physical_disk_bytes

    result = enforce_budget(repository, now=NOW)

    assert result.budget_mode == "total"
    assert result.action == "hard"
    assert result.applied_ids
    assert result.satisfied_after_apply is True
    assert result.physical_bytes_after is not None
    assert result.physical_bytes_before is not None
    assert result.physical_bytes_after <= before - 2500
    assert result.physical_bytes_after < result.physical_bytes_before
    assert result.physical_bytes_before == pre_enforcement_bytes
    assert sum(result.physical_breakdown_before.values()) == pre_enforcement_bytes
    assert sum(result.physical_breakdown_after.values()) == result.physical_bytes_after
    assert any(event.kind == EventKind.DELETE for event in repository.events())


def test_anchor_metadata_floor_records_one_deduplicated_unsatisfied_event(
    tmp_path: Path,
) -> None:
    repository = make_repo(tmp_path / "anchor-floor")
    repository.add("Pinned", "cannot forget", memory_id="pinned", layer=Layer.ANCHOR)
    configure(
        repository,
        budget_mode="total",
        max_bytes=0,
        forget_policy="fifo",
        forget_action="hard",
        budget_scope="all",
    )

    first = enforce_budget(repository, now=NOW)
    event_count = sum(event.kind == EventKind.BUDGET_UNSATISFIABLE for event in repository.events())
    second = enforce_budget(repository, now=NOW)
    final_events = [
        event for event in repository.events() if event.kind == EventKind.BUDGET_UNSATISFIABLE
    ]

    assert first.satisfied_after_apply is False
    assert first.unsatisfied_reason_code == "anchor_floor"
    assert "exceeds the configured budget by" in (first.unsatisfied_reason or "")
    assert first.blocking_anchor_ids == ("pinned",)
    assert first.floor_components
    assert first.floor_anchor_count == 1
    assert first.floor_remaining_memory_count == 1
    assert "not a theoretical" in (first.floor_explanation or "")
    assert first.physical_overage_bytes == first.physical_bytes_after
    assert first.overage_component is not None
    assert first.overage_component in first.physical_breakdown_after
    assert first.overage_component_bytes == first.physical_breakdown_after[first.overage_component]
    assert "allocated bytes over budget" in (first.unsatisfied_explanation or "")
    assert second.unsatisfied_reason_code == "anchor_floor"
    assert len(final_events) == event_count == 1
    assert final_events[0].details["anchor_ids"] == "pinned"
    assert json.loads(final_events[0].details["physical_breakdown"])
    assert final_events[0].details["dominant_current_component"]
    assert final_events[0].details["physical_overage_bytes"] == first.physical_overage_bytes
    assert final_events[0].details["dominant_component_bytes"] == first.overage_component_bytes
    assert "exceeds the total budget by" in final_events[0].details["overage_explanation"]
    assert final_events[0].details["rebuildable_index_bytes"] == 0


def test_soft_max_count_defaults_to_hot_scope_and_cools_oldest(tmp_path: Path) -> None:
    repository = make_repo(tmp_path / "soft-hot-count")
    repository.add("Older", "old body", memory_id="a-older")
    repository.add("Newer", "new body", memory_id="z-newer")
    configure(repository, max_count=1)

    result = enforce_budget(repository, now=NOW)

    assert result.scope == "hot"
    assert result.action == "soft"
    assert result.applied_ids == ("a-older",)
    assert result.satisfied_after_apply is True
    assert repository.get("a-older").tier == Tier.COLD
    assert repository.get("a-older").is_stub
    assert repository.get("z-newer").tier == Tier.HOT
    assert sum(document.tier == Tier.HOT for document in repository.list()) == 1


def test_cold_restore_removes_duplicate_archive_and_history_copies(tmp_path: Path) -> None:
    repository = make_repo(tmp_path / "restore-once")
    repository.add("Cold", "restored body", memory_id="cold", tier=Tier.COLD)
    repository.cold_archive("restore-source")
    stub = repository.get("cold")
    archive_path = repository.archives_dir / f"{stub.archive_ref}.tar.zst"
    assert archive_path.exists()

    restored = repository.restore_cold("cold")

    assert not restored.is_stub
    assert restored.body == "restored body"
    assert repository.get("cold").body == "restored body"
    current = repository._current_path("cold").read_bytes()
    current_hash = hashlib.sha256(current).hexdigest()
    matching = [
        path
        for path in [repository._current_path("cold"), *repository.history_dir.glob("cold/*.md")]
        if hashlib.sha256(path.read_bytes()).hexdigest() == current_hash
    ]
    assert matching == [repository._current_path("cold")]
    if archive_path.exists():
        archived = zstandard.ZstdDecompressor().decompress(archive_path.read_bytes())
        import io

        with tarfile.open(fileobj=io.BytesIO(archived), mode="r:") as archive:
            assert "memories/cold.md" not in archive.getnames()


@pytest.mark.parametrize(
    ("anchor_always_hot", "expected_tier"),
    [(True, Tier.HOT), (False, Tier.COLD)],
)
def test_anchor_tier_defaults_hot_and_can_be_configured_out(
    tmp_path: Path, anchor_always_hot: bool, expected_tier: Tier
) -> None:
    repository = make_repo(tmp_path / f"anchor-hot-{anchor_always_hot}")
    configure(repository, anchor_always_hot=anchor_always_hot)
    repository.add("Memory", "body", memory_id="memory", tier=Tier.COLD)

    anchored = repository.update("memory", layer=Layer.ANCHOR, actor="human")

    assert anchored.layer == Layer.ANCHOR
    assert anchored.tier == expected_tier
    assert repository.get("memory").tier == expected_tier
