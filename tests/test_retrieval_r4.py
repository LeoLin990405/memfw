"""R4: ``recorded_at`` (write time) on memories and candidates, and the 30-day TTL.

Expiry only marks and hides a staged candidate. No file is ever deleted.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from memfw.capture.lifecycle import mark_candidate_applied, mark_expired_candidates
from memfw.capture.review import (
    CANDIDATE_TTL_DAYS,
    list_staged_candidates,
    read_candidate,
    validate_candidate_hash,
)
from memfw.core.repository import Repository
from memfw.index import MemoryIndex
from memfw.mcp.server import Gateway
from scripts.memfw_readonly_service import search_vault, stage_candidate

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def _clock(start: datetime) -> Iterator[datetime]:
    current = start
    while True:
        yield current
        current += timedelta(hours=1)


def _repository(tmp_path: Path) -> Repository:
    ticks = _clock(T0)
    repository = Repository(tmp_path / "vault", clock=lambda: next(ticks))
    repository.init()
    return repository


def _frontmatter(path: Path) -> dict[str, object]:
    text = path.read_text(encoding="utf-8")
    loaded = yaml.safe_load(text.split("---\n", 2)[1])
    assert isinstance(loaded, dict)
    return loaded


def test_add_stamps_recorded_at_and_update_keeps_it(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    document = repository.add("Fact", "sky colour note", memory_id="mem-a", tenant="t")
    assert document.recorded_at == T0
    on_disk = _frontmatter(repository.root / "memories" / "mem-a.md")
    assert datetime.fromisoformat(str(on_disk["recorded_at"])) == T0
    updated = repository.update("mem-a", body="sky colour note, revised")
    assert updated.updated_at > T0
    assert updated.recorded_at == T0
    assert repository.get("mem-a").recorded_at == T0


def test_update_cannot_set_recorded_at(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    repository.add("Fact", "body", memory_id="mem-a")
    with pytest.raises(Exception, match="Update accepts fields"):
        repository.update("mem-a", recorded_at=T0 - timedelta(days=400))


def _legacy_markdown(repository: Repository, memory_id: str) -> str:
    """A memory written before recorded_at existed: no such key on disk."""
    path = repository.root / "memories" / f"{memory_id}.md"
    text = path.read_text(encoding="utf-8")
    lines = [line for line in text.split("\n") if not line.startswith("recorded_at:")]
    legacy = "\n".join(lines)
    path.write_text(legacy, encoding="utf-8")
    return legacy


def test_legacy_file_without_recorded_at_keeps_its_bytes(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    repository.add("Fact", "legacy body", memory_id="mem-old")
    legacy = _legacy_markdown(repository, "mem-old")
    document = repository.get("mem-old")
    assert document.recorded_at is None
    assert document.ignored_keys == ()
    assert repository.render(document) == legacy


def test_mcp_search_carries_recorded_at_in_every_mode(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    repository.add("New", "corpus keyword fresh", memory_id="mem-new", tenant="a")
    repository.add("Old", "corpus keyword legacy", memory_id="mem-old", tenant="a")
    _legacy_markdown(repository, "mem-old")
    MemoryIndex(repository.index_path).rebuild(repository)
    expected = {
        # A new memory reports the stamp written at add time.
        "mem-new": T0.isoformat(),
        # A legacy memory reports created_at, which add stamped from the same clock.
        "mem-old": repository.get("mem-old").created_at.isoformat(),
    }
    gateway = Gateway(repository.root, tenant="a")
    for mode in ("fts", "multihop"):
        result = gateway.search("corpus keyword", limit=10, mode=mode)
        rows = {row["id"]: row["recorded_at"] for row in result["results"]}
        assert rows == expected, mode


def test_http_v2_search_keys_are_unchanged(tmp_path: Path) -> None:
    """recorded_at reaches HTTP /search with memfw-http/3 (R6), not inside v2."""
    repository = _repository(tmp_path)
    repository.add("New", "corpus keyword fresh", memory_id="mem-new", tenant="a")
    MemoryIndex(repository.index_path).rebuild(repository)
    http = search_vault(repository.root, "corpus keyword", limit=10, tenant="a")
    assert [row["id"] for row in http["results"]] == ["mem-new"]
    assert "recorded_at" not in http["results"][0]


def test_stage_and_remember_write_recorded_at(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    before = datetime.now(UTC)
    staged = stage_candidate(
        repository.root, {"proposal_id": "p-1", "title": "T", "content": "a staged note"}
    )
    remembered = Gateway(repository.root, tenant="").remember("a remembered note", title="R")
    after = datetime.now(UTC)
    for relative in (staged["path"], remembered["staged_candidate"]):
        header = _frontmatter(repository.root / relative)
        stamp = datetime.fromisoformat(str(header["recorded_at"]))
        assert before <= stamp <= after
        nested = header["metadata"]
        assert isinstance(nested, dict)
        assert nested["recorded_at"] == header["recorded_at"]
        assert read_candidate(repository.root / relative).recorded_at == header["recorded_at"]


def _stage(root: Path, proposal_id: str, content: str) -> Path:
    result = stage_candidate(root, {"proposal_id": proposal_id, "title": "T", "content": content})
    return root / result["path"]


def _set_header(path: Path, **values: object) -> None:
    text = path.read_text(encoding="utf-8")
    _, header, body = text.split("---\n", 2)
    loaded = yaml.safe_load(header)
    for key, value in values.items():
        if value is None:
            loaded.pop(key, None)
            loaded["metadata"].pop(key, None)
        else:
            loaded[key] = value
            loaded["metadata"][key] = value
    dumped = yaml.safe_dump(loaded, sort_keys=False)
    path.write_text(f"---\n{dumped}---\n{body}", encoding="utf-8")


def test_candidate_is_hidden_after_the_ttl_and_never_deleted(tmp_path: Path) -> None:
    root = _repository(tmp_path).root
    path = _stage(root, "p-old", "an old candidate")
    _set_header(path, recorded_at=T0.isoformat(), created_at=T0.isoformat())

    almost = T0 + timedelta(days=CANDIDATE_TTL_DAYS) - timedelta(seconds=1)
    assert [row["proposal_id"] for row in list_staged_candidates(root, now=almost)] == ["p-old"]
    due = T0 + timedelta(days=CANDIDATE_TTL_DAYS)
    assert list_staged_candidates(root, now=due) == []
    shown = list_staged_candidates(root, now=due, include_expired=True)
    assert [row["status"] for row in shown] == ["expired"]

    dry = mark_expired_candidates(root, now=due, dry_run=True)
    assert dry["expired"] == [path.stem]
    assert "expired" not in _frontmatter(path)

    swept = mark_expired_candidates(root, now=due)
    assert swept["expired"] == [path.stem]
    assert swept["deleted"] == 0
    assert path.is_file()
    header = _frontmatter(path)
    assert header["expired"] is True
    assert header["lifecycle"] == "expired"
    assert header["status"] == "expired"
    assert header["recorded_at"] == T0.isoformat()
    candidate = read_candidate(path)
    validate_candidate_hash(candidate)
    assert candidate.body == "an old candidate"
    assert candidate.status == "expired"
    # Marked candidates stay hidden whatever clock the reader has.
    assert list_staged_candidates(root, now=T0) == []
    # A second sweep finds nothing new.
    assert mark_expired_candidates(root, now=due)["expired"] == []


def test_legacy_candidate_ages_from_created_at(tmp_path: Path) -> None:
    root = _repository(tmp_path).root
    path = _stage(root, "p-legacy", "staged before recorded_at")
    _set_header(path, recorded_at=None, created_at=T0.isoformat())
    assert read_candidate(path).recorded_at is None
    due = T0 + timedelta(days=CANDIDATE_TTL_DAYS)
    assert list_staged_candidates(root, now=due) == []
    assert mark_expired_candidates(root, now=due)["expired"] == [path.stem]


def test_candidate_without_any_stamp_never_expires(tmp_path: Path) -> None:
    root = _repository(tmp_path).root
    path = _stage(root, "p-undated", "no stamp at all")
    _set_header(path, recorded_at=None, created_at=None)
    far = T0 + timedelta(days=10 * CANDIDATE_TTL_DAYS)
    assert [row["proposal_id"] for row in list_staged_candidates(root, now=far)] == ["p-undated"]
    assert mark_expired_candidates(root, now=far)["expired"] == []


def test_applied_candidate_never_expires(tmp_path: Path) -> None:
    root = _repository(tmp_path).root
    path = _stage(root, "p-applied", "already applied")
    _set_header(path, recorded_at=T0.isoformat())
    mark_candidate_applied(read_candidate(path), "mem-x")
    due = T0 + timedelta(days=CANDIDATE_TTL_DAYS + 5)
    rows = list_staged_candidates(root, now=due)
    assert [row["status"] for row in rows] == ["applied"]
    assert mark_expired_candidates(root, now=due)["expired"] == []
    assert _frontmatter(path).get("expired") is None


def test_applying_an_expired_candidate_lifts_the_expiry(tmp_path: Path) -> None:
    root = _repository(tmp_path).root
    path = _stage(root, "p-late", "applied after expiry")
    _set_header(path, recorded_at=T0.isoformat())
    due = T0 + timedelta(days=CANDIDATE_TTL_DAYS)
    assert mark_expired_candidates(root, now=due)["expired"] == [path.stem]
    mark_candidate_applied(read_candidate(path), "mem-late")
    header = _frontmatter(path)
    assert "expired" not in header
    assert header["lifecycle"] == "active"
    assert header["status"] == "applied"
    assert "expired_at" in header
    assert read_candidate(path).status == "applied"
    assert [row["status"] for row in list_staged_candidates(root, now=due)] == ["applied"]


def test_sweep_requires_an_aware_clock(tmp_path: Path) -> None:
    root = _repository(tmp_path).root
    with pytest.raises(ValueError):
        mark_expired_candidates(root, now=datetime(2026, 1, 1))
