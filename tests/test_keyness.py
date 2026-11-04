from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

from memfw.knowledge import KnowledgeCard, KnowledgeKind, validate_source_refs
from memfw.policies import DecayPolicy, PolicyCandidate, SaliencePolicy
from memfw.stats.keyness import KeynessScorer, observable_strength, tokenize
from memfw.stats.tail_fit import fit_discrete_tail

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def candidate(identifier: str, keyness: float) -> PolicyCandidate:
    return PolicyCandidate(id=identifier, body="same words", created_at=NOW, keyness=keyness)


def test_g2_and_percent_difference_for_known_tokens() -> None:
    result = KeynessScorer(["apple banana banana banana"]).score_documents(
        {"doc": "apple apple apple banana"}
    )["doc"]
    apple = next(term for term in result.terms if term.term == "apple")
    assert apple.document_count == 3
    assert apple.reference_count == 1
    assert apple.g2 > 0
    assert apple.percent_difference == 200.0
    assert 0 <= result.score <= 1


def test_percent_difference_marks_absent_reference_as_undefined() -> None:
    result = KeynessScorer(["banana banana banana banana"]).score_documents(
        {"doc": "apple apple apple apple"}
    )["doc"]
    apple = result.terms[0]
    assert apple.reference_absent
    assert apple.percent_difference is None


def test_keyness_empty_reference_and_empty_documents_are_zero() -> None:
    assert KeynessScorer([]).score_documents({"x": "apple"})["x"].score == 0
    assert KeynessScorer(["apple"]).score_documents({"x": ""})["x"].score == 0
    assert KeynessScorer().score_documents({"x": ""})["x"].terms == ()


def test_scoring_and_tokenization_are_deterministic_and_bilingual() -> None:
    scorer = KeynessScorer(["苹果 香蕉 apple banana"])
    docs = {"b": "苹果苹果 apple", "a": "香蕉 banana"}
    assert scorer.score_documents(docs) == scorer.score_documents(docs)
    assert "apple" in tokenize("An Apple.")
    assert tokenize("中文文本") == tokenize("中文文本")


def test_rolling_reference_uses_prior_window() -> None:
    rows = [
        ("a", NOW, "apple baseline"),
        ("b", NOW + timedelta(days=1), "apple baseline"),
        ("c", NOW + timedelta(days=2), "unique unique apple"),
    ]
    scores = KeynessScorer().score_rolling_documents(rows, window_size=1)
    assert scores["a"].score == 0
    assert scores["c"].terms
    assert scores["c"].terms[0].term == "unique"


def test_measured_keyness_changes_both_policies_and_observable_strength() -> None:
    low, high = candidate("low", 0.0), candidate("high", 1.0)
    assert SaliencePolicy()._rank((low, high), NOW)[0][0].id == "low"
    assert "+2*keyness" in SaliencePolicy()._rank((high,), NOW)[0][1]
    assert DecayPolicy().score(high, NOW) > DecayPolicy().score(low, NOW)
    assert observable_strength(2, NOW, NOW, 0.8) > observable_strength(2, NOW, NOW, 0.0)
    assert observable_strength(
        0, None, NOW, 0.0, created_at=NOW - timedelta(days=90)
    ) < observable_strength(0, None, NOW, 0.0, created_at=NOW)


def test_knowledge_card_reports_forgotten_source_ids() -> None:
    card = KnowledgeCard(KnowledgeKind.INSIGHT, "Observed pattern", ("keep", "forgotten"))
    validation = validate_source_refs(card, {"keep"})
    assert validation.valid_refs == ("keep",)
    assert validation.missing_refs == ("forgotten",)


def test_tail_fit_labels_small_samples_and_reports_nonpositive_counts() -> None:
    empty = fit_discrete_tail([0, 0])
    assert empty.status == "insufficient_sample"
    assert empty.sample_size == 0
    assert empty.excluded_nonpositive == 2
    small = fit_discrete_tail([0, 1, 2, 3])
    assert small.status == "insufficient_sample"
    assert small.sample_size == 3
    assert small.excluded_nonpositive == 1
    values = [0, *range(1, 22)]
    fitted = fit_discrete_tail(values)
    assert fitted.status == "descriptive_approximation"
    assert fitted.ks_distance is not None
    assert fitted.log_likelihood_ratio is not None
    assert fitted.gof_pvalue is not None


def test_keyness_uses_only_repeated_non_stopwords_and_configurable_top_k() -> None:
    document = "the apple apple banana banana cherry cherry rareword 的"
    reference = ["the apple banana cherry fruit color" for _ in range(5)]
    one = KeynessScorer(reference, top_k=1).score_documents({"doc": document})["doc"]
    three = KeynessScorer(reference, top_k=3).score_documents({"doc": document})["doc"]
    terms = {term.term for term in three.terms}
    assert terms == {"apple", "banana", "cherry"}
    assert one.score <= three.score


def test_keyness_allows_custom_stopwords_and_single_use_opt_in() -> None:
    reference = ["orchid lavender"]
    default = KeynessScorer(reference).score_documents({"doc": "orchid raretoken raretoken"})["doc"]
    custom = KeynessScorer(
        reference, minimum_term_frequency=1, stopwords=["orchid"]
    ).score_documents({"doc": "orchid raretoken raretoken"})["doc"]
    assert [term.term for term in default.terms] == ["raretoken"]
    assert [term.term for term in custom.terms] == ["raretoken"]
    single_use = KeynessScorer(reference, minimum_term_frequency=1).score_documents(
        {"doc": "solitary"}
    )["doc"]
    assert [term.term for term in single_use.terms] == ["solitary"]


def test_underrepresented_terms_do_not_count_as_significant_or_increase_score() -> None:
    reference = ["signal " * 20 + "common " * 100]
    result = KeynessScorer(reference, top_k=2).score_documents(
        {"doc": "signal signal common common"}
    )["doc"]
    assert [term.term for term in result.terms] == ["signal"]
    signal = result.terms[0]
    expected = 1.0 - math.exp(-signal.g2 / 20.0)
    assert result.score == expected
