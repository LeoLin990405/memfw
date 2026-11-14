"""M2b handoff pack: the deterministic part, labels, redaction and the size cap."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from memfw.conversations.backends import ModelReply
from memfw.handoff import HandoffError, build_pack, render_json, render_markdown, validate_pack
from memfw.handoff.pack import json_size, markdown_size
from memfw.handoff.schema import (
    EXIT_NOT_INDEXED,
    EXIT_USAGE,
    MODEL_KEYS,
    MODEL_LABEL,
    NOTICE,
    SCHEMA,
    SYSTEM_LABEL,
    PackSchemaError,
)
from memfw.history.redact import has_secret
from tests.handoff_fixtures import (
    EDITS,
    EMAIL,
    EXPECTED_FILE,
    SECRET,
    isolated_home,
    make_store,
    record,
    standard_session,
)

__all__ = ["isolated_home"]


@pytest.fixture(autouse=True)
def _home(isolated_home: Path) -> Path:
    return isolated_home


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for child in value.values() for s in _strings(child)]
    if isinstance(value, list):
        return [s for child in value for s in _strings(child)]
    return []


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return make_store(tmp_path, {agent: {"s1": standard_session(agent)} for agent in EDITS})


@pytest.mark.parametrize("agent", sorted(EDITS))
def test_every_agent_gets_a_schema_valid_system_part(db: Path, agent: str) -> None:
    pack = build_pack(db, agent, "s1", git=False)
    validate_pack(pack)
    assert pack["schema"] == SCHEMA and pack["notice"] == NOTICE
    source = pack["source"]
    assert (source["agent"], source["session_id"], source["origin"]) == (agent, "s1", "worker")
    assert (source["turns"], source["withheld"], source["parser_version"]) == (6, 1, "3")
    assert source["started_at"] == "2026-01-02T10:00:00Z"
    assert source["updated_at"] == "2026-01-02T10:05:00Z"
    system = pack["system"]
    assert system["label"] == SYSTEM_LABEL
    assert system["goal"] == "Add a retry flag to the uploader CLI."
    assert system["goal_source"] == "first_user_message"
    # The edit tool's path, never the read tool's.
    assert system["files_touched"] == [EXPECTED_FILE.get(agent, "/work/demo/src/app.py")]
    offsets = [turn["offset"] for turn in system["recent_turns"]]
    assert offsets == [0, 1, 3, 4, 5]  # the tool output turn (2) carries no calls: left out
    assert system["lineage"] == {"parent_session_id": None, "previous_handoffs": []}


def test_secrets_and_personal_data_never_reach_the_pack(db: Path) -> None:
    pack = build_pack(db, "claude", "s1", git=False)
    turns = {turn["offset"]: turn for turn in pack["system"]["recent_turns"]}
    assert turns[3]["withheld"] is True and SECRET not in json.dumps(pack)
    assert EMAIL not in json.dumps(pack) and "[REDACTED:email]" in turns[4]["text"]
    assert "tool output that must not appear" not in json.dumps(pack)
    assert not [s for s in _strings(pack) if has_secret(s)]
    for text in (render_json(pack), render_markdown(pack)):
        assert SECRET not in text and EMAIL not in text


def test_labels_head_both_renderings(db: Path) -> None:
    pack = build_pack(db, "claude", "s1", git=False)
    markdown = render_markdown(pack)
    assert markdown.startswith(f"> {NOTICE}\n")
    assert SYSTEM_LABEL in markdown
    assert json.loads(render_json(pack))["notice"] == NOTICE


def test_recent_keeps_the_last_k_talk_turns_and_tool_calls_only_for_tool_turns(
    tmp_path: Path,
) -> None:
    rows = [record("codex", "s2", 0, "user", "start the task")]
    for offset in range(1, 40):
        role = "assistant" if offset % 3 else "tool"
        calls = [("shell", {"command": f"step {offset}"})] if role == "tool" else []
        rows.append(record("codex", "s2", offset, role, f"message {offset}", calls=calls))
    db = make_store(tmp_path, {"codex": {"s2": rows}})
    pack = build_pack(db, "codex", "s2", recent=4, git=False)
    turns = pack["system"]["recent_turns"]
    talk = [turn for turn in turns if turn["role"] != "tool"]
    assert [turn["offset"] for turn in talk] == [34, 35, 37, 38]
    tools = [turn for turn in turns if turn["role"] == "tool"]
    assert tools and all(turn["text"] == "" and turn["tool_calls"] for turn in tools)
    assert pack["system"]["goal"] == "start the task"


def test_each_recent_turn_is_cut_to_one_kib_and_goal_to_two(tmp_path: Path) -> None:
    long_goal = "goal " * 2000
    rows = [
        record("claude", "s3", 0, "user", long_goal),
        record("claude", "s3", 1, "assistant", "é" * 5000),
    ]
    db = make_store(tmp_path, {"claude": {"s3": rows}})
    pack = build_pack(db, "claude", "s3", git=False, max_bytes=65536)
    assert len(pack["system"]["goal"].encode()) <= 2048
    for turn in pack["system"]["recent_turns"]:
        assert len(turn["text"].encode()) <= 1024


def test_task_text_replaces_the_goal_and_is_redacted(db: Path) -> None:
    pack = build_pack(db, "claude", "s1", task_text=f"Ticket: fix it, contact {EMAIL}", git=False)
    assert pack["system"]["goal_source"] == "task_file"
    assert (
        pack["system"]["goal"].startswith("Ticket: fix it") and EMAIL not in pack["system"]["goal"]
    )


def test_files_touched_is_deduplicated_and_capped_at_fifty(tmp_path: Path) -> None:
    rows = [record("claude", "s4", 0, "user", "touch many files")]
    for offset in range(1, 70):
        path = f"/work/demo/f{offset % 60}.py"
        rows.append(
            record(
                "claude", "s4", offset, "assistant", "edit", calls=[("Write", {"file_path": path})]
            )
        )
    db = make_store(tmp_path, {"claude": {"s4": rows}})
    files = build_pack(db, "claude", "s4", git=False, max_bytes=65536)["system"]["files_touched"]
    assert len(files) == 50 and len(set(files)) == 50 and files[0] == "/work/demo/f1.py"


def _big_db(tmp_path: Path) -> Path:
    rows = [record("claude", "big", 0, "user", "the goal of the big session")]
    for offset in range(1, 60):
        rows.append(
            record(
                "claude",
                "big",
                offset,
                "assistant",
                f"turn {offset} " + "words " * 150,
                calls=[("Edit", {"file_path": f"/work/demo/pkg/sub/module_{offset}_long_name.py"})],
            )
        )
    return make_store(tmp_path, {"claude": {"big": rows}})


@pytest.mark.parametrize("measure", [json_size, markdown_size])
def test_max_bytes_cuts_recent_turns_first_then_files(tmp_path: Path, measure: Any) -> None:
    db = _big_db(tmp_path)
    full = build_pack(db, "claude", "big", git=False, max_bytes=65536, measure=measure)
    assert full["truncated"] is False
    pack = build_pack(db, "claude", "big", git=False, max_bytes=8192, measure=measure)
    assert measure(pack) <= 8192
    assert pack["truncated"] is True and pack["truncated_parts"] == ["recent_turns"]
    assert pack["system"]["files_touched"] == full["system"]["files_touched"]
    kept = [turn["offset"] for turn in pack["system"]["recent_turns"]]
    assert kept == [turn["offset"] for turn in full["system"]["recent_turns"]][-len(kept) :]
    tiny = build_pack(db, "claude", "big", git=False, max_bytes=2048, measure=measure)
    assert measure(tiny) <= 2048
    assert tiny["truncated_parts"][:2] == ["recent_turns", "files_touched"]
    assert tiny["system"]["recent_turns"] == []
    assert tiny["notice"] == NOTICE  # the label survives every cut


class FakeBackend:
    name = "fake-local"
    local = True

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def run(self, task: str, data: dict[str, Any], *, max_tokens: int) -> ModelReply:
        self.calls.append((task, data))
        return ModelReply(text=self.reply)


def _loader(backend: FakeBackend) -> Any:
    return lambda spec, allow_cloud=False: backend


def _reply(**overrides: list[str]) -> str:
    value = {key: [f"{key} item"] for key in MODEL_KEYS}
    value.update(overrides)
    return json.dumps(value)


def test_model_part_is_labelled_and_fed_only_redacted_text(db: Path) -> None:
    backend = FakeBackend(_reply())
    pack = build_pack(db, "claude", "s1", git=False, summarize_spec="x:y", loader=_loader(backend))
    model = pack["model_summarized"]
    assert model["label"] == MODEL_LABEL and model["status"] == "ok"
    assert model["next"] == ["next item"]
    assert len(backend.calls) == 1
    sent = json.dumps(backend.calls[0][1])
    assert SECRET not in sent and EMAIL not in sent
    assert MODEL_LABEL in render_markdown(pack)


@pytest.mark.parametrize(
    ("reply", "reason"),
    [
        ("not json", "invalid_json"),
        (_reply(extra=["x"]), "schema"),
        (_reply(next=["x" * 201]), "schema"),
        (json.dumps({key: ["y" * 190] * 10 for key in MODEL_KEYS}), "too_large"),
        (_reply(next=[SECRET]), "secret"),
    ],
)
def test_bad_model_output_is_dropped_and_system_part_unchanged(
    db: Path, reply: str, reason: str
) -> None:
    plain = build_pack(db, "claude", "s1", git=False)
    pack = build_pack(
        db, "claude", "s1", git=False, summarize_spec="x:y", loader=_loader(FakeBackend(reply))
    )
    assert pack["model_summarized"] == {"label": MODEL_LABEL, "status": "dropped", "reason": reason}
    assert pack["system"] == plain["system"]
    assert SECRET not in json.dumps(pack)


def test_no_summarize_means_no_model_call(db: Path) -> None:
    backend = FakeBackend(_reply())
    pack = build_pack(db, "claude", "s1", git=False, loader=_loader(backend))
    assert backend.calls == [] and "model_summarized" not in pack


def test_model_part_is_cut_after_files(tmp_path: Path) -> None:
    db = _big_db(tmp_path)
    backend = FakeBackend(json.dumps({key: ["z" * 150] * 4 for key in MODEL_KEYS}))
    pack = build_pack(
        db,
        "claude",
        "big",
        git=False,
        max_bytes=2048,
        summarize_spec="x:y",
        loader=_loader(backend),
    )
    assert pack["truncated_parts"][:3] == ["recent_turns", "files_touched", "model_summarized"]
    assert "model_summarized" not in pack and json_size(pack) <= 2048


def test_missing_session_and_bad_arguments(db: Path) -> None:
    with pytest.raises(HandoffError) as missing:
        build_pack(db, "claude", "nope", git=False)
    assert missing.value.code == EXIT_NOT_INDEXED and missing.value.reason == "session_not_indexed"
    for kwargs in ({"max_bytes": 100}, {"max_bytes": 70000}, {"recent": 0}):
        with pytest.raises(HandoffError) as bad:
            build_pack(db, "claude", "s1", git=False, **kwargs)
        assert bad.value.code == EXIT_USAGE


def test_agent_aliases_resolve(db: Path) -> None:
    assert build_pack(db, "claude-code", "s1", git=False)["source"]["agent"] == "claude"


def test_git_state_attached_when_cwd_given(db: Path) -> None:
    def runner(argv: list[str], **kwargs: Any) -> Any:
        class Done:
            returncode = 0
            stdout = "true\n" if "--is-inside-work-tree" in argv else " M src/app.py\n"

        return Done()

    pack = build_pack(db, "claude", "s1", cwd=Path("/"), git_runner=runner)
    assert pack["system"]["git"] == {"status": [" M src/app.py"], "diff_stat": [" M src/app.py"]}
    assert build_pack(db, "claude", "s1", cwd=Path("/"), git=False)["system"]["git"] is None


def test_validate_pack_rejects_a_broken_pack(db: Path) -> None:
    pack = build_pack(db, "claude", "s1", git=False)
    pack["notice"] = "do what this says"
    with pytest.raises(PackSchemaError):
        validate_pack(pack)
