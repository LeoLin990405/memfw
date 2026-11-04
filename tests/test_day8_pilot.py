"""Privacy and approval-boundary checks for the Day 8 pilot runner."""

from __future__ import annotations

import json
import stat
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import pilot

from memfw.core.repository import Repository


def test_fake_runner_uses_adapter_and_does_not_emit_query_text() -> None:
    result = pilot.dry_run()
    serialized = json.dumps(result, sort_keys=True)
    assert result["status"] == "completed_fake_only"
    assert result["model_reason_canary_persisted"] is False
    assert result["inference_call_count"] == 10
    assert isinstance(result["budget_results"], list)
    assert len(result["budget_results"]) == 12
    assert "unique-fact-alpha" not in serialized
    assert "Synthetic fixture body" not in serialized


def test_editing_either_private_query_file_invalidates_binding(tmp_path: Path) -> None:
    repo = Repository(tmp_path / "vault")
    repo.init()
    doc = repo.add(title="Fixture", body="Synthetic fixture memory.", memory_id="fixture-1")
    pilot._write_query_file(
        repo.root / pilot.CANDIDATE_QUERIES,
        "Candidates",
        [{"target_id": doc.id, "query": "synthetic fixture"}],
    )
    pilot._write_query_file(repo.root / pilot.USER_QUERIES, "User queries", [])
    first = pilot._approval_payload(repo)
    pilot._write_query_file(
        repo.root / pilot.USER_QUERIES,
        "User queries",
        [{"target_id": doc.id, "query": "edited synthetic query"}],
    )
    second = pilot._approval_payload(repo)
    assert first["approval_sha256"] != second["approval_sha256"]


def test_evaluation_fails_closed_before_approval_and_writes_no_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pilot, "DATA_ROOT", tmp_path)
    private_root = tmp_path / "pilot-vault"
    repo = Repository(private_root)
    repo.init()
    repo.add(title="Fixture", body="Synthetic fixture memory.", memory_id="fixture-1")
    before = {path.name for path in private_root.iterdir()}
    with pytest.raises(SystemExit, match="Human approvals"):
        pilot.evaluate()
    assert {path.name for path in private_root.iterdir()} == before


def test_approval_info_reports_binding_without_approval_or_event_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(pilot, "DATA_ROOT", tmp_path)
    vault = tmp_path / "pilot-vault"
    repo = Repository(vault)
    repo.init()
    doc = repo.add(title="Fixture", body="Synthetic fixture memory.", memory_id="fixture-1")
    pilot._write_query_file(
        vault / pilot.CANDIDATE_QUERIES,
        "Candidates",
        [{"target_id": doc.id, "query": "synthetic fixture"}],
    )
    pilot._write_query_file(vault / pilot.USER_QUERIES, "User queries", [])
    before = (vault / "events.jsonl").read_bytes()

    assert pilot.main(["approval-info"]) == 0
    output = json.loads(capsys.readouterr().out)

    assert output["status"] == "ready_for_private_human_review_not_approved"
    assert output["approval_input_format"] == f"APPROVE {output['approval_sha256']}"
    assert output["candidate_query_sha256"] == pilot._sha256(
        (vault / pilot.CANDIDATE_QUERIES).read_bytes()
    )
    assert output["approved"] is False
    assert (vault / "events.jsonl").read_bytes() == before


def test_private_root_creation_repairs_existing_permissions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pilot, "DATA_ROOT", tmp_path)
    vault = tmp_path / "pilot-vault"
    nested = vault / "nested"
    nested.mkdir(parents=True)
    private_file = nested / "private.md"
    private_file.write_text("synthetic", encoding="utf-8")
    vault.chmod(0o755)
    nested.chmod(0o755)
    private_file.chmod(0o644)

    assert pilot._safe_private_root(create=True) == vault.resolve()
    assert stat.S_IMODE(vault.stat().st_mode) == 0o700
    assert stat.S_IMODE(nested.stat().st_mode) == 0o700
    assert stat.S_IMODE(private_file.stat().st_mode) == 0o600


