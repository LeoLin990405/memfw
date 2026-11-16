from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from memfw.capture.lifecycle import mark_expired_candidates
from memfw.capture.review import InvalidProposalIdError, list_staged_candidates, read_candidate
from memfw.cli import app
from memfw.core.repository import Repository


def _repository(tmp_path: Path) -> Repository:
    repository = Repository(tmp_path / "vault")
    repository.init()
    return repository


def _write_candidate(
    root: Path,
    filename: str,
    content: str,
    frontmatter: dict[str, Any],
    *,
    with_hash: bool = True,
) -> Path:
    path = root / "_candidates" / "remember" / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    header_fields = dict(frontmatter)
    if with_hash:
        header_fields.setdefault(
            "content_hash", "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()
        )
    header = yaml.safe_dump(header_fields, allow_unicode=True, sort_keys=False)
    path.write_text(f"---\n{header}---\n\n{content}\n", encoding="utf-8")
    return path


@pytest.mark.parametrize("bad", [False, True, 0, 7, [], ["p"], {"a": 1}])
def test_read_candidate_rejects_non_string_proposal_id(tmp_path: Path, bad: object) -> None:
    path = _write_candidate(tmp_path, "c.md", "body", {"title": "t", "proposal_id": bad})
    with pytest.raises(InvalidProposalIdError):
        read_candidate(path)


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_read_candidate_treats_blank_proposal_id_as_missing(tmp_path: Path, blank: str) -> None:
    path = _write_candidate(tmp_path, "c.md", "body", {"title": "t", "proposal_id": blank})
    assert read_candidate(path).proposal_id is None


def test_read_candidate_rejects_non_string_proposal_id_in_metadata(tmp_path: Path) -> None:
    path = _write_candidate(
        tmp_path, "c.md", "body", {"title": "t", "metadata": {"proposal_id": False}}
    )
    with pytest.raises(InvalidProposalIdError):
        read_candidate(path)


def test_read_candidate_keeps_valid_and_missing_proposal_id(tmp_path: Path) -> None:
    good = _write_candidate(tmp_path, "good.md", "body", {"title": "t", "proposal_id": "p-1"})
    legacy = _write_candidate(tmp_path, "legacy.md", "body", {"title": "t"})
    assert read_candidate(good).proposal_id == "p-1"
    assert read_candidate(legacy).proposal_id is None


@pytest.mark.parametrize(
    ("bad", "error"),
    [("", "missing_proposal_id"), ("   ", "missing_proposal_id"), (False, "invalid_proposal_id")],
)
def test_candidate_apply_refuses_bad_proposal_id(tmp_path: Path, bad: object, error: str) -> None:
    repository = _repository(tmp_path)
    root = repository.root
    path = _write_candidate(root, "c.md", "applied body", {"title": "t", "proposal_id": bad})
    before = sorted(p.name for p in root.rglob("*.md") if "_candidates" not in p.parts)

    result = CliRunner().invoke(app, ["candidate-apply", str(path), "--root", str(root), "--apply"])

    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"] == error
    after = sorted(p.name for p in root.rglob("*.md") if "_candidates" not in p.parts)
    assert after == before
    assert "applied: true" not in path.read_text(encoding="utf-8")


def _stage_listing(root: Path, outside: Path) -> None:
    _write_candidate(root, "good.md", "good body", {"title": "good", "proposal_id": "p-good"})
    # Body edited after staging: the hash no longer matches.
    _write_candidate(
        root,
        "tampered.md",
        "edited body",
        {"title": "bad", "proposal_id": "p-bad", "content_hash": "sha256:" + "0" * 64},
    )
    (root / "_candidates" / "remember" / "broken.md").write_text(
        "---\n: [unclosed\n---\nbody\n", encoding="utf-8"
    )
    outside.mkdir(parents=True, exist_ok=True)
    secret = _write_candidate(outside, "secret.md", "secret", {"title": "s", "proposal_id": "p-s"})
    (root / "_candidates" / "remember" / "escape.md").symlink_to(secret)


def test_list_staged_candidates_skips_bad_files(tmp_path: Path) -> None:
    from memfw.capture.review import list_staged_candidates

    root = tmp_path / "vault"
    _stage_listing(root, tmp_path / "outside")

    skipped: list[str] = []
    rows = list_staged_candidates(root, skipped=skipped)

    assert [row["proposal_id"] for row in rows] == ["p-good"]
    assert sorted(skipped) == ["broken", "escape", "tampered"]
    # The existing call shape keeps working and does not raise.
    assert [row["proposal_id"] for row in list_staged_candidates(root)] == ["p-good"]


def test_list_staged_candidates_keeps_symlink_inside_vault(tmp_path: Path) -> None:
    from memfw.capture.review import list_staged_candidates

    root = tmp_path / "vault"
    target = _write_candidate(
        root / "kept", "inner.md", "inner", {"title": "i", "proposal_id": "p-i"}
    )
    remember = root / "_candidates" / "remember"
    remember.mkdir(parents=True, exist_ok=True)
    (remember / "inner.md").symlink_to(target)

    skipped: list[str] = []
    assert [r["proposal_id"] for r in list_staged_candidates(root, skipped=skipped)] == ["p-i"]
    assert skipped == []


def test_candidates_cli_survives_tampered_and_outside_symlink(tmp_path: Path) -> None:
    import json

    repository = _repository(tmp_path)
    root = repository.root
    _stage_listing(root, tmp_path / "outside")

    result = CliRunner().invoke(app, ["candidates", "--root", str(root)])

    assert result.exception is None, result.output
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert [row["proposal_id"] for row in payload["candidates"]] == ["p-good"]
    assert "secret" not in result.stdout


def test_symlinked_candidates_dir_outside_vault_is_skipped(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    root = repository.root
    outside = tmp_path / "outside" / "remember"
    outside.mkdir(parents=True)
    secret = outside / "secret.md"
    body = "outside body"
    header = yaml.safe_dump(
        {
            "title": "secret",
            "proposal_id": "p-secret",
            "recorded_at": "2000-01-01T00:00:00Z",
            "status": "pending",
            "content_hash": "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest(),
        },
        sort_keys=False,
    )
    secret.write_text(f"---\n{header}---\n\n{body}\n", encoding="utf-8")
    before = secret.read_bytes()
    (root / "_candidates").mkdir()
    (root / "_candidates" / "remember").symlink_to(outside, target_is_directory=True)

    skipped: list[str] = []
    assert list_staged_candidates(root, include_expired=True, skipped=skipped) == []
    assert skipped == ["secret"]
    result = CliRunner().invoke(app, ["candidates", "--root", str(root), "--include-expired"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"candidates": [], "skipped": ["secret"]}
    assert "outside body" not in result.stdout

    report = mark_expired_candidates(root)
    assert report["skipped"] == ["secret"]
    assert secret.read_bytes() == before
