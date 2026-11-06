from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import pytest
import yaml
from typer.testing import CliRunner

from memfw.cli import app
from memfw.core.errors import IntegrityError
from memfw.core.models import MemoryDocument
from memfw.core.repository import Repository


def _repository(tmp_path: Path) -> Repository:
    repository = Repository(tmp_path / "vault")
    repository.init()
    return repository


def _content_hash(content: str) -> str:
    return f"sha256:{hashlib.sha256(content.encode('utf-8')).hexdigest()}"


def _write_candidate(
    root: Path,
    filename: str,
    content: str,
    frontmatter: dict[str, Any],
) -> Path:
    path = root / "_candidates" / "remember" / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    header = yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False)
    path.write_text(f"---\n{header}---\n\n{content}\n", encoding="utf-8")
    return path


def _stage_new_candidate(
    root: Path,
    content: str,
    *,
    candidate_id: str = "candidate-new",
    proposal_id: str = "proposal-new",
    title: str = "New contract",
    source: str = "conductor",
    extra_frontmatter: dict[str, Any] | None = None,
) -> Path:
    frontmatter: dict[str, Any] = {
        "candidate_id": candidate_id,
        "proposal_id": proposal_id,
        "status": "pending",
        "title": title,
        "tags": ["new-contract"],
        "related": [],
        "tenant": "",
        "source": source,
        "content_hash": _content_hash(content),
    }
    if extra_frontmatter:
        frontmatter.update(extra_frontmatter)
    return _write_candidate(root, f"{candidate_id}.md", content, frontmatter)


def _stage_legacy_candidate(root: Path, content: str) -> Path:
    frontmatter: dict[str, Any] = {
        "name": "Legacy candidate",
        "metadata": {
            "type": "candidate",
            "lifecycle": "active",
            "tags": ["legacy-tag"],
            "related": [],
            "tenant": "",
            "suggestions": [],
        },
    }
    return _write_candidate(root, "legacy-candidate.md", content, frontmatter)


def _frontmatter(path: Path) -> dict[str, Any]:
    _prefix, header, _body = path.read_text(encoding="utf-8").split("---\n", 2)
    loaded: Any = yaml.safe_load(header)
    assert isinstance(loaded, dict)
    return cast(dict[str, Any], loaded)


def _apply(candidate: Path, repository: Repository) -> list[str]:
    return [
        "candidate-apply",
        str(candidate),
        "--root",
        str(repository.root),
        "--apply",
    ]