def test_import_sets_permissions_for_new_private_vault_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "fixture.md").write_text(
        "---\ntitle: Synthetic fixture\ncreated_at: 2026-01-01T00:00:00Z\n"
        "updated_at: 2026-01-01T00:00:00Z\n---\nSynthetic fixture body.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(pilot, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(pilot, "PILOT_ROOT", source)
    monkeypatch.setattr(pilot, "PRIVATE_ROOT", tmp_path / "pilot-vault")

    result = pilot.import_records(source)
    vault = tmp_path / "pilot-vault"

    assert result["record_count"] == 1
    assert stat.S_IMODE(vault.stat().st_mode) == 0o700
    for child in vault.rglob("*"):
        if child.is_symlink():
            continue
        expected = 0o700 if child.is_dir() else 0o600
        assert stat.S_IMODE(child.stat().st_mode) == expected


def test_query_generation_reports_each_safe_failure_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = Repository(tmp_path / "vault")
    repo.init()
    for index in range(3):
        repo.add(
            title=f"Fixture {index}",
            body="Synthetic fixture body.",
            memory_id=f"fixture-{index}",
        )
    monkeypatch.setattr(pilot, "_private_repo", lambda: repo)

    class QueryFailure(ValueError):
        code = "invalid_json"

    class FailingJudge:
        def suggest_queries(self, _documents: list[Any], count: int) -> list[dict[str, str]]:
            assert count == 1
            raise QueryFailure("CANARY_PRIVATE_EXCEPTION_TEXT_9237")

    monkeypatch.setattr(pilot, "_local_judge", lambda *_args, **_kwargs: FailingJudge())
    result = cast(dict[str, Any], pilot.generate_queries(tmp_path / "unused-model", count=3))

    assert result["candidate_count"] == 0
    assert result["failure_count"] == 3
    assert [failure["reason"] for failure in result["failures"]] == ["invalid_json"] * 3
    assert all(failure["target_id"] for failure in result["failures"])
    assert "Synthetic fixture body" not in json.dumps(result)
    assert "CANARY_PRIVATE_EXCEPTION_TEXT_9237" not in json.dumps(result)


def test_day8_strict_parser_requires_exact_target_and_plain_json_shape() -> None:
    expected_id = "expected-target"
    valid = json.dumps(
        {"queries": [{"target_id": expected_id, "query": "synthetic query", "extra": "ok"}]}
    )
    wrong_target = json.dumps({"queries": [{"target_id": "other", "query": "synthetic query"}]})
    missing_target = json.dumps({"queries": [{"query": "synthetic query"}]})
    legacy_string = json.dumps({"queries": ["synthetic query"]})
    fenced = f"```json\n{valid}\n```"

    assert pilot._legacy_query_error(valid, expected_id) is None
    assert pilot._legacy_query_error(wrong_target, expected_id) == "invalid_schema"
    assert pilot._legacy_query_error(missing_target, expected_id) == "invalid_schema"
    assert pilot._legacy_query_error(legacy_string, expected_id) == "invalid_schema"
    assert pilot._legacy_query_error(fenced, expected_id) == "invalid_json"


def test_query_generation_keeps_successes_and_maps_failures_by_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = Repository(tmp_path / "vault")
    repo.init()
    docs = [
        repo.add(title=f"Fixture {index}", body="Synthetic fixture.", memory_id=f"mixed-{index}")
        for index in range(3)
    ]
    monkeypatch.setattr(pilot, "_private_repo", lambda: repo)

    class MixedJudge:
        def suggest_queries(self, selected: list[Any], count: int) -> list[dict[str, str]]:
            assert count == 1
            doc = selected[0]
            if doc.id == docs[1].id:
                raise ValueError("CANARY_PRIVATE_MIXED_FAILURE")
            return [{"target_id": doc.id, "query": "synthetic query"}]

    monkeypatch.setattr(pilot, "_local_judge", lambda *_args, **_kwargs: MixedJudge())
    result = cast(dict[str, Any], pilot.generate_queries(tmp_path / "unused", count=3))

    assert result["candidate_count"] == 2
    assert result["failed_record_ids"] == [docs[1].id]
    assert result["failure_reasons"] == ["generation_failure"]
    assert len(pilot._query_rows(repo.root / pilot.CANDIDATE_QUERIES)) == 2
    assert "CANARY_PRIVATE_MIXED_FAILURE" not in json.dumps(result)


def test_empty_judge_result_is_reported_as_empty_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = Repository(tmp_path / "vault")
    repo.init()
    repo.add(title="Fixture", body="Synthetic fixture.", memory_id="empty-result")
    monkeypatch.setattr(pilot, "_private_repo", lambda: repo)

    class EmptyJudge:
        def suggest_queries(self, _selected: list[Any], count: int) -> list[dict[str, str]]:
            assert count == 1
            return []

    monkeypatch.setattr(pilot, "_local_judge", lambda *_args, **_kwargs: EmptyJudge())
    result = cast(dict[str, Any], pilot.generate_queries(tmp_path / "unused", count=1))

    assert result["failed_record_ids"] == ["empty-result"]
    assert result["failure_reasons"] == ["empty_output"]


def test_repair_missing_queries_adds_only_missing_targets_and_keeps_private_template_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = Repository(tmp_path / "vault")
    repo.init()
    docs = [
        repo.add(
            title=f"Fixture {index}",
            body="Synthetic fixture.",
            memory_id=f"repair-{index:02}",
        )
        for index in range(30)
    ]
    candidate = repo.root / pilot.CANDIDATE_QUERIES
    pilot._write_query_file(
        candidate,
        "Existing candidates",
        [
            {"target_id": doc.id, "query": f"existing query {index}"}
            for index, doc in enumerate(docs[:29])
        ],
    )
    original = candidate.read_bytes()
    user_file = repo.root / pilot.USER_QUERIES
    user_file.write_text("USER TEMPLATE CANARY", encoding="utf-8")
    user_original = user_file.read_bytes()
    monkeypatch.setattr(pilot, "_private_repo", lambda: repo)
    monkeypatch.setattr(pilot, "_safe_private_root", lambda *, create=False: repo.root)
    monkeypatch.setattr(pilot, "_preflight_private_write", lambda _path: None)
    original_read_bytes = Path.read_bytes
    original_read_text = Path.read_text

    def guarded_read_bytes(path: Path) -> bytes:
        if path == user_file:
            raise AssertionError("repair must not read the private user-query template")
        return original_read_bytes(path)

    def guarded_read_text(path: Path, *args: Any, **kwargs: Any) -> str:
        if path == user_file:
            raise AssertionError("repair must not read the private user-query template")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_bytes", guarded_read_bytes)
    monkeypatch.setattr(Path, "read_text", guarded_read_text)

    class MissingOnlyJudge:
        calls: list[str] = []

        def suggest_queries(self, selected: list[Any], count: int) -> list[dict[str, str]]:
            assert count == 1
            self.calls.append(selected[0].id)
            return [{"target_id": selected[0].id, "query": "synthetic repair query"}]

    judge = MissingOnlyJudge()
    result = pilot.repair_missing_queries(tmp_path / "unused-model", count=30, judge=judge)

    assert result["status"] == "repaired"
    assert result["candidate_count"] == 30
    assert result["repaired_count"] == 1
    assert judge.calls == [docs[29].id]
    assert "synthetic repair query" not in json.dumps(result)
    assert "USER TEMPLATE CANARY" not in json.dumps(result)
    assert original_read_bytes(user_file) == user_original
    assert (repo.root / f"{pilot.CANDIDATE_QUERIES}.repair-backup").read_bytes() == original
    assert len(pilot._query_rows(candidate)) == 30
    second = pilot.repair_missing_queries(tmp_path / "unused-model", count=30, judge=judge)
    assert second["status"] == "complete"
    assert second["candidate_count"] == 30
    assert judge.calls == [docs[29].id]


def test_repair_missing_queries_preflights_before_repository_or_model_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_preflight(_path: Path) -> None:
        raise PermissionError("private write unavailable")

    monkeypatch.setattr(pilot, "_safe_private_root", lambda *, create=False: tmp_path)
    monkeypatch.setattr(pilot, "_preflight_private_write", fail_preflight)
    monkeypatch.setattr(
        pilot,
        "_private_repo",
        lambda: pytest.fail("repository opened before output preflight"),
    )
    with pytest.raises(PermissionError):
        pilot.repair_missing_queries(tmp_path / "unused-model")


def test_repair_missing_queries_refuses_concurrent_candidate_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = Repository(tmp_path / "vault")
    repo.init()
    docs = [
        repo.add(title=f"Fixture {index}", body="Synthetic fixture.", memory_id=f"race-{index}")
        for index in range(2)
    ]
    candidate = repo.root / pilot.CANDIDATE_QUERIES
    pilot._write_query_file(
        candidate, "Existing candidates", [{"target_id": docs[0].id, "query": "kept query"}]
    )
    monkeypatch.setattr(pilot, "_private_repo", lambda: repo)
    monkeypatch.setattr(pilot, "_safe_private_root", lambda *, create=False: repo.root)
    monkeypatch.setattr(pilot, "_preflight_private_write", lambda _path: None)

    class ConcurrentEditJudge:
        def suggest_queries(self, selected: list[Any], count: int) -> list[dict[str, str]]:
            candidate.write_text("concurrent edit", encoding="utf-8")
            return [{"target_id": selected[0].id, "query": "synthetic query"}]

    result = pilot.repair_missing_queries(
        tmp_path / "unused-model", count=2, judge=ConcurrentEditJudge()
    )
    assert result == {"status": "refused", "reason": "candidate_changed", "candidate_count": 1}
    assert candidate.read_text(encoding="utf-8") == "concurrent edit"
    assert not (repo.root / f"{pilot.CANDIDATE_QUERIES}.repair-backup").exists()


def test_repair_missing_queries_generation_failure_preserves_candidate_without_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = Repository(tmp_path / "vault")
    repo.init()
    docs = [
        repo.add(title=f"Fixture {index}", body="Synthetic fixture.", memory_id=f"fail-{index}")
        for index in range(2)
    ]
    candidate = repo.root / pilot.CANDIDATE_QUERIES
    pilot._write_query_file(
        candidate, "Existing candidates", [{"target_id": docs[0].id, "query": "kept query"}]
    )
    original = candidate.read_bytes()
    monkeypatch.setattr(pilot, "_private_repo", lambda: repo)
    monkeypatch.setattr(pilot, "_safe_private_root", lambda *, create=False: repo.root)
    monkeypatch.setattr(pilot, "_preflight_private_write", lambda _path: None)

    class FailingJudge:
        def suggest_queries(self, _selected: list[Any], count: int) -> list[dict[str, str]]:
            assert count == 1
            raise ValueError("PRIVATE_EXCEPTION_CANARY")

    result = pilot.repair_missing_queries(tmp_path / "unused-model", count=2, judge=FailingJudge())
    assert result["status"] == "partial_failure"
    assert result["reason_counts"] == {"generation_failure": 1}
    assert "PRIVATE_EXCEPTION_CANARY" not in json.dumps(result)
    assert candidate.read_bytes() == original
    assert not (repo.root / f"{pilot.CANDIDATE_QUERIES}.repair-backup").exists()


@pytest.mark.parametrize(
    ("row_ids", "expected_reason"),
    [
        ([0, 0], "duplicate_target_id"),
        (["not-a-record"], "unknown_target_id"),
        ([2], "unexpected_target_id"),
    ],
)
def test_repair_missing_queries_refuses_duplicate_foreign_or_unselected_targets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    row_ids: list[int | str],
    expected_reason: str,
) -> None:
    repo = Repository(tmp_path / "vault")
    repo.init()
    docs = [
        repo.add(title=f"Fixture {index}", body="Synthetic fixture.", memory_id=f"refuse-{index}")
        for index in range(3)
    ]
    candidate = repo.root / pilot.CANDIDATE_QUERIES
    rows = [
        {
            "target_id": row_id if isinstance(row_id, str) else docs[row_id].id,
            "query": "synthetic kept query",
        }
        for row_id in row_ids
    ]
    pilot._write_query_file(candidate, "Existing candidates", rows)
    original = candidate.read_bytes()
    monkeypatch.setattr(pilot, "_private_repo", lambda: repo)
    monkeypatch.setattr(pilot, "_safe_private_root", lambda *, create=False: repo.root)
    monkeypatch.setattr(pilot, "_preflight_private_write", lambda _path: None)

    class UnusedJudge:
        def suggest_queries(self, *_args: Any, **_kwargs: Any) -> list[dict[str, str]]:
            pytest.fail("invalid candidate set must be refused before inference")

    result = pilot.repair_missing_queries(tmp_path / "unused-model", count=2, judge=UnusedJudge())
    assert result["status"] == "refused"
    assert result["reason"] == expected_reason
    assert candidate.read_bytes() == original


