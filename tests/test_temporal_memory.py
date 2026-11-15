from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from memfw.core.models import EventKind
from memfw.core.repository import Repository
from memfw.index import MemoryIndex
from memfw.mcp.server import Gateway
from memfw.memory_hybrid import MemoryHybridRetriever
from memfw.notes import HashEmbeddingProvider


class _SteppedClock:
    """A clock that moves only when the test says so.

    Wall-clock time is not a test input. With the real clock the replacement's
    ``created_at`` and the old record's ``valid_to`` are stamped around file
    writes, so a slow disk can land the midpoint ``as_of`` inside a window where
    both records look current: that is what made this test fail on CI's Python
    3.11 while a fast laptop passed. Pin each operation to one instant instead.
    """

    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


def test_supersede_keeps_old_body_and_current_search(tmp_path: Path) -> None:
    clock = _SteppedClock(datetime(2026, 11, 6, 12, tzinfo=UTC))
    repository = Repository(tmp_path / "vault", clock=clock)
    repository.init()
    repository.add("Old policy", "version one", memory_id="old-policy")
    clock.advance(timedelta(hours=1))
    new = repository.supersede(
        "old-policy", "New policy", "version two", memory_id_new="new-policy"
    )
    historical = repository.get("old-policy")
    assert historical.valid_to is not None
    assert historical.superseded_by == "new-policy"
    assert historical.body == "version one"
    assert new.valid_to is None
    assert any(event.kind == EventKind.SUPERSEDE for event in repository.events())
    assert repository.get("old-policy").body == "version one"

    index = MemoryIndex(repository.index_path)
    index.rebuild(repository)
    assert index.search("policy") == [("new-policy", "New policy")]
    assert [row[0] for row in index.rows()] == ["new-policy", "old-policy"]

    hybrid = MemoryHybridRetriever(repository, embedder=HashEmbeddingProvider(8))
    current = hybrid.search("policy", limit=5, mode="hybrid")
    assert [hit["id"] for hit in current["results"]] == ["new-policy"]
    history = hybrid.search("policy", limit=5, mode="hybrid", include_history=True)
    assert {hit["id"] for hit in history["results"]} >= {"new-policy", "old-policy"}
    before_supersede = historical.created_at + (historical.valid_to - historical.created_at) / 2
    as_of = hybrid.search("policy", limit=5, mode="hybrid", as_of=before_supersede.isoformat())
    assert [hit["id"] for hit in as_of["results"]] == ["old-policy"]


def test_mcp_current_and_include_history(tmp_path: Path) -> None:
    repository = Repository(tmp_path / "vault")
    repository.init()
    repository.add("Policy", "old body", memory_id="old")
    repository.supersede("old", "Policy new", "new body", memory_id_new="new")
    gateway = Gateway(repository.root, embedder=HashEmbeddingProvider(8))
    current = gateway.search("policy", limit=5, mode="hybrid")
    assert {item["id"] for item in current["results"]} == {"new"}
    history = gateway.search("policy", limit=5, mode="hybrid", include_history=True)
    assert {item["id"] for item in history["results"]} == {"new", "old"}


def test_supersede_hands_over_at_one_instant(tmp_path: Path) -> None:
    """The replacement takes effect exactly when the old record turns historical.

    Two records must never be current at the same ``as_of``: a fresh ``_now()``
    for ``valid_to`` after the replacement was stamped leaves an overlap window
    where an ``as_of`` query inside it returns both.
    """

    clock = _SteppedClock(datetime(2026, 11, 6, 12, tzinfo=UTC))
    repository = Repository(tmp_path / "vault", clock=clock)
    repository.init()
    repository.add("Old policy", "version one", memory_id="old-policy")
    clock.advance(timedelta(hours=1))
    new = repository.supersede(
        "old-policy", "New policy", "version two", memory_id_new="new-policy"
    )
    historical = repository.get("old-policy")
    assert historical.valid_to == new.created_at

    hybrid = MemoryHybridRetriever(repository, embedder=HashEmbeddingProvider(8))
    at_handoff = hybrid.search("policy", limit=5, mode="hybrid", as_of=new.created_at.isoformat())
    assert [hit["id"] for hit in at_handoff["results"]] == ["new-policy"]
    just_before = historical.valid_to - timedelta(microseconds=1)
    before_handoff = hybrid.search(
        "policy", limit=5, mode="hybrid", as_of=just_before.isoformat()
    )
    assert [hit["id"] for hit in before_handoff["results"]] == ["old-policy"]
