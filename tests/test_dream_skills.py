"""Tests for ``memfw dream skills``: verify-first, proposal-only skill candidates.

All fixtures are synthetic and live under ``tmp_path``; the drafter is a fake
callable, so there is no model and no network.
"""

from __future__ import annotations

import json
import sys
import textwrap
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from memfw.cli import app
from memfw.core.repository import Repository
from memfw.dream.skills import SKILLS_DIR, SkillOptions, clean, run_skills, segment, step_signature
from memfw.dream.skills.draft import UnsafeText
from memfw.dream.skills.run import _refuse_skill_home
from memfw.dream.stage import DreamStageError
from memfw.history import TranscriptIndex, default_db, sources_from_config

NOW = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
FAKE_HOME_USER = "zz" + "tester"
FAKE_TOKEN = "gh" + "p_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"


def _line(**fields: Any) -> str:
    return json.dumps(fields) + "\n"


def _claude(session: str, turns: list[tuple[str, Any]], day: int) -> str:
    """Claude-style lines: ("user", text) / ("bash", command) / ("result", text)."""

    lines = []
    for number, (kind, value) in enumerate(turns):
        ts = f"2026-01-{day:02d}T10:{number:02d}:00Z"
        uuid = f"{session}-{number}"
        if kind == "user":
            message: dict[str, Any] = {"role": "user", "content": value}
            role = "user"
        elif kind == "result":
            message = {"role": "user", "content": [{"type": "tool_result", "content": value}]}
            role = "user"
        elif kind == "say":
            message = {"role": "assistant", "content": [{"type": "text", "text": value}]}
            role = "assistant"
        else:
            name = "Bash" if kind == "bash" else kind
            payload = {"command": value, "description": "step"} if kind == "bash" else value
            message = {
                "role": "assistant",
                "content": [{"type": "tool_use", "name": name, "input": payload}],
            }
            role = "assistant"
        lines.append(_line(type=role, sessionId=session, uuid=uuid, timestamp=ts, message=message))
    return "".join(lines)


def _codex(session: str, turns: list[tuple[str, str]], day: int) -> str:
    lines = [
        _line(
            timestamp=f"2026-01-{day:02d}T09:00:00Z",
            type="session_meta",
            payload={"id": session},
        )
    ]
    for number, (kind, value) in enumerate(turns):
        ts = f"2026-01-{day:02d}T09:{number + 1:02d}:00Z"
        if kind == "user":
            payload: dict[str, Any] = {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": value}],
            }
        elif kind == "bash":
            payload = {
                "type": "function_call",
                "name": "shell",
                "arguments": json.dumps({"command": ["bash", "-lc", value]}),
            }
        else:
            payload = {"type": "function_call_output", "output": value}
        lines.append(_line(timestamp=ts, type="response_item", payload=payload))
    return "".join(lines)


def _release(home: str, extra: list[tuple[str, Any]] | None = None) -> list[tuple[str, Any]]:
    return [
        ("user", "please cut the release of the widget package"),
        ("bash", f"cd {home}/proj && uv run pytest -q"),
        ("result", "12 passed"),
        ("bash", "uv build --wheel"),
        ("result", "built"),
        *(extra or []),
        ("bash", "git tag v1.2.0"),
        ("result", "ok"),
        ("say", "Release done."),
    ]


