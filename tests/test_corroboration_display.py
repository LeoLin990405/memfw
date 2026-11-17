"""U5: corroboration shown on recall rows (labels=true) and the review-queue order."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from memfw.cli import app as cli_app
from memfw.conversations.policy import SharePolicy
from memfw.core.epistemic import OPEN_QUESTION_TYPE
from memfw.core.models import Tier
from memfw.core.repository import Repository
from memfw.corroboration import projection_path, rebuild
from memfw.index import MemoryIndex
from memfw.mcp.server import Gateway
from memfw.notes import HashEmbeddingProvider
from memfw.recall.priority import (
    CORROBORATED_MIN_APPS,
    QuestionEntry,
    Visible,
    order_questions,
)
from memfw.runtime import build_runtime

T0 = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Out-of-vault projections live under the home directory: give every test its own."""

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    return home


PORT = "concept:service-port"
SHARE: dict[str, Any] = {
    "conversation_share": {
        "enabled": True,
        "apps": {
            "cursor": {"readable": False},
            "grok": {"can_read": False},
            "kimi": {"can_read": ["claude"]},
        },
    },
}
#: Evidence of the corroborated memory: five apps, one hidden (cursor).
EVIDENCE_A = [
    "conv:claude:s1@1",
    "conv:claude:s1@7",  # same app, same session: counts once
    "conv:codex:s2@3",
    "conv:grok:s3@1",
    "conv:kimi:s4@2",
    "conv:cursor:s5@9",
]
MODES = ("fts", "hybrid", "vector", "multihop", "two_stage")
COUNT_KEYS = {"count", "apps", "sessions", "first_seen", "last_seen"}


def _clock() -> Iterator[datetime]:
    moment = T0
    while True:
        yield moment
        moment += timedelta(minutes=1)


def _vault(tmp_path: Path, *, build: bool = True) -> Repository:
    ticks = _clock()
    repository = Repository(tmp_path / "vault", clock=lambda: next(ticks))
    repository.init()
    config = repository.read_config()
    config.update(SHARE)
    repository.configure(config)
    add = repository.add
    add(
        "Port A",
        "the service port is 8080",
        memory_id="m-port-a",
        about=[PORT],
        evidence=EVIDENCE_A,
    )
    add(
        "Port B",
        "the service port is 9090",
        memory_id="m-port-b",
        about=[PORT],
        evidence=["conv:claude:s9@1"],
    )
    add("Port legacy", "legacy service port notes", memory_id="m-legacy")
    add("Unrelated", "service backup notes", memory_id="m-other", about=["concept:backup"])
    MemoryIndex(repository.index_path).rebuild(repository)
    if build:
        rebuild(repository, SharePolicy.from_config(repository.read_config()))
    return repository


def _question(repository: Repository, memory_id: str, *conflicts: str) -> None:
    repository.add(
        "Service port unknown",
        "Two memories disagree on which service port is used.",
        memory_id=memory_id,
        memory_type=OPEN_QUESTION_TYPE,
        about=[PORT],
        open_question={"question": "Which service port is used?", "conflicts": list(conflicts)},
    )


def _gateway(root: Path, app: str | None = "claude", **kwargs: Any) -> Gateway:
    kwargs.setdefault("tenant", "")
    kwargs.setdefault("entity_enabled", False)
    kwargs.setdefault("allowed_sources", ())
    return Gateway(root, app=app, **kwargs)


