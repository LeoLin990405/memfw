"""R0 synthetic baseline: power analysis, held-out questions, shipped search."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import scripts.memfw_readonly_service as service
from eval.retrieval_r0.baseline import build_report
from eval.retrieval_r0.cases import CASE_TYPES, SPLITS, generate, gold_leaks
from eval.retrieval_r0.corpus import load_corpus, search_path
from eval.retrieval_r0.metrics import all_evidence, evidence_recall
from eval.retrieval_r0.power import (
    bootstrap_mean_interval,
    holm_alpha,
    required_per_class,
    select_sample_size,
)
from memfw.index import MemoryIndex
from memfw.mcp.server import Gateway, client_endpoint_fields
from memfw.notes.index import NotesIndex
from memfw.stats.keyness import tokenize
from scripts.memfw_readonly_service import ServiceError, reject_client_endpoints


# The suite-wide socket guard would refuse the loopback /stage and /search client.
@pytest.fixture(autouse=True)
def disable_network() -> None:
    """These tests talk to the in-process readonly service on loopback."""


def test_holm_alpha_is_stricter_than_the_family_alpha() -> None:
    assert holm_alpha(6, 0.05) == pytest.approx(0.05 / 6)
    assert holm_alpha(6, 0.05) < 0.05


def test_bootstrap_interval_separates_a_zero_gap_from_a_certain_gain() -> None:
    import random

    lower, upper = bootstrap_mean_interval(
        [0.0] * 40, alpha=0.05, n_bootstrap=80, rng=random.Random(1)
    )
    assert lower <= 0 <= upper
    lower, upper = bootstrap_mean_interval(
        [1.0] * 40, alpha=0.05, n_bootstrap=80, rng=random.Random(1)
    )
    assert lower > 0
    assert upper > 0


def test_select_sample_size_uses_the_smallest_powered_n_and_the_floor() -> None:
    assert select_sample_size(
        {50: 0.4, 100: 0.79, 150: 0.81, 200: 0.95}, power_target=0.8, minimum=50
    ) == (150, True)
    assert select_sample_size({50: 0.99}, power_target=0.8, minimum=50) == (50, True)
    assert select_sample_size({50: 0.2}, power_target=0.8, minimum=50) == (50, False)


def test_required_per_class_runs_the_bootstrap_for_a_large_gap() -> None:
    result = required_per_class(
        gap=0.40,
        n_sim=30,
        n_bootstrap=40,
        candidates=(50,),
        n_hypotheses=6,
        seed=1,
    )
    assert result.per_class == 50
    assert result.holm_alpha == pytest.approx(0.05 / 6)
    assert result.meets_power_target is True
    assert result.estimated_power >= 0.8
    assert result.n_sim == 30


def test_required_per_class_records_when_the_candidate_is_underpowered() -> None:
    result = required_per_class(
        gap=0.01,
        n_sim=20,
        n_bootstrap=40,
        candidates=(50,),
        power_target=0.8,
        minimum=50,
        seed=2,
    )
    assert result.per_class == 50
    assert result.meets_power_target is False
    assert result.estimated_power < 0.8


def test_questions_keep_gold_ids_and_secrets_out_of_the_text() -> None:
    corpora = generate(4, seed=11)
    assert gold_leaks(corpora) == []
    dev_ids = {
        question.question_id
        for (split, _case), corpus in corpora.items()
        if split == "dev"
        for question in corpus.questions
    }
    report_ids = {
        question.question_id
        for (split, _case), corpus in corpora.items()
        if split == "report"
        for question in corpus.questions
    }
    assert dev_ids.isdisjoint(report_ids)
    assert len(dev_ids) == len(CASE_TYPES) * 4
    assert len(report_ids) == len(CASE_TYPES) * 4
    channels = {
        channel
        for question in corpora[("report", "poisoning")].questions
        for channel in question.poison_channels
    }
    assert channels == {"stage", "notes"}


def test_each_class_uses_at_least_three_templates() -> None:
    corpora = generate(6, seed=5)
    for case_type in CASE_TYPES:
        indexes = {question.template_index for question in corpora[("report", case_type)].questions}
        assert indexes == {0, 1, 2}


def test_full_chain_scores_bridge_and_terminal_separately() -> None:
    chain = ("bridge", "gold")
    assert evidence_recall(["bridge"], chain, (), k=10) == 0.5
    assert all_evidence(["bridge"], chain, (), k=10) == 0.0
    assert evidence_recall(["gold"], chain, (), k=10) == 0.5
    assert all_evidence(["gold"], chain, (), k=10) == 0.0
    assert evidence_recall(["bridge", "gold"], chain, (), k=10) == 1.0
    assert all_evidence(["bridge", "gold"], chain, (), k=10) == 1.0


def test_multihop_terminal_shares_no_tokenizer_term_with_the_question() -> None:
    corpus = generate(6, seed=5)[("report", "multihop")]
    documents = {document.doc_id: document for document in corpus.documents}
    for question in corpus.questions:
        query_terms = set(tokenize(question.question))
        assert question.lexical_key in question.question
        bridge = documents[question.evidence_ids[0]]
        terminal = documents[question.evidence_ids[1]]
        assert question.lexical_key in bridge.body
        terminal_terms = set(tokenize(terminal.title)) | set(tokenize(terminal.body))
        assert query_terms.isdisjoint(terminal_terms)
        assert question.lexical_key not in terminal.body


def test_single_hop_gold_contains_the_lexical_key(tmp_path: Path) -> None:
    corpus = generate(1, seed=5)[("report", "time_update")]
    built = load_corpus(corpus, tmp_path / "vault")
    question = corpus.questions[0]
    gold = built.by_id[question.evidence_ids[0]]
    assert question.lexical_key in gold.body
    retrieved = search_path("memory_bm25", built, question, limit=10)
    assert question.evidence_ids[0] in retrieved
    assert set(question.forbidden_ids).isdisjoint(retrieved)


def test_deleted_valid_to_row_is_absent_from_the_shipped_index(tmp_path: Path) -> None:
    corpus = generate(1, seed=5)[("report", "delete")]
    built = load_corpus(corpus, tmp_path / "vault")
    question = corpus.questions[0]
    for path in ("memory_fts", "memory_bm25", "vault_bm25"):
        retrieved = search_path(path, built, question, limit=10)
        assert set(question.forbidden_ids).isdisjoint(retrieved)
        assert (
            evidence_recall(retrieved, question.evidence_ids, question.forbidden_ids, k=10) == 1.0
        )
        assert all_evidence(retrieved, question.evidence_ids, question.forbidden_ids, k=10) == 1.0


def test_search_receives_the_question_and_not_the_gold_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    corpus = generate(1, seed=9)[("report", "conflict")]
    seen: list[str] = []
    real = MemoryIndex.ranked_search

    def spy(  # type: ignore[no-untyped-def]
        self: MemoryIndex,
        query: str,
        limit: int = 20,
        *,
        ranking: str = "bm25",
        candidate_ids=None,
    ):
        seen.append(query)
        return real(self, query, limit=limit, ranking=ranking, candidate_ids=candidate_ids)

    monkeypatch.setattr(MemoryIndex, "ranked_search", spy)
    built = load_corpus(corpus, tmp_path / "vault")
    question = corpus.questions[0]
    search_path("memory_bm25", built, question, limit=10)
    assert seen
    assert seen[-1] == question.question
    for evidence_id in question.evidence_ids:
        assert evidence_id not in seen[-1]


def test_request_endpoint_is_rejected_before_search(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(ServiceError) as rejected:
        reject_client_endpoints({"query": "hello", "endpoint": "http://127.0.0.1/v1"})
    assert rejected.value.status == 400
    assert "endpoint" in str(rejected.value)
    reject_client_endpoints({"query": "hello", "limit": 10, "tenant": "alpha"})
    assert client_endpoint_fields({"query": "hello", "judge_endpoint": "http://127.0.0.1"}) == [
        "judge_endpoint"
    ]
    gateway = Gateway(tmp_path / "missing", tenant="alpha", entity_enabled=False)
    gateway.handle(
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {
                "name": "memory_search",
                "arguments": {"query": "hello", "embedding_url": "http://127.0.0.1/v1"},
            },
        }
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["code"] == -32602
    assert "endpoint" in payload["error"]["message"]


def test_report_split_runs_every_shipped_path(tmp_path: Path) -> None:
    report = build_report(tmp_path / "run", per_class=1, seed=4, power=None)
    assert report["per_class"] == 1
    assert report["dev"]["held_out"] is True
    assert report["gold_in_prompts"] is False
    assert set(report["report"]["classes"]) == set(CASE_TYPES)
    assert set(SPLITS) == {"dev", "report"}
    measured = report["measured"]["paths"]
    assert set(measured) == {
        "memory_fts",
        "memory_bm25",
        "memory_count",
        "vault_bm25",
        "hybrid_fts",
        "mcp_fts",
        "mcp_default",
        "http_search",
        "notes_fts",
    }
    for payload in measured.values():
        assert payload["ranking_repeat_matched"] is True
        assert payload["overall"]["n"] == len(CASE_TYPES)
        assert payload["by_class"]["delete"]["n"] == 1
        multihop = payload["by_class"]["multihop"]
        assert multihop["all_evidence@10"] == 0.0
        # One of the two hop documents shares a query term. Term OR recalls it.
        assert multihop["evidence_recall@10"] == 0.5
    memory_delete = measured["memory_bm25"]["by_class"]["delete"]
    assert memory_delete["evidence_recall@10"] == 1.0
    assert memory_delete["forbidden_in_topk_rate"] == 0.0
    notes_delete = measured["notes_fts"]["by_class"]["delete"]
    assert notes_delete["evidence_recall@10"] == 0.0
    assert notes_delete["forbidden_in_topk_rate"] == 1.0
    library_tenant = measured["memory_fts"]["by_class"]["multi_tenant"]
    assert library_tenant["evidence_recall@10"] == 1.0
    assert library_tenant["mean_hit_count"] == 10.0
    # MemoryIndex.search does not apply the tenant predicate. HTTP and MCP do.
    assert library_tenant["other_tenant_in_topk_rate"] == 1.0
    http_tenant = measured["http_search"]["by_class"]["multi_tenant"]
    assert http_tenant["evidence_recall@10"] == 1.0
    assert http_tenant["other_tenant_in_topk_rate"] == 0.0
    mcp_tenant = measured["mcp_fts"]["by_class"]["multi_tenant"]
    assert mcp_tenant["evidence_recall@10"] == 1.0
    assert mcp_tenant["other_tenant_in_topk_rate"] == 0.0
    memory_tenant = measured["memory_bm25"]["by_class"]["multi_tenant"]
    assert memory_tenant["other_tenant_in_topk_rate"] == 1.0
    hybrid_tenant = measured["hybrid_fts"]["by_class"]["multi_tenant"]
    assert hybrid_tenant["evidence_recall@10"] == 1.0
    assert hybrid_tenant["other_tenant_in_topk_rate"] == 0.0
    poison = measured["memory_bm25"]["by_class"]["poisoning"]
    assert poison["poison_in_topk_rate"] == 0.0
    assert measured["http_search"]["observed_mode"] == "fts"
    assert measured["http_search"]["observed_degraded"] is False
    assert measured["mcp_default"]["observed_mode"] == "fts"
    assert measured["mcp_default"]["observed_degraded"] is True


def test_stage_and_notes_use_their_write_entry_points(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = {"stage": 0, "notes": 0, "search_vault": 0, "gateway": 0}
    real_stage = service.stage_candidate
    real_search = service.search_vault
    real_index = NotesIndex.index
    real_gateway = Gateway.search

    def spy_stage(root: Path, payload: dict) -> dict:
        calls["stage"] += 1
        result = real_stage(root, payload)
        assert result["authority"] is False
        return result

    def spy_search(*args, **kwargs):  # type: ignore[no-untyped-def]
        calls["search_vault"] += 1
        return real_search(*args, **kwargs)

    def spy_index(self: NotesIndex, root: Path, **kwargs):  # type: ignore[no-untyped-def]
        calls["notes"] += 1
        return real_index(self, root, **kwargs)

    def spy_gateway(self: Gateway, *args, **kwargs):  # type: ignore[no-untyped-def]
        calls["gateway"] += 1
        return real_gateway(self, *args, **kwargs)

    monkeypatch.setattr(service, "stage_candidate", spy_stage)
    monkeypatch.setattr(service, "search_vault", spy_search)
    monkeypatch.setattr(NotesIndex, "index", spy_index)
    monkeypatch.setattr(Gateway, "search", spy_gateway)
    corpus = generate(2, seed=3)[("report", "poisoning")]
    built = load_corpus(corpus, tmp_path / "vault")
    assert calls["stage"] == 1
    assert calls["notes"] == 1
    assert built.stage_calls == 1
    assert built.repository_adds > 0
    for question in corpus.questions:
        poison_id = question.poison_ids[0]
        memory_ids = search_path("memory_bm25", built, question, limit=10)
        assert poison_id not in memory_ids
        if question.poison_channels == ("stage",):
            notes_ids = search_path("notes_fts", built, question, limit=10)
            http_ids = search_path("http_search", built, question, limit=10)
            assert poison_id not in notes_ids
            assert poison_id not in http_ids
            candidate = (
                built.repository.root / "_candidates" / "remember" / f"conductor-{poison_id}.md"
            )
            assert candidate.is_file()
            assert "authority: false" in candidate.read_text(encoding="utf-8")
            assert not (built.repository.root / "memories" / f"{poison_id}.md").exists()
        else:
            notes_ids = search_path("notes_fts", built, question, limit=10)
            assert poison_id in notes_ids
            assert not (built.repository.root / "memories" / f"{poison_id}.md").exists()
    search_path("mcp_default", built, corpus.questions[0], limit=5)
    assert calls["search_vault"] >= 1
    assert calls["gateway"] == 1


def test_search_path_refuses_an_endpoint_argument(tmp_path: Path) -> None:
    corpus = generate(1, seed=3)[("dev", "conflict")]
    built = load_corpus(corpus, tmp_path / "vault")
    with pytest.raises(ValueError, match="endpoint"):
        search_path(
            "memory_fts",
            built,
            corpus.questions[0],
            limit=5,
            endpoint="http://127.0.0.1/v1",
        )
