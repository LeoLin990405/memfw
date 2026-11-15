"""Experience points: draft, GitHub adapter, invalidation, and vault safety."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from memfw.cli import app
from memfw.core.epistemic import EXPERIENCE_DISCLAIMER
from memfw.core.errors import IntegrityError
from memfw.core.repository import Repository
from memfw.experience.candidate import CANDIDATE_DIR
from memfw.experience.cli import experience_app
from memfw.experience.github import draft_input_from_pull_request, fetch_pull_request
from memfw.experience.model import ExperienceDraftInput, build_draft, load_draft_input

runner = CliRunner()
FIXTURE = Path(__file__).parent / "fixtures" / "experience_point_example.json"

MERGED_PR: dict[str, Any] = {
    "title": "Stabilise the synthetic exporter",
    "body": "Synthetic PR body.",
    "mergedAt": "2026-01-02T03:04:05Z",
    "url": "https://github.com/example/synthetic/pull/7",
    "reviews": [
        {"author": {"login": "reviewer-one"}, "state": "APPROVED"},
        {"author": {"login": "reviewer-two"}, "state": "COMMENTED"},
    ],
    "commits": [{"oid": "a" * 40}, {"oid": "b" * 40}],
    "files": [{"path": "src/exporter.py"}, {"path": "tests/test_exporter.py"}],
    "mergedBy": {"login": "merger"},
    "author": {"login": "author"},
}


def _vault(tmp_path: Path) -> Repository:
    repository = Repository(tmp_path / "vault")
    repository.init()
    return repository


def verify(root: Path, *args: str) -> Any:
    return runner.invoke(app, [*args, "--root", str(root)])


# --- draft -------------------------------------------------------------------


def test_draft_preview_writes_nothing(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    before = sorted(path.name for path in repository.root.rglob("*"))
    result = verify(repository.root, "experience", "draft", "--from", str(FIXTURE))
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["dry_run"] is True
    assert payload["staged"] is None
    assert payload["authority"] is False
    assert payload["document"]["type"] == "experience-point"
    assert payload["document"]["causal"] == "none"
    assert EXPERIENCE_DISCLAIMER in payload["document"]["body"]
    assert not (repository.root / CANDIDATE_DIR).exists()
    assert sorted(path.name for path in repository.root.rglob("*")) == before


def test_draft_apply_stages_a_candidate_not_a_memory(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    result = verify(
        repository.root, "experience", "draft", "--from", str(FIXTURE), "--apply"
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["dry_run"] is False
    assert payload["authority"] is False
    candidate = repository.root / payload["staged"]
    assert candidate.is_file()
    text = candidate.read_text(encoding="utf-8")
    assert EXPERIENCE_DISCLAIMER in text
    assert "authority: false" in text
    front = yaml.safe_load(text.split("---\n")[1])
    assert front["type"] == "experience-point-candidate"
    assert front["verdict"] == "accepted"
    assert front["reviewer"] == "synthetic-reviewer"
    assert front["payload"]["type"] == "experience-point"
    assert repository.list() == []


def test_draft_candidate_payload_promotes_through_the_write_gate(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    result = verify(
        repository.root, "experience", "draft", "--from", str(FIXTURE), "--apply"
    )
    candidate = repository.root / json.loads(result.stdout)["staged"]
    front = yaml.safe_load(candidate.read_text(encoding="utf-8").split("---\n")[1])
    payload = dict(front["payload"])
    document = repository.add(
        payload["title"],
        payload["body"],
        memory_type=payload["type"],
        epistemic=payload["epistemic"],
        causal=payload["causal"],
        experience=payload["experience"],
        tags=payload["tags"],
        source=payload["source"],
        provenance=payload["provenance"],
    )
    assert document.type == "experience-point"
    assert document.causal == "none"
    assert repository.get(document.id).experience is not None


def test_draft_failed_verdict_is_allowed(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    data["review"]["verdict"] = "failed"
    draft_path = tmp_path / "failed.json"
    draft_path.write_text(json.dumps(data), encoding="utf-8")
    result = verify(repository.root, "experience", "draft", "--from", str(draft_path), "--apply")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["verdict"] == "failed"


@pytest.mark.parametrize(
    ("mutate", "fragment"),
    [
        (lambda data: data["review"].pop("reviewer"), "reviewer"),
        (lambda data: data["review"].update({"reviewer": ""}), "reviewer"),
        (lambda data: data["review"].update({"verdict": "maybe"}), "verdict"),
        (lambda data: data.update({"unknown": 1}), "unknown"),
    ],
)
def test_draft_requires_a_real_review(
    tmp_path: Path, mutate: Any, fragment: str
) -> None:
    repository = _vault(tmp_path)
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    mutate(data)
    draft_path = tmp_path / "bad.json"
    draft_path.write_text(json.dumps(data), encoding="utf-8")
    result = verify(repository.root, "experience", "draft", "--from", str(draft_path))
    assert result.exit_code == 2
    assert fragment in result.stdout or fragment in (result.stderr or "")


def test_draft_input_and_build_are_consistent(tmp_path: Path) -> None:
    draft_input = load_draft_input(FIXTURE)
    draft = build_draft(draft_input)
    assert draft.values["epistemic"] == "experience"
    assert draft.values["causal"] == "none"
    assert draft.values["experience"].review.verdict == "accepted"
    assert draft.values["experience"].review.reviewer == "synthetic-reviewer"
    assert draft.payload["experience"]["review"]["scope_reviewed"]


# --- GitHub adapter (synthetic JSON, no network) -----------------------------


def test_merged_pull_request_becomes_a_draft() -> None:
    data = draft_input_from_pull_request(MERGED_PR, repo="example/synthetic", pr=7)
    assert data["task"] == "Stabilise the synthetic exporter"
    assert data["review"]["verdict"] == "accepted"
    assert data["review"]["reviewer"] == "reviewer-one, reviewer-two"
    assert "pr:example/synthetic#7" in data["evidence"]
    assert f"commit:{'a' * 40}" in data["evidence"]
    assert any("pull request review" in item for item in data["review"]["scope_reviewed"])
    ExperienceDraftInput.model_validate(data)


def test_unmerged_pull_request_is_refused() -> None:
    data = dict(MERGED_PR, mergedAt="")
    with pytest.raises(IntegrityError, match="not merged"):
        draft_input_from_pull_request(data, repo="example/synthetic", pr=7)


def test_pull_request_without_reviews_names_the_merger() -> None:
    data = dict(MERGED_PR, reviews=[])
    draft = draft_input_from_pull_request(data, repo="example/synthetic", pr=7)
    assert draft["review"]["reviewer"] == "merger"
    assert any("merge decision only" in item for item in draft["review"]["scope_reviewed"])
    assert draft["review"]["not_reviewed"]
    ExperienceDraftInput.model_validate(draft)


def test_fetch_uses_gh_json_and_reports_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    class Completed:
        def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_run(command: Any) -> Completed:
        calls.append(list(command))
        return Completed(0, json.dumps(MERGED_PR))

    data = fetch_pull_request("example/synthetic", 7, runner=fake_run)
    assert data["title"] == MERGED_PR["title"]
    assert calls[0][:3] == ["gh", "pr", "view"]
    assert "--json" in calls[0]

    def failing_run(command: Any) -> Completed:
        return Completed(1, "", "boom: no such pull request")

    with pytest.raises(IntegrityError, match="gh pr view failed"):
        fetch_pull_request("example/synthetic", 999, runner=failing_run)


def test_from_github_cli_stages_without_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _vault(tmp_path)
    seen: list[tuple[str, int]] = []

    def fake_fetch(repo: str, pr: int) -> dict[str, Any]:
        seen.append((repo, pr))
        return dict(MERGED_PR)

    monkeypatch.setattr("memfw.experience.cli.fetch_pull_request", fake_fetch)
    result = runner.invoke(
        experience_app,
        ["from-github", "--repo", "example/synthetic", "--pr", "7", "--apply", "--root", str(
            repository.root
        )],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["authority"] is False
    assert (repository.root / payload["staged"]).is_file()
    assert seen == [("example/synthetic", 7)]
    assert repository.list() == []


# --- invalidation ------------------------------------------------------------


def _experience_point(repository: Repository) -> str:
    body = EXPERIENCE_DISCLAIMER + "\n\nran the synthetic checklist"
    document = repository.add(
        "Synthetic experience point",
        body,
        memory_type="experience-point",
        epistemic="experience",
        experience={
            "task": "run the synthetic checklist",
            "conditions": ["fixture vault"],
            "actions": ["run the checklist"],
            "review": {"reviewer": "synthetic-reviewer", "verdict": "accepted"},
            "invalidated_when": ["the checklist changes"],
        },
    )
    return document.id


def test_invalidate_stages_a_revision_proposal(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    memory_id = _experience_point(repository)
    before = (repository.root / "memories" / f"{memory_id}.md").read_bytes()
    result = verify(
        repository.root,
        "experience",
        "invalidate",
        memory_id,
        "--reason",
        "the export format changed",
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["action"] == "revise"
    assert payload["authority"] is False
    candidate = repository.root / payload["staged"]
    front = yaml.safe_load(candidate.read_text(encoding="utf-8").split("---\n")[1])
    assert front["type"] == "experience-invalidation"
    assert front["experience_id"] == memory_id
    assert "the export format changed" in front["suggested_changes"]["experience"][
        "invalidated_when"
    ][-1]
    assert (repository.root / "memories" / f"{memory_id}.md").read_bytes() == before


def test_invalidate_rejects_a_non_experience_point(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    plain = repository.add("plain", "an ordinary record")
    result = verify(
        repository.root, "experience", "invalidate", plain.id, "--reason", "changed"
    )
    assert result.exit_code == 2
    assert "not an experience point" in result.stdout or "not an experience point" in (
        result.stderr or ""
    )
