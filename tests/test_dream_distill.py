"""Dream operation ③: a lesson needs at least two independent events."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from memfw.core.models import MemoryDocument
from memfw.core.repository import Repository
from memfw.dream import DreamOptions, run_dream
from memfw.dream.distill import distill, event_key
from memfw.dream.model import Candidate, EvidenceRef

NOW = datetime(2026, 6, 1, tzinfo=UTC)
CONCEPT = "concept:fixture:uploader"


def _event(memory_id: str, title: str, body: str, **fields: Any) -> MemoryDocument:
    stamp = fields.pop("created_at", NOW - timedelta(days=10))
    values: dict[str, Any] = {
        "id": memory_id,
        "title": title,
        "body": body,
        "type": fields.pop("type", "incident"),
        "about": fields.pop("about", [CONCEPT]),
        "created_at": stamp,
        "updated_at": stamp,
    }
    values.update(fields)
    return MemoryDocument(**values)


def _two_sessions() -> list[MemoryDocument]:
    return [
        _event(
            "e1",
            "Uploader timeout during nightly batch",
            "The uploader timed out because the retry backoff was missing.",
            evidence=["logs:aaaa1111@10"],
        ),
        _event(
            "e2",
            "Uploader timeout after the storage move",
            "Again the uploader timed out; adding retry backoff fixed it.",
            evidence=["logs:bbbb2222@40"],
            created_at=NOW - timedelta(days=3),
        ),
    ]


def test_two_independent_events_give_one_lesson() -> None:
    candidates, stats = distill(_two_sessions())
    (candidate,) = candidates
    assert candidate.operation == "distill" and candidate.action == "distill"
    assert candidate.memory_ids == ["e1", "e2"]
    assert candidate.oid == CONCEPT
    assert candidate.changes["independent_events"] == 2
    memory = candidate.changes["new_memory"]
    assert memory["type"] == "lesson"
    assert memory["status"] == "unconfirmed"  # an existing status value, no new one
    assert memory["about"] == [CONCEPT]
    assert memory["evidence"] == ["logs:aaaa1111@10", "logs:bbbb2222@40"]
    assert {item.ref for item in candidate.evidence} == set(memory["evidence"])
    assert "uploader" in memory["title"] and "retry" in memory["body"]
    assert stats.proposed == 1


def test_one_event_gives_nothing() -> None:
    candidates, stats = distill(_two_sessions()[:1])
    assert candidates == []
    assert stats.rejected_single_event == 1


def test_two_notes_from_one_session_are_one_event() -> None:
    docs = _two_sessions()
    docs[1] = docs[1].model_copy(update={"evidence": ["logs:aaaa1111@90"]})
    candidates, stats = distill(docs)
    assert candidates == []
    assert stats.rejected_dependent == 1


def test_uncited_notes_from_the_same_day_are_one_event() -> None:
    docs = [doc.model_copy(update={"evidence": [], "created_at": NOW}) for doc in _two_sessions()]
    assert event_key(docs[0]) == event_key(docs[1])
    assert distill(docs)[0] == []


def test_three_events_two_sessions_count_two() -> None:
    docs = [
        *_two_sessions(),
        _event(
            "e3",
            "Uploader timeout seen again",
            "The uploader timed out; retry backoff was still missing there.",
            evidence=["logs:bbbb2222@70"],
        ),
    ]
    (candidate,) = distill(docs)[0]
    assert candidate.changes["independent_events"] == 2
    assert candidate.memory_ids == ["e1", "e2", "e3"]


def test_min_events_three_needs_three_sessions() -> None:
    assert distill(_two_sessions(), min_events=3)[0] == []
    with pytest.raises(ValueError):
        distill(_two_sessions(), min_events=1)


def test_this_runs_verify_evidence_makes_events_independent() -> None:
    docs = [doc.model_copy(update={"evidence": [], "created_at": NOW}) for doc in _two_sessions()]
    verified = [
        Candidate(
            operation="verify",
            action="confirm",
            memory_ids=[doc.id],
            reason="support",
            evidence=[EvidenceRef(ref=f"logs:{doc.id}xx@1", polarity="support")],
        )
        for doc in docs
    ]
    (candidate,) = distill(docs, verified)[0]
    assert candidate.changes["independent_events"] == 2


def test_counterexamples_are_listed_but_do_not_block() -> None:
    docs = [
        *_two_sessions(),
        _event(
            "n1",
            "Uploader retry note",
            "Retry backoff did not help the uploader timeout on the test rack.",
            type="note",
        ),
        _event(
            "n2",
            "Uploader timeout",
            "Uploader timeout retry backoff.",
            type="note",
            status="disputed",
        ),
    ]
    disputed = Candidate(
        operation="verify", action="mark-disputed", memory_ids=["e2"], reason="mixed"
    )
    (candidate,) = distill(docs, [disputed])[0]
    assert candidate.changes["counterexamples"] == ["e2", "n1", "n2"]
    assert "counterexamples to check" in candidate.reason


def test_existing_lesson_suppresses_the_proposal() -> None:
    docs = [
        *_two_sessions(),
        _event(
            "l1",
            "Lesson: uploader retry backoff",
            "Uploader timed out without retry backoff; always add backoff.",
            type="lesson",
        ),
    ]
    candidates, stats = distill(docs)
    assert candidates == []
    assert stats.covered_by_lesson == 1


def test_different_concepts_do_not_combine() -> None:
    docs = _two_sessions()
    docs[1] = docs[1].model_copy(update={"about": ["concept:fixture:other"]})
    assert distill(docs)[0] == []


def test_non_events_are_ignored() -> None:
    docs = [doc.model_copy(update={"type": "note"}) for doc in _two_sessions()]
    assert distill(docs)[1].events == 0


def test_secret_in_title_drops_the_proposal() -> None:
    docs = _two_sessions()
    secret = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
    docs[0] = docs[0].model_copy(update={"title": f"Uploader timeout token {secret}"})
    candidates, stats = distill(docs)
    for candidate in candidates:
        assert secret not in json.dumps(candidate.as_dict())
    assert stats.proposed + stats.dropped_unsafe == 1


# -- through run_dream -------------------------------------------------------------------


def _vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    repository = Repository(root)
    repository.init()
    for doc in _two_sessions():
        added = repository.add(doc.title, doc.body, memory_id=doc.id)
        path = repository.root / "memories" / f"{added.id}.md"
        _, raw, body = path.read_text(encoding="utf-8").split("---\n", 2)
        meta = yaml.safe_load(raw)
        meta.update(type="incident", about=[CONCEPT], evidence=list(doc.evidence))
        dumped = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False).rstrip()
        path.write_text(f"---\n{dumped}\n---\n{body}", encoding="utf-8")
    return root


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_run_dream_reports_distill_and_dry_run_writes_nothing(tmp_path: Path) -> None:
    root = _vault(tmp_path)
    before = _snapshot(root)
    result = run_dream(root, now=NOW, dry_run=True)
    assert result.report["operations"]["distill"].startswith("run: lessons from >= 2")
    assert result.report["distill"]["proposed"] == 1
    assert [item.action for item in result.candidates if item.operation == "distill"] == ["distill"]
    assert _snapshot(root) == before


def test_run_dream_stages_the_lesson_without_authority(tmp_path: Path) -> None:
    root = _vault(tmp_path)
    memories = {key: value for key, value in _snapshot(root).items() if key.startswith("memories")}
    result = run_dream(root, now=NOW, options=DreamOptions(dream_id="dream-distill-test"))
    staged = root / str(result.staged)
    text = (staged / "concept_fixture_uploader.md").read_text(encoding="utf-8")
    front = yaml.safe_load(text.split("---\n")[1])
    assert front["authority"] is False
    (lesson,) = [item for item in front["candidates"] if item["action"] == "distill"]
    assert lesson["changes"]["new_memory"]["type"] == "lesson"
    after = {key: value for key, value in _snapshot(root).items() if key.startswith("memories")}
    assert after == memories


def test_run_dream_rejects_min_events_below_two(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        run_dream(_vault(tmp_path), now=NOW, dry_run=True, options=DreamOptions(min_events=1))


def test_cli_min_events_flag(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from memfw.cli import app

    root = _vault(tmp_path)
    runner = CliRunner()
    bad = runner.invoke(
        app, ["dream", "run", "--root", str(root), "--dry-run", "--min-events", "1"]
    )
    assert bad.exit_code == 2
    good = runner.invoke(
        app,
        [
            "dream",
            "run",
            "--root",
            str(root),
            "--dry-run",
            "--min-events",
            "3",
            "--now",
            "2026-06-01T00:00:00Z",
        ],
    )
    assert good.exit_code == 0, good.output
    assert json.loads(good.output)["report"]["distill"]["min_events"] == 3