def test_candidates_list_supports_new_and_legacy_formats_with_top_level_precedence(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    _stage_new_candidate(
        repository.root,
        "new body",
        title="Top-level title",
        source="top-level-source",
        extra_frontmatter={
            "name": "Legacy name must not win",
            "tags": ["top-level-tag"],
            "related": ["top-level-related"],
            "tenant": "top-tenant",
            "suggestions": [{"memory_id": "top-level-suggestion"}],
            "metadata": {
                "tags": ["metadata-tag"],
                "related": ["metadata-related"],
                "tenant": "metadata-tenant",
                "suggestions": [{"memory_id": "metadata-suggestion"}],
                "source": "metadata-source",
            },
        },
    )
    legacy = _stage_legacy_candidate(repository.root, "legacy body")

    listed = CliRunner().invoke(app, ["candidates", "--root", str(repository.root)])

    assert listed.exit_code == 0, listed.output
    rows = json.loads(listed.output)["candidates"]
    new_row = next(row for row in rows if row.get("candidate_id") == "candidate-new")
    legacy_row = next(row for row in rows if row["title"] == "Legacy candidate")
    assert new_row["title"] == "Top-level title"
    assert new_row["status"] == "pending"
    assert new_row["proposal_id"] == "proposal-new"
    assert new_row["tags"] == ["top-level-tag"]
    assert new_row["related"] == ["top-level-related"]
    assert new_row["tenant"] == "top-tenant"
    assert new_row["suggestions"] == [{"memory_id": "top-level-suggestion"}]
    assert new_row["source"] == "top-level-source"
    assert new_row["content_hash"] == _content_hash("new body")
    assert legacy_row["tags"] == ["legacy-tag"]
    assert legacy_row["source"] == "candidate"

    applied = CliRunner().invoke(app, _apply(legacy, repository))

    assert applied.exit_code == 0, applied.output
    document = next(doc for doc in repository.list() if doc.title == "Legacy candidate")
    assert document.source == "candidate"


def test_candidate_dry_run_preserves_bytes_and_reports_pending_status(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    candidate = _stage_new_candidate(repository.root, "preview only")
    original_bytes = candidate.read_bytes()

    preview = CliRunner().invoke(
        app,
        [
            "candidate-apply",
            str(candidate),
            "--root",
            str(repository.root),
            "--dry-run",
        ],
    )

    assert preview.exit_code == 0, preview.output
    assert json.loads(preview.output)["status"] == "pending"
    assert repository.list() == []
    assert candidate.read_bytes() == original_bytes


def test_apply_records_provenance_transitions_status_and_is_idempotent(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    content = "  exact UTF-8 content\n保留空白  "
    candidate = _stage_new_candidate(
        repository.root,
        content,
        candidate_id="candidate-provenance",
        proposal_id="proposal-provenance",
        source="approved-source",
    )
    original_body = candidate.read_bytes().split(b"---\n", 2)[2]
    runner = CliRunner()
    pending = runner.invoke(app, ["candidates", "--root", str(repository.root)])
    assert pending.exit_code == 0, pending.output
    pending_row = next(
        row
        for row in json.loads(pending.output)["candidates"]
        if row["candidate_id"] == "candidate-provenance"
    )
    assert pending_row["status"] == "pending"

    applied = runner.invoke(app, _apply(candidate, repository))

    assert applied.exit_code == 0, applied.output
    applied_payload = json.loads(applied.output)
    assert applied_payload["status"] == "applied"
    memory_id = applied_payload["memory_id"]
    documents = repository.list()
    assert len(documents) == 1
    document = documents[0]
    assert document.id == memory_id
    assert document.source == "approved-source"
    assert document.provenance == (
        "candidate:candidate-provenance;"
        "proposal_id=proposal-provenance;"
        "source=approved-source;"
        f"content_hash={_content_hash(content)}"
    )
    marker = _frontmatter(candidate)
    assert marker["status"] == "applied"
    assert marker["applied"] is True
    assert marker["applied_memory_id"] == memory_id
    assert marker["applied_at"]
    assert candidate.read_bytes().split(b"---\n", 2)[2] == original_body

    applied_listing = runner.invoke(app, ["candidates", "--root", str(repository.root)])
    assert applied_listing.exit_code == 0, applied_listing.output
    applied_row = next(
        row
        for row in json.loads(applied_listing.output)["candidates"]
        if row["candidate_id"] == "candidate-provenance"
    )
    assert applied_row["status"] == "applied"
    assert applied_row["applied_memory_id"] == memory_id

    repeated = runner.invoke(app, _apply(candidate, repository))

    assert repeated.exit_code != 0
    repeated_payload = json.loads(repeated.output)
    assert repeated_payload["status"] == "already_applied"
    assert repeated_payload["memory_id"] == memory_id
    assert [document.id for document in repository.list()] == [memory_id]


def test_tampered_candidate_body_fails_hash_check_without_writing(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    original_content = "  content covered by the hash\n原文  "
    candidate = _stage_new_candidate(repository.root, original_content)
    original_text = candidate.read_text(encoding="utf-8")
    candidate.write_text(
        original_text.replace(original_content, "tampered body", 1),
        encoding="utf-8",
    )

    rejected = CliRunner().invoke(app, _apply(candidate, repository))

    assert rejected.exit_code != 0
    assert json.loads(rejected.output)["status"] == "hash_mismatch"
    assert repository.list() == []


def test_marker_replace_failure_preserves_candidate_and_retry_uses_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _repository(tmp_path)
    candidate = _stage_new_candidate(
        repository.root,
        "memory committed before status marker",
        candidate_id="candidate-marker-failure",
        proposal_id="proposal-marker-failure",
    )
    original_bytes = candidate.read_bytes()
    original_add = Repository.add

    def fail_replace(*args: Any, **kwargs: Any) -> None:
        raise OSError("simulated candidate marker replacement failure")

    def add_then_fail_marker(
        self: Repository,
        *args: Any,
        **kwargs: Any,
    ) -> MemoryDocument:
        document = original_add(self, *args, **kwargs)
        monkeypatch.setattr(os, "replace", fail_replace)
        return document

    monkeypatch.setattr(Repository, "add", add_then_fail_marker)
    failed = CliRunner().invoke(app, _apply(candidate, repository))

    assert failed.exit_code != 0
    documents = repository.list()
    assert len(documents) == 1
    memory_id = documents[0].id
    assert memory_id in failed.output
    assert candidate.read_bytes() == original_bytes
    assert "candidate-marker-failure" in documents[0].provenance

    monkeypatch.undo()
    retried = CliRunner().invoke(app, _apply(candidate, repository))

    assert retried.exit_code != 0
    retry_payload = json.loads(retried.output)
    assert retry_payload["status"] == "already_applied"
    assert retry_payload["memory_id"] == memory_id
    assert [document.id for document in repository.list()] == [memory_id]


def test_concurrent_processes_apply_one_candidate_without_duplicate_memory(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    candidate = _stage_new_candidate(
        repository.root,
        "one candidate applied by two real processes",
        candidate_id="candidate-concurrent",
        proposal_id="proposal-concurrent",
    )
    worktree = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(worktree / "src")
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    command = [
        sys.executable,
        "-c",
        "from memfw.cli import app; app()",
        *_apply(candidate, repository),
    ]
    processes = [
        subprocess.Popen(
            command,
            cwd=worktree,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(2)
    ]
    completed: list[tuple[str, str]] = []
    try:
        for process in processes:
            completed.append(process.communicate(timeout=30))
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
        for process in processes:
            process.communicate()

    results = [
        (cast(int, process.returncode), stdout, stderr)
        for process, (stdout, stderr) in zip(processes, completed, strict=True)
    ]
    assert sorted(returncode for returncode, _stdout, _stderr in results) == [0, 1], results
    succeeded = next(result for result in results if result[0] == 0)
    rejected = next(result for result in results if result[0] == 1)
    success_payload = json.loads(succeeded[1])
    rejected_payload = json.loads(rejected[1])
    assert success_payload["status"] == "applied"
    assert rejected_payload["status"] == "already_applied"
    assert success_payload["memory_id"] == rejected_payload["memory_id"]
    documents = repository.list()
    assert len(documents) == 1
    assert documents[0].id == success_payload["memory_id"]


def test_candidates_list_surfaces_integrity_error_for_bad_frontmatter(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    broken = repository.root / "_candidates" / "remember" / "broken.md"
    broken.parent.mkdir(parents=True)
    broken.write_text("---\nname: [broken\n---\n\nbody\n", encoding="utf-8")

    result = CliRunner().invoke(app, ["candidates", "--root", str(repository.root)])

    assert result.exit_code != 0
    assert isinstance(result.exception, IntegrityError)