def _logs(tmp_path: Path) -> Path:
    logs = tmp_path / "logs"
    logs.mkdir()
    home = f"/Users/{FAKE_HOME_USER}"
    (logs / "c1.jsonl").write_text(
        _claude("c1", _release(home, [("bash", "make docs"), ("result", "ok")]), 1), "utf-8"
    )
    (logs / "c2.jsonl").write_text(
        _claude(
            "c2",
            [
                *_release(home, [("Read", {"file_path": f"{home}/proj/CHANGELOG.md"})]),
                ("user", "now run the lint for the widget package"),
                ("bash", "uv run ruff check src"),
                ("result", "clean"),
                ("bash", "uv run mypy src"),
                ("result", "clean"),
            ],
            2,
        ),
        "utf-8",
    )
    (logs / "x1.jsonl").write_text(
        _codex(
            "x1",
            [
                ("user", "cut a release for the widget package please"),
                ("bash", "uv run pytest -q"),
                ("out", "12 passed"),
                ("bash", f"GH_TOKEN={FAKE_TOKEN} uv build"),
                ("out", "built"),
                ("bash", "uv build"),
                ("out", "built"),
                ("bash", "git tag v1.3.0"),
                ("out", "ok"),
                ("user", "run the lint for the widget package"),
                ("bash", "uv run ruff check src"),
                ("out", "clean"),
                ("bash", "uv run mypy src"),
                ("out", "clean"),
            ],
            3,
        ),
        "utf-8",
    )
    (logs / "c3.jsonl").write_text(
        _claude(
            "c3",
            [
                ("user", "what is the weather like"),
                ("bash", "curl https://weather.example.invalid/today"),
                ("result", "sunny"),
            ],
            4,
        ),
        "utf-8",
    )
    return logs


def _vault(tmp_path: Path) -> Path:
    logs = _logs(tmp_path)
    root = tmp_path / "vault"
    repository = Repository(root)
    repository.init()
    config = repository.read_config()
    config["history_sources"] = {"logs": str(logs)}
    repository.configure(config)
    return root


def _build_index(root: Path) -> None:
    config = Repository(root).read_config()
    TranscriptIndex(default_db(root), sources_from_config(config)).refresh()


def _staged(root: Path) -> dict[str, dict[str, str]]:
    base = root / SKILLS_DIR
    if not base.is_dir():
        return {}
    return {
        item.name: {path.name: path.read_text("utf-8") for path in sorted(item.iterdir())}
        for item in sorted(base.iterdir())
    }


# -- step signatures and episodes -------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("[tool Bash] command: cd /x/y && uv run pytest -q; description: t", "cmd:uv run pytest"),
        ("[tool shell] command: bash; -lc; git tag v1.2.0", "cmd:git tag"),
        ("[tool exec_command] cmd: FOO=1 sudo make build; workdir: /tmp", "cmd:make build"),
        ("[tool Bash] description: x; subcommand: no; command: uv sync", "cmd:uv sync"),
        # Arguments never survive: hosts, users, branches, containers.
        ("[tool Bash] command: ssh host7 uptime", "cmd:ssh"),
        ("[tool Bash] command: git push origin someone-private-branch", "cmd:git push"),
        ("[tool Bash] command: docker exec prod-db psql", "cmd:docker exec"),
        ("[tool Bash] command: sudo -u someone make", None),
        ("[tool Bash] command: ./deploy-host7.sh", None),
        ("[tool Bash] command: /usr/bin/python3 -m pytest", "cmd:python3"),
        ("[tool Read] file_path: /x/y.md", None),
        ("[tool WebFetch] url: https://example.invalid", "tool:webfetch"),
        ("plain prose", None),
    ],
)
def test_step_signature(text: str, expected: str | None) -> None:
    assert step_signature(text) == expected


def test_tool_results_do_not_open_episodes() -> None:
    entries = [
        {"role": "user", "snippet": "cut the release", "ref": "r:1@0", "agent": "claude"},
        {"role": "assistant", "snippet": "[tool Bash] command: uv build", "ref": "r:1@1"},
        {"role": "user", "snippet": "built", "ref": "r:1@2"},
        {"role": "assistant", "snippet": "[tool Bash] command: git tag v1", "ref": "r:1@3"},
        {"role": "user", "snippet": "ok", "ref": "r:1@4"},
        {"role": "assistant", "snippet": "done", "ref": "r:1@5"},
        {"role": "user", "snippet": "now lint", "ref": "r:1@6"},
        {"role": "tool", "snippet": "[tool shell] command: bash; -lc; ruff check", "ref": "r:1@7"},
        {"role": "tool", "snippet": "clean", "ref": "r:1@8"},
        {"role": "user", "snippet": "and type check", "ref": "r:1@9"},
        {"role": "tool", "snippet": "[tool shell] command: mypy src", "ref": "r:1@10"},
    ]
    episodes = segment("r", "s", entries)
    assert [[step.signature for step in item.steps] for item in episodes] == [
        ["cmd:uv build", "cmd:git tag"],
        ["cmd:ruff check"],
        ["cmd:mypy"],
    ]


