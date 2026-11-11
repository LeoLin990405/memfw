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


def test_injected_file_through_a_symlinked_directory_is_skipped(tmp_path: Path) -> None:
    root = _vault(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "AGENTS.md").write_text("# a\n" + "y\n" * 400, "utf-8")
    (root / "linkdir").symlink_to(outside)
    (report,) = check_budget(root, {"injected_files": ["linkdir/AGENTS.md"]})
    assert report.skipped == "symlink" and report.over is False


# -- curate: subjects and negation (the review's curate5 cases) ---------------------------


CURATE_CASES = {
    "env_port": (
        ["The web port for staging is 8080.", "The web port for production is 8080."],
        set(),
    ),
    "negation": (
        [
            "Nightly backups run on the staging cluster at 02:00.",
            "Nightly backups do not run on the staging cluster at 02:00.",
        ],
        {"supersede"},
    ),
    "two_services": (
        ["- service: api\n- region: us-east-1", "- service: worker\n- region: eu-west-1"],
        set(),
    ),
    "different_people": (
        [
            "Alice uses a laptop with 32 GB of memory and 2 TB disk.",
            "Bob uses a laptop with 32 GB of memory and 2 TB disk.",
        ],
        set(),
    ),
    "count_vs_times": (
        [
            "The infra team has 5 members on call.",
            "The infra team has on call shifts 3 times a week.",
        ],
        set(),
    ),
    "true_dup": (
        ["The irrigation valve pressure is 18 bar.", "Irrigation valve pressure: 18 bar."],
        {"merge"},
    ),
    "true_conflict": (
        ["The irrigation valve pressure is 18 bar.", "The irrigation valve pressure is 21 bar."],
        {"supersede"},
    ),
}


@pytest.mark.parametrize("name", sorted(CURATE_CASES))
def test_curate_respects_subjects_and_negation(name: str) -> None:
    from datetime import timedelta

    from memfw.core.models import MemoryDocument
    from memfw.dream.curate import curate

    bodies, expected = CURATE_CASES[name]
    documents = [
        MemoryDocument(
            id=f"{name}-{index}",
            title="note",
            body=body,
            about=["concept:x:shared"],
            reference_count=1,
            created_at=NOW - timedelta(days=10 - index),
            updated_at=NOW - timedelta(days=10 - index),
        )
        for index, body in enumerate(bodies)
    ]
    actions = {candidate.action for candidate in curate(documents, now=NOW)}
    assert actions == expected


# -- correct never copies a phone- or card-like value -----------------------------------------


