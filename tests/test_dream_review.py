"""Review fixes for ``memfw dream``; each test fails on c0cb01b.

Fixtures are synthetic under ``tmp_path``; secret-shaped values are built at
runtime.
"""

from __future__ import annotations

import json
import sys
import textwrap
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from memfw.core.repository import Repository
from memfw.dream import DreamOptions, run_dream
from memfw.dream.budget import check_budget
from memfw.history import TranscriptIndex, default_db, sources_from_config

NOW = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
GHP = "gh" + "p_" + "A1b2C3d4" * 5
EMAIL = "someone" + "@" + "example.org"
IP = "192.0.2." + "77"


def _vault(tmp_path: Path, **config: Any) -> Path:
    root = tmp_path / "vault"
    repository = Repository(root)
    repository.init()
    if config:
        merged = repository.read_config()
        merged.update(config)
        repository.configure(merged)
    return root


def _staged_text(root: Path, staged: str | None) -> str:
    assert staged is not None
    return "".join(path.read_text("utf-8") for path in sorted((root / staged).iterdir()))


# -- blocker 1: injected-file headings -------------------------------------------------


def test_secret_and_personal_headings_never_reach_candidates(tmp_path: Path) -> None:
    root = _vault(tmp_path)
    (root / "MEMORY.md").write_text(
        f"# Notes for {EMAIL} on {IP}\n"
        + "line\n" * 150
        + f"## GITHUB_TOKEN={GHP}\n"
        + "detail\n" * 150,
        "utf-8",
    )
    result = run_dream(root, now=NOW)
    text = _staged_text(root, result.staged) + json.dumps(result.as_dict())
    assert GHP not in text and "A1b2C3d4" not in text
    assert EMAIL not in text and IP not in text
    assert "[withheld heading: secret detected]" in text


def test_budget_proposals_are_capped_and_symlinks_reported(tmp_path: Path) -> None:
    root = _vault(tmp_path)
    target = tmp_path / "elsewhere.md"
    target.write_text("# outside\n", "utf-8")
    (root / "MEMORY.md").symlink_to(target)
    agents = root / "AGENTS.md"
    agents.write_text("".join(f"## part {n}\n" + "x\n" * 5 for n in range(300)), "utf-8")
    reports = check_budget(root, {"injected_files": ["AGENTS.md"]})
    by_path = {report.path: report for report in reports}
    assert by_path["MEMORY.md"].skipped == "symlink"
    assert by_path["AGENTS.md"].over and len(by_path["AGENTS.md"].proposals) == 20


# -- blocker 2: the vault must not choose the judge --------------------------------------


def _judge_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    module = tmp_path / "hooks"
    module.mkdir()
    marker = tmp_path / "judge-ran"
    (module / "vault_judge.py").write_text(
        textwrap.dedent(
            f"""
            from pathlib import Path

            def grab(memory_text, evidence_text):
                Path({str(marker)!r}).write_text("ran")
                return "neutral"
            """
        ),
        "utf-8",
    )
    monkeypatch.syspath_prepend(str(module))
    return marker


def test_dream_judge_in_vault_yaml_is_ignored_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = _judge_module(tmp_path, monkeypatch)
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "s.jsonl").write_text(
        json.dumps({"session_id": "s", "text": "the greenhouse sensor reads 18 degrees"}) + "\n",
        "utf-8",
    )
    root = _vault(tmp_path, dream_judge="vault_judge:grab", history_sources={"logs": str(logs)})
    repository = Repository(root)
    repository.add("Sensor", "The greenhouse sensor reads 18 degrees.", memory_id="m1")
    TranscriptIndex(default_db(root), sources_from_config(repository.read_config())).refresh()
    result = run_dream(root, now=NOW, dry_run=True, options=DreamOptions(all_memories=True))
    assert not marker.exists()
    assert result.report["judge"].startswith("none")
    assert result.report["config_judge_ignored"] is True
    allowed = run_dream(
        root,
        now=NOW,
        dry_run=True,
        options=DreamOptions(all_memories=True, allow_config_judge=True),
    )
    assert marker.exists() and allowed.report["judge"] == "local hook"
    sys.modules.pop("vault_judge", None)


# -- dry runs write nothing ---------------------------------------------------------------


def _tree(root: Path) -> dict[str, str]:
    import hashlib

    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _with_transcripts(tmp_path: Path) -> Path:
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "s.jsonl").write_text(
        json.dumps({"session_id": "s", "text": "the greenhouse sensor reads 18 degrees"}) + "\n",
        "utf-8",
    )
    root = _vault(tmp_path, history_sources={"logs": str(logs)})
    Repository(root).add("Sensor", "The greenhouse sensor reads 18 degrees.", memory_id="m1")
    return root


def test_dry_run_without_an_index_creates_nothing(tmp_path: Path) -> None:
    root = _with_transcripts(tmp_path)
    before = _tree(root)
    result = run_dream(root, now=NOW, dry_run=True, options=DreamOptions(all_memories=True))
    assert _tree(root) == before
    assert not (root / ".memfw-history.sqlite").exists()
    assert result.report["history"]["available"] is False


def test_dry_run_reads_an_existing_index_without_writing(tmp_path: Path) -> None:
    root = _with_transcripts(tmp_path)
    run_dream(root, now=NOW, options=DreamOptions(dream_id="first", all_memories=True))
    before = _tree(root)
    result = run_dream(root, now=NOW, dry_run=True, options=DreamOptions(all_memories=True))
    assert _tree(root) == before
    assert result.report["history"]["read_only"] is True
    assert any(item.action == "confirm" for item in result.candidates)


# -- staging errors ------------------------------------------------------------------------


def test_staging_through_a_file_or_symlink_is_a_clean_error(tmp_path: Path) -> None:
    from memfw.dream.stage import DreamStageError

    root = _vault(tmp_path)
    (root / "_candidates").write_text("not a directory", "utf-8")
    with pytest.raises(DreamStageError):
        run_dream(root, now=NOW, options=DreamOptions(dream_id="d1"))
    (root / "_candidates").unlink()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "_candidates").mkdir()
    (root / "_candidates" / "dream").symlink_to(outside)
    with pytest.raises(DreamStageError):
        run_dream(root, now=NOW, options=DreamOptions(dream_id="d1"))
    assert list(outside.iterdir()) == []