def test_parallel_tool_results_stay_in_one_episode() -> None:
    entries = [
        {"role": "user", "snippet": "check the repo", "ref": "r:1@0"},
        {"role": "assistant", "snippet": "[tool Bash] command: git status", "ref": "r:1@1"},
        {"role": "assistant", "snippet": "[tool Bash] command: git log", "ref": "r:1@2"},
        {"role": "user", "snippet": "On branch main nothing to commit", "ref": "r:1@3"},
        {"role": "user", "snippet": "abc123 initial", "ref": "r:1@4"},
        {"role": "assistant", "snippet": "[tool Bash] command: git diff", "ref": "r:1@5"},
        {"role": "user", "snippet": "no changes", "ref": "r:1@6"},
        {"role": "assistant", "snippet": "All clean.", "ref": "r:1@7"},
        {"role": "user", "snippet": "thanks, now build", "ref": "r:1@8"},
        {"role": "assistant", "snippet": "[tool Bash] command: uv build", "ref": "r:1@9"},
    ]
    episodes = segment("r", "s", entries)
    assert [item.request for item in episodes] == ["check the repo", "thanks, now build"]
    assert [step.signature for step in episodes[0].steps] == [
        "cmd:git status",
        "cmd:git log",
        "cmd:git diff",
    ]


def test_clean_removes_paths_and_refuses_secrets() -> None:
    assert clean(f"Run it in /Users/{FAKE_HOME_USER}/proj now") == "Run it in <path> now"
    assert clean("open ~/notes/a.md or C:\\Users\\x\\y") == "open <path> or <path>"
    assert clean("see https://host.invalid/x?a=1") == "see <url>"
    with pytest.raises(UnsafeText):
        clean("token " + FAKE_TOKEN)


# -- end to end ------------------------------------------------------------------


def test_repeated_procedure_across_agents_becomes_a_candidate(tmp_path: Path) -> None:
    root = _vault(tmp_path)
    result = run_skills(root, now=NOW)
    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert candidate.status == "staged"
    evidence = candidate.evidence
    assert [step["signature"] for step in evidence["steps"]] == [
        "cmd:uv run pytest",
        "cmd:uv build",
        "cmd:git tag",
    ]
    assert all(step["sessions"] == 3 and step["refs"] for step in evidence["steps"])
    # Verify-first: a step seen in one session only is dropped, not drafted.
    assert "cmd:make" in evidence["dropped_steps"]
    assert "make" not in candidate.skill_md.split("## Steps")[1]
    assert evidence["agents"] == {"claude": 2, "codex": 1}
    assert sorted(item["session_id"] for item in evidence["sessions"]) == ["c1", "c2", "x1"]
    assert 0.0 < candidate.confidence <= 1.0

    files = _staged(root)[candidate.candidate_id]
    assert set(files) == {"SKILL.md", "candidate.json", "evidence.json"}
    _, front, body = files["SKILL.md"].split("---\n", 2)
    meta = yaml.safe_load(front)
    assert set(meta) == {"name", "description"}
    assert meta["name"] == candidate.record["name"]
    assert "widget" in meta["name"] and "release" in meta["name"]
    assert "## When to use" in body and "## Steps" in body
    assert "1. Run `uv run pytest`" in body and "3. Run `git tag`" in body
    record = json.loads(files["candidate.json"])
    assert record["status"] == "proposed"
    assert record["approved"] is False and record["installed"] is False
    assert record["authority"] is False


