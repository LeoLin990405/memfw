"""U2: recall labels ``open_question:<id>`` and ``conflicted`` ("show unknown")."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from memfw.core.epistemic import OPEN_QUESTION_TYPE
from memfw.core.repository import Repository
from memfw.index import MemoryIndex
from memfw.mcp.server import TOOLS, Gateway
from memfw.notes import HashEmbeddingProvider
from memfw.recall import (
    CONFLICTED_LABEL,
    FilterPolicy,
    OpenQuestionIndex,
    open_question_labels,
)
from memfw.recall.builtin import open_question_index, open_question_labels_for

PORT = "concept:service-port"
QUESTION = "q-port"
OPEN = f"open_question:{QUESTION}"


def _base(tmp_path: Path) -> Repository:
    repository = Repository(tmp_path / "vault")
    repository.init()
    add = repository.add
    add("Port A", "the service port is 8080", memory_id="m-port-a", about=[PORT])
    add("Port B", "the service port is 9090", memory_id="m-port-b", about=[PORT])
    add("Port runbook", "service port runbook notes", memory_id="m-runbook", about=[PORT])
    add("Port legacy", "legacy service port notes", memory_id="m-legacy")
    add("Unrelated", "service backup notes", memory_id="m-other", about=["concept:backup"])
    return repository


def _question(repository: Repository, *conflicts: str, **kwargs: Any) -> Any:
    kwargs.setdefault("memory_id", QUESTION)
    kwargs.setdefault("about", [PORT])
    return repository.add(
        "Service port unknown",
        "Two memories disagree on which service port is used.",
        memory_type=OPEN_QUESTION_TYPE,
        open_question={"question": "Which service port is used?", "conflicts": list(conflicts)},
        **kwargs,
    )


def _indexed(repository: Repository) -> Repository:
    MemoryIndex(repository.index_path).rebuild(repository)
    return repository


def _gateway(root: Path, **kwargs: Any) -> Gateway:
    kwargs.setdefault("tenant", "")
    kwargs.setdefault("entity_enabled", False)
    kwargs.setdefault("allowed_sources", ())
    return Gateway(root, **kwargs)


def _labels(payload: dict[str, Any]) -> dict[str, list[str]]:
    return {row["id"]: row["recall_labels"] for row in payload["results"]}


def _open_labels(payload: dict[str, Any]) -> dict[str, list[str]]:
    """Only the U2 labels of each row (two_stage rows carry policy labels too)."""

    return {
        row["id"]: [
            label
            for label in row["recall_labels"]
            if label.startswith("open_question:") or label == CONFLICTED_LABEL
        ]
        for row in payload["results"]
    }


# --- the pure function ------------------------------------------------------------


def test_pure_labels_order_self_and_empty_index() -> None:
    index = OpenQuestionIndex.build(
        [("q-b", [PORT], ["m-1"]), ("q-a", [PORT, "concept:x"], []), ("q-c", ["concept:y"], [])]
    )
    assert open_question_labels("m-1", [PORT], index) == (
        "open_question:q-a",
        "open_question:q-b",
        CONFLICTED_LABEL,
    )
    assert open_question_labels("m-2", ["concept:x"], index) == ("open_question:q-a",)
    assert open_question_labels("q-a", [PORT], index) == ("open_question:q-b",)  # never itself
    assert open_question_labels("m-3", ["concept:z"], index) == ()
    assert not OpenQuestionIndex()
    assert open_question_labels("m-1", [PORT], OpenQuestionIndex()) == ()


# --- two_stage -------------------------------------------------------------------


def test_two_stage_labels_about_match_and_conflicted(tmp_path: Path) -> None:
    repository = _base(tmp_path)
    _question(repository, "m-port-a", "m-port-b", "m-legacy")
    _indexed(repository)
    payload = _gateway(repository.root).search("service port", limit=20, mode="two_stage")
    labels = _open_labels(payload)
    assert labels["m-port-a"] == [OPEN, CONFLICTED_LABEL]
    assert labels["m-port-b"] == [OPEN, CONFLICTED_LABEL]
    assert labels["m-runbook"] == [OPEN]  # about-match only
    assert labels["m-legacy"] == [CONFLICTED_LABEL]  # no about, listed in conflicts
    assert labels["m-other"] == []
    # The question itself is a hit like any memory, typed so an agent can see it.
    rows = {row["id"]: row for row in payload["results"]}
    assert rows[QUESTION]["type"] == OPEN_QUESTION_TYPE
    assert labels[QUESTION] == []


def test_two_stage_appends_after_policy_labels(tmp_path: Path) -> None:
    repository = _base(tmp_path)
    repository.add(
        "Port anchor", "service port anchor", memory_id="m-anchor", about=[PORT], layer="anchor"
    )
    _question(repository, "m-anchor")
    _indexed(repository)
    payload = _gateway(repository.root).search("service port anchor", limit=20, mode="two_stage")
    assert _labels(payload)["m-anchor"] == ["anchor", OPEN, CONFLICTED_LABEL]


def test_resolved_question_gives_no_label(tmp_path: Path) -> None:
    repository = _base(tmp_path)
    _question(repository, "m-port-a", "m-port-b")
    repository.resolve_open_question(QUESTION, "m-port-a")
    _indexed(repository)
    gateway = _gateway(repository.root)
    for policy in ("compat_fts", "strict"):
        payload = gateway.search("service port", limit=20, mode="two_stage", policy=policy)
        assert all(not labels for labels in _open_labels(payload).values())
    payload = gateway.search("service port", limit=20, mode="fts", labels=True)
    assert all(not labels for labels in _labels(payload).values())


def test_other_tenant_question_gives_no_label_and_no_id(tmp_path: Path) -> None:
    repository = _base(tmp_path)
    repository.add("Beta port", "beta service port", memory_id="m-beta", tenant="beta")
    _question(repository, "m-beta", memory_id="q-beta", tenant="beta")
    _indexed(repository)
    gateway = _gateway(repository.root)
    for kwargs in (
        {"mode": "two_stage"},
        {"mode": "two_stage", "policy": "strict"},
        {"mode": "fts", "labels": True},
    ):
        payload = gateway.search("service port", limit=20, **kwargs)
        assert payload["results"]
        assert all(not labels for labels in _open_labels(payload).values())
        assert "q-beta" not in json.dumps(payload)
    # The beta caller does see it on its own hits.
    beta = _gateway(repository.root, tenant="beta").search("beta service port", mode="two_stage")
    assert _open_labels(beta)["m-beta"] == [CONFLICTED_LABEL]


def test_strict_dropped_private_question_leaks_nothing(tmp_path: Path) -> None:
    repository = _base(tmp_path)
    _question(repository, "m-port-a", tags=["sensitivity:private"])
    _indexed(repository)
    gateway = _gateway(repository.root)
    strict = gateway.search("service port", limit=20, mode="two_stage", policy="strict")
    assert strict["results"]
    assert all(not labels for labels in _open_labels(strict).values())
    assert QUESTION not in json.dumps(strict)
    # compat_fts has no privacy rule: the question is a visible hit there, and labels.
    compat = gateway.search("service port", limit=20, mode="two_stage")
    assert QUESTION in _labels(compat)
    assert _open_labels(compat)["m-port-a"] == [OPEN, CONFLICTED_LABEL]


def test_source_filter_applies_to_the_question(tmp_path: Path) -> None:
    repository = _base(tmp_path)
    _question(repository, "m-port-a", source="ops")
    _indexed(repository)
    gateway = _gateway(repository.root, allowed_sources=("manual",))
    payload = gateway.search("service port", limit=20, mode="two_stage")
    assert all(not labels for labels in _open_labels(payload).values())
    assert QUESTION not in json.dumps(payload)


def test_vault_without_open_questions_keeps_two_stage_labels(tmp_path: Path) -> None:
    repository = _indexed(_base(tmp_path))
    payload = _gateway(repository.root).search("service port", limit=20, mode="two_stage")
    assert payload["results"]
    assert all(row["recall_labels"] == [] for row in payload["results"])


# --- the other modes ------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["fts", "hybrid"])
def test_labels_true_on_other_modes(tmp_path: Path, mode: str) -> None:
    repository = _base(tmp_path)
    _question(repository, "m-port-a", "m-legacy")
    _indexed(repository)
    gateway = _gateway(repository.root, embedder=HashEmbeddingProvider(dimension=64))
    payload = gateway.search("service port", limit=20, mode=mode, labels=True)
    labels = _labels(payload)
    assert labels["m-port-a"] == [OPEN, CONFLICTED_LABEL]
    assert labels["m-port-b"] == [OPEN]
    assert labels["m-legacy"] == [CONFLICTED_LABEL]
    assert all(isinstance(row["recall_labels"], list) for row in payload["results"])
    rows = {row["id"]: row for row in payload["results"]}
    assert rows[QUESTION]["type"] == OPEN_QUESTION_TYPE


@pytest.mark.parametrize("mode", ["fts", "hybrid", "vector", "multihop"])
def test_default_output_has_no_labels(tmp_path: Path, mode: str) -> None:
    repository = _base(tmp_path)
    _question(repository, "m-port-a")
    _indexed(repository)
    gateway = _gateway(repository.root, embedder=HashEmbeddingProvider(dimension=64))
    default = gateway.search("service port", limit=20, mode=mode)
    explicit = gateway.search("service port", limit=20, mode=mode, labels=False)
    assert default == explicit
    assert all("recall_labels" not in row for row in default["results"])


def test_mcp_parses_labels_and_rejects_non_boolean(tmp_path: Path) -> None:
    repository = _base(tmp_path)
    _question(repository, "m-port-a")
    _indexed(repository)
    gateway = _gateway(repository.root)
    sent: list[dict[str, Any]] = []
    gateway.respond = sent.append  # type: ignore[method-assign]
    for msg_id, arguments in enumerate(
        [
            {"query": "service port", "mode": "fts", "labels": True},
            {"query": "service port", "mode": "fts", "labels": "yes"},
        ],
        start=1,
    ):
        gateway.handle(
            {
                "jsonrpc": "2.0",
                "id": msg_id,
                "method": "tools/call",
                "params": {"name": "memory_search", "arguments": arguments},
            }
        )
    first = json.loads(sent[0]["result"]["content"][0]["text"])
    assert _labels(first)["m-port-a"] == [OPEN, CONFLICTED_LABEL]
    assert "labels must be a boolean" in json.dumps(sent[1])


def test_tool_description_tells_agents_to_say_unknown() -> None:
    tool = next(item for item in TOOLS if item["name"] == "memory_search")
    assert "open-question" in tool["description"]
    assert "open_question:<id>" in tool["description"]
    assert "never guess" in tool["description"]
    assert tool["inputSchema"]["properties"]["labels"]["default"] is False


# --- builder and Runtime ------------------------------------------------------------


def test_builder_keeps_only_visible_unresolved_questions(tmp_path: Path) -> None:
    repository = _base(tmp_path)
    _question(repository, "m-port-a")
    _question(repository, "m-port-b", memory_id="q-private", tags=["sensitivity:private"])
    _question(repository, "m-port-b", memory_id="q-done")
    repository.resolve_open_question("q-done", "m-runbook")
    strict = FilterPolicy(tenant="")
    index = open_question_index(repository, strict, now=datetime.now(UTC))
    assert dict(index.by_concept) == {PORT: (QUESTION,)}
    assert index.conflicted == frozenset({"m-port-a"})
    compat = FilterPolicy.compat_fts(tenant="")
    index = open_question_index(repository, compat, now=datetime.now(UTC))
    assert dict(index.by_concept) == {PORT: ("q-port", "q-private")}
    found = open_question_labels_for(
        repository, ["m-port-b", "m-other", "missing", "../x"], strict, now=datetime.now(UTC)
    )
    assert found == {"m-port-b": (OPEN,)}


def test_runtime_open_question_labels_fails_closed_on_unknown_policy(tmp_path: Path) -> None:
    repository = _base(tmp_path)
    runtime = _gateway(repository.root).runtime
    with pytest.raises(ValueError, match="unknown policy"):
        runtime.open_question_labels(
            repository,
            ["m-port-a"],
            policy_name="open",
            source="",
            include_history=False,
            as_of=None,
        )


def test_as_of_before_question_gives_no_label(tmp_path: Path) -> None:
    repository = _base(tmp_path)
    _question(repository, "m-port-a")
    _indexed(repository)
    runtime = _gateway(repository.root).runtime
    found = runtime.open_question_labels(
        repository,
        ["m-port-a"],
        policy_name="compat_fts",
        source="",
        include_history=False,
        as_of=datetime(2000, 1, 1, tzinfo=UTC),
    )
    assert found == {}


def test_scope_changes_no_label(tmp_path: Path) -> None:
    repository = _base(tmp_path)
    _question(repository, "m-port-a")
    _indexed(repository)
    gateway = _gateway(repository.root)
    plain = gateway.search("service port", limit=20, mode="fts", labels=True)
    scoped = gateway.search("service port", limit=20, mode="fts", labels=True, scope="team")
    assert _labels(scoped) == _labels(plain)
    assert _labels(scoped)["m-port-a"] == [OPEN, CONFLICTED_LABEL]


def test_question_with_valid_to_is_hidden_even_for_an_earlier_as_of(tmp_path: Path) -> None:
    repository = _base(tmp_path)
    _question(repository, "m-port-a")
    repository.resolve_open_question(QUESTION, "m-port-b")
    _indexed(repository)
    runtime = _gateway(repository.root).runtime
    found = runtime.open_question_labels(
        repository,
        ["m-port-a", "m-runbook"],
        policy_name="compat_fts",
        source="",
        include_history=False,
        as_of=datetime(2099, 1, 1, tzinfo=UTC),
    )
    assert found == {}
    earlier = runtime.open_question_labels(
        repository,
        ["m-port-a"],
        policy_name="compat_fts",
        source="",
        include_history=True,
        as_of=None,
    )
    assert earlier == {}


def test_labels_use_the_given_now(tmp_path: Path) -> None:
    repository = _base(tmp_path)
    _question(repository, "m-port-a")
    repository.update(QUESTION, valid_from=datetime(2090, 1, 1, tzinfo=UTC))
    _indexed(repository)
    runtime = _gateway(repository.root).runtime
    kwargs: dict[str, Any] = {
        "policy_name": "strict",
        "source": "",
        "include_history": False,
        "as_of": None,
    }
    assert runtime.open_question_labels(repository, ["m-port-a"], **kwargs) == {}
    later = runtime.open_question_labels(
        repository, ["m-port-a"], now=datetime(2091, 1, 1, tzinfo=UTC), **kwargs
    )
    assert later == {"m-port-a": (OPEN, CONFLICTED_LABEL)}


def test_two_stage_labels_share_the_coordinator_now(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import memfw.recall.builtin as builtin
    from memfw.recall import Coordinator

    repository = _base(tmp_path)
    _question(repository, "m-port-a")
    _indexed(repository)
    seen: dict[str, Any] = {}
    real_search = Coordinator.search
    real_labels = builtin.open_question_labels_for

    def search(self: Coordinator, *args: Any, **kwargs: Any) -> Any:
        seen["coordinator"] = self.clock()
        return real_search(self, *args, **kwargs)

    def labels(*args: Any, **kwargs: Any) -> Any:
        seen["labels"] = kwargs["now"]
        return real_labels(*args, **kwargs)

    monkeypatch.setattr(Coordinator, "search", search)
    monkeypatch.setattr(builtin, "open_question_labels_for", labels)
    _gateway(repository.root).search("service port", limit=20, mode="two_stage")
    assert seen["coordinator"] == seen["labels"]


def test_candidates_come_from_the_index_with_a_scan_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import memfw.recall.builtin as builtin

    repository = _base(tmp_path)
    _question(repository, "m-port-a")
    _indexed(repository)
    policy = FilterPolicy.compat_fts(tenant="")
    now = datetime.now(UTC)
    assert builtin._indexed_question_ids(repository, "") == [QUESTION]
    assert builtin._indexed_question_ids(repository, "beta") == []

    def no_scan(_repository: Repository) -> list[str]:
        raise AssertionError("scanned although the index is readable")

    monkeypatch.setattr(builtin, "_scanned_question_ids", no_scan)
    assert open_question_index(repository, policy, now=now).conflicted == {"m-port-a"}
    monkeypatch.undo()
    repository.index_path.unlink()
    assert builtin._indexed_question_ids(repository, "") is None
    assert open_question_index(repository, policy, now=now).conflicted == {"m-port-a"}


def test_no_rows_reads_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import memfw.recall.builtin as builtin

    repository = _indexed(_base(tmp_path))
    _question(repository, "m-port-a")

    def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("built the index for an empty page")

    monkeypatch.setattr(builtin, "open_question_index", boom)
    payload = _gateway(repository.root).search("zzzz-nothing", limit=5, mode="fts", labels=True)
    assert payload["results"] == []
