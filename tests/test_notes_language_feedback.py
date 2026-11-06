from __future__ import annotations

import io
import json
import urllib.request
from pathlib import Path

import pytest

from memfw.notes.feedback import CandidateTrace, FeedbackEvent, FeedbackStore, RetrievalTrace
from memfw.notes.language_model import HttpLanguageModelProvider


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _completion(content: object) -> bytes:
    return json.dumps(
        {"choices": [{"message": {"content": json.dumps(content, ensure_ascii=False)}}]}
    ).encode("utf-8")


def test_http_language_model_structured_candidates(monkeypatch) -> None:
    responses = [
        {"queries": ["corpus", "KWIC"]},
        {
            "candidates": [
                {"content": "remember this", "title": "T", "evidence": ["e1"], "confidence": 2}
            ]
        },
        {"kind": "duplicate", "confidence": 0.8, "reason": "same"},
        {"summary": "short"},
        {"entities": [{"text": "LancsBox", "kind": "tool"}]},
    ]
    calls: list[dict[str, object]] = []

    def fake_urlopen(request, timeout=0):  # noqa: ARG001
        calls.append(json.loads(request.data.decode("utf-8")))
        return _Response(_completion(responses[len(calls) - 1]))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    provider = HttpLanguageModelProvider("http://example.invalid/v1/chat/completions", model="test")
    rewritten = provider.rewrite_query("corpus", limit=3)
    assert rewritten.queries[0] == "corpus"
    assert "KWIC" in rewritten.queries
    candidates = provider.extract_memories("conversation")
    assert candidates[0].content == "remember this"
    assert candidates[0].confidence == 1.0
    relationship = provider.suggest_relationship("new", "old")
    assert relationship.kind == "duplicate"
    assert relationship.confidence == 0.8
    assert provider.summarize("long text").summary == "short"
    assert provider.extract_entities("text")[0].text == "LancsBox"
    assert len(calls) == 5


def test_feedback_store_minimizes_content_and_requires_trace(tmp_path: Path) -> None:
    store = FeedbackStore(tmp_path / "feedback.sqlite")
    trace = RetrievalTrace(
        trace_id="trace-1",
        task_id="task-1",
        query="corpus",
        mode="hybrid",
        rewrites=("corpus",),
        candidates=(
            CandidateTrace(
                path="a.md",
                rank=1,
                score=0.5,
                section_id=3,
                content_hash="abc",
                source="hybrid",
                content="private body",
            ),
        ),
        model_versions={"embedding": "v1"},
        include_content=False,
    )
    store.record_trace(trace)
    rows = store.traces(task_id="task-1")
    assert rows[0]["candidates"][0]["path"] == "a.md"
    assert "content" not in rows[0]["candidates"][0]
    feedback_id = store.add_feedback(
        FeedbackEvent(
            trace_id="trace-1",
            task_id="task-1",
            kind="citation",
            candidate_path="a.md",
            relevance="weak_positive",
        )
    )
    assert feedback_id
    events = store.feedback(trace_id="trace-1")
    assert events[0]["relevance"] == "weak_positive"
    with pytest.raises(ValueError):
        store.add_feedback(FeedbackEvent(trace_id="missing", task_id="x", kind="citation"))


def test_retriever_records_trace_and_mcp_feedback_roundtrip(tmp_path: Path) -> None:
    from memfw.notes import HashEmbeddingProvider, NotesIndex, NotesRetriever, RetrievalConfig
    from memfw.notes.mcp import NotesGateway, handle_request

    root = tmp_path / "vault"
    root.mkdir()
    (root / "a.md").write_text("# A\n\ncorpus alpha\n", encoding="utf-8")
    index = NotesIndex(tmp_path / "index.sqlite", embedder=HashEmbeddingProvider(8))
    index.index(root)
    store = FeedbackStore(tmp_path / "feedback.sqlite")
    retriever = NotesRetriever(
        index,
        config=RetrievalConfig(final_k=2),
        trace_store=store,
    )
    result = retriever.search("corpus", task_id="task-1", tenant="t1", source="test")
    assert result.trace_id
    trace = store.trace(result.trace_id)
    assert trace and trace["task_id"] == "task-1"
    assert trace["candidates"]
    assert "content" not in trace["candidates"][0]
    assert trace["candidates"][0]["content_hash"]

    gateway = NotesGateway(root, tmp_path / "index.sqlite", feedback_store=store)
    searched = gateway.call(
        "notes_search",
        {"query": "corpus", "task_id": "task-2", "tenant": "t1", "source": "mcp"},
    )
    assert searched["trace_id"]
    assert searched["results"][0]["path"] == "a.md"
    feedback_id = gateway.call(
        "retrieval_feedback",
        {
            "trace_id": searched["trace_id"],
            "task_id": "task-2",
            "kind": "citation",
            "candidate_path": "a.md",
            "relevance": "weak_positive",
        },
    )["feedback_id"]
    assert feedback_id
    trace_response = handle_request(
        gateway,
        {
            "jsonrpc": "2.0",
            "id": 9,
            "method": "tools/call",
            "params": {
                "name": "retrieval_trace",
                "arguments": {"trace_id": searched["trace_id"]},
            },
        },
    )
    assert trace_response and trace_response["result"]["isError"] is False