def test_min_sessions_is_configurable(tmp_path: Path) -> None:
    root = _vault(tmp_path)
    default = run_skills(root, now=NOW, dry_run=False)
    assert len(default.candidates) == 1  # the lint procedure repeats in two sessions only
    lowered = run_skills(root, now=NOW, dry_run=True, options=SkillOptions(min_sessions=2))
    signatures = sorted(
        tuple(step["signature"] for step in item.evidence["steps"]) for item in lowered.candidates
    )
    assert ("cmd:uv run ruff", "cmd:uv run mypy") in signatures
    assert len(lowered.candidates) == 2
    with pytest.raises(ValueError):
        run_skills(root, now=NOW, dry_run=True, options=SkillOptions(min_sessions=1))


def test_rerun_with_same_evidence_is_idempotent(tmp_path: Path) -> None:
    root = _vault(tmp_path)
    first = run_skills(root, now=NOW)
    before = _staged(root)
    later = datetime(2026, 4, 1, tzinfo=UTC)
    second = run_skills(root, now=later)
    assert [item.candidate_id for item in second.candidates] == [
        item.candidate_id for item in first.candidates
    ]
    assert [item.status for item in second.candidates] == ["exists"]
    assert _staged(root) == before


def test_new_evidence_for_a_staged_procedure_is_not_staged_twice(tmp_path: Path) -> None:
    root = _vault(tmp_path)
    first = run_skills(root, now=NOW).candidates[0]
    logs = tmp_path / "logs"
    (logs / "c4.jsonl").write_text(_claude("c4", _release("/srv"), 9), "utf-8")
    second = run_skills(root, now=NOW).candidates[0]
    assert second.candidate_id != first.candidate_id
    assert second.procedure_key == first.procedure_key
    assert second.status == "known"
    assert second.path == (SKILLS_DIR / first.candidate_id).as_posix()
    assert list(_staged(root)) == [first.candidate_id]


