from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from memfw.cli import app
from memfw.core.repository import Repository


def _make_candidate(
    root: Path,
    *,
    candidate_id: str,
    proposal_id: str,
    source: str,
    body: str,
    content_hash: str | None = None,
) -> Path:
    path = root / "_candidates" / "remember" / f"{candidate_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    frontmatter: dict[str, Any] = {
        "title": candidate_id,
        "proposal_id": proposal_id,
        "source": source,
        "tenant": "",
        "tags": [],
        "related": [],
        "created_at": "2026-11-06T00:00:00Z",
        "authority": False,
    }
    if content_hash is None:
        content_hash = "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()
    frontmatter["content_hash"] = content_hash
    header = yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False)
    path.write_text(f"---\n{header}---\n\n{body}\n", encoding="utf-8")
    return path


def _apply(candidate: Path, repository: Repository):
    return CliRunner().invoke(
        app,
        ["candidate-apply", str(candidate), "--root", str(repository.root), "--apply"],
    )


def test_empty_content_hash_is_rejected_before_authoritative_write(tmp_path: Path) -> None:
    repository = Repository(tmp_path / "vault")
    repository.init()
    candidate = _make_candidate(
        repository.root,
        candidate_id="conductor-p-empty",
        proposal_id="p-empty",
        source="conductor",
        body="must not be written",
        content_hash="",
    )

    result = _apply(candidate, repository)

    assert result.exit_code != 0
    assert repository.list() == []
    # The Planner's ruling names the reason: a missing hash is its own refusal,
    # not a mismatch, and it happens before any authoritative write.
    assert json.loads(result.output)["error"] == "missing_content_hash"


@pytest.mark.parametrize(
    "injected_source",
    [
        "conductor;proposal_id=p2",
        "conductor;candidate:conductor-p2",
    ],
)
def test_source_provenance_tokens_do_not_deduplicate_another_candidate(
    tmp_path: Path, injected_source: str
) -> None:
    repository = Repository(tmp_path / "vault")
    repository.init()
    first = _make_candidate(
        repository.root,
        candidate_id="conductor-p1",
        proposal_id="p1",
        source=injected_source,
        body="first distinct body",
    )
    second = _make_candidate(
        repository.root,
        candidate_id="conductor-p2",
        proposal_id="p2",
        source="conductor",
        body="second distinct body",
    )

    first_result = _apply(first, repository)
    second_result = _apply(second, repository)

    assert first_result.exit_code == 0, first_result.output
    assert second_result.exit_code == 0, second_result.output
    documents = repository.list()
    assert len(documents) == 2
    assert {document.body for document in documents} == {
        "first distinct body",
        "second distinct body",
    }
