"""A2a: the seams that replaced the conversations/dream, stats/curation and kernel cycles.

* claim ports (``memfw.ports.claims``) with their contract checks, and the
  dream implementations the CLI injects into the conversation worker;
* the episode feed the dream skills queue reads through;
* the handoff summarizer's injected backend loader;
* helpers moved down: the cold-archive body read (``memfw.storage``), the
  capture stage WAL and its recovery (``memfw.storage.capture_stage``), and the
  transcript reader (``memfw.history.transcripts``).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from memfw.dream.claims import DreamClaimStager, DreamClaimVerifier
from memfw.dream.skills.queue import EpisodeFeed
from memfw.dream.verify import classify
from memfw.ports.claims import (
    POLARITIES,
    ClaimEvidence,
    ClaimStager,
    ClaimVerifier,
    StagedClaim,
)
from memfw.ports.testing import ContractError, check_claim_stager, check_claim_verifier
from tests.conversation_fixtures import make_vault, standard_rows, write_store

PAIRS = [
    ("Uploader retry uses exponential backoff.", "The uploader retry uses exponential backoff."),
    ("Uploader retry uses exponential backoff.", "The uploader retry does not use backoff."),
    ("Port is 8080.", "Unrelated text about lunch."),
]
CLAIMS = [
    StagedClaim(
        claim="Uploader retry uses exponential backoff.",
        oid="conversation-codex",
        evidence=(ClaimEvidence(ref="conv:codex:s1@0", session_id="s1", timestamp=None),),
    )
]


# --- claim ports ------------------------------------------------------------


def test_dream_verifier_passes_the_claim_verifier_contract() -> None:
    verifier = DreamClaimVerifier()
    assert isinstance(verifier, ClaimVerifier)
    answers = check_claim_verifier(verifier, pairs=PAIRS)
    assert answers == [classify(claim, text)[0] for claim, text in PAIRS]
    assert set(answers) <= set(POLARITIES)


def test_dream_stager_passes_the_claim_stager_contract(tmp_path: Path) -> None:
    vault = make_vault(tmp_path / "vault")
    stager = DreamClaimStager()
    assert isinstance(stager, ClaimStager)
    staged = check_claim_stager(stager, vault=vault, claims=CLAIMS)
    assert any(path.startswith("dream/contract-claims/") for path in staged)


class _BadVerifier:
    def polarity(self, claim: str, text: str) -> str:
        return "maybe"


class _NotIdempotent:
    def __init__(self) -> None:
        self.calls = 0

    def stage_claims(self, vault: Path, batch_id: str, claims: Any, report: Any, **_: Any) -> bool:
        self.calls += 1
        target = vault / "_candidates" / "x"
        target.mkdir(parents=True, exist_ok=True)
        (target / f"{self.calls}.md").write_text("x", encoding="utf-8")
        return True


def test_claim_contract_checks_reject_broken_implementations(tmp_path: Path) -> None:
    with pytest.raises(ContractError, match="not one of"):
        check_claim_verifier(_BadVerifier(), pairs=PAIRS)
    with pytest.raises(ContractError, match="return False"):
        check_claim_stager(_NotIdempotent(), vault=make_vault(tmp_path / "v"), claims=CLAIMS)


def test_worker_with_a_vault_needs_the_claim_ports(tmp_path: Path) -> None:
    from memfw.conversations.adapter import SqliteStoreSource
    from memfw.conversations.policy import SharePolicy
    from memfw.conversations.store import ConversationError
    from memfw.conversations.worker import ConversationWorker, WorkState
    from tests.test_conversations_worker import FakeBackend

    store = write_store(tmp_path / "data" / "conversations.sqlite", standard_rows())
    common: dict[str, Any] = {
        "backend": FakeBackend(),
        "policy": SharePolicy.from_config({}),
        "vault": make_vault(tmp_path / "vault"),
    }
    state = WorkState(tmp_path / "data" / "work.sqlite")
    with pytest.raises(ConversationError, match="claim stager"):
        ConversationWorker(SqliteStoreSource(store), state, **common)
    with pytest.raises(ConversationError, match="claim stager"):
        ConversationWorker(SqliteStoreSource(store), state, stager=DreamClaimStager(), **common)
    ConversationWorker(
        SqliteStoreSource(store), state, stager=DreamClaimStager(), judge=lambda c, t: "", **common
    )
    common["vault"] = None
    ConversationWorker(SqliteStoreSource(store), state, **common)  # no vault: nothing staged


# --- episode feed -------------------------------------------------------------


def test_worker_episode_feed_is_an_episode_feed(tmp_path: Path) -> None:
    from memfw.conversations.adapter import SqliteStoreSource
    from memfw.conversations.feed import WorkerEpisodeFeed
    from memfw.conversations.worker import EpisodeQueue, WorkState

    feed: EpisodeFeed = WorkerEpisodeFeed()
    store = write_store(tmp_path / "conversations.sqlite", standard_rows())
    state = WorkState(tmp_path / "work.sqlite")
    config = {"conversation_share": {"enabled": True, "apps": {"codex": {"process": False}}}}
    processable, queue, source = feed.open(
        config, tmp_path, state=state, source=SqliteStoreSource(store)
    )
    assert processable("claude") and not processable("codex")
    assert isinstance(queue, EpisodeQueue) and queue.state is state
    assert source.available()
    assert isinstance(feed.queue(config, tmp_path, state=state), EpisodeQueue)


def test_from_queue_without_a_feed_is_refused(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from memfw.dream.skills import SkillOptions, run_skills

    vault = make_vault(tmp_path / "vault")
    with pytest.raises(ValueError, match="episode feed"):
        run_skills(vault, now=datetime.now(UTC), options=SkillOptions(from_queue=True))


# --- handoff summarizer -----------------------------------------------------


def test_handoff_summarize_needs_an_injected_loader() -> None:
    from memfw.handoff.summarize import SummarizeError, summarize

    with pytest.raises(SummarizeError, match="backend loader"):
        summarize("x:y", goal="g", turns=[])
    with pytest.raises(SummarizeError, match="module:factory"):
        summarize("extractive", goal="g", turns=[])  # checked before the loader


# --- helpers moved down -------------------------------------------------------


def test_moved_helpers_keep_their_old_names() -> None:
    import memfw.capture as capture
    import memfw.conversations.store as old_store
    import memfw.history.transcripts as transcripts
    from memfw.curation.engine import _with_archived_body
    from memfw.storage import capture_stage, with_archived_body

    assert _with_archived_body is with_archived_body
    assert capture.CaptureCandidate is capture_stage.CaptureCandidate
    assert capture._read_stage_records is capture_stage.read_stage_records
    assert capture._recover_stage_intent is capture_stage.recover_stage_intent
    assert capture._append_stage_record is capture_stage.append_stage_record
    for name in old_store.__all__:
        assert getattr(old_store, name) is getattr(transcripts, name), name


@pytest.mark.parametrize(
    ("module", "forbidden"),
    [
        ("memfw.storage.capture_stage", "memfw.capture"),
        ("memfw.storage.operations", "memfw.curation"),
        ("memfw.stats", "memfw.curation"),
        ("memfw.handoff.pack", "memfw.conversations"),
        ("memfw.dream.skills.queue", "memfw.conversations"),
        ("memfw.conversations.worker", "memfw.dream"),
    ],
)
def test_lower_side_does_not_load_the_other_capability(module: str, forbidden: str) -> None:
    code = (
        "import sys, importlib\n"
        f"importlib.import_module({module!r})\n"
        f"loaded = sorted(m for m in sys.modules if m == {forbidden!r}"
        f" or m.startswith({forbidden + '.'!r}))\n"
        "print(','.join(loaded))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True, timeout=60
    )
    assert completed.stdout.strip() == "", f"{module} loaded {completed.stdout.strip()}"


def test_stage_recovery_runs_from_storage_without_capture(tmp_path: Path) -> None:
    """``Repository.recover_pending`` replays a pending stage append via storage."""

    import hashlib
    import json
    from datetime import UTC, datetime

    from memfw.core.repository import Repository
    from memfw.storage.capture_stage import CaptureCandidate, read_stage_records

    repository = Repository(tmp_path / "vault")
    repository.init()
    candidate = CaptureCandidate(
        candidate_id="cand-1",
        session_id="s",
        sequence=1,
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        source="model",
        text="remember the recovery path",
    )
    content = (
        b"---\n"
        + (
            b"candidate_id: cand-1\nsequence: 1\nsession_id: s\nsource: model\n"
            b"timestamp: 2026-01-01T00:00:00Z\n"
        )
        + b"---\nremember the recovery path"
    )
    staging = repository.root / ".staging"
    staging.mkdir(exist_ok=True)
    intent = {
        "candidate": json.loads(candidate.model_dump_json()),
        "content": content.decode(),
        "content_hash": hashlib.sha256(content).hexdigest(),
        "base_size": 0,
        "prefix_hash": hashlib.sha256(b"").hexdigest(),
    }
    (staging / "capture-candidates.pending.json").write_text(json.dumps(intent), "utf-8")
    assert ".staging/capture-candidates.jsonl" in repository.recover_pending()
    records = read_stage_records(staging / "capture-candidates.jsonl")
    assert [item.candidate_id for item, _raw in records] == ["cand-1"]
    assert not (staging / "capture-candidates.pending.json").exists()