def test_keywords_leave_out_words_with_digits(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    for number in range(3):
        turns = [
            ("user", f"deploy the widget to host7 ticket{number}"),
            ("bash", "uv build"),
            ("result", "ok"),
            ("bash", "uv publish"),
            ("result", "ok"),
        ]
        (logs / f"d{number}.jsonl").write_text(_claude(f"d{number}", turns, number + 1), "utf-8")
    root = tmp_path / "vault"
    repository = Repository(root)
    repository.init()
    config = repository.read_config()
    config["history_sources"] = {"logs": str(logs)}
    repository.configure(config)
    candidate = run_skills(root, now=NOW).candidates[0]
    assert candidate.record["name"] == "deploy-widget"
    assert "host7" not in json.dumps(_staged(root))


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    root = _vault(tmp_path)
    _build_index(root)
    result = run_skills(root, now=NOW, dry_run=True)
    assert len(result.candidates) == 1
    assert result.candidates[0].status == "dry-run"
    assert not (root / "_candidates").exists()


def test_candidates_carry_no_secrets_or_paths(tmp_path: Path) -> None:
    root = _vault(tmp_path)
    run_skills(root, now=NOW, options=SkillOptions(min_sessions=2))
    blob = json.dumps(_staged(root))
    assert blob != "{}"
    for needle in (FAKE_HOME_USER, "/Users/", FAKE_TOKEN, "GH_TOKEN", "CHANGELOG", "weather"):
        assert needle not in blob


def test_never_installs_into_a_skills_home(tmp_path: Path, monkeypatch: Any) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    root = _vault(tmp_path)
    run_skills(root, now=NOW)
    assert list(home.iterdir()) == []
    inside = home / ".claude" / "skills" / "vault"
    inside.mkdir(parents=True)
    with pytest.raises(DreamStageError):
        _refuse_skill_home(inside)


# -- the optional drafter (a model behind an injectable hook) ---------------------


def _drafter_module(tmp_path: Path, monkeypatch: Any, body: str) -> str:
    package = tmp_path / "fakehooks"
    package.mkdir(exist_ok=True)
    name = f"drafter_{abs(hash(body)) % 10**8}"
    (package / f"{name}.py").write_text(textwrap.dedent(body), "utf-8")
    monkeypatch.syspath_prepend(str(package))
    sys.modules.pop(name, None)
    return f"{name}:draft"


def test_drafter_steps_must_cite_verified_evidence(tmp_path: Path, monkeypatch: Any) -> None:
    spec = _drafter_module(
        tmp_path,
        monkeypatch,
        f"""
        SEEN = []
        def draft(procedure):
            SEEN.append(procedure)
            ids = [step["id"] for step in procedure["steps"]]
            return {{
                "name": "Release Widget",
                "description": "Cut a widget release.",
                "when_to_use": "When asked to release the widget package.",
                "steps": [
                    {{"text": "Run the test suite with pytest", "evidence": [ids[0]]}},
                    {{"text": "Invent a deploy step", "evidence": []}},
                    {{"text": "Push to prod", "evidence": ["s99"]}},
                    {{"text": "Build with uv in /Users/{FAKE_HOME_USER}/x", "evidence": [ids[1]]}},
                    {{"text": "uv build with token {FAKE_TOKEN}", "evidence": [ids[1]]}},
                    {{"text": "Run rm -rf on the build dir", "evidence": [ids[0]]}},
                    {{"text": "Run uv build", "evidence": [ids[1]]}},
                    {{"text": "Run git tag for the release", "evidence": [ids[2]]}},
                ],
            }}
        """,
    )
    root = _vault(tmp_path)
    result = run_skills(root, now=NOW, options=SkillOptions(drafter=spec))
    candidate = result.candidates[0]
    md = candidate.skill_md
    assert candidate.record["name"] == "release-widget"
    assert candidate.record["drafter"] == "hook"
    assert "1. Run the test suite with pytest" in md
    assert "2. Build with uv in <path>" in md
    assert "3. Run git tag for the release" in md
    for dropped in (
        "Invent a deploy step",
        "Push to prod",
        "rm -rf",
        "Run uv build",
        FAKE_TOKEN,
        FAKE_HOME_USER,
    ):
        assert dropped not in md
    notes = candidate.record["drafter_notes"]
    assert "dropped a step without verified evidence" in notes
    assert "dropped a step with unsafe text" in notes
    assert "dropped a step that does not name its cited command or tool" in notes
    assert "dropped a step that repeats cited evidence" in notes
    seen = sys.modules[spec.split(":")[0]].SEEN[0]
    assert "request" not in json.dumps(seen)  # the hook gets steps, not transcript text


def test_drafter_that_fails_or_cites_nothing_keeps_the_rule_draft(
    tmp_path: Path, monkeypatch: Any
) -> None:
    root = _vault(tmp_path)
    _build_index(root)
    baseline = run_skills(root, now=NOW, dry_run=True).candidates[0].skill_md
    for body in (
        "def draft(procedure):\n    raise RuntimeError('model offline')\n",
        "def draft(procedure):\n    return {'name': 'x', 'steps': [{'text': 'a'}]}\n",
        "def draft(procedure):\n    return ['not', 'a', 'mapping']\n",
    ):
        spec = _drafter_module(tmp_path, monkeypatch, body)
        result = run_skills(root, now=NOW, dry_run=True, options=SkillOptions(drafter=spec))
        assert result.candidates[0].skill_md == baseline
        assert result.candidates[0].record["drafter"] == "deterministic"


def test_cli_dream_skills(tmp_path: Path) -> None:
    root = _vault(tmp_path)
    runner = CliRunner()
    output = runner.invoke(
        app,
        ["dream", "skills", "--root", str(root), "--now", "2026-03-01T12:00:00Z"],
    )
    assert output.exit_code == 0, output.output
    payload = json.loads(output.stdout)
    assert payload["authority"] is False
    assert payload["report"]["installed"] is False
    assert [item["status"] for item in payload["candidates"]] == ["staged"]
    again = runner.invoke(
        app,
        ["dream", "skills", "--root", str(root), "--min-sessions", "1"],
    )
    assert again.exit_code == 2
    assert "min_sessions" in again.output
    missing = runner.invoke(
        app, ["dream", "skills", "--root", str(root), "--drafter", "json:no_such_function"]
    )
    assert missing.exit_code == 2
    assert "drafter is not callable" in missing.output
