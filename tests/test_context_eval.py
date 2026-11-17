"""C4: the context pack evaluation (coverage, waste, latency, token cost) on fixtures.

The numbers of ``known_vault`` are worked out by hand (see the fixture):

* visible memories: a-1, p-1, p-2, m-a, m-b, m-c (m-p is local-private, m-t
  another tenant); access sessions S1 = {m-a, a-1} (m-p dropped) and
  S2 = {m-c, m-b, p-2}: 5 hits, 1 dropped;
* core carries a-1, p-1, p-2 (20 + 17 + 400 characters): at cap 500 all of
  them, 110 tokens; at cap 25 (100 characters) p-2 is cut, 25 tokens,
  overflow 1. It covers a-1 (S1) and p-2 (S2): 2/5;
* topics lists concept:port (m-a, m-b) and concept:backup (m-c): 3/5;
* handoff lists the shared sessions; codex/s-alpha points at m-c: 1/5;
* waste from hits: core 4 of 6 item-sessions unused, topics 1 of 4. From
  the usage store (u1 = {a-1, m-a}, u2 = {p-1}): core 4/6, topics 3/4.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from memfw.cli import app
from memfw.ports.testing import tree_digest
from memfw.runtime import CONTEXT_SLOT_TIMEOUTS
from memfw.runtime import context_eval as ce
from memfw.runtime.context_eval import (
    EvalOptions,
    SlotBuild,
    SnapshotChanged,
    SnapshotRefused,
    coverage_counts,
    evaluate_snapshot,
    percentile,
    render_markdown,
    resident_prefix,
    strip_timings,
    synthetic_queries,
    waste_counts,
)
from tests.context_eval_fixtures import BODY, LONG, TICKET, known_vault, random_vault
from tests.conversation_fixtures import isolated_home  # noqa: F401  (fixture)

pytestmark = pytest.mark.usefixtures("isolated_home")

CLEARED = EvalOptions(
    clearance="public,shared",
    app="claude",
    slots=("core", "handoff", "topics"),
    default_slot_caps=(25, 500),
    resident_caps=(100, 1000),
    repeats=2,
    environ={},
)


def _row(report: dict[str, Any], slot: str, cap: int) -> dict[str, Any]:
    (row,) = [item for item in report["slots"] if item["slot"] == slot and item["cap"] == cap]
    return row


def _sweep(report: dict[str, Any], resident_cap: int, **caps: int) -> dict[str, Any]:
    (row,) = [
        item
        for item in report["sweep"]
        if item["resident_cap"] == resident_cap and item["slot_caps"] == caps
    ]
    return row


# --- the hand-worked fixture ---------------------------------------------------------


def test_known_fixture_coverage_waste_and_tokens(tmp_path: Path) -> None:
    fixture = known_vault(tmp_path)
    report = evaluate_snapshot(fixture.vault, options=CLEARED, conversations=fixture.conversations)

    assert report["basis"] == {"coverage": "memory_access", "waste": "estimated_from_hits"}
    assert report["sessions"] == {"coverage": 2, "waste": 2, "hits": 5, "hits_dropped": 1}
    assert report["snapshot"] == {"visible": 6}

    core = _row(report, "core", 500)
    assert (core["items"], core["tokens"], core["overflow"]) == (3, 110, 0)
    assert (core["coverage"], core["covered_hits"], core["coverage_kind"]) == (0.4, 2, "direct")
    assert (core["waste"], core["unused_item_sessions"]) == (0.666667, 4)
    small = _row(report, "core", 25)
    assert (small["items"], small["tokens"], small["overflow"]) == (3, 25, 1)
    assert len("Always cite sources.") + len("1. Search 2. Read") + len(LONG) == 437

    topics = _row(report, "topics", 500)
    assert (topics["items"], topics["coverage"], topics["coverage_kind"]) == (2, 0.6, "pointer")
    assert (topics["waste"], topics["unused_item_sessions"]) == (0.25, 1)

    handoff = _row(report, "handoff", 500)
    assert handoff["degraded"] is None and handoff["items"] == 3
    assert (handoff["coverage"], handoff["covered_hits"]) == (0.2, 1)

    whole = _sweep(report, 1000, core=500, handoff=500, topics=500)
    assert whole["resident"] == ["core", "handoff", "topics"] and whole["demoted"] == []
    assert whole["coverage"] == 1.0 and whole["coverage_direct"] == 0.4
    assert whole["tokens"] == 110 + handoff["tokens"] + 5
    # 110 core tokens do not fit under 100: core and everything after it are demoted.
    tight = _sweep(report, 100, core=500, handoff=500, topics=500)
    assert tight["resident"] == [] and tight["demoted"] == ["core", "handoff", "topics"]
    assert (tight["tokens"], tight["coverage"], tight["waste"]) == (0, 0.0, None)
    fits = _sweep(report, 100, core=25, handoff=25, topics=25)
    assert fits["resident"] == ["core", "handoff", "topics"]
    assert fits["tokens"] <= 100


def test_usage_store_is_the_waste_basis(tmp_path: Path) -> None:
    fixture = known_vault(tmp_path)
    report = evaluate_snapshot(
        fixture.vault, options=CLEARED, conversations=fixture.conversations, usage=fixture.usage
    )

    assert report["basis"] == {"coverage": "memory_access", "waste": "memory_used"}
    assert report["sessions"]["waste"] == 2
    core = _row(report, "core", 500)
    assert (core["waste"], core["unused_item_sessions"]) == (0.666667, 4)
    topics = _row(report, "topics", 500)
    assert (topics["waste"], topics["unused_item_sessions"]) == (0.75, 3)
    assert report["settings"]["usage_store"] is True


def test_usage_signals_stand_in_for_searches_without_access_events(tmp_path: Path) -> None:
    fixture = known_vault(tmp_path, events=False)
    report = evaluate_snapshot(fixture.vault, options=CLEARED, usage=fixture.usage)

    assert report["basis"] == {"coverage": "memory_used", "waste": "memory_used"}
    # u1 = {a-1, m-a} (m-p withheld by the clearance), u2 = {p-1}.
    assert report["sessions"] == {"coverage": 2, "waste": 2, "hits": 3, "hits_dropped": 1}
    assert _row(report, "core", 500)["coverage"] == 0.666667


def test_handoff_without_a_store_is_unavailable(tmp_path: Path) -> None:
    fixture = known_vault(tmp_path)
    report = evaluate_snapshot(fixture.vault, options=CLEARED)

    handoff = _row(report, "handoff", 500)
    assert handoff["degraded"] == "unavailable" and handoff["coverage"] is None
    assert _row(report, "handoff", 25)["degraded"] == "unavailable"
    # Unavailable at every cap: listed, and not a sweep column (no repeated rows).
    assert report["settings"]["unavailable_slots"] == ["handoff"]
    assert all("handoff" not in row["slot_caps"] for row in report["sweep"])
    assert len(report["sweep"]) == 2 * 2 * 2
    row = _sweep(report, 1000, core=500, topics=500)
    assert row["resident"] == ["core", "topics"]


def _record_state_reads(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    from memfw.conversations.worker import WorkState

    paths: list[Path] = []
    real = WorkState.session_summary

    def recording(self: WorkState, app_name: str, session_id: str) -> str | None:
        paths.append(Path(self.path))
        return real(self, app_name, session_id)

    monkeypatch.setattr(WorkState, "session_summary", recording)
    return paths


def test_handoff_reads_no_worker_state_without_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reads = _record_state_reads(monkeypatch)
    fixture = known_vault(tmp_path)
    report = evaluate_snapshot(fixture.vault, options=CLEARED, conversations=fixture.conversations)
    assert _row(report, "handoff", 500)["items"] == 3
    assert reads == [] and report["settings"]["state_store"] is False


def test_an_explicit_state_file_is_read_from_a_private_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from memfw.conversations.worker import WorkState

    reads = _record_state_reads(monkeypatch)
    fixture = known_vault(tmp_path)
    state = tmp_path / "stores" / "state.sqlite3"
    with WorkState(state).open():
        pass
    before = _files(state.parent)
    report = evaluate_snapshot(
        fixture.vault, options=CLEARED, conversations=fixture.conversations, state=state
    )
    assert report["settings"]["state_store"] is True
    assert reads and all(path != state and path.name == "state.sqlite3" for path in reads)
    assert _files(state.parent) == before

    real = ce._evaluate_copy

    def touching(*args: Any, **kwargs: Any) -> dict[str, Any]:
        out = real(*args, **kwargs)
        state.touch()
        return out

    monkeypatch.setattr(ce, "_evaluate_copy", touching)
    with pytest.raises(SnapshotChanged):
        evaluate_snapshot(
            fixture.vault, options=CLEARED, conversations=fixture.conversations, state=state
        )


CURRENT = EvalOptions(
    clearance="public,shared",
    slots=("core", "current"),
    default_slot_caps=(25, 500),
    resident_caps=(1000,),
    repeats=1,
    environ={},
)


def test_current_is_unavailable_while_the_vault_has_it_off(tmp_path: Path) -> None:
    fixture = known_vault(tmp_path, current=True)
    report = evaluate_snapshot(fixture.vault, options=CURRENT)

    assert report["settings"]["unavailable_slots"] == ["current"]
    assert (report["settings"]["current_enabled"], report["settings"]["current_simulated"]) == (
        False,
        False,
    )
    rows = [row for row in report["slots"] if row["slot"] == "current"]
    assert len(rows) == 1 and rows[0]["available"] is False and rows[0]["coverage"] is None
    assert all("current" not in row["slot_caps"] for row in report["sweep"])
    assert len(report["sweep"]) == 2


@pytest.mark.parametrize("how", ["vault", "flag"])
def test_current_slot_hand_computed(tmp_path: Path, how: str) -> None:
    # c-1 (15 chars, 4 tokens) is current; c-old's TTL ran out from its created_at
    # although a mechanical write refreshed its updated_at. Hits: S1 = {m-a, a-1},
    # S2 = {m-c, m-b, p-2, c-1}: 6 in all. current covers c-1: 1/6; c-1 is
    # unused in S1: waste 1/2.
    fixture = known_vault(tmp_path, current=True, current_on=how == "vault")
    options = CURRENT if how == "vault" else replace(CURRENT, current=True)
    report = evaluate_snapshot(fixture.vault, options=options)

    settings = report["settings"]
    assert settings["unavailable_slots"] == []
    assert (settings["current_enabled"], settings["current_simulated"]) == (True, how == "flag")
    assert report["sessions"] == {"coverage": 2, "waste": 2, "hits": 6, "hits_dropped": 1}
    assert len(TICKET) == 15
    for cap in (25, 500):
        row = _row(report, "current", cap)
        assert row["available"] is True and row["degraded"] is None
        assert (row["items"], row["tokens"], row["overflow"]) == (1, 4, 0)
        assert (row["coverage"], row["covered_hits"], row["coverage_kind"]) == (
            0.166667,
            1,
            "direct",
        )
        assert (row["waste"], row["unused_item_sessions"]) == (0.5, 1)
    whole = _sweep(report, 1000, core=500, current=500)
    assert whole["resident"] == ["core", "current"]
    assert whole["tokens"] == 110 + 4
    assert whole["coverage"] == whole["coverage_direct"] == 0.5


def test_privacy_clearance_and_policy_apply_like_the_slots(tmp_path: Path) -> None:
    fixture = known_vault(tmp_path)
    open_options = EvalOptions(
        slots=("core", "topics"), default_slot_caps=(500,), repeats=1, environ={}
    )
    report = evaluate_snapshot(fixture.vault, options=open_options)
    # No clearance filters nothing: the private memory is a hit and in a topic.
    assert report["sessions"]["hits"] == 6 and report["sessions"]["hits_dropped"] == 0
    assert report["snapshot"]["visible"] == 7

    other = EvalOptions(
        tenant="beta", slots=("core",), default_slot_caps=(500,), repeats=1, environ={}
    )
    beta = evaluate_snapshot(fixture.vault, options=other)
    assert beta["snapshot"] == {"visible": 1}
    # Nothing tells tenant beta that seven more memories exist.
    assert "- memories:" not in render_markdown(beta)
    assert (
        beta["snapshot"]
        == evaluate_snapshot(known_vault(tmp_path / "bigger", current=True).vault, options=other)[
            "snapshot"
        ]
    )
    assert beta["sessions"]["hits"] == 0 and beta["sessions"]["hits_dropped"] == 6


def test_the_environment_supplies_the_visibility_like_the_mcp_server(tmp_path: Path) -> None:
    fixture = known_vault(tmp_path)
    options = EvalOptions(
        slots=("core",),
        default_slot_caps=(500,),
        repeats=1,
        environ={"MEMFW_MCP_CLEARANCE": "public,shared", "MEMFW_EMBEDDING_URL": "http://x"},
    )
    report = evaluate_snapshot(fixture.vault, options=options)
    assert report["settings"]["clearance"] == ["public", "shared"]
    assert report["snapshot"]["visible"] == 6


# --- read-only and refusals --------------------------------------------------------


def test_the_snapshot_and_stores_are_never_written(tmp_path: Path) -> None:
    fixture = known_vault(tmp_path)
    before = tree_digest(fixture.vault)
    stores = (fixture.conversations.read_bytes(), fixture.usage.read_bytes())
    listing = sorted(path.name for path in fixture.vault.iterdir())

    evaluate_snapshot(
        fixture.vault, options=CLEARED, conversations=fixture.conversations, usage=fixture.usage
    )
    # The synthetic basis builds a search index: in the copy, never in the snapshot.
    evaluate_snapshot(fixture.vault, options=EvalOptions(slots=("core",), repeats=1, environ={}))

    assert tree_digest(fixture.vault) == before
    assert sorted(path.name for path in fixture.vault.iterdir()) == listing
    assert (fixture.conversations.read_bytes(), fixture.usage.read_bytes()) == stores


def _files(directory: Path) -> dict[str, bytes]:
    return {path.name: path.read_bytes() for path in sorted(directory.iterdir())}


def test_wal_mode_stores_are_copied_with_their_sidecars_never_opened(tmp_path: Path) -> None:
    import shutil
    import sqlite3

    fixture = known_vault(tmp_path)
    # The conversation store in WAL mode with no sidecar (a clean close).
    with sqlite3.connect(fixture.conversations) as connection:
        assert connection.execute("PRAGMA journal_mode = WAL").fetchone()[0] == "wal"
    connection.close()
    # The usage store with its last write still in -wal (copied while open).
    wal_dir = tmp_path / "wal-store"
    wal_dir.mkdir()
    live = sqlite3.connect(fixture.usage, isolation_level=None)
    try:
        live.execute("PRAGMA journal_mode = WAL")
        live.execute("PRAGMA wal_autocheckpoint = 0")
        live.execute("DELETE FROM used WHERE session_ref = 'u2'")
        for suffix in ("", "-wal", "-shm"):
            shutil.copy2(f"{fixture.usage}{suffix}", wal_dir / f"usage.sqlite3{suffix}")
    finally:
        live.close()
    usage = wal_dir / "usage.sqlite3"
    stores = fixture.conversations.parent
    before = (_files(stores), _files(wal_dir))

    report = evaluate_snapshot(
        fixture.vault, options=CLEARED, conversations=fixture.conversations, usage=usage
    )

    assert (_files(stores), _files(wal_dir)) == before
    assert sorted(_files(wal_dir)) == ["usage.sqlite3", "usage.sqlite3-shm", "usage.sqlite3-wal"]
    # The copy carried the -wal: u2 is gone, one waste session is left.
    assert report["basis"]["waste"] == "memory_used" and report["sessions"]["waste"] == 1
    assert _row(report, "handoff", 500)["items"] == 3


def test_a_store_sidecar_that_changes_fails_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = known_vault(tmp_path)
    real = ce._evaluate_copy

    def touching(*args: Any, **kwargs: Any) -> dict[str, Any]:
        out = real(*args, **kwargs)
        Path(f"{fixture.usage}-wal").write_bytes(b"")
        return out

    monkeypatch.setattr(ce, "_evaluate_copy", touching)
    with pytest.raises(SnapshotChanged):
        evaluate_snapshot(fixture.vault, options=CLEARED, usage=fixture.usage)


def test_refuses_a_snapshot_that_needs_recovery(tmp_path: Path) -> None:
    fixture = known_vault(tmp_path)
    (fixture.vault / ".pending.json").write_text("{}", encoding="utf-8")
    with pytest.raises(SnapshotRefused, match="interrupted write"):
        evaluate_snapshot(fixture.vault, options=CLEARED)


@pytest.mark.parametrize("target", ["file", "dir", "dangling"])
def test_refuses_a_snapshot_with_a_symlink(tmp_path: Path, target: str) -> None:
    fixture = known_vault(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "x.md").write_text("x", encoding="utf-8")
    link = fixture.vault / "memories" / "link"
    pointee = {"file": outside / "x.md", "dir": outside, "dangling": outside / "gone"}[target]
    link.symlink_to(pointee)
    with pytest.raises(SnapshotRefused, match="symlink"):
        evaluate_snapshot(fixture.vault, options=CLEARED)


def test_refuses_outputs_and_stores_inside_the_snapshot(tmp_path: Path) -> None:
    fixture = known_vault(tmp_path)
    with pytest.raises(SnapshotRefused, match="output path is inside"):
        evaluate_snapshot(fixture.vault, options=CLEARED, outputs=[fixture.vault / "r.json"])
    inside = fixture.vault / "usage.sqlite3"
    inside.write_bytes(fixture.usage.read_bytes())
    with pytest.raises(SnapshotRefused, match="store path is inside"):
        evaluate_snapshot(fixture.vault, options=CLEARED, usage=inside)
    with pytest.raises(SnapshotRefused, match="not a vault"):
        evaluate_snapshot(tmp_path / "missing", options=CLEARED)


def test_a_snapshot_changed_during_the_run_gives_no_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = known_vault(tmp_path)
    real = ce._evaluate_copy

    def touching(*args: Any, **kwargs: Any) -> dict[str, Any]:
        out = real(*args, **kwargs)
        (fixture.vault / "memories" / "a-1.md").touch()
        return out

    monkeypatch.setattr(ce, "_evaluate_copy", touching)
    with pytest.raises(SnapshotChanged):
        evaluate_snapshot(fixture.vault, options=CLEARED)


# --- privacy and determinism ---------------------------------------------------------


def test_report_has_counts_and_timings_only(tmp_path: Path) -> None:
    fixture = known_vault(tmp_path)
    report = evaluate_snapshot(
        fixture.vault, options=CLEARED, conversations=fixture.conversations, usage=fixture.usage
    )
    text = json.dumps(report) + render_markdown(report)

    forbidden = [BODY, LONG[:20], "Always cite", "Port A", "Proc two", "Service port"]
    forbidden += ["s-alpha", "codex", "u1", "a-1", "m-c", "beta", str(tmp_path), "concept:"]
    for value in forbidden:
        assert value not in text, value


def test_same_snapshot_and_seed_give_the_same_report(tmp_path: Path) -> None:
    fixture = known_vault(tmp_path, events=False)
    options = EvalOptions(slots=("core", "topics"), seed=3, sessions=4, queries=3, environ={})
    first = evaluate_snapshot(fixture.vault, options=options)
    second = evaluate_snapshot(fixture.vault, options=options)

    assert first["basis"]["coverage"] == "synthetic"
    assert first["sessions"]["coverage"] == 4 and first["sessions"]["hits"] > 0
    assert json.dumps(strip_timings(first), sort_keys=True) == json.dumps(
        strip_timings(second), sort_keys=True
    )
    assert all("latency_ms" not in row for row in strip_timings(first)["slots"])


def test_synthetic_queries_depend_on_the_seed_only() -> None:
    from memfw.core.models import MemoryDocument

    now = datetime(2026, 1, 1, tzinfo=UTC)
    documents = [
        MemoryDocument(
            id=f"x-{index}",
            title=f"Title {index}",
            body="alpha beta gamma delta",
            created_at=now,
            updated_at=now,
        )
        for index in range(6)
    ]
    first = synthetic_queries(documents, seed=1, sessions=3, queries=2)
    assert first == synthetic_queries(list(reversed(documents)), seed=1, sessions=3, queries=2)
    assert [len(session) for session in first] == [2, 2, 2]
    assert synthetic_queries([], seed=1, sessions=3, queries=2) == []


def test_generated_vault_invariants(tmp_path: Path) -> None:
    root = random_vault(tmp_path)
    options = EvalOptions(
        slots=("core", "topics"),
        default_slot_caps=(50, 200, 2000),
        resident_caps=(100, 1000, 4000),
        repeats=1,
        environ={},
    )
    report = evaluate_snapshot(root, options=options)

    assert report["basis"]["coverage"] == "memory_access"
    assert len(report["sweep"]) == 3 * 3 * 3
    for row in report["sweep"]:
        assert row["tokens"] <= row["resident_cap"]
        for key in ("coverage", "coverage_direct", "waste"):
            assert row[key] is None or 0.0 <= row[key] <= 1.0
        if row["coverage"] is not None:
            assert row["coverage_direct"] <= row["coverage"]
    core = [row for row in report["slots"] if row["slot"] == "core"]
    assert [row["tokens"] for row in core] == sorted(row["tokens"] for row in core)
    assert strip_timings(report) == strip_timings(evaluate_snapshot(root, options=options))


# --- the pure metrics ----------------------------------------------------------------


def _build(name: str, tokens: int, *, usable: bool = True) -> SlotBuild:
    return SlotBuild(name, 1, True, None if usable else "timeout", (), True, tokens, 0, ())


def test_metric_functions() -> None:
    hits = [frozenset({"a", "b"}), frozenset({"b", "c", "d"})]
    assert coverage_counts(hits, frozenset({"b", "d"})) == (3, 5)
    items = [frozenset({"a"}), frozenset({"z"}), frozenset()]
    assert waste_counts(hits, items) == (5, 6)
    assert percentile([5.0, 1.0, 3.0, 2.0, 4.0], 50) == 3.0
    assert percentile([5.0, 1.0, 3.0, 2.0, 4.0], 95) == 5.0
    assert percentile([], 50) is None
    assert ce.ratio(1, 3) == 0.333333 and ce.ratio(1, 0) is None

    builds = [_build("core", 60), _build("handoff", 0, usable=False), _build("topics", 30)]
    assert resident_prefix(builds, 100) == (["core", "topics"], [])
    assert resident_prefix(builds, 80) == (["core"], ["topics"])
    assert resident_prefix([_build("a", 90), _build("b", 20), _build("c", 5)], 100) == (
        ["a"],
        ["b", "c"],
    )


def test_options_are_checked() -> None:
    for bad in (
        EvalOptions(policy="loose"),
        EvalOptions(slots=()),
        EvalOptions(slots=("core", "core")),
        EvalOptions(slots=("core",), slot_caps={"topics": (10,)}),
        EvalOptions(default_slot_caps=(0,)),
        EvalOptions(resident_caps=()),
        EvalOptions(repeats=0),
        EvalOptions(session_gap_minutes=0),
        EvalOptions(default_slot_caps=tuple(range(1, 30)), resident_caps=tuple(range(1, 30))),
    ):
        with pytest.raises(ValueError):
            bad.check()


def test_defaults_are_not_changed_by_the_evaluation(tmp_path: Path) -> None:
    timeouts = dict(CONTEXT_SLOT_TIMEOUTS)
    fixture = known_vault(tmp_path)
    evaluate_snapshot(fixture.vault, options=CLEARED)
    assert dict(CONTEXT_SLOT_TIMEOUTS) == timeouts
    assert ce.DEFAULT_LIMIT_TOKENS == 2000


def test_timed_builds_are_cold_never_served_from_the_c3_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from memfw.runtime import runtime as runtime_module

    def served(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("a timed core build was served from the C3 cache")

    monkeypatch.setattr(runtime_module, "cached_core_candidates", served)
    fixture = known_vault(tmp_path)
    report = evaluate_snapshot(fixture.vault, options=CLEARED)
    assert report["settings"]["latency"] == "cold"
    assert _row(report, "core", 500)["degraded"] is None
    assert _row(report, "core", 500)["latency_ms"]["n"] == CLEARED.repeats


def test_p95_over_the_slot_deadline_is_flagged_not_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(CONTEXT_SLOT_TIMEOUTS, "core", 1e-9)
    timeouts = dict(CONTEXT_SLOT_TIMEOUTS)
    fixture = known_vault(tmp_path)
    report = evaluate_snapshot(fixture.vault, options=CLEARED)

    core = _row(report, "core", 500)
    assert core["deadline_ms"] == 0.0 and core["over_deadline"] is True
    topics = _row(report, "topics", 500)
    assert topics["deadline_ms"] == 3000.0 and topics["over_deadline"] is False
    assert "p95 over deadline" in render_markdown(report)
    assert dict(CONTEXT_SLOT_TIMEOUTS) == timeouts
    assert all("over_deadline" not in row for row in strip_timings(report)["slots"])


# --- the command -------------------------------------------------------------------


def test_cli_writes_json_and_markdown(tmp_path: Path) -> None:
    fixture = known_vault(tmp_path)
    out = tmp_path / "report"
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "eval",
            "context",
            str(fixture.vault),
            "--conversations",
            str(fixture.conversations),
            "--usage",
            str(fixture.usage),
            "--clearance",
            "public,shared",
            "--app",
            "claude",
            "--slots",
            "core,handoff,topics",
            "--slot-caps",
            "25,500",
            "--slot-cap",
            "topics=500",
            "--resident-caps",
            "100,1000",
            "--repeats",
            "1",
            "--out",
            str(out),
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    printed = json.loads(result.output)
    assert json.loads((out / "context-eval.json").read_text(encoding="utf-8")) == printed
    assert (out / "context-eval.md").read_text(encoding="utf-8").startswith("# C4 context pack")
    assert {row["slot_caps"]["topics"] for row in printed["sweep"]} == {500}
    assert len(printed["sweep"]) == 2 * 2 * 1 * 2

    refused = runner.invoke(
        app, ["eval", "context", str(fixture.vault), "--out", str(fixture.vault / "r")]
    )
    assert refused.exit_code != 0
    assert not (fixture.vault / "r").exists()


def test_cli_current_flag_simulates_the_slot(tmp_path: Path) -> None:
    fixture = known_vault(tmp_path, current=True)
    base = ["eval", "context", str(fixture.vault), "--clearance", "public,shared"]
    base += ["--slots", "core,current", "--slot-caps", "500", "--resident-caps", "1000"]
    base += ["--repeats", "1", "--json"]
    runner = CliRunner()
    off = json.loads(runner.invoke(app, base).output)
    on = json.loads(runner.invoke(app, [*base, "--current"]).output)
    assert off["settings"]["unavailable_slots"] == ["current"]
    assert on["settings"]["unavailable_slots"] == []
    assert on["settings"]["current_simulated"] is True
    assert _row(on, "current", 500)["items"] == 1
