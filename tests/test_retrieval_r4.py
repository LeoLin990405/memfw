"""R4: stage writes a candidate, never a silent overwrite."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import scripts.memfw_readonly_service as service
from scripts.memfw_readonly_service import ServiceError, mark_expired_candidates, stage_candidate

# Before this check, a new proposal id always created a file.
BASELINE_CROSS_PROPOSAL_DUPLICATE_RATE = 0.0


def _vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    root.mkdir()
    return root


def test_same_hash_on_a_new_proposal_does_not_write_again(tmp_path: Path) -> None:
    root = _vault(tmp_path)
    body = "The weekly bulletin notice about parking item one"
    first = stage_candidate(root, {"proposal_id": "orig", "title": "T", "content": body})
    assert first["duplicate"] is False
    duplicates = 0
    for index in range(10):
        again = stage_candidate(
            root, {"proposal_id": f"rep-{index}", "title": "T", "content": body}
        )
        assert again["duplicate"] is True
        assert again["duplicate_of"] == "orig"
        assert again["authority"] is False
        duplicates += 1
    stored = list((root / "_candidates" / "remember").glob("*.md"))
    assert len(stored) == 1
    rate = duplicates / 10
    assert rate == 1.0
    assert rate > BASELINE_CROSS_PROPOSAL_DUPLICATE_RATE


def test_same_topic_different_value_is_always_a_conflict_candidate(tmp_path: Path) -> None:
    root = _vault(tmp_path)
    produced = 0
    for index in range(5):
        topic = f"The cluster k{index} in the platform note is colored"
        left = stage_candidate(
            root,
            {"proposal_id": f"left-{index}", "title": "Cluster note", "content": f"{topic} red"},
        )
        right = stage_candidate(
            root,
            {"proposal_id": f"right-{index}", "title": "Cluster note", "content": f"{topic} blue"},
        )
        assert left["duplicate"] is False
        assert right["authority"] is False
        identifiers = {item["proposal_id"] for item in right["conflict_candidates"]}
        assert f"left-{index}" in identifiers
        produced += 1
        assert (root / right["path"]).is_file()
    assert produced == 5


def test_high_overlap_is_a_merge_candidate_and_still_writes(tmp_path: Path) -> None:
    root = _vault(tmp_path)
    base = "alpha beta gamma delta epsilon zeta eta theta iota kappa"
    first = stage_candidate(root, {"proposal_id": "near-a", "title": "T", "content": base})
    second = stage_candidate(
        root,
        {"proposal_id": "near-b", "title": "T", "content": base + " lambda"},
    )
    assert first["duplicate"] is False
    assert second["duplicate"] is False
    assert "conflict_candidates" not in second
    assert second["merge_candidates"][0]["proposal_id"] == "near-a"
    assert (root / first["path"]).is_file()
    assert (root / second["path"]).is_file()


def test_same_proposal_different_content_stays_409(tmp_path: Path) -> None:
    root = _vault(tmp_path)
    stage_candidate(root, {"proposal_id": "p-3", "title": "T", "content": "one"})
    with pytest.raises(ServiceError) as caught:
        stage_candidate(root, {"proposal_id": "p-3", "title": "T", "content": "two"})
    assert caught.value.status == 409


def test_recorded_at_is_separate_from_the_event_time(tmp_path: Path) -> None:
    root = _vault(tmp_path)
    staged = stage_candidate(
        root,
        {
            "proposal_id": "when",
            "title": "T",
            "content": "body text",
            "occurred_at": "2026-01-02T03:04:05+00:00",
        },
    )
    meta = service._metadata(root / staged["path"])
    assert meta["created_at"] == "2026-01-02T03:04:05+00:00"
    assert meta["recorded_at"] != meta["created_at"]
    assert meta["authority"] is False


def test_expired_candidates_are_marked_and_kept(tmp_path: Path) -> None:
    root = _vault(tmp_path)
    staged = stage_candidate(root, {"proposal_id": "old", "title": "T", "content": "old body"})
    path = root / staged["path"]
    meta = service._metadata(path)
    old = (datetime(2026, 4, 1, tzinfo=UTC) - timedelta(days=31)).isoformat()
    text = path.read_text(encoding="utf-8").replace(str(meta["recorded_at"]), old)
    path.write_text(text, encoding="utf-8")
    result = mark_expired_candidates(root, now=datetime(2026, 4, 1, tzinfo=UTC))
    assert result == {"expired": 1, "deleted": 0}
    assert path.is_file()
    marked = service._metadata(path)
    assert marked["lifecycle"] == "expired"
    assert marked["expired"] is True
    fresh = stage_candidate(root, {"proposal_id": "new", "title": "T", "content": "new body"})
    again = mark_expired_candidates(root, now=datetime(2026, 4, 1, tzinfo=UTC))
    assert again == {"expired": 0, "deleted": 0}
    assert service._metadata(root / fresh["path"])["lifecycle"] == "active"
