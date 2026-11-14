"""Quality of ``memfw dream skills`` candidates: boilerplate, procedures, independence.

Every fixture is synthetic. The "harness" and "orchestrator" texts below are
made up to look like injected prompts; nothing comes from a real transcript.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from memfw.cli import app
from memfw.core.repository import Repository
from memfw.dream.skills import SkillOptions, run_skills, segment
from memfw.dream.skills.boilerplate import (
    BoilerplateFilter,
    fingerprint,
    strip_structural,
)
from memfw.dream.skills.distill import distill, signature_weights
from memfw.dream.skills.episodes import (
    classify_result,
    command_calls,
    is_continuation,
    tool_calls,
)

NOW = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
CONTRACT = (
    "Delivery contract - finish the assigned work yourself, do not hand it to another "
    "operator, and report the result in the final message with the commit id."
)
"""A made-up orchestrator paragraph injected in front of every worker task."""
REVIEW_RUBRIC = (
    "You review one line of a parallel batch. Reply with a verdict object only and "
    "list every blocking finding with its file."
)


# -- static harness markers ---------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("<system-reminder>\nbe nice\n</system-reminder>\nbump the widget version", "bump"),
        ("<environment_context>cwd: x</environment_context>tag the release", "tag the release"),
        ("<user_query>\nbump the widget version\n</user_query>", "bump the widget version"),
        ("# AGENTS.md instructions for x\n<INSTRUCTIONS>\nrules\n</INSTRUCTIONS>\nship it", "ship"),
        ("# AGENTS.md instructions for x\n<INSTRUCTIONS>\nrules", ""),
        ("[SYSTEM NOTIFICATION - NOT USER INPUT] task finished", ""),
        ("Tool loaded.", ""),
        ("<task-notification>\n<task-id>a1</task-id>\n</task-notification>", ""),
        ("<system-reminder>unclosed reminder text", ""),
    ],
)
def test_structural_markers_are_stripped(text: str, expected: str) -> None:
    residual = strip_structural(text)
    if expected:
        assert expected in residual
        assert "reminder" not in residual and "INSTRUCTIONS" not in residual
    else:
        assert residual.strip() == ""


def test_tool_schema_paragraphs_are_stripped() -> None:
    schema = json.dumps({"name": "x", "parameters": {"type": "object", "properties": {}}})
    assert strip_structural(f"{schema}\n\nrebuild the docs site") == "rebuild the docs site"


def test_fingerprint_matches_templates_with_other_ids() -> None:
    left = "Work in /tmp/a1b2c3/w1 on ticket 4521 at https://x.invalid/9 (id 0a1b2c3d4e)."
    right = "Work in /srv/zz/w9 on ticket 77 at https://y.invalid/1 (id ffee112233)."
    assert fingerprint(left) == fingerprint(right)


def _observe(bp: BoilerplateFilter, text: str, sessions: int, agents: int) -> None:
    for number in range(sessions):
        bp.observe(("src", f"s{number}"), f"agent{number % agents}", text + f"\n\ntask {number}")


def test_corpus_repeated_paragraph_is_stripped_but_the_task_stays() -> None:
    bp = BoilerplateFilter(min_sessions=5, min_agents=2, solo_sessions=15)
    _observe(bp, CONTRACT, sessions=6, agents=3)
    residual = bp.strip(CONTRACT + "\n\nrotate the widget signing key")
    assert residual == "rotate the widget signing key"
    assert bp.is_injected(CONTRACT)
    report = bp.report()
    assert report["paragraphs_removed_repeated"] == 1
    assert report["repeated_fingerprints"] >= 1


def test_single_agent_repetition_needs_more_sessions() -> None:
    few = BoilerplateFilter(min_sessions=5, min_agents=2, solo_sessions=15)
    _observe(few, CONTRACT, sessions=8, agents=1)
    assert few.strip(CONTRACT) == CONTRACT
    many = BoilerplateFilter(min_sessions=5, min_agents=2, solo_sessions=15)
    _observe(many, CONTRACT, sessions=16, agents=1)
    assert many.strip(CONTRACT) == ""


def test_text_seen_in_two_sessions_is_kept() -> None:
    bp = BoilerplateFilter()
    _observe(bp, CONTRACT, sessions=2, agents=2)
    assert bp.strip(CONTRACT) == CONTRACT


def test_observe_memory_is_bounded() -> None:
    bp = BoilerplateFilter(min_sessions=3, solo_sessions=6)
    _observe(bp, CONTRACT, sessions=50, agents=5)
    assert max(len(item) for item in bp._sessions.values()) <= 7
    assert max(len(item) for item in bp._agents.values()) <= 3


# -- steps --------------------------------------------------------------------------


def test_compound_commands_become_several_steps_and_read_only_programs_are_skipped() -> None:
    calls = command_calls("cd repo && rg -n foo src | head && uv run pytest -q -p x && git push")
    assert [call.signature for call in calls] == ["uv run pytest", "git push"]
    assert calls[0].flags == ("-q", "-p")


def test_input_kinds_and_flag_names_never_values() -> None:
    (call,) = command_calls("uv run ruff check --select=E501 src/mod.py docs/a.md")
    assert call.flags == ("--select",)
    assert call.inputs == ("<file>.py", "<file>.md")
    assert "E501" not in json.dumps(call.__dict__)


def test_script_wrapped_shell_calls_and_orchestration_tools() -> None:
    script = '[tool exec] const r = await tools.exec_command({cmd:"uv build && git tag v1"});'
    assert [call.signature for call in tool_calls(script)] == ["cmd:uv build", "cmd:git tag"]
    assert tool_calls("[tool wait_agent] timeout_ms: 60000") == []
    assert tool_calls("[tool send_message] target: x; message: y") == []
    assert tool_calls("[tool Bash] command: python3 - <<'EOF'\nprint(1)\nEOF") == []
    assert [c.signature for c in tool_calls("[tool mcp__srv__memory_search] q")] == [
        "tool:memory_search"
    ]


@pytest.mark.parametrize(
    ("text", "check", "outcome"),
    [
        ("==== 12 passed in 0.3s ====", "tests pass", "success"),
        ("1 failed, 3 passed", "tests fail", "failed"),
        ("All checks passed!", "lint clean", "success"),
        ("[main 1a2b3c4] add widget", "commit created", "success"),
        ("Traceback (most recent call last):\n  x", "error", "failed"),
        ("whatever", "", ""),
    ],
)
def test_result_checks(text: str, check: str, outcome: str) -> None:
    assert classify_result(text) == (check, outcome)


def test_continuations() -> None:
    assert is_continuation("continue") and is_continuation("请继续") and is_continuation("ok.")
    assert not is_continuation("now build the docs")


def _entries(
    request: str, agent: str, day: int, *, extra_intent: str = "", ts: Any = None
) -> list[dict[str, Any]]:
    ts = ts if ts is not None else f"2026-01-{day:02d}T10:00:00Z"
    return [
        {"role": "user", "snippet": request, "ref": "r@0", "agent": agent, "timestamp": ts},
        {
            "role": "assistant",
            "snippet": "Run the test suite before cutting anything.\n"
            "[tool Bash] command: uv run pytest -q",
            "ref": "r@1",
        },
        {"role": "user", "snippet": "40 passed in 2.1s", "ref": "r@2"},
        {
            "role": "assistant",
            "snippet": "[tool Bash] command: uv build --wheel; description: Build the wheel"
            + extra_intent,
            "ref": "r@3",
        },
        {"role": "user", "snippet": "Successfully built widget", "ref": "r@4"},
        {"role": "assistant", "snippet": "[tool Bash] command: git tag v1.0.0", "ref": "r@5"},
        {"role": "user", "snippet": "ok", "ref": "r@6"},
        {"role": "assistant", "snippet": "Done.", "ref": "r@7"},
    ]


def test_injected_and_continuation_turns_do_not_open_episodes() -> None:
    entries = [
        {"role": "user", "snippet": "<system-reminder>x</system-reminder>", "ref": "r@0"},
        *_entries("cut the widget release", "claude", 1),
        {"role": "user", "snippet": "continue", "ref": "r@8"},
        {"role": "assistant", "snippet": "[tool Bash] command: git push", "ref": "r@9"},
    ]
    bp = BoilerplateFilter()
    episodes = segment("src", "s1", entries, bp)
    assert len(episodes) == 1
    episode = episodes[0]
    assert episode.request == "cut the widget release"
    assert [step.signature for step in episode.steps] == [
        "cmd:uv run pytest",
        "cmd:uv build",
        "cmd:git tag",
        "cmd:git push",
    ]
    assert episode.steps[0].intent == "Run the test suite before cutting anything"
    assert episode.steps[0].check == "tests pass"
    assert episode.steps[1].intent == "Build the wheel"
    assert episode.steps[1].flags == ("--wheel",)
    assert episode.outcome == "success"
    assert bp.report()["requests_injected"] == 1
    assert bp.report()["requests_continuation"] == 1


# -- independence and spread --------------------------------------------------------


def _episodes(specs: list[tuple[str, str, int]]) -> list[Any]:
    out = []
    for number, (request, agent, day) in enumerate(specs):
        out += segment(agent, f"s{number}", _entries(request, agent, day))
    return out


def test_the_same_request_fanned_out_on_one_day_counts_once() -> None:
    specs = [("cut the widget release", agent, 1) for agent in ("claude", "codex", "kimi")]
    stats: dict[str, int] = {}
    assert distill(_episodes(specs), min_sessions=3, stats=stats) == []
    assert stats["rejected_not_independent"] == 1


def test_one_day_is_not_enough_spread() -> None:
    specs = [(f"cut the widget release {word}", "claude", 1) for word in ("alpha", "beta", "gamma")]
    stats: dict[str, int] = {}
    assert distill(_episodes(specs), min_sessions=3, stats=stats) == []
    assert stats["rejected_too_few_days"] == 1


def test_independent_sessions_with_spread_make_a_procedure() -> None:
    specs = [
        ("cut the widget release", "claude", 1),
        ("cut the widget release", "claude", 1),  # same day, same text: a retry
        ("please cut a widget release", "codex", 2),
        ("widget release cut today", "kimi", 3),
    ]
    (procedure,) = distill(_episodes(specs), min_sessions=3)
    assert procedure.independent == 3
    assert len(procedure.sessions) == 4
    assert procedure.days == ["2026-01-01", "2026-01-02", "2026-01-03"]
    assert procedure.outcomes == {"success": 3}
    first = procedure.steps[0]
    assert first.signature == "cmd:uv run pytest" and first.command == "uv run pytest -q"
    assert first.intent == "Run the test suite before cutting anything"
    assert first.checks == ["tests pass"]
    assert "claude:s0" in first.session_ids and "kimi:s3" in first.session_ids
    assert any("git checkout" in item for item in procedure.preconditions)
    assert "widget" in procedure.keywords and "release" in procedure.keywords


def test_ubiquitous_steps_do_not_link_episodes_in_a_large_corpus() -> None:
    episodes = []
    for number in range(60):
        entries = [
            {"role": "user", "snippet": f"task {number}", "ref": "r@0", "agent": "a"},
            {"role": "assistant", "snippet": "[tool Bash] command: git status", "ref": "r@1"},
            {"role": "assistant", "snippet": f"[tool tool{number % 30}x] q", "ref": "r@2"},
        ]
        episodes += segment("src", f"s{number}", entries)
    weights = signature_weights(episodes)
    assert weights is not None and weights["cmd:git status"] < 0.05
    assert signature_weights(episodes[:10]) is None


# -- end to end: boilerplate never names a skill ------------------------------------


def _claude_line(session: str, number: int, day: int, role: str, message: Any) -> str:
    return (
        json.dumps(
            {
                "type": role,
                "sessionId": session,
                "uuid": f"{session}-{number}",
                "timestamp": f"2026-01-{day:02d}T10:{number:02d}:00Z",
                "message": message,
            }
        )
        + "\n"
    )


def _worker_log(session: str, day: int, task: str, steps: list[str]) -> str:
    lines = [
        _claude_line(session, 0, day, "user", {"role": "user", "content": f"{CONTRACT}\n\n{task}"})
    ]
    number = 1
    for command in steps:
        lines.append(
            _claude_line(
                session,
                number,
                day,
                "assistant",
                {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "name": "Bash", "input": {"command": command}}
                    ],
                },
            )
        )
        lines.append(
            _claude_line(
                session,
                number + 1,
                day,
                "user",
                {"role": "user", "content": [{"type": "tool_result", "content": "3 passed"}]},
            )
        )
        number += 2
    return "".join(lines)


def _boilerplate_vault(tmp_path: Path) -> Path:
    """Six one-agent worker sessions: the same contract in front of six different tasks."""

    logs = tmp_path / "logs"
    logs.mkdir(parents=True)
    tasks = [
        "rotate the signing key for the widget service",
        "rename the gadget module and its imports",
        "add pagination to the sprocket listing",
        "translate the gizmo help pages",
        "upgrade the doohickey parser dependency",
        "profile the thingamajig importer",
    ]
    steps = ["uv run pytest -q", "git commit -m x"]
    for number, task in enumerate(tasks):
        (logs / f"w{number}.jsonl").write_text(
            _worker_log(f"w{number}", number + 1, task, steps), "utf-8"
        )
    root = tmp_path / "vault"
    repository = Repository(root)
    repository.init()
    config = repository.read_config()
    config["history_sources"] = {"logs": str(logs)}
    repository.configure(config)
    return root


def test_injected_contract_is_filtered_and_reported(tmp_path: Path) -> None:
    root = _boilerplate_vault(tmp_path)
    options = SkillOptions(min_sessions=2, boilerplate_min_sessions=2)
    result = run_skills(root, now=NOW, options=options)
    blob = json.dumps(result.as_dict())
    for word in ("delivery", "contract", "operator"):
        assert word not in blob.lower()
    quality = result.report["quality"]
    assert quality["candidates"] == len(result.candidates)
    filtered = quality["boilerplate_filtered"]
    assert filtered["paragraphs_removed_repeated"] + filtered["pieces_removed_repeated"] >= 6
    assert quality["boilerplate_filtered"]["texts_stripped"] >= 5
    assert set(quality) >= {"candidates", "boilerplate_filtered", "rejected", "example"}


def test_without_the_corpus_filter_the_contract_names_candidates(tmp_path: Path) -> None:
    """Regression guard: this is the noise the filter removes."""

    unfiltered = run_skills(
        _boilerplate_vault(tmp_path / "a"),
        now=NOW,
        options=SkillOptions(min_sessions=2, boilerplate_min_sessions=0),
    ).candidates
    filtered = run_skills(
        _boilerplate_vault(tmp_path / "b"),
        now=NOW,
        options=SkillOptions(min_sessions=2, boilerplate_min_sessions=2),
    ).candidates
    assert any("contract" in item.record["name"] for item in unfiltered)
    assert filtered and not any("contract" in item.record["name"] for item in filtered)
    # The same procedure is found either way; only its name and request words change.
    assert [s["signature"] for s in filtered[0].evidence["steps"]] == [
        "cmd:uv run pytest",
        "cmd:git commit",
    ]


def test_quality_example_and_cli_summary(tmp_path: Path) -> None:
    root = _boilerplate_vault(tmp_path)
    runner = CliRunner()
    output = runner.invoke(
        app,
        ["dream", "skills", "--root", str(root), "--min-sessions", "2", "--summary"],
    )
    assert output.exit_code == 0, output.output
    data = json.loads(output.output[output.output.index("{") :])
    assert "candidates" not in data
    quality = data["report"]["quality"]
    if quality["candidates"]:
        assert quality["example"]["name"] and quality["example"]["steps"]
    with pytest.raises(ValueError):
        run_skills(root, now=NOW, options=SkillOptions(boilerplate_min_sessions=1))


def test_intents_are_cleaned_in_evidence(tmp_path: Path) -> None:
    specs = [
        ("cut the widget release", "claude", 1),
        ("please cut a widget release", "codex", 2),
        ("widget release cut today", "kimi", 3),
    ]
    from memfw.dream.skills.run import build

    episodes = []
    for number, (request, agent, day) in enumerate(specs):
        entries = _entries(request, agent, day)
        entries[1] = {
            **entries[1],
            "snippet": "Run the suite in /Users/zztester/proj first.\n"
            "[tool Bash] command: uv run pytest -q",
        }
        episodes += segment(agent, f"s{number}", entries)
    (procedure,) = distill(episodes, min_sessions=3)
    candidate = build(procedure, SkillOptions(), None, "2026-03-01T00:00:00Z")
    assert candidate is not None
    first = candidate.evidence["steps"][0]
    assert first["intent"] == "Run the suite in <place> first"
    assert first["intent_source"] == "shared"
    assert "zztester" not in candidate.skill_md + json.dumps(candidate.evidence)


def test_representative_intent_needs_two_independent_groups() -> None:
    from memfw.dream.skills.distill import _representative_intent

    shared = {0: ["", "Build the release wheel"], 1: ["Build the release wheel."], 2: ["x"]}
    assert _representative_intent(shared) == "Build the release wheel"
    assert _representative_intent({0: ["Build the wheel", "Build the wheel"], 1: [""]}) == ""
    assert _representative_intent({0: ["", ""]}) == ""


def test_paths_and_generic_tool_aliases_are_not_words_or_steps() -> None:
    assert tool_calls("[tool glob_file_search] pattern: x") == []
    assert tool_calls("[tool edit_file_v2] path: x") == []
    assert [c.signature for c in tool_calls("[tool web_fetch] url: x")] == ["tool:web_fetch"]
    entries = _entries("fix the widget in /Users/zztester/src-tree/proj now", "claude", 1)
    (episode,) = segment("src", "s1", entries)
    assert "widget" in episode.words
    assert not {"users", "zztester", "src-tree", "proj"} & set(episode.words)


def test_git_dash_c_is_skipped_and_inspection_only_procedures_are_rejected() -> None:
    assert [c.signature for c in command_calls("git -C /x/y status --short")] == [
        "cmd:git status"[4:]
    ]
    specs = [
        ("look around the widget repo", agent, day) for agent, day in (("a", 1), ("b", 2), ("c", 3))
    ]
    episodes = []
    for number, (request, agent, day) in enumerate(specs):
        entries = [
            {
                "role": "user",
                "snippet": request,
                "ref": "r@0",
                "agent": agent,
                "timestamp": f"2026-01-0{day}T10:00:00Z",
            },
            {"role": "assistant", "snippet": "[tool Bash] command: git status", "ref": "r@1"},
            {"role": "user", "snippet": "nothing to commit, working tree clean", "ref": "r@2"},
            {"role": "assistant", "snippet": "[tool Bash] command: git log", "ref": "r@3"},
            {"role": "user", "snippet": "exit code 0", "ref": "r@4"},
        ]
        episodes += segment(agent, f"s{number}", entries)
    stats: dict[str, int] = {}
    assert distill(episodes, min_sessions=3, stats=stats) == []
    assert stats["rejected_inspection_only"] == 1


def test_batch_prose_states_the_intent_of_the_first_call_only() -> None:
    entries = [
        {"role": "user", "snippet": "ship the widget", "ref": "r@0", "agent": "a"},
        {
            "role": "assistant",
            "snippet": "Build and tag it.\n[tool Bash] command: uv build\n"
            "[tool Bash] command: git tag x",
            "ref": "r@1",
        },
    ]
    (episode,) = segment("src", "s", entries)
    assert [step.intent for step in episode.steps] == ["Build and tag it", ""]


def test_script_text_and_plumbing_tools_are_not_steps() -> None:
    assert [c.signature for c in command_calls("uv build\nsecret = 1\ngit tag v1")] == [
        "uv build",
        "git tag",
    ]
    assert command_calls("const x = require('y')") == []
    for name in ("get_goal", "update_goal", "team_task_update", "get_mcp_tools", "spawn_agent"):
        assert tool_calls(f"[tool {name}] x") == [], name
    assert tool_calls("[tool update_current_step] x") == []


def test_prose_words_are_not_programs() -> None:
    assert command_calls("se") == []
    assert command_calls("s x") == []
    assert [c.signature for c in command_calls("pre-commit run")] == ["pre-commit run"]
    assert [c.signature for c in command_calls("./release-notes.sh")] == ["release-notes.sh"]


# -- review fixes: privacy, recall, independence, ranking, literal tags --------------

LEAKS = ("secret_project", "github.com", "private-repo", "hunter2", "prod-pg-01", "plan.py")


def _with_intent(entries: list[dict[str, Any]], intent: str) -> list[dict[str, Any]]:
    entries[1] = {**entries[1], "snippet": f"{intent}\n[tool Bash] command: uv run pytest -q"}
    return entries


def test_one_session_intents_never_reach_the_candidate() -> None:
    """Scenario 1: a sentence from one session is not copied; leaks are scrubbed."""

    from memfw.dream.skills.run import build, quality_report

    intents = [
        "Edit src/memfw/secret_project/plan.py before the run",
        "Clone github.com/alice/private-repo before the run",
        "Login with password hunter2 on prod-pg-01 before the run",
    ]
    episodes = []
    for number, intent in enumerate(intents):
        entries = _with_intent(
            _entries(f"cut widget release {number}", f"a{number}", number + 1), intent
        )
        episodes += segment(f"a{number}", f"s{number}", entries)
    (procedure,) = distill(episodes, min_sessions=3)
    candidate = build(procedure, SkillOptions(), None, "2026-03-01T00:00:00Z")
    assert candidate is not None
    report = quality_report([candidate], None, {}, len(episodes))
    blob = (candidate.skill_md + json.dumps(candidate.evidence) + json.dumps(report)).lower()
    for leak in LEAKS:
        assert leak not in blob, leak
    first = candidate.evidence["steps"][0]
    assert first["intent"] == "" and first["intent_source"] == "generated"
    assert "1. Run the tests: run `uv run pytest -q`" in candidate.skill_md
    assert report["example"]["steps"][0] == "1. Run the tests: run `uv run pytest -q`"


def test_shared_intents_are_scrubbed_of_places_and_credentials() -> None:
    episodes = []
    for number in range(3):
        intent = "Open src/pkg/plan.py and use token abc123 on db-node-01 before the run"
        entries = _with_intent(
            _entries(f"cut widget release {number}", f"a{number}", number + 1), intent
        )
        episodes += segment(f"a{number}", f"s{number}", entries)
    (procedure,) = distill(episodes, min_sessions=3)
    intent = procedure.steps[0].intent
    assert "plan.py" not in intent and "abc123" not in intent and "db-node-01" not in intent
    assert intent.startswith("Open <place> and use token <secret> on <place>")


def test_a_request_the_user_really_repeats_still_yields_a_procedure() -> None:
    """Scenario 2: the corpus filter empties the request, but the episode stays."""

    request = "Please cut the weekly widget release and publish the notes for the team."
    sessions = [
        (f"s{number}", "claude" if number % 2 else "codex", number + 1) for number in range(6)
    ]
    bp = BoilerplateFilter(min_sessions=5)
    for session, agent, _ in sessions:
        bp.observe((agent, session), agent, request)
    episodes = []
    for session, agent, day in sessions:
        episodes += segment(agent, session, _entries(request, agent, day), bp)
    assert len(episodes) == 6 and all(episode.request == "" for episode in episodes)
    assert bp.report()["requests_template_only"] == 6
    (procedure,) = distill(episodes, min_sessions=3)
    assert procedure.independent == 6
    assert [step.signature for step in procedure.steps][:2] == ["cmd:uv run pytest", "cmd:uv build"]


def test_a_fan_out_across_midnight_counts_once() -> None:
    """Scenario 3a: the same request 1h apart on two dates is one task."""

    specs = [
        ("cut the widget release", "a", "2026-01-01T23:30:00Z"),
        ("cut the widget release", "b", "2026-01-02T00:30:00Z"),
        ("ship a widget release now", "c", "2026-01-05T10:00:00Z"),
    ]
    episodes = []
    for number, (request, agent, ts) in enumerate(specs):
        episodes += segment(agent, f"s{number}", _entries(request, agent, 1, ts=ts))
    stats: dict[str, int] = {}
    assert distill(episodes, min_sessions=3, stats=stats) == []
    assert stats["rejected_not_independent"] == 1


def test_per_worker_wording_is_one_task() -> None:
    """Scenario 3b: "worker one / two / three" of one fan-out is one task."""

    specs = [
        ("You are worker one: review the widget batch", "2026-01-01T22:00:00Z"),
        ("You are worker two: review the widget batch", "2026-01-01T23:59:00Z"),
        ("You are worker three: review the widget batch", "2026-01-02T03:00:00Z"),
    ]
    episodes = []
    for number, (request, ts) in enumerate(specs):
        episodes += segment("a", f"s{number}", _entries(request, "a", 1, ts=ts))
    stats: dict[str, int] = {}
    assert distill(episodes, min_sessions=3, stats=stats) == []
    assert stats["rejected_not_independent"] == 1


def test_dispatch_line_merges_sessions() -> None:
    episodes = []
    for number in range(3):
        entries = _entries(f"cut widget release take {'abc'[number]}x", f"a{number}", number + 1)
        entries[0] = {**entries[0], "dispatch_line": "line-1" if number < 2 else "line-2"}
        episodes += segment(f"a{number}", f"s{number}", entries)
    stats: dict[str, int] = {}
    assert distill(episodes, min_sessions=3, stats=stats) == []
    assert stats["rejected_not_independent"] == 1


def test_epoch_timestamps_are_parsed_into_real_days() -> None:
    """Scenario 3c: epoch seconds or milliseconds are not fake days."""

    base = 1767225600  # 2026-01-01T00:00:00Z
    episodes = []
    for number in range(3):
        ts = str(base + 3600 * number) if number < 2 else (base + 7200) * 1000
        request = f"cut widget release {'xyz'[number]}"
        episodes += segment("a", f"s{number}", _entries(request, "a", 1, ts=ts))
    assert {episode.day for episode in episodes} == {"2026-01-01"}
    stats: dict[str, int] = {}
    assert distill(episodes, min_sessions=3, stats=stats) == []
    assert stats["rejected_too_few_days"] == 1


def test_two_successful_independent_sessions_are_required() -> None:
    episodes = []
    for number in range(3):
        entries = _entries(f"cut widget release {'xyz'[number]}", f"a{number}", number + 1)
        if number:
            # Every result of these sessions shows a failure.
            entries = [
                {**entry, "snippet": "1 failed, 2 passed"} if entry["ref"] in RESULTS else entry
                for entry in entries
            ]
        episodes += segment(f"a{number}", f"s{number}", entries)
    stats: dict[str, int] = {}
    assert distill(episodes, min_sessions=3, stats=stats) == []
    assert stats["rejected_too_few_successes"] == 1


RESULTS = {"r@2", "r@4", "r@6"}


def test_candidate_clusters_are_ranked_by_shared_weight_not_age() -> None:
    """Item 4: more than the cap of older clusters must not hide the closest one."""

    from memfw.dream.skills.distill import MAX_CANDIDATE_CLUSTERS, cluster
    from memfw.dream.skills.episodes import Episode, Step

    def episode(number: int, signatures: list[str], request: str = "") -> Episode:
        return Episode(
            source="src",
            session_id=f"s{number}",
            agent="a",
            ref=f"r{number}",
            timestamp=f"2026-01-01T{number // 60:02d}:{number % 60:02d}:00Z",
            request=request,
            steps=[Step(signature=item, ref=f"r{number}", position=0.0) for item in signatures],
        )

    older = MAX_CANDIDATE_CLUSTERS + 6
    episodes = [episode(n, ["cmd:mid", f"tool:only{n}"]) for n in range(older)]
    episodes += [episode(older + n, [f"tool:filler{n}"]) for n in range(130)]
    target = older + 130
    episodes.append(episode(target, ["cmd:mid", "cmd:rare a", "cmd:rare b"]))
    episodes.append(episode(target + 1, ["cmd:mid", "cmd:rare a", "cmd:rare b"]))
    weights = signature_weights(episodes)
    assert weights is not None and weights["cmd:mid"] >= 0.15
    clusters = cluster(episodes, 0.4, weights)
    joined = [members for members in clusters if len(members) == 2]
    assert [[item.session_id for item in members] for members in joined] == [
        [f"s{target}", f"s{target + 1}"]
    ]


@pytest.mark.parametrize(
    "text",
    [
        "<instructions>keep it short</instructions> summarise the widget notes",
        "<task>rename the gadget module</task>",
        "<request>tag the release</request>",
        "<Tools>hammer and saw</Tools> plan the shed",
    ],
)
def test_literal_tags_a_person_types_are_kept(text: str) -> None:
    """Item 5: only the harness spellings are markers."""

    assert strip_structural(text) == text


# -- re-review: backticked agent text and assignments ---------------------------------


@pytest.mark.parametrize(
    ("intent", "leaks"),
    [
        ("Edit `secret_project/plan.py` now", ("secret_project", "plan.py")),
        ("Push to `github.com/zz-team/private-repo`", ("github.com", "private-repo")),
        ("Use `password hunter` to log in", ("hunter",)),
        ("Open `db-node-01` first", ("db-node-01",)),
    ],
)
def test_backticked_agent_text_is_scrubbed(intent: str, leaks: tuple[str, ...]) -> None:
    from memfw.dream.skills.distill import VerifiedStep, _representative_intent
    from memfw.dream.skills.draft import clean, step_headline

    shared = _representative_intent({0: [intent], 1: [intent]})
    step = VerifiedStep(
        key="s1",
        signature="cmd:python3 -m pytest",
        sessions=2,
        refs=[],
        position=0.0,
        intent=clean(shared, 160),
        flags=["-q"],
    )
    headline = step_headline(step)
    for leak in leaks:
        assert leak not in headline, leak
    # Generated command text keeps its own spelling (python3 is not a host).
    assert headline.endswith(": run `python3 -m pytest -q`")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Run export FOO=secret before tests", "Run export FOO=<secret> before tests"),
        ("Set FOO=letmein then build", "Set FOO=<secret> then build"),
        ("use `DEBUG=verbose make` here", "use `DEBUG=<secret> make` here"),
        ("token=abc then go", "token=<secret> then go"),
    ],
)
def test_assignment_values_are_never_kept(text: str, expected: str) -> None:
    from memfw.dream.skills.scrub import scrub

    assert scrub(text) == expected


def test_shared_backticked_intent_end_to_end() -> None:
    from memfw.dream.skills.run import build

    episodes = []
    for number in range(3):
        intent = "Edit `secret_project/plan.py` and set FOO=letmein before the run"
        entries = _with_intent(
            _entries(f"cut widget release {number}", f"a{number}", number + 1), intent
        )
        episodes += segment(f"a{number}", f"s{number}", entries)
    (procedure,) = distill(episodes, min_sessions=3)
    candidate = build(procedure, SkillOptions(), None, "2026-03-01T00:00:00Z")
    assert candidate is not None
    blob = candidate.skill_md + json.dumps(candidate.evidence)
    assert "secret_project" not in blob and "letmein" not in blob
    assert "Edit `<place>` and set FOO=<secret> before the run" in candidate.skill_md


# -- round 3: quoted assignments, backslash paths, trailing slashes, cue fillers ------


def _shared_clean(text: str) -> str:
    from memfw.dream.skills.distill import _representative_intent
    from memfw.dream.skills.draft import clean

    return clean(_representative_intent({0: [text], 1: [text]}), 160)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Run `export FOO='hunter two'` first", "Run `export FOO=<secret>` first"),
        ('Set FOO="hunter two" now', "Set FOO=<secret> now"),
        ("Use $env:FOO='x' here", "Use $env:FOO=<secret> here"),
        ("Set FOO = hunter then", "Set FOO=<secret> then"),
    ],
)
def test_quoted_and_spaced_assignments_lose_their_value(text: str, expected: str) -> None:
    assert _shared_clean(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Copy \\\\nas\\share\\x over", "Copy <place> over"),
        ("Open secret_project\\plan.py now", "Open <place> now"),
        ("Edit `secret_project/`plan.py now", "Edit `<place>`plan.py now"),
        ("Run the tests in src/ first", "Run the tests in <place> first"),
    ],
)
def test_backslash_and_trailing_slash_paths_are_places(text: str, expected: str) -> None:
    assert _shared_clean(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Password - hunter ok", "Password - <secret> ok"),
        ("set the token to hunter now", "set the token to <secret> now"),
        ("token: abc then", "token: <secret> then"),
        ("the secret is hunter here", "the secret is <secret> here"),
    ],
)
def test_filler_after_a_cue_does_not_move_the_mask(text: str, expected: str) -> None:
    from memfw.dream.skills.scrub import scrub

    assert _shared_clean(text) == expected
    assert scrub(scrub(text)) == scrub(text)  # idempotent: clean() scrubs scrubbed text


# -- round 4: substitutions, comma filler, pass:, and no over-scrubbing ---------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("export TOKEN=$(cat secret_file) now", "export TOKEN=<secret> now"),
        ("Run FOO=`whoami` first", "Run FOO=<secret> first"),
        ("Use the token, hunter, to log in", "Use the token, <secret>, to log in"),
        ("pass: hunter then", "pass: <secret> then"),
        ("passwd hunter then", "passwd <secret> then"),
        ("Set RETRIES=3 but API_TOKEN=3 too", "Set RETRIES=3 but API_TOKEN=<secret> too"),
    ],
)
def test_round4_leaks_are_masked(text: str, expected: str) -> None:
    from memfw.dream.skills.scrub import scrub

    assert scrub(text) == expected
    assert scrub(expected) == expected


@pytest.mark.parametrize(
    "text",
    [
        "Run the tests and check the token count",
        "Rotate the API key before release",
        "Update the password reset flow tests",
        "Set retries = 3 and rerun",
        "Check the secret scanner passes",
        "Use key bindings for vim",
        "the test is passing",
    ],
)
def test_plain_english_after_a_cue_is_kept(text: str) -> None:
    assert _shared_clean(text) == text