def test_day9_runner_preserves_empty_backup_and_reaches_readonly_approval_info(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pilot, "DATA_ROOT", tmp_path)
    repo = Repository(tmp_path / "pilot-vault")
    repo.init()
    documents = [
        repo.add(
            title=f"Fixture {index}", body="Synthetic fixture body.", memory_id=f"fixture-{index}"
        )
        for index in range(30)
    ]
    monkeypatch.setattr(pilot, "_private_repo", lambda: repo)
    candidate = repo.root / pilot.CANDIDATE_QUERIES
    user_queries = repo.root / pilot.USER_QUERIES
    pilot._write_query_file(candidate, "Empty previous candidates", [])
    original_candidate = candidate.read_bytes()
    pilot._write_query_file(
        user_queries,
        "User queries",
        [{"target_id": documents[0].id, "query": "human fixture query"}],
    )
    original_user = user_queries.read_bytes()

    class FakeJudge:
        def __init__(
            self,
            callback: Callable[[str | None, BaseException | None], None] | None = None,
        ) -> None:
            self.diagnostic_callback = callback

        def suggest_queries(self, selected: list[Any], count: int) -> list[dict[str, str]]:
            document = selected[0]
            if self.diagnostic_callback is not None:
                self.diagnostic_callback('{"queries": []}', None)
            return [{"target_id": document.id, "query": f"synthetic query {document.id}"}]

    def fake_local_judge(
        _model: Path,
        _repo: Repository,
        *,
        diagnostic_callback: Callable[[str | None, BaseException | None], None] | None = None,
    ) -> FakeJudge:
        return FakeJudge(diagnostic_callback)

    monkeypatch.setattr(pilot, "_local_judge", fake_local_judge)
    diagnostic_log = tmp_path / "logs" / "diagnostic.jsonl"
    steps_log = tmp_path / "logs" / "steps.jsonl"

    result = pilot._day9_runner(tmp_path / "model", diagnostic_log, steps_log)

    backup = repo.root / "pilot_queries_to_review.day8-empty.md"
    assert result["status"] == "ready_for_private_human_review_not_approved"
    assert result["generation"]["candidate_count"] == 30
    assert result["approval_info"]["approved"] is False
    assert backup.is_file() and pilot._query_rows(backup) == []
    assert backup.read_bytes() == original_candidate
    assert len(pilot._query_rows(candidate)) == 30
    assert user_queries.read_bytes() == original_user
    diagnostic_rows = [
        json.loads(line) for line in diagnostic_log.read_text(encoding="utf-8").splitlines()
    ]
    diagnostic_rows = [row for row in diagnostic_rows if row.get("target_id")]
    assert len(diagnostic_rows) == 3
    assert diagnostic_rows[0]["raw_output"] == '{"queries": []}'
    assert stat.S_IMODE(diagnostic_log.stat().st_mode) == 0o600
    assert stat.S_IMODE(steps_log.stat().st_mode) == 0o600


def test_day9_private_logs_reject_repository_symlink_and_same_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pilot, "DATA_ROOT", tmp_path / "data")
    repo = tmp_path / "repository"
    repo.mkdir()
    monkeypatch.setattr(pilot, "REPO_ROOT", repo)
    linked_logs = tmp_path / "data" / "logs"
    linked_logs.parent.mkdir()
    linked_logs.symlink_to(repo, target_is_directory=True)
    with pytest.raises(SystemExit, match="cannot resolve inside the repository"):
        pilot._day9_private_log(linked_logs / "raw.jsonl")

    normal_logs = tmp_path / "data2" / "logs"
    monkeypatch.setattr(pilot, "DATA_ROOT", tmp_path / "data2")
    same_path = normal_logs / "same.jsonl"
    repo_obj = Repository(tmp_path / "pilot-vault")
    repo_obj.init()
    monkeypatch.setattr(pilot, "_private_repo", lambda: repo_obj)
    with pytest.raises(SystemExit, match="must be separate files"):
        pilot._day9_runner(tmp_path / "model", same_path, same_path)
