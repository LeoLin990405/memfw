"""The judge stays off unless the server allow-list names a local endpoint."""

from __future__ import annotations

from pathlib import Path

import pytest

from memfw.mcp.server import Gateway
from memfw.retrieval.judge import (
    LocalJudge,
    apply_judge,
    judge_from_environment,
)
from scripts.memfw_readonly_service import ServiceError, reject_client_endpoints
from tests.test_retrieval_r2 import _vault


def test_missing_url_builds_no_judge() -> None:
    assert judge_from_environment({}) is None


def test_endpoint_must_be_allow_listed() -> None:
    with pytest.raises(ValueError, match="allow-listed"):
        LocalJudge("http://127.0.0.1:9/judge", ())


def test_cache_and_cap_bound_the_calls() -> None:
    calls: list[str] = []

    def transport(endpoint: str, question: str, passage: str, context: str) -> float:
        del endpoint, question, context
        calls.append(passage)
        return 0.25 if "alpha" in passage else 0.75

    judge = LocalJudge("http://127.0.0.1:9/judge", ("http://127.0.0.1:9/judge",), transport)
    results = [
        {"id": "a", "score": 1.0},
        {"id": "a-copy", "score": 0.5},
        {"id": "b", "score": 0.2},
        {"id": "c", "score": 0.1},
    ]
    passages = {"a": "alpha", "a-copy": "alpha", "b": "beta", "c": "gamma"}
    scored, used = apply_judge(results, passages, "question", judge, cap=2)
    assert used == 2
    assert calls == ["alpha", "beta"]
    by_id = {item["id"]: item["judge_score"] for item in scored}
    assert by_id["a"] == 0.25
    assert by_id["a-copy"] == 0.25
    assert by_id["c"] is None
    assert scored[-1]["id"] == "c"


def test_score_outside_the_unit_interval_is_rejected() -> None:
    def transport(endpoint: str, question: str, passage: str, context: str) -> float:
        del endpoint, question, passage, context
        return 1.5

    judge = LocalJudge("http://127.0.0.1:9/judge", ("http://127.0.0.1:9/judge",), transport)
    with pytest.raises(ValueError, match="0, 1"):
        judge.score("q", "p", "")


def test_module_presets_no_hosted_judge() -> None:
    source = Path(__file__).resolve().parents[1] / "src" / "memfw" / "retrieval" / "judge.py"
    text = source.read_text(encoding="utf-8")
    for banned in ("jev", "openai.com", "api.anthropic.com", "generativelanguage"):
        assert banned not in text.casefold()


def test_default_search_makes_no_judge_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MEMFW_JUDGE_URL", raising=False)
    monkeypatch.delenv("MEMFW_JUDGE_ALLOWLIST", raising=False)
    seen: list[int] = []

    def forbidden(*args: object, **kwargs: object) -> None:
        del args, kwargs
        seen.append(1)

    monkeypatch.setattr("memfw.mcp.server.apply_judge", forbidden)
    repository = _vault(tmp_path)
    found = Gateway(repository.root, tenant="alpha").search(
        "Where is the labelkey marker recorded",
        limit=10,
        mode="multihop",
    )
    assert seen == []
    assert "gold" in {item["id"] for item in found["results"]}
    assert all("judge_score" not in item for item in found["results"])


def test_injected_judge_stops_at_the_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEMFW_JUDGE_CAP", "1")
    calls: list[str] = []

    def transport(endpoint: str, question: str, passage: str, context: str) -> float:
        del endpoint, question, context
        calls.append(passage)
        return 0.9

    judge = LocalJudge("http://127.0.0.1:9/judge", ("http://127.0.0.1:9/judge",), transport)
    repository = _vault(tmp_path)
    found = Gateway(repository.root, tenant="alpha").search(
        "Where is the labelkey marker recorded",
        limit=10,
        mode="multihop",
        judge=judge,
    )
    assert len(calls) == 1
    assert found["results"][0]["judge_score"] == 0.9
    assert found["results"][-1]["judge_score"] is None


def test_request_body_cannot_name_a_judge_endpoint() -> None:
    with pytest.raises(ServiceError) as caught:
        reject_client_endpoints({"judge_endpoint": "https://jev.example/judge"})
    assert caught.value.status == 400
    assert caught.value.message == "endpoint fields are not accepted"