def test_correct_refuses_long_digit_replacements(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    phone = "41555" + "52671"
    (logs / "s.jsonl").write_text(
        json.dumps(
            {
                "session_id": "s",
                "ts": "2026-01-04T00:00:00Z",
                "text": f"the door pin code for the office router room is {phone}",
            }
        )
        + "\n",
        "utf-8",
    )
    root = _vault(tmp_path, history_sources={"logs": str(logs)})
    Repository(root).add(
        "Door pin", "The door pin code for the office router room is 1234.", memory_id="m-pin"
    )
    result = run_dream(root, now=NOW, options=DreamOptions(all_memories=True))
    (candidate,) = [item for item in result.candidates if item.operation == "verify"]
    assert candidate.action == "mark-disputed"
    assert phone not in _staged_text(root, result.staged)


# -- read-only index: checkpointed vs uncheckpointed WAL ----------------------------------


def _checkpoint(db: Path) -> None:
    import gc
    import sqlite3

    gc.collect()  # close any connection objects the refresh left for the GC
    connection = sqlite3.connect(db)
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    connection.close()


def test_dry_run_on_a_checkpointed_index_creates_no_side_files(tmp_path: Path) -> None:
    root = _with_transcripts(tmp_path)
    db = root / ".memfw-history.sqlite"
    run_dream(root, now=NOW, options=DreamOptions(dream_id="first", all_memories=True))
    _checkpoint(db)
    for side in (".memfw-history.sqlite-wal", ".memfw-history.sqlite-shm"):
        (root / side).unlink(missing_ok=True)
    before = _tree(root)
    result = run_dream(root, now=NOW, dry_run=True, options=DreamOptions(all_memories=True))
    assert _tree(root) == before
    assert not (root / ".memfw-history.sqlite-shm").exists()
    assert not (root / ".memfw-history.sqlite-wal").exists()
    assert any(item.action == "confirm" for item in result.candidates)


def test_unreadable_pending_wal_is_unavailable_not_stale(tmp_path: Path) -> None:
    import os
    import subprocess

    root = _with_transcripts(tmp_path)
    db = root / ".memfw-history.sqlite"
    run_dream(root, now=NOW, options=DreamOptions(dream_id="first", all_memories=True))
    _checkpoint(db)
    logs = tmp_path / "logs"
    (logs / "s2.jsonl").write_text(
        json.dumps({"session_id": "s2", "text": "the greenhouse sensor reads 25 degrees"}) + "\n",
        "utf-8",
    )
    # A refresh that dies before checkpointing leaves committed rows in the WAL.
    child = (
        "import os, sys\n"
        "from pathlib import Path\n"
        "import memfw.history.index as index\n"
        "keep = []\n"
        "original = index.TranscriptIndex._connect\n"
        "def hold(self):\n"
        "    connection = original(self)\n"
        "    keep.append(connection)\n"
        "    return connection\n"
        "index.TranscriptIndex._connect = hold\n"
        "index.TranscriptIndex(Path(sys.argv[1]),"
        " [index.SourceRoot.create('logs', Path(sys.argv[2]))]).refresh()\n"
        "os._exit(0)\n"
    )
    subprocess.run([sys.executable, "-W", "ignore", "-c", child, str(db), str(logs)], check=True)
    wal = root / ".memfw-history.sqlite-wal"
    assert wal.stat().st_size > 0
    (root / ".memfw-history.sqlite-shm").unlink(missing_ok=True)
    os.chmod(wal, 0)
    try:
        result = run_dream(root, now=NOW, dry_run=True, options=DreamOptions(all_memories=True))
    finally:
        os.chmod(wal, 0o600)
    assert result.report["history"]["available"] is False
    assert not [item for item in result.candidates if item.operation == "verify"]


# -- curate: the re-review's curate6 pairs ----------------------------------------------------


CURATE6 = [
    (
        "dup-extra-detail",
        "merge",
        "The irrigation valve pressure is 18 bar.",
        "The irrigation valve pressure is 18 bar, set by the night shift.",
    ),
    (
        "dup-reordered",
        "merge",
        "Backups run nightly on the staging cluster.",
        "On the staging cluster, backups run nightly.",
    ),
    (
        "dup-kv-vs-prose",
        "merge",
        "- host: db1\n- port: 5432",
        "The host is db1 and the port is 5432.",
    ),
    ("dup-both-negated", "merge", "Do not deploy on Fridays.", "Never deploy on a Friday."),
    (
        "con-changed-value",
        "supersede",
        "The irrigation valve pressure is 18 bar.",
        "After the upgrade the irrigation valve pressure is 21 bar.",
    ),
    ("con-host-moved", "supersede", "- host: db1\n- port: 5432", "- host: db2\n- port: 6432"),
    (
        "con-owner-changed",
        "supersede",
        "- owner: alice\n- service: api",
        "- owner: bob\n- service: api",
    ),
    (
        "con-negation-added",
        "supersede",
        "Nightly backups run on the staging cluster.",
        "Nightly backups no longer run on the staging cluster.",
    ),
    (
        "con-version-bump",
        "supersede",
        "The api service runs Postgres 14.",
        "The api service now runs Postgres 16 since March.",
    ),
    ("con-region", "supersede", "- region: us-east-1", "- region: eu-west-1"),
    (
        "none-different-env",
        "none",
        "The web port for staging is 8080.",
        "The web port for production is 8080.",
    ),
]


def _pair_action(first: str, second: str) -> str:
    from datetime import timedelta

    from memfw.core.models import MemoryDocument
    from memfw.dream.curate import curate

    documents = [
        MemoryDocument(
            id=f"m{index}",
            title="note",
            body=body,
            about=["concept:x:shared"],
            reference_count=1,
            created_at=NOW - timedelta(days=10 - index),
            updated_at=NOW - timedelta(days=10 - index),
        )
        for index, body in enumerate((first, second))
    ]
    actions = {item.action for item in curate(documents, now=NOW) if len(item.memory_ids) == 2}
    return "merge" if "merge" in actions else "supersede" if "supersede" in actions else "none"


@pytest.mark.parametrize(
    ("name", "expected", "first", "second"), CURATE6, ids=[c[0] for c in CURATE6]
)
def test_curate6_pairs(name: str, expected: str, first: str, second: str) -> None:
    assert _pair_action(first, second) == expected


@pytest.mark.xfail(
    strict=True, reason="synonyms (deploys/deployments, happen/scheduled) are not matched"
)
def test_curate_synonym_duplicate_is_a_known_limit() -> None:
    assert (
        _pair_action(
            "Deploys happen on Tuesdays at 10:00.",
            "Deployments are scheduled for Tuesday at 10:00.",
        )
        == "merge"
    )