def _by_id(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {row["id"]: row for row in payload["results"]}


def _search(gateway: Gateway, mode: str, **kwargs: Any) -> dict[str, Any]:
    return gateway.search("service port", limit=20, mode=mode, **kwargs)


# --- display -------------------------------------------------------------------


@pytest.mark.parametrize("mode", MODES)
def test_field_only_with_labels(tmp_path: Path, mode: str) -> None:
    repository = _vault(tmp_path)
    gateway = _gateway(repository.root, embedder=HashEmbeddingProvider(dimension=64))
    plain = _search(gateway, mode)
    assert plain["results"]
    assert all("corroboration" not in row for row in plain["results"])
    labelled = _search(gateway, mode, labels=True)
    assert labelled["results"]
    for row in labelled["results"]:
        assert set(row["corroboration"]) == COUNT_KEYS, row["id"]
    rows = _by_id(labelled)
    if "m-port-a" in rows:
        # claude may read every readable app; cursor is hidden and never counted.
        assert rows["m-port-a"]["corroboration"]["apps"] == ["claude", "codex", "grok", "kimi"]
        assert rows["m-port-a"]["corroboration"]["count"] == 4


def test_default_output_is_unchanged(tmp_path: Path) -> None:
    """Without labels the projection and the caller app change no byte."""

    with_projection = _vault(tmp_path / "a")
    without_projection = _vault(tmp_path / "b", build=False)
    for mode in MODES:
        first = _search(_gateway(with_projection.root, app="claude"), mode)
        second = _search(_gateway(without_projection.root, app=None), mode)
        text = json.dumps(first, sort_keys=True).replace(str(with_projection.root), "<v>")
        other = json.dumps(second, sort_keys=True).replace(str(without_projection.root), "<v>")
        if mode == "two_stage":  # timings differ per call
            first.pop("recall_metrics"), second.pop("recall_metrics")
            text = json.dumps(first, sort_keys=True).replace(str(with_projection.root), "<v>")
            other = json.dumps(second, sort_keys=True).replace(str(without_projection.root), "<v>")
        assert text == other, mode
        assert "corroboration" not in text


@pytest.mark.parametrize("mode", ["fts", "two_stage"])
def test_can_read_trimming_never_leaks(tmp_path: Path, mode: str) -> None:
    repository = _vault(tmp_path)
    grok = _by_id(_search(_gateway(repository.root, app="grok"), mode, labels=True))
    assert grok["m-port-a"]["corroboration"] == {
        "count": 1,
        "apps": ["grok"],
        "sessions": ["grok:s3"],
        "first_seen": grok["m-port-a"]["corroboration"]["first_seen"],
        "last_seen": grok["m-port-a"]["corroboration"]["last_seen"],
    }
    # Grok's own share is empty for a memory only claude mentioned.
    assert grok["m-port-b"]["corroboration"]["count"] == 0
    assert grok["m-port-b"]["corroboration"]["apps"] == []
    kimi = _by_id(_search(_gateway(repository.root, app="kimi"), mode, labels=True))
    assert kimi["m-port-a"]["corroboration"]["apps"] == ["claude", "kimi"]
    assert kimi["m-port-a"]["corroboration"]["count"] == 2
    # Zero leak: no other app's name or session appears anywhere in the payloads.
    for payload, hidden in ((grok, ("claude", "codex", "kimi")), (kimi, ("codex", "grok"))):
        text = json.dumps([row["corroboration"] for row in payload.values()])
        for name in (*hidden, "cursor"):
            assert name not in text, name
    claude = _by_id(_search(_gateway(repository.root, app="claude"), mode, labels=True))
    assert "cursor" not in json.dumps([row["corroboration"] for row in claude.values()])


def test_two_stage_and_fts_agree(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    gateway = _gateway(repository.root, app="kimi")
    fts = _by_id(_search(gateway, "fts", labels=True))
    staged = _by_id(_search(gateway, "two_stage", labels=True))
    for memory_id in set(fts) & set(staged):
        assert fts[memory_id]["corroboration"] == staged[memory_id]["corroboration"]


@pytest.mark.parametrize("mode", ["fts", "two_stage"])
def test_missing_projection_degrades(tmp_path: Path, mode: str) -> None:
    repository = _vault(tmp_path, build=False)
    payload = _search(_gateway(repository.root), mode, labels=True)
    assert payload["results"]
    assert payload["degraded"] is False  # the search itself is fine
    for row in payload["results"]:
        assert row["corroboration"] == {"degraded": "projection_missing"}


def test_unreadable_projection_degrades(tmp_path: Path) -> None:
    repository = _vault(tmp_path, build=False)
    path = projection_path(repository.root)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"not a sqlite file")
    for mode in ("fts", "two_stage"):
        for row in _search(_gateway(repository.root), mode, labels=True)["results"]:
            assert row["corroboration"] == {"degraded": "projection_unreadable"}


@pytest.mark.parametrize("app", [None, "operator", "  "])
def test_no_caller_app_shows_nothing(tmp_path: Path, app: str | None) -> None:
    repository = _vault(tmp_path)
    for row in _search(_gateway(repository.root, app=app), "fts", labels=True)["results"]:
        assert row["corroboration"] == {"degraded": "caller_app_required"}


def test_invalid_share_policy_degrades(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from memfw.runtime import corroboration as wiring

    class Refusing:
        @staticmethod
        def from_config(_config: Any) -> Any:
            raise ValueError("bad share policy")

    repository = _vault(tmp_path)
    monkeypatch.setattr(wiring, "SharePolicy", Refusing)
    for row in _search(_gateway(repository.root), "fts", labels=True)["results"]:
        assert row["corroboration"] == {"degraded": "share_policy_invalid"}


def test_display_never_writes(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    memories = sorted(
        (path.name, path.read_bytes()) for path in (repository.root / "memories").iterdir()
    )
    projection = projection_path(repository.root).read_bytes()
    # The search's own access events are unchanged by U5; only the files are checked.
    for mode in ("fts", "two_stage"):
        _search(_gateway(repository.root), mode, labels=True)
    assert projection_path(repository.root).read_bytes() == projection
    assert (
        sorted((path.name, path.read_bytes()) for path in (repository.root / "memories").iterdir())
        == memories
    )


# --- review order: the pure rule --------------------------------------------------


def test_rule_orders_corroborated_conflicts_first() -> None:
    entries = [
        QuestionEntry("q-old", ("m-plain",), "2026-01-01T00:00:00+00:00"),
        QuestionEntry("q-one", ("m-two-apps",), "2026-03-01T00:00:00+00:00"),
        QuestionEntry("q-both", ("m-two-apps", "m-three-apps"), "2026-04-01T00:00:00+00:00"),
        QuestionEntry("q-more", ("m-three-apps",), "2026-03-02T00:00:00+00:00"),
        QuestionEntry("q-tie-b", (), "2026-02-01T00:00:00+00:00"),
        QuestionEntry("q-tie-a", (), "2026-02-01T00:00:00+00:00"),
    ]
    visible = {
        "m-plain": Visible(count=5, apps=1),  # many sessions, one app: not corroborated
        "m-two-apps": Visible(count=2, apps=CORROBORATED_MIN_APPS),
        "m-three-apps": Visible(count=4, apps=3),
    }
    ranked = order_questions(entries, visible)
    assert [item.question_id for item in ranked] == [
        "q-both",  # two corroborated conflicts
        "q-more",  # one, support 4
        "q-one",  # one, support 2
        "q-old",  # none: oldest first
        "q-tie-a",  # same instant: by id
        "q-tie-b",
    ]
    assert ranked[0].as_dict() == {
        "corroborated_conflicts": ["m-three-apps", "m-two-apps"],
        "support": 6,
    }
    assert ranked[3].as_dict() == {"corroborated_conflicts": [], "support": 0}


def test_rule_without_corroboration_is_oldest_first() -> None:
    entries = [
        QuestionEntry("q-b", ("m-1",), "2026-02-01T00:00:00+00:00"),
        QuestionEntry("q-a", ("m-2",), "2026-03-01T00:00:00+00:00"),
        QuestionEntry("q-c", ("m-1", "m-1"), "2026-01-01T00:00:00+00:00"),
    ]
    assert [item.question_id for item in order_questions(entries, {})] == ["q-c", "q-b", "q-a"]
    # Deterministic: the input order does not matter.
    assert order_questions(entries, {}) == order_questions(list(reversed(entries)), {})


# --- review order: the runtime queue and the CLI ---------------------------------


def _queue_vault(tmp_path: Path, *, build: bool = True) -> Repository:
    repository = _vault(tmp_path, build=False)
    _question(repository, "q-legacy", "m-legacy")  # oldest, not corroborated
    _question(repository, "q-port", "m-port-a", "m-port-b")  # m-port-a: corroborated
    MemoryIndex(repository.index_path).rebuild(repository)
    if build:
        rebuild(repository, SharePolicy.from_config(repository.read_config()))
    return repository


def _queue(repository: Repository, app: str, **kwargs: Any) -> dict[str, Any]:
    runtime = build_runtime(repository.root)
    return runtime.open_question_queue(repository, app=app, **kwargs)


def test_queue_puts_corroborated_conflicts_first(tmp_path: Path) -> None:
    repository = _queue_vault(tmp_path)
    payload = _queue(repository, "operator", allow_operator=True)
    assert [item["id"] for item in payload["questions"]] == ["q-port", "q-legacy"]
    top = payload["questions"][0]
    assert top["priority"] == {"corroborated_conflicts": ["m-port-a"], "support": 4}
    assert top["question"] == "Which service port is used?"
    assert payload["questions"][1]["priority"] == {"corroborated_conflicts": [], "support": 0}
    assert payload["corroboration"] == {"degraded": None}
    assert "corroborated" in payload["rule"]


def test_queue_is_trimmed_to_the_caller(tmp_path: Path) -> None:
    repository = _queue_vault(tmp_path)
    # Grok sees only its own share (one app): nothing is corroborated for it,
    # so hidden apps cannot move a question up: plain oldest-first order.
    grok = _queue(repository, "grok")
    assert [item["id"] for item in grok["questions"]] == ["q-legacy", "q-port"]
    assert all(item["priority"]["support"] == 0 for item in grok["questions"])
    # Kimi reads claude: two apps, so m-port-a counts, with only that support.
    kimi = _queue(repository, "kimi")
    assert kimi["questions"][0]["priority"] == {
        "corroborated_conflicts": ["m-port-a"],
        "support": 2,
    }


def test_queue_operator_only_from_the_cli(tmp_path: Path) -> None:
    repository = _queue_vault(tmp_path)
    payload = _queue(repository, "operator")
    assert payload["corroboration"] == {"degraded": "caller_app_required"}
    assert [item["id"] for item in payload["questions"]] == ["q-legacy", "q-port"]


def test_queue_without_projection_keeps_plain_order(tmp_path: Path) -> None:
    repository = _queue_vault(tmp_path, build=False)
    payload = _queue(repository, "operator", allow_operator=True)
    assert payload["corroboration"] == {"degraded": "projection_missing"}
    assert [item["id"] for item in payload["questions"]] == ["q-legacy", "q-port"]


def test_queue_skips_resolved_and_other_tenant_questions(tmp_path: Path) -> None:
    repository = _queue_vault(tmp_path, build=False)
    repository.add(
        "Other tenant question",
        "elsewhere",
        memory_id="q-elsewhere",
        memory_type=OPEN_QUESTION_TYPE,
        tenant="alpha",
        open_question={"question": "Elsewhere?", "conflicts": []},
    )
    MemoryIndex(repository.index_path).rebuild(repository)
    ids = [item["id"] for item in _queue(repository, "claude")["questions"]]
    assert ids == ["q-legacy", "q-port"]
    alpha = build_runtime(repository.root, tenant="alpha")
    payload = alpha.open_question_queue(repository, app="claude")
    assert [item["id"] for item in payload["questions"]] == ["q-elsewhere"]


def test_cli_queue(tmp_path: Path) -> None:
    repository = _queue_vault(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli_app, ["corroboration", "queue", "--root", str(repository.root)])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert [item["id"] for item in payload["questions"]] == ["q-port", "q-legacy"]
    grok = runner.invoke(
        cli_app, ["corroboration", "queue", "--as-app", "grok", "--root", str(repository.root)]
    )
    assert grok.exit_code == 0, grok.output
    assert [item["id"] for item in json.loads(grok.output)["questions"]] == ["q-legacy", "q-port"]


# --- review order: fail closed (strict policy by default) ------------------------


def _hidden_question(repository: Repository, memory_id: str, **kwargs: Any) -> None:
    repository.add(
        "Secret question title",
        "Secret question body.",
        memory_id=memory_id,
        memory_type=OPEN_QUESTION_TYPE,
        about=[PORT],
        open_question={
            "question": "Secret question text?",
            "conflicts": ["m-port-a"],
            "missing": "secret missing text",
        },
        **kwargs,
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"tags": ["privacy:local-private"]},
        {"tags": ["sensitivity:private"]},
        {"tags": ["privacy:secret"]},  # unknown privacy: dropped
        {"tags": ["lifecycle:rejected"]},
        {"tags": ["lifecycle:archived"]},
        {"tier": Tier.COLD},  # archived to cold storage
    ],
)
def test_queue_never_lists_private_rejected_or_archived(
    tmp_path: Path, kwargs: dict[str, Any]
) -> None:
    repository = _queue_vault(tmp_path)
    _hidden_question(repository, "q-hidden", **kwargs)
    MemoryIndex(repository.index_path).rebuild(repository)
    for app in ("grok", "claude"):
        payload = _queue(repository, app)
        assert sorted(item["id"] for item in payload["questions"]) == ["q-legacy", "q-port"]
        assert "secret" not in json.dumps(payload).lower(), app
    runner = CliRunner()
    result = runner.invoke(cli_app, ["corroboration", "queue", "--root", str(repository.root)])
    assert result.exit_code == 0, result.output
    assert "q-hidden" not in result.output
    # compat_fts is the explicit, wider opt-in (the v1 fts filters only).
    wide = runner.invoke(
        cli_app,
        ["corroboration", "queue", "--policy", "compat_fts", "--root", str(repository.root)],
    )
    assert wide.exit_code == 0, wide.output
    if "tags" in kwargs:
        assert "q-hidden" in wide.output


def test_private_conflict_adds_no_support(tmp_path: Path) -> None:
    repository = _vault(tmp_path, build=False)
    repository.add(
        "Port private",
        "the service port is 7070",
        memory_id="m-port-private",
        about=[PORT],
        tags=["privacy:local-private"],
        evidence=EVIDENCE_A,
    )
    _question(repository, "q-legacy", "m-legacy")
    _question(repository, "q-private", "m-port-private")
    MemoryIndex(repository.index_path).rebuild(repository)
    rebuild(repository, SharePolicy.from_config(repository.read_config()))
    payload = _queue(repository, "operator", allow_operator=True)
    assert [item["id"] for item in payload["questions"]] == ["q-legacy", "q-private"]
    assert all(item["priority"]["support"] == 0 for item in payload["questions"])
    wide = _queue(repository, "operator", allow_operator=True, policy_name="compat_fts")
    assert wide["questions"][0]["id"] == "q-private"  # only the wider policy counts it


def test_queue_unknown_policy_fails(tmp_path: Path) -> None:
    repository = _queue_vault(tmp_path)
    with pytest.raises(ValueError, match="unknown policy"):
        _queue(repository, "claude", policy_name="bogus")
    result = CliRunner().invoke(
        cli_app, ["corroboration", "queue", "--policy", "bogus", "--root", str(repository.root)]
    )
    assert result.exit_code == 2
