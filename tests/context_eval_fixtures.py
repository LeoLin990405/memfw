"""Synthetic fixtures for the C4 context pack evaluation (no private data).

``known_vault`` builds a small vault whose numbers are worked out by hand in
``tests/test_context_eval.py``; ``random_vault`` generates a larger one with
seeded sessions (``memory_access`` events and ``memory_used`` signals) for
determinism and invariant checks. Every title, body, session and app name
here is made up.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from memfw.core.models import EventKind, Layer
from memfw.core.repository import Repository
from memfw.ports.usage import UsageSignal
from memfw.usage import SqliteUsageSink
from tests.conversation_fixtures import standard_rows, write_store

T0 = datetime(2026, 3, 2, 9, 0, tzinfo=UTC)
BODY = "a synthetic body that must never appear in an evaluation report"
#: 400 characters: the core slot cuts it at small caps.
LONG = "step " * 79 + "steps"


@dataclass(frozen=True)
class Fixture:
    vault: Path
    conversations: Path
    usage: Path


def _concept(root: Path, oid: str, label: str) -> None:
    slug = oid.split(":")[-1]
    path = root / "concepts" / "Concept" / f"{slug}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\noid: {oid}\nclass: Concept\npref_label: {label}\nstatus: active\n---\n\nDef.\n",
        encoding="utf-8",
    )


class _Clock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now


def access(repository: Repository, memory_id: str, at: datetime, **extra: str) -> None:
    """One search hit as ``memory_access`` (operation ``fts_retrieve``) at ``at``."""

    repository.capture_event(
        EventKind.MEMORY_ACCESS,
        actor="mechanism",
        details={"memory_id": memory_id, "operation": "fts_retrieve", **extra},
        timestamp=at,
    )


def known_vault(tmp_path: Path, *, events: bool = True) -> Fixture:
    """The hand-worked fixture (see ``test_context_eval.py`` for the numbers).

    Visible (clearance ``public,shared``): ``a-1`` (anchor, 20 chars),
    ``p-1`` (procedural, 17 chars), ``p-2`` (procedural, 400 chars),
    ``m-a``/``m-b`` (about ``concept:port``), ``m-c`` (about ``concept:backup``,
    evidence from session ``codex/s-alpha``). Hidden: ``m-p`` (local-private)
    and ``m-t`` (tenant ``beta``). Access sessions (``events``): S1 at T0 =
    {m-a, a-1, m-p}, S2 two hours later = {m-c, m-b, p-2}, plus one
    ``memory_access`` that is not a search (ignored).
    """

    clock = _Clock(T0 - timedelta(days=1))
    repository = Repository(tmp_path / "snapshot", clock=clock)
    repository.init()
    repository.add("Anchor", "Always cite sources.", memory_id="a-1", layer=Layer.ANCHOR)
    repository.add("Proc one", "1. Search 2. Read", memory_id="p-1", memory_type="procedural")
    repository.add("Proc two", LONG, memory_id="p-2", memory_type="procedural")
    repository.add("Port A", BODY, memory_id="m-a", about=["concept:port"])
    repository.add("Port B", BODY, memory_id="m-b", about=["concept:port"])
    repository.add(
        "Backup",
        BODY,
        memory_id="m-c",
        about=["concept:backup"],
        evidence=["conv:codex:s-alpha@1"],
    )
    repository.add(
        "Private", BODY, memory_id="m-p", about=["concept:backup"], privacy="local-private"
    )
    repository.add("Other tenant", BODY, memory_id="m-t", about=["concept:port"], tenant="beta")
    _concept(repository.root, "concept:port", "Service port")
    _concept(repository.root, "concept:backup", "Backup")
    conversations = write_store(tmp_path / "stores" / "conversations.sqlite", standard_rows())
    config = repository.read_config()
    config["conversation_share"] = {"enabled": True}
    config["legacy_privacy_default"] = "shared"
    repository.configure(config)
    if events:
        for offset, memory_id in enumerate(("m-a", "a-1", "m-p")):
            access(repository, memory_id, T0 + timedelta(minutes=offset))
        later = T0 + timedelta(hours=2)
        for offset, memory_id in enumerate(("m-c", "m-b", "p-2")):
            access(repository, memory_id, later + timedelta(minutes=offset))
        repository.capture_event(
            EventKind.MEMORY_ACCESS,
            actor="mechanism",
            details={"memory_id": "p-1", "operation": "cool"},
            timestamp=later,
        )
    usage = tmp_path / "stores" / "usage.sqlite3"
    sink = SqliteUsageSink(usage)
    sink.record(UsageSignal(("a-1", "m-a", "m-p"), "u1", "claude", T0))
    sink.record(UsageSignal(("p-1",), "u2", "claude", T0 + timedelta(hours=3)))
    return Fixture(repository.root, conversations, usage)


def random_vault(tmp_path: Path, *, seed: int = 5, memories: int = 40, sessions: int = 6) -> Path:
    """A generated vault: anchors, procedures and topical memories, seeded access sessions."""

    rng = random.Random(seed)
    clock = _Clock(T0 - timedelta(days=2))
    repository = Repository(tmp_path / "generated", clock=clock)
    repository.init()
    topics = ["concept:alpha", "concept:beta", "concept:gamma"]
    for oid in topics:
        _concept(repository.root, oid, oid.split(":")[1].title())
    ids: list[str] = []
    for index in range(memories):
        memory_id = f"g-{index:03d}"
        ids.append(memory_id)
        words = " ".join(rng.choice(["ledger", "harbor", "violet", "copper"]) for _ in range(30))
        kind = index % 5
        repository.add(
            f"Note {index}",
            words,
            memory_id=memory_id,
            layer=Layer.ANCHOR if kind == 0 else Layer.CURRENT,
            memory_type="procedural" if kind == 1 else None,
            about=[rng.choice(topics)],
            privacy="local-private" if kind == 4 and index % 3 == 0 else None,
        )
    start = T0
    for _ in range(sessions):
        for offset in range(rng.randint(2, 6)):
            access(repository, rng.choice(ids), start + timedelta(minutes=offset))
        start += timedelta(hours=rng.randint(1, 5))
    return repository.root
