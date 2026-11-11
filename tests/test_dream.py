"""Tests for ``memfw dream``: verify-first, proposal-only, deterministic.

All fixtures are synthetic and live under ``tmp_path``; no model, no network.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import textwrap
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from memfw.cli import app
from memfw.core.errors import IntegrityError
from memfw.core.repository import Repository
from memfw.dream import DreamOptions, classify, load_judge, run_dream
from memfw.dream.stage import DreamExistsError
from memfw.history import TranscriptIndex, default_db, sources_from_config

NOW = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*:[0-9a-f]{16}@\d+$")
TRANSCRIPT_ONLY = "zebrafinch"


def _line(**fields: Any) -> str:
    return json.dumps(fields) + "\n"


def _set_meta(repository: Repository, memory_id: str, **fields: Any) -> None:
    path = repository.root / "memories" / f"{memory_id}.md"
    _, raw, body = path.read_text(encoding="utf-8").split("---\n", 2)
    meta = yaml.safe_load(raw)
    meta.update(fields)
    dumped = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False).rstrip()
    path.write_text(f"---\n{dumped}\n---\n{body}", encoding="utf-8")


def _vault(tmp_path: Path, *, history: bool = True) -> Path:
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "s1.jsonl").write_text(
        _line(
            session_id="s1",
            ts="2026-01-01T10:00:00Z",
            text=f"{TRANSCRIPT_ONLY}: the greenhouse humidity sensor is calibrated to 41 percent",
        )
        + _line(
            session_id="s1",
            ts="2026-01-01T10:05:00Z",
            text="the irrigation valve pressure is set to 18 bar for now",
        ),
        "utf-8",
    )
    (logs / "s2.jsonl").write_text(
        _line(
            session_id="s2",
            ts="2026-02-01T09:00:00Z",
            text=f"{TRANSCRIPT_ONLY} update: irrigation valve pressure is set to 21 bar",
        )
        + _line(
            session_id="s2",
            ts="2026-02-01T09:10:00Z",
            text="nightly backups do not run on the staging cluster any more",
        )
        + _line(session_id="s2", ts="2026-02-01T09:20:00Z", text="password: " + "hunter2" + "abc"),
        "utf-8",
    )
    root = tmp_path / "vault"
    repository = Repository(root)
    repository.init()
    repository.add(
        "Humidity sensor",
        "The greenhouse humidity sensor is calibrated to 41 percent.",
        memory_id="m-humidity",
    )
    repository.add(
        "Irrigation valve",
        "The irrigation valve pressure is set to 18 bar.",
        memory_id="m-valve",
    )
    repository.add(
        "Backups",
        "Nightly backups run on the staging cluster.",
        memory_id="m-backups",
    )
    repository.add("Unrelated", "The office plant needs water on Fridays.", memory_id="m-plant")
    _set_meta(repository, "m-humidity", about=["concept:sensor:humidity"])
    _set_meta(repository, "m-valve", about=["concept:device:valve"])
    _set_meta(repository, "m-backups", about=["concept:ops:backups"])
    if history:
        config = repository.read_config()
        config["history_sources"] = {"logs": str(logs)}
        repository.configure(config)
        _build_index(root)
    return root


def _build_index(root: Path) -> None:
    """Dry runs never build the transcript index; tests build it up front."""

    config = Repository(root).read_config()
    TranscriptIndex(default_db(root), sources_from_config(config)).refresh()


def _vault_state(root: Path) -> dict[str, str]:
    state = {}
    for path in sorted((root / "memories").glob("*.md")):
        state[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    state["events"] = hashlib.sha256((root / "events.jsonl").read_bytes()).hexdigest()
    return state


def _by_memory(result: Any) -> dict[str, dict[str, Any]]:
    return {
        candidate.memory_ids[0]: candidate.as_dict()
        for candidate in result.candidates
        if candidate.operation == "verify"
    }


# -- operation ① verify ----------------------------------------------------------------


def test_verify_confirms_corrects_and_disputes(tmp_path: Path) -> None:
    root = _vault(tmp_path)
    result = run_dream(root, now=NOW, dry_run=True)
    verified = _by_memory(result)
    assert verified["m-humidity"]["action"] == "confirm"
    assert verified["m-humidity"]["changes"]["status"] == "confirmed"
    assert verified["m-valve"]["action"] == "correct"
    assert verified["m-valve"]["changes"]["body"] == (
        "The irrigation valve pressure is set to 21 bar."
    )
    assert verified["m-backups"]["action"] == "mark-disputed"
    assert verified["m-backups"]["changes"]["status"] == "disputed"
    assert "m-plant" not in verified  # not cited: no concept, no references
    for candidate in verified.values():
        assert candidate["evidence"]
        for item in candidate["evidence"]:
            assert REF.match(item["ref"])
            assert "/" not in item["ref"] and ".jsonl" not in item["ref"]
        assert all(REF.match(ref) for ref in candidate["changes"]["evidence"])
    stats = result.report["verify"]
    assert stats["sessions_read"] == 2 and stats["memories_checked"] == 3


def test_all_flag_includes_uncited_memories(tmp_path: Path) -> None:
    root = _vault(tmp_path)
    result = run_dream(root, now=NOW, dry_run=True, options=DreamOptions(all_memories=True))
    assert result.report["memories"]["verified"] == 4


def test_secret_entries_never_become_evidence(tmp_path: Path) -> None:
    root = _vault(tmp_path)
    result = run_dream(root, now=NOW)
    staged = (root / result.staged).rglob("*") if result.staged else []
    text = "".join(path.read_text("utf-8") for path in staged if path.is_file())
    assert "hunter2" not in text


def test_max_sessions_caps_the_transcripts_read(tmp_path: Path) -> None:
    root = _vault(tmp_path)
    result = run_dream(root, now=NOW, dry_run=True, options=DreamOptions(max_sessions=1))
    assert result.report["verify"]["sessions_read"] == 1


def test_classify_rules() -> None:
    memory = "The irrigation valve pressure is set to 18 bar."
    assert classify(memory, "irrigation valve pressure is set to 18 bar")[0] == "support"
    assert classify(memory, "irrigation valve pressure is set to 21 bar") == (
        "contradict",
        {"21"},
    )
    assert classify(memory, "the valve pressure is not 18 bar")[0] == "contradict"
    assert classify(memory, "lunch is at noon")[0] == "neutral"
    assert classify(memory, "[REDACTED:secret] irrigation")[0] == "neutral"


# -- staging and authority ----------------------------------------------------------------


def test_run_stages_one_file_per_concept_and_never_writes_the_vault(tmp_path: Path) -> None:
    root = _vault(tmp_path)
    before = _vault_state(root)
    result = run_dream(root, now=NOW, options=DreamOptions(dream_id="dream-test"))
    assert _vault_state(root) == before
    assert result.staged == "_candidates/dream/dream-test"
    directory = root / "_candidates" / "dream" / "dream-test"
    names = sorted(path.name for path in directory.iterdir())
    assert "report.json" in names
    assert "concept_sensor_humidity.md" in names and "concept_device_valve.md" in names
    for path in directory.glob("*.md"):
        text = path.read_text("utf-8")
        _, raw, _body = text.split("---\n", 2)
        front = yaml.safe_load(raw)
        assert front["authority"] is False and front["dream_id"] == "dream-test"
        assert TRANSCRIPT_ONLY not in text
        assert str(tmp_path) not in text and ".jsonl" not in text
    report = json.loads((directory / "report.json").read_text("utf-8"))
    assert report["authority"] is False
    assert report["operations"]["distill"].startswith("stub")
    assert TRANSCRIPT_ONLY not in json.dumps(report)


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    root = _vault(tmp_path)
    before = _vault_state(root)
    result = run_dream(root, now=NOW, dry_run=True)
    assert result.staged is None
    assert not (root / "_candidates").exists()
    assert _vault_state(root) == before


def test_runs_are_deterministic(tmp_path: Path) -> None:
    root = _vault(tmp_path)
    run_dream(root, now=NOW, dry_run=True)  # builds the transcript index once
    options = DreamOptions(refresh=False)
    first = run_dream(root, now=NOW, dry_run=True, options=options).as_dict()
    second = run_dream(root, now=NOW, dry_run=True, options=options).as_dict()
    assert first == second
    assert first["dream_id"].startswith("dream-20260301T120000Z-")


def test_concurrent_dreams_never_overwrite_each_other(tmp_path: Path) -> None:
    root = _vault(tmp_path)
    run_dream(root, now=NOW, options=DreamOptions(dream_id="dream-a"))
    with pytest.raises(DreamExistsError):
        run_dream(root, now=NOW, options=DreamOptions(dream_id="dream-a"))
    run_dream(root, now=NOW, options=DreamOptions(dream_id="dream-b"))
    base = root / "_candidates" / "dream"
    assert sorted(path.name for path in base.iterdir()) == ["dream-a", "dream-b"]
    with pytest.raises(ValueError):
        run_dream(root, now=NOW, options=DreamOptions(dream_id="../escape"))


def test_without_history_sources_verify_is_skipped(tmp_path: Path) -> None:
    root = _vault(tmp_path, history=False)
    result = run_dream(root, now=NOW, dry_run=True)
    assert result.report["history"]["available"] is False
    assert not [item for item in result.candidates if item.operation == "verify"]


# -- item 4: injected-layer budget -----------------------------------------------------------


def test_over_budget_memory_md_gets_a_demote_proposal(tmp_path: Path) -> None:
    root = _vault(tmp_path)
    sections = "".join(f"## Topic {n}\n" + "detail line\n" * 30 for n in range(10))
    (root / "MEMORY.md").write_text(sections, "utf-8")
    before = (root / "MEMORY.md").read_bytes()
    result = run_dream(root, now=NOW, dry_run=True)
    demotes = [item for item in result.candidates if item.action == "demote"]
    assert len(demotes) == 1 and demotes[0].operation == "promote"
    assert result.report["budget"][0]["over"] is True
    assert (root / "MEMORY.md").read_bytes() == before


# -- item 3: status and evidence fields ---------------------------------------------------------


def test_status_and_evidence_fields_round_trip(tmp_path: Path) -> None:
    repository = Repository(tmp_path / "vault")
    repository.init()
    repository.add("Legacy", "an entry written before the fields existed", memory_id="m-old")
    raw = (repository.root / "memories" / "m-old.md").read_text("utf-8")
    assert "status:" not in raw and "evidence:" not in raw
    legacy = repository.get("m-old")
    assert legacy.status == "unconfirmed" and legacy.evidence == []
    _set_meta(repository, "m-old", status="confirmed", evidence=["logs:0123456789abcdef@0"])
    loaded = repository.get("m-old")
    assert loaded.status == "confirmed" and loaded.evidence == ["logs:0123456789abcdef@0"]
    rendered = repository.render(loaded)
    assert "status: confirmed" in rendered and "logs:0123456789abcdef@0" in rendered
    _set_meta(repository, "m-old", status="maybe")
    with pytest.raises(IntegrityError):
        repository.get("m-old")


# -- optional local judge -----------------------------------------------------------------------


def test_local_judge_hook_is_optional_and_isolated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _vault(tmp_path)
    module = tmp_path / "hooks"
    module.mkdir()
    (module / "fake_judge.py").write_text(
        textwrap.dedent(
            """
            def always_contradict(memory_text, evidence_text):
                return "contradict"

            def broken(memory_text, evidence_text):
                raise RuntimeError("judge failed")
            """
        ),
        "utf-8",
    )
    monkeypatch.syspath_prepend(str(module))
    flipped = run_dream(
        root, now=NOW, dry_run=True, options=DreamOptions(judge="fake_judge:always_contradict")
    )
    assert _by_memory(flipped)["m-humidity"]["action"] != "confirm"
    assert flipped.report["judge"] == "local hook"
    broken = run_dream(root, now=NOW, dry_run=True, options=DreamOptions(judge="fake_judge:broken"))
    assert broken.report["verify"]["judge_errors"] > 0
    assert _by_memory(broken)["m-humidity"]["action"] == "confirm"
    with pytest.raises(ValueError):
        load_judge("no-colon")
    assert load_judge(None) is None
    sys.modules.pop("fake_judge", None)


# -- CLI ------------------------------------------------------------------------------------------


def test_cli_dream_run(tmp_path: Path) -> None:
    root = _vault(tmp_path)
    runner = CliRunner()
    dry = runner.invoke(
        app, ["dream", "run", "--root", str(root), "--dry-run", "--now", "2026-03-01T12:00:00Z"]
    )
    assert dry.exit_code == 0, dry.output
    payload = json.loads(dry.stdout)
    assert payload["dry_run"] is True and payload["authority"] is False
    assert not (root / "_candidates").exists()
    staged = runner.invoke(
        app, ["dream", "run", "--root", str(root), "--now", "2026-03-01T12:00:00Z"]
    )
    assert staged.exit_code == 0, staged.output
    assert (root / json.loads(staged.stdout)["staged"] / "report.json").is_file()
    again = runner.invoke(
        app, ["dream", "run", "--root", str(root), "--now", "2026-03-01T12:00:00Z"]
    )
    assert again.exit_code == 2 and "already staged" in again.stderr
    missing = runner.invoke(app, ["dream", "run", "--root", str(tmp_path / "nope")])
    assert missing.exit_code == 2


def test_human_can_apply_status_and_evidence_through_update(tmp_path: Path) -> None:
    root = _vault(tmp_path)
    result = run_dream(root, now=NOW, dry_run=True)
    confirm = _by_memory(result)["m-humidity"]
    runner = CliRunner()
    args = ["update", "m-humidity", "--root", str(root), "--status", "confirmed"]
    for ref in confirm["changes"]["evidence"]:
        args += ["--evidence", ref]
    applied = runner.invoke(app, args)
    assert applied.exit_code == 0, applied.output
    stored = Repository(root).get("m-humidity")
    assert stored.status == "confirmed" and stored.evidence == confirm["changes"]["evidence"]
    bad = runner.invoke(app, ["update", "m-humidity", "--root", str(root), "--status", "maybe"])
    assert bad.exit_code != 0
