"""v0.5.1 conversation search cards, get --around, and redaction fields."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from memfw.cli import app as cli_app
from memfw.conversations import ConversationError, open_share
from memfw.conversations.adapter import SqliteStoreSource
from memfw.conversations.backends import ModelReply
from memfw.conversations.share import ConversationShare
from memfw.conversations.store import WITHHELD
from memfw.conversations.two_stage import _shrink
from memfw.conversations.worker import WorkState, state_path
from memfw.handoff.pack import build_pack
from memfw.handoff.schema import (
    CONTINUATION_KEYS,
    MODEL_KEYS,
    OMITTED_FIELD_NAMES,
    PACK_SCHEMA,
    REDACTION_POLICY,
    SCHEMA,
    listed_omissions,
    validate_pack,
)
from tests.conversation_fixtures import (
    SECRET,
    isolated_home,  # noqa: F401
    make_vault,
    turn,
    write_store,
)
from tests.handoff_fixtures import make_store, standard_session
from tests.test_conversations_share import SHARE_ON, _setup
from tests.test_conversations_two_stage import _share as two_stage_share


@pytest.fixture(autouse=True)
def _home(isolated_home: Path) -> Path:  # noqa: F811
    return isolated_home


CARD_KEYS = (
    "session_id",
    "app",
    "started_at",
    "updated_at",
    "turns",
    "summary",
    "intent",
    "outcome",
    "tags",
    "suggested_start",
)


def test_default_search_session_card_uses_labels_not_turn_text(tmp_path: Path) -> None:
    share = two_stage_share(tmp_path)
    found = share.search("uploader")
    assert found["redaction_policy"] == REDACTION_POLICY
    assert found["omitted_fields"] == []
    cards = [item["session_card"] for item in found["results"]]
    assert cards and all(tuple(card) == CARD_KEYS for card in cards)
    alpha = next(item for item in found["results"] if item["session_id"] == "s-alpha")
    card = alpha["session_card"]
    assert card["suggested_start"] == alpha["offset"]
    assert card["summary"] == "Added exponential backoff retries to the uploader."
    assert card["intent"] == "add a retry wrapper"
    assert card["outcome"] == "done"
    assert card["tags"]["project"] == ["uploader"]
    assert "snippet" not in card
    assert alpha["snippet"] not in json.dumps(card)
    # s-beta has no worker summary: null, not a sentence made up from the turn.
    beta = share.search("Contact")["results"][0]["session_card"]
    assert beta["session_id"] == "s-beta"
    assert beta["summary"] is None and beta["intent"] is None and beta["outcome"] is None
    assert beta["tags"] is None


def test_two_stage_session_card_hit_offset_or_null(tmp_path: Path) -> None:
    share = two_stage_share(tmp_path)
    ranked = share.search("uploader", mode="two_stage")
    assert ranked["redaction_policy"] == REDACTION_POLICY
    alpha = next(item for item in ranked["sessions"] if item["session_id"] == "s-alpha")
    card = alpha["session_card"]
    assert tuple(card) == CARD_KEYS
    assert card["summary"].startswith("Added exponential backoff")
    assert alpha["windows"] and card["suggested_start"] == alpha["windows"][0]["hit_offset"]
    assert "turns" not in json.dumps(card["tags"])
    tagged = share.search("", mode="two_stage", task_type="debug", project="memfw")
    only = tagged["sessions"][0]
    assert only["windows"] == []
    assert only["session_card"]["suggested_start"] is None
    assert only["session_card"]["outcome"] == "blocked"
    assert tagged["redaction_policy"] == REDACTION_POLICY


def test_gapped_offsets_suggested_start_is_the_hit_offset_not_the_index(tmp_path: Path) -> None:
    vault = make_vault(tmp_path / "vault", SHARE_ON)
    rows = [
        turn("codex", "gaps", offset, "user", f"uploader at {offset}", f"2026-01-01T00:0{i}:00Z")
        for i, offset in enumerate((10, 30, 50))
    ]
    store = write_store(tmp_path / "data" / "c.sqlite", rows)
    share = open_share(vault, "claude", store=store)
    hit = share.search("uploader at 30")["results"][0]
    assert hit["offset"] == 30
    assert hit["session_card"]["suggested_start"] == 30
    # suggested_start is the store offset, not the 0-based turn index (1).
    window = share.get("gaps", around=hit["session_card"]["suggested_start"], radius=0)
    assert window["center_offset"] == 30
    assert window["center_index"] == 1
    assert [item["index"] for item in window["turns"]] == [1]
    assert window["turns"][0]["ref"].endswith("@30")
    with pytest.raises(ConversationError):
        share.get("gaps", around=1, radius=0)


def test_search_card_suggested_start_feeds_get_around(tmp_path: Path) -> None:
    """search → session_card.suggested_start → get --around, on gapped store offsets."""

    offsets = (0, 812, 2051, 4400)
    vault = make_vault(tmp_path / "vault", SHARE_ON)
    rows = [
        turn("codex", "s-gap", offset, "user", f"needle {offset}", f"2026-01-01T00:0{i}:00Z")
        for i, offset in enumerate(offsets)
    ]
    store = write_store(tmp_path / "data" / "c.sqlite", rows)
    share = open_share(vault, "claude", store=store)
    hit = share.search("needle 2051")["results"][0]
    suggested = hit["session_card"]["suggested_start"]
    assert suggested == 2051 == hit["offset"]
    window = share.get("s-gap", around=suggested, radius=1)
    assert [item["ref"].rsplit("@", 1)[1] for item in window["turns"]] == ["812", "2051", "4400"]
    assert window["center_offset"] == 2051
    assert window["center_index"] == 2
    center = next(item for item in window["turns"] if item["index"] == window["center_index"])
    assert center["ref"].endswith("@2051")
    with pytest.raises(ConversationError):
        missed = share.get("s-gap", around=1, radius=1)
        assert missed["turns"] == []  # must not come back empty

    runner = CliRunner()
    base = ["--root", str(vault), "--store", str(store)]
    searched = runner.invoke(cli_app, ["conversations", "search", "needle 2051", *base])
    assert searched.exit_code == 0, searched.output
    card_start = json.loads(searched.output)["results"][0]["session_card"]["suggested_start"]
    assert card_start == 2051
    got = runner.invoke(
        cli_app,
        ["conversations", "get", "s-gap", "--around", str(card_start), "--radius", "1", *base],
    )
    assert got.exit_code == 0, got.output
    body = json.loads(got.output)
    assert [item["ref"].rsplit("@", 1)[1] for item in body["turns"]] == ["812", "2051", "4400"]
    assert body["center_offset"] == 2051
    assert body["center_index"] == 2
    missing = runner.invoke(
        cli_app, ["conversations", "get", "s-gap", "--around", "999", "--radius", "0", *base]
    )
    assert missing.exit_code == 2
    assert not missing.output.strip().startswith("{")
    help_text = runner.invoke(cli_app, ["conversations", "get", "--help"])
    assert help_text.exit_code == 0
    # Rich wraps the option panel, and a forced-color terminal paints each
    # line. Drop those color codes before joining the wrapped words.
    plain_help = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", help_text.output)
    flat_help = " ".join(
        "".join(ch if ch.isalnum() or ch in " .-_@" else " " for ch in plain_help).split()
    ).lower()
    assert "store offset" in flat_help
    assert "not a turn index" in flat_help


def test_get_around_is_inclusive_and_ignores_start_end(tmp_path: Path) -> None:
    vault = make_vault(tmp_path / "vault", SHARE_ON)
    rows = [
        turn("codex", "long", i, "user", f"step {i}", f"2026-01-01T00:{i:02d}:00Z")
        for i in range(10)
    ]
    store = write_store(tmp_path / "data" / "c.sqlite", rows)
    share = open_share(vault, "claude", store=store)
    window = share.get("long", around=4, radius=2, start=0, end=2)
    assert [item["index"] for item in window["turns"]] == [2, 3, 4, 5, 6]
    assert share.get("long", around=0)["turns"][0]["index"] == 0
    assert [item["index"] for item in share.get("long", around=0)["turns"]] == [0, 1, 2]
    edge = share.get("long", around=9, radius=2)
    assert [item["index"] for item in edge["turns"]] == [7, 8, 9]
    assert share.get("long", around=4, radius=1, max_bytes=4096)["redaction_policy"] == (
        REDACTION_POLICY
    )


def test_around_and_radius_reject_illegal_values(tmp_path: Path) -> None:
    vault, store = _setup(tmp_path)
    share = open_share(vault, "claude", store=store)
    for kwargs in (
        {"around": -1},
        {"around": 1, "radius": -1},
        {"radius": 2},
        {"around": True},
        {"around": 1, "radius": True},
    ):
        with pytest.raises(ConversationError):
            share.get("s-alpha", **kwargs)  # type: ignore[arg-type]


def test_cli_around_radius_and_illegal_exit_code(tmp_path: Path) -> None:
    vault, store = _setup(tmp_path)
    runner = CliRunner()
    base = ["--root", str(vault), "--store", str(store)]
    result = runner.invoke(
        cli_app, ["conversations", "get", "s-alpha", "--around", "1", "--radius", "0", *base]
    )
    assert result.exit_code == 0, result.output
    body = json.loads(result.output)
    assert [item["index"] for item in body["turns"]] == [1]
    assert body["redaction_policy"] == REDACTION_POLICY
    assert "omitted_fields" in body
    default = runner.invoke(cli_app, ["conversations", "get", "s-alpha", "--around", "1", *base])
    assert default.exit_code == 0
    assert [item["index"] for item in json.loads(default.output)["turns"]] == [0, 1, 2, 3]
    bad = runner.invoke(cli_app, ["conversations", "get", "s-alpha", "--around", "-1", *base])
    assert bad.exit_code == 2
    bad_radius = runner.invoke(
        cli_app, ["conversations", "get", "s-alpha", "--radius", "-3", *base]
    )
    assert bad_radius.exit_code == 2


def test_withheld_row_text_is_empty_and_withheld_is_true(tmp_path: Path) -> None:
    """A withheld row stays, with text "" — readers use the flag, not a marker string."""

    vault = make_vault(tmp_path / "vault", SHARE_ON)
    held = turn("codex", "s-hold", 0, "assistant", "must-not-leak", "2026-01-01T00:00:00Z")
    held["withheld"] = 1
    held["text"] = ""
    plain = turn("codex", "s-hold", 1, "user", "still here", "2026-01-01T00:01:00Z")
    store = write_store(tmp_path / "data" / "c.sqlite", [held, plain])
    rows = SqliteStoreSource(store).turns("codex", "s-hold")
    assert [row.offset for row in rows] == [0, 1]
    withheld_rows = [row for row in rows if row.withheld]
    assert len(withheld_rows) == 1
    assert withheld_rows[0].text == "" and withheld_rows[0].withheld is True
    assert WITHHELD not in withheld_rows[0].text
    got = open_share(vault, "claude", store=store).get("s-hold")
    shown = [item for item in got["turns"] if item["withheld"]]
    assert len(shown) == 1
    assert shown[0]["text"] == "" and shown[0]["withheld"] is True
    assert WITHHELD not in json.dumps(got)
    assert len(got["turns"]) == 2


def test_get_withheld_text_is_empty_and_named_in_omitted_fields(tmp_path: Path) -> None:
    vault, store = _setup(tmp_path)
    got = open_share(vault, "claude", store=store).get("s-beta")
    secret = [turn for turn in got["turns"] if turn["withheld"]]
    assert secret and secret[0]["text"] == "" and secret[0]["withheld"] is True
    assert "text" in got["omitted_fields"]
    assert SECRET not in json.dumps(got)
    assert got["redaction_policy"] == REDACTION_POLICY


def test_memfw_handoff_schema_locks_continuation_keys(tmp_path: Path) -> None:
    system_required = PACK_SCHEMA["properties"]["system"]["required"]
    assert PACK_SCHEMA["$id"] == SCHEMA == "memfw.handoff/v1"
    assert tuple(CONTINUATION_KEYS) == (
        "goal",
        "decisions",
        "open_questions",
        "constraints",
        "files",
        "next_step",
        "evidence_refs",
    )
    for key in CONTINUATION_KEYS:
        assert key in system_required
    assert "redaction_policy" in PACK_SCHEMA["required"]
    assert "omitted_fields" in PACK_SCHEMA["required"]
    assert PACK_SCHEMA["properties"]["redaction_policy"]["const"] == REDACTION_POLICY
    db = make_store(tmp_path, {"claude": {"s1": standard_session("claude")}})
    pack = build_pack(db, "claude", "s1", git=False)
    validate_pack(pack)
    system = pack["system"]
    assert set(CONTINUATION_KEYS) <= set(system)
    assert system["constraints"] == []
    assert system["next_step"] == ""
    assert system["decisions"] == [] and system["open_questions"] == []
    assert system["files"] == system["files_touched"]
    assert system["evidence_refs"] == [
        f"claude:s1@{turn['offset']}" for turn in system["recent_turns"]
    ]
    assert pack["schema"] == SCHEMA
    assert pack["redaction_policy"] == REDACTION_POLICY
    assert pack["omitted_fields"] == [
        name for name in OMITTED_FIELD_NAMES if name in pack["omitted_fields"]
    ]
    dumped = json.dumps(pack)
    assert SECRET not in dumped
    withheld = [turn for turn in system["recent_turns"] if turn["withheld"]]
    assert withheld and all(turn["text"] == "" for turn in withheld)


def test_ok_model_part_copies_decisions_and_next_step(tmp_path: Path) -> None:
    class Backend:
        name = "fake-local"
        local = True

        def run(self, task: str, data: dict[str, object], *, max_tokens: int) -> ModelReply:
            body = {key: [f"{key} item"] for key in MODEL_KEYS}
            return ModelReply(text=json.dumps(body))

    db = make_store(tmp_path, {"claude": {"copy": standard_session("claude", "copy")}})
    pack = build_pack(
        db,
        "claude",
        "copy",
        git=False,
        summarize_spec="x:y",
        loader=lambda spec, allow_cloud=False: Backend(),
    )
    validate_pack(pack)
    assert pack["model_summarized"]["status"] == "ok"
    assert pack["system"]["decisions"] == ["decisions item"]
    assert pack["system"]["open_questions"] == ["open_questions item"]
    assert pack["system"]["next_step"] == "next item"
    assert pack["system"]["constraints"] == []
    assert "next_step" not in pack["omitted_fields"]


def test_handoff_continuation_keys_and_evidence_refs(tmp_path: Path) -> None:
    vault, store = _setup(tmp_path)
    pack = open_share(vault, "claude", store=store).handoff("s-alpha", for_agent="kimi")
    system = pack["system"]
    assert pack["schema"] == "memfw.conversations.handoff/v1"
    for key in (
        "goal",
        "decisions",
        "open_questions",
        "constraints",
        "files",
        "next_step",
        "evidence_refs",
    ):
        assert key in system
    assert system["constraints"] == []
    assert system["next_step"] == ""
    assert system["files"] == system["files_touched"]
    assert system["evidence_refs"] == [turn["ref"] for turn in system["recent_turns"]]
    assert all(" " not in ref and "@" in ref for ref in system["evidence_refs"])
    assert pack["redaction_policy"] == REDACTION_POLICY
    assert isinstance(pack["omitted_fields"], list)
    # Existing keys stay.
    assert system["goal"].startswith("Add a retry")
    assert "files_touched" in system and "recent_turns" in system


def test_two_stage_shrink_names_cleared_tags_and_intent() -> None:
    """Emptying tags or intent in a two_stage reply lists those names in omitted_fields."""

    tags = {"intent": "classify the uploader failure", "project": ["uploader"], "outcome": "done"}
    item = {
        "cwd": "c" * 200,
        "project": "demo",
        "model_output": {"summary": "s" * 300, "tags": dict(tags)},
        "session_card": {
            "summary": "s" * 300,
            "intent": tags["intent"],
            "outcome": "done",
            "tags": dict(tags),
        },
    }
    omitted: set[str] = set()
    _shrink(item, 32, omitted)
    assert item["session_card"]["tags"] is None
    assert item["session_card"]["intent"] is None
    assert "tags" in omitted and "intent" in omitted
    named = listed_omissions(omitted)
    assert "tags" in named and "intent" in named


def test_search_omits_a_secret_summary_instead_of_inventing_one(tmp_path: Path) -> None:
    vault = make_vault(tmp_path / "vault", SHARE_ON)
    store = write_store(
        tmp_path / "data" / "c.sqlite",
        [turn("codex", "s1", 0, "user", "uploader topic", "2026-01-01T00:00:00Z")],
    )
    state = state_path({})
    with WorkState(state).open() as connection:
        connection.execute(
            "INSERT INTO summaries (app, session_id, chunk_start, chunk_end, prompt_version,"
            " model, summary, intent, outcome, created_at, tags) VALUES"
            " ('codex', 's1', -1, 1, 'conv-v2', 'fake', ?, '', '',"
            " '2026-01-02T00:00:00+00:00', NULL)",
            (f"The secret is {SECRET}",),
        )
    found = open_share(vault, "claude", store=store).search("uploader")
    card = found["results"][0]["session_card"]
    assert card["summary"] is None
    assert "summary" in found["omitted_fields"]
    assert SECRET not in json.dumps(found)


def test_share_without_labels_leaves_summary_null(tmp_path: Path) -> None:
    vault, store = _setup(tmp_path)
    bare = ConversationShare(
        open_share(vault, "claude", store=store).source,
        open_share(vault, "claude", store=store).policy,
        "claude",
    )
    card = bare.search("uploader")["results"][0]["session_card"]
    assert card["summary"] is None and card["tags"] is None and card["intent"] is None
