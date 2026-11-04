"""Day 8 runner approval and privacy regressions using temporary synthetic data only."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest

from memfw.agents.models import Proposal, canonical_payload_hash, mock_signature
from memfw.core.errors import IntegrityError
from memfw.core.repository import Repository

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import pilot

_FIXED = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
_CANARY = "PRIVATE_CANARY_7f8a9b_should_never_be_reported"


def _environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Path, Path]:
    data_root = tmp_path / "data"
    source_root = data_root / "pilot"
    source_root.mkdir(parents=True)
    monkeypatch.setattr(pilot, "DATA_ROOT", data_root)
    monkeypatch.setattr(pilot, "PILOT_ROOT", source_root)
    monkeypatch.setattr(pilot, "PRIVATE_ROOT", data_root / "pilot-vault")
    for index in range(1, 6):
        source_id = f"source-{index}"
        link = " [[source-2]]" if index == 1 else ""
        created = datetime(2026, 1, index, tzinfo=UTC).isoformat()
        updated = datetime(2026, 1, index, 1, tzinfo=UTC).isoformat()
        (source_root / f"note-{index}.md").write_text(
            f"---\nid: {source_id}\ntitle: Synthetic note {index}\n"
            f"created_at: {created}\nupdated_at: {updated}\n---\n"
            f"Synthetic body {index}{link}.\n",
            encoding="utf-8",
        )
    return data_root, source_root


def _import(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Repository, list[Any]]:
    _data_root, source_root = _environment(monkeypatch, tmp_path)
    pilot.import_records(source_root)
    repo = pilot._private_repo()
    return repo, repo.list()


def _write_query_files(repo: Repository, target_id: str, query: str = "synthetic body") -> None:
    pilot._write_query_file(
        repo.root / pilot.CANDIDATE_QUERIES,
        "Synthetic candidates",
        [{"target_id": target_id, "query": query}],
    )
    pilot._write_query_file(
        repo.root / pilot.USER_QUERIES,
        "Synthetic user queries",
        [{"target_id": target_id, "query": "body synthetic"}],
    )


def _approve_queries(monkeypatch: pytest.MonkeyPatch, repo: Repository) -> None:
    monkeypatch.setattr("builtins.input", lambda prompt: prompt.rsplit(" ", 1)[-1].rstrip(": "))
    # The approval command requires the literal APPROVE prefix and exact digest.
    monkeypatch.setattr(
        "builtins.input",
        lambda prompt: f"APPROVE {prompt.split('APPROVE ', 1)[1].rstrip(': ')}",
    )
    pilot.approve_queries()


def _create_score_proposals(repo: Repository) -> list[Proposal]:
    proposals: list[Proposal] = []
    for index, document in enumerate(repo.list()):
        score = (index + 1) / 10
        output: dict[str, Any] = {
            "score": score,
            "input_hash": canonical_payload_hash(document.model_dump(mode="json")),
        }
        payload_hash = canonical_payload_hash(output)
        target_hash = canonical_payload_hash(document.model_dump(mode="json"))
        input_hash = canonical_payload_hash([document.id, score])
        proposal_id = f"fake-score-{index + 1}"
        proposal = Proposal(
            proposal_id=proposal_id,
            kind="salience",
            target_ids=(document.id,),
            target_hashes={document.id: target_hash},
            agent_name="mlx",
            model_name="fake-only-v1",
            input_hash=input_hash,
            output=output,
            reason="Synthetic test score only.",
            payload_hash=payload_hash,
            actor="agent:mlx",
            signature=mock_signature(
                proposal_id, input_hash, payload_hash, _FIXED, {document.id: target_hash}
            ),
            created_at=_FIXED,
            updated_at=_FIXED,
        )
        repo.record_proposal(proposal)
        proposals.append(proposal)
    return proposals


def _approve_scores(monkeypatch: pytest.MonkeyPatch, repo: Repository) -> list[Proposal]:
    proposals = _create_score_proposals(repo)
    monkeypatch.setattr(
        "builtins.input",
        lambda prompt: f"APPROVE {prompt.split('APPROVE ', 1)[1].rstrip(': ')}",
    )
    pilot.approve_salience()
    return proposals


def _write_forged_score_sidecar(repo: Repository, proposals: list[Proposal]) -> None:
    pairs = [
        {"proposal_id": item.proposal_id, "payload_hash": item.payload_hash} for item in proposals
    ]
    (repo.root / "score_approval.json").write_text(
        json.dumps({"proposal_pairs": pairs, "approval_sha256": "forged"}), encoding="utf-8"
    )


def _write_score_sidecar(repo: Repository, proposals: list[Proposal]) -> None:
    from pilot_evaluation import corpus_fingerprint

    pairs = [
        {"proposal_id": item.proposal_id, "payload_hash": item.payload_hash} for item in proposals
    ]
    pairs.sort(key=lambda item: item["proposal_id"])
    (repo.root / "score_approval.json").write_text(
        json.dumps(
            {
                "proposal_pairs": pairs,
                "approval_sha256": "test-approved",
                "corpus_fingerprint": corpus_fingerprint(repo.list()),
            }
        ),
        encoding="utf-8",
    )


def test_import_preserves_times_and_resolves_wikilinks_in_repository(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo, documents = _import(monkeypatch, tmp_path)
    first = next(item for item in documents if item.title == "Synthetic note 1")
    second = next(item for item in documents if item.title == "Synthetic note 2")

    assert len(documents) == 5
    assert first.created_at == datetime(2026, 1, 1, tzinfo=UTC)
    assert first.updated_at == datetime(2026, 1, 1, 1, tzinfo=UTC)
    assert "[[source-2]]" in first.body
    provenance = json.loads(first.provenance)
    assert second.id in provenance
    assert any(value.startswith("markdown:") for value in provenance)
    assert repo.get(first.id).created_at == first.created_at


def test_forged_query_approval_sidecar_without_human_event_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo, documents = _import(monkeypatch, tmp_path)
    _approve_scores(monkeypatch, repo)
    _write_query_files(repo, documents[0].id)
    binding = pilot._approval_payload(repo)
    (repo.root / "query_approval.json").write_text(json.dumps(binding), encoding="utf-8")
    # Score approvals are genuinely recorded; only the query sidecar is forged.

    with pytest.raises(SystemExit, match="human.*approval|approval.*event"):
        pilot.evaluate()


def test_unapproved_score_proposals_do_not_satisfy_evaluation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo, documents = _import(monkeypatch, tmp_path)
    proposals = _create_score_proposals(repo)
    _write_query_files(repo, documents[0].id)
    _approve_queries(monkeypatch, repo)
    _write_forged_score_sidecar(repo, proposals)

    with pytest.raises(SystemExit, match="approved|approval"):
        pilot.evaluate()


def test_stale_approved_scores_are_rejected_after_target_changes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo, documents = _import(monkeypatch, tmp_path)
    proposals = _approve_scores(monkeypatch, repo)
    _write_query_files(repo, documents[0].id)
    _approve_queries(monkeypatch, repo)
    repo.update(documents[0].id, title="Changed synthetic title")
    # Bind a fresh query snapshot so rejection must come from the stale score proposal.
    _approve_queries(monkeypatch, repo)
    _write_score_sidecar(repo, proposals)

    with pytest.raises((SystemExit, IntegrityError), match="stale|snapshot|score"):
        pilot.evaluate()


def test_query_mutation_after_approval_invalidates_evaluation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo, documents = _import(monkeypatch, tmp_path)
    _approve_scores(monkeypatch, repo)
    _write_query_files(repo, documents[0].id)
    _approve_queries(monkeypatch, repo)
    _write_query_files(repo, documents[0].id, query="mutated synthetic query")

    with pytest.raises(SystemExit, match="changed|stale|approval"):
        pilot.evaluate()


def test_query_parser_rejects_malformed_rows_and_unknown_targets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo, documents = _import(monkeypatch, tmp_path)
    bad = repo.root / pilot.CANDIDATE_QUERIES
    bad.write_text(
        "# Bad rows\n\n```json\n"
        + json.dumps(
            [
                {"target_id": documents[0].id, "query": "valid row"},
                {"target_id": "", "query": ""},
            ]
        )
        + "\n```\n",
        encoding="utf-8",
    )
    pilot._write_query_file(repo.root / pilot.USER_QUERIES, "Empty", [])

    payload = pilot._approval_payload(repo)
    target_ids = cast(list[str], payload["target_ids"])
    assert documents[0].id in target_ids

    bad.write_text(
        "# Partial blank\n\n```json\n"
        + json.dumps(
            [
                {"target_id": documents[0].id, "query": "valid row"},
                {"target_id": documents[0].id, "query": ""},
            ]
        )
        + "\n```\n",
        encoding="utf-8",
    )
    with pytest.raises(SystemExit):
        pilot._approval_payload(repo)

    pilot._write_query_file(
        bad, "Unknown target", [{"target_id": "not-in-vault", "query": "synthetic"}]
    )
    pilot._write_query_file(repo.root / pilot.USER_QUERIES, "Empty", [])
    with pytest.raises(SystemExit, match="target|query|review"):
        pilot._approval_payload(repo)


def test_private_query_symlink_outside_vault_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo, documents = _import(monkeypatch, tmp_path)
    outside = tmp_path / "outside.md"
    outside.write_text(
        json.dumps([{"target_id": documents[0].id, "query": _CANARY}]), encoding="utf-8"
    )
    (repo.root / pilot.CANDIDATE_QUERIES).symlink_to(outside)
    pilot._write_query_file(repo.root / pilot.USER_QUERIES, "Empty", [])

    with pytest.raises(SystemExit, match="path|vault|symlink|inside"):
        pilot._approval_payload(repo)


def test_canary_private_query_text_never_appears_in_stdout_or_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, documents = _import(monkeypatch, tmp_path)
    _write_query_files(repo, documents[0].id, query=_CANARY)
    pilot._write_query_file(
        repo.root / pilot.USER_QUERIES,
        "Unknown target",
        [{"target_id": "not-in-vault", "query": _CANARY}],
    )

    with pytest.raises(SystemExit) as caught:
        pilot._approval_payload(repo)
    output = capsys.readouterr().out + str(caught.value)

    assert _CANARY not in output


def test_score_approval_command_handles_proposal_models(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo, _documents = _import(monkeypatch, tmp_path)
    _create_score_proposals(repo)
    monkeypatch.setattr(
        "builtins.input",
        lambda prompt: f"APPROVE {prompt.split('APPROVE ', 1)[1].rstrip(': ')}",
    )

    result = pilot.approve_salience()

    assert result["status"] == "approved"
    assert result["proposal_count"] == 5
