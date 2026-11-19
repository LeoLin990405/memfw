"""Unit tests for the pure ``memfw.retrieval.health`` module."""

from __future__ import annotations

import ast
import copy
import re
from pathlib import Path
from typing import Any

import pytest

from memfw.retrieval import health
from memfw.retrieval.health import (
    REASON_CODES,
    SCHEMA,
    WITHHELD_CATEGORIES,
    check_reason,
    derive,
    from_context_pack,
    from_conversation_search,
    from_memory_search,
    from_recall,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
HEALTH_KEYS = {"schema", "mode", "confidence", "reasons", "score_kind", "note"}


def _assert_shape(out: dict[str, Any]) -> None:
    assert set(out) == HEALTH_KEYS
    assert out["schema"] == SCHEMA == "retrieval-health/1"
    assert isinstance(out["note"], str) and out["note"]
    assert out["reasons"] == sorted(set(out["reasons"]))
    for code in out["reasons"]:
        check_reason(code)


def _labels(**overrides: Any) -> dict[str, Any]:
    labels: dict[str, Any] = {
        "route": "default",
        "providers": ["fts", "hybrid"],
        "degraded": {},
        "projection_versions": {},
        "fail_closed": False,
        "fallback_reason": None,
        "resolvers_degraded": {},
    }
    labels.update(overrides)
    return labels


def _hit(n: int = 1) -> dict[str, Any]:
    return {"path": f"memories/note-{n}.md", "score": 0.5, "title": f"note {n}"}


# -- derive --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "why"),
    [
        ({}, "no_match"),
        ({"failed": True}, "all_sources_failed"),
        ({"reasons": ["withheld:privacy:2"]}, "withheld_only"),
    ],
)
def test_derive_empty(kwargs: dict[str, Any], why: str) -> None:
    out = derive(result_count=0, **kwargs)
    _assert_shape(out)
    assert out["mode"] == "empty"
    assert out["confidence"] == "low"
    assert why in out["reasons"]


def test_derive_empty_failed_beats_withheld() -> None:
    out = derive(result_count=0, reasons=["withheld:tenant:1"], failed=True)
    assert "all_sources_failed" in out["reasons"]
    assert "withheld_only" not in out["reasons"]


def test_derive_fallback_low() -> None:
    out = derive(result_count=3, reasons=["route_fallback"], fallback=True)
    _assert_shape(out)
    assert (out["mode"], out["confidence"]) == ("fallback", "low")


def test_derive_degraded_medium() -> None:
    out = derive(result_count=3, reasons=["source_timeout:qdrant"])
    _assert_shape(out)
    assert (out["mode"], out["confidence"]) == ("degraded", "medium")


def test_derive_cause_code_degrades_keyword_only_does_not() -> None:
    # Aligned with fleet-memory: the cause code carries the degradation.
    out = derive(result_count=3, reasons=["semantic_unavailable", "keyword_only"])
    _assert_shape(out)
    assert (out["mode"], out["confidence"]) == ("degraded", "medium")
    for informational in ("keyword_only", "adapters_shadow"):
        alone = derive(result_count=3, reasons=[informational])
        assert (alone["mode"], alone["confidence"]) == ("full", "high")
        assert alone["reasons"] == [informational]


def test_derive_full_high() -> None:
    out = derive(result_count=3)
    _assert_shape(out)
    assert (out["mode"], out["confidence"]) == ("full", "high")
    assert out["reasons"] == []
    assert out["score_kind"] == "rank"


def test_derive_reasons_sorted_and_deduplicated() -> None:
    out = derive(
        result_count=1,
        reasons=["source_timeout:b", "keyword_only", "source_timeout:b", "fts_fallback"],
    )
    assert out["reasons"] == ["fts_fallback", "keyword_only", "source_timeout:b"]


def test_derive_informational_only_stays_full() -> None:
    out = derive(result_count=2, reasons=["withheld:privacy:2", "access_not_recorded"])
    _assert_shape(out)
    assert (out["mode"], out["confidence"]) == ("full", "high")
    assert out["reasons"] == ["access_not_recorded", "withheld:privacy:2"]


def test_derive_drops_empty_only_reasons_when_results() -> None:
    out = derive(result_count=2, reasons=["no_match"])
    assert out["mode"] == "full"
    assert out["reasons"] == []


@pytest.mark.parametrize("bad", ["foo", "source_timeout:", "withheld:secret:1"])
def test_derive_unknown_reason_raises(bad: str) -> None:
    with pytest.raises(ValueError):
        derive(result_count=1, reasons=[bad])


def test_derive_unknown_score_kind_raises() -> None:
    with pytest.raises(ValueError):
        derive(result_count=1, score_kind="relevance")


# -- check_reason --------------------------------------------------------------------


def _filled_templates() -> list[str]:
    out: list[str] = []
    for template in REASON_CODES:
        filled = template.replace("<name>", "qdrant").replace("<n>", "3")
        if "<category>" in filled:
            out.extend(filled.replace("<category>", cat) for cat in WITHHELD_CATEGORIES)
        else:
            out.append(filled)
    return out


@pytest.mark.parametrize("code", _filled_templates())
def test_check_reason_accepts_every_template(code: str) -> None:
    assert check_reason(code) == code


@pytest.mark.parametrize(
    "bad",
    [
        "source_timeout:",
        "withheld:secret:1",
        "foo",
        "",
        "withheld:privacy:",
        "source_error:has space",
        "qmd_live_read_errors:x",
    ],
)
def test_check_reason_rejects(bad: str) -> None:
    with pytest.raises(ValueError):
        check_reason(bad)


def test_check_reason_rejects_non_str() -> None:
    with pytest.raises(ValueError):
        check_reason(3)  # type: ignore[arg-type]


# -- from_recall ---------------------------------------------------------------------


def test_recall_healthy_full() -> None:
    out = from_recall(_labels(), {"dropped": {}}, result_count=4)
    _assert_shape(out)
    assert (out["mode"], out["confidence"], out["score_kind"]) == ("full", "high", "rank")
    assert out["reasons"] == []


def test_recall_hybrid_timeout_keyword_only() -> None:
    labels = _labels(providers=["fts"], degraded={"hybrid": "timeout"})
    out = from_recall(labels, {"dropped": {}}, result_count=4)
    _assert_shape(out)
    assert out["reasons"] == ["keyword_only", "source_timeout:hybrid"]
    assert (out["mode"], out["confidence"]) == ("degraded", "medium")


def test_recall_adapter_busy() -> None:
    out = from_recall(_labels(degraded={"qdrant": "busy"}), None, result_count=4)
    assert {"source_busy:qdrant", "adapters_busy"} <= set(out["reasons"])
    assert (out["mode"], out["confidence"]) == ("degraded", "medium")


def test_recall_adapter_timeout() -> None:
    out = from_recall(_labels(degraded={"qdrant": "timeout"}), None, result_count=4)
    assert {"source_timeout:qdrant", "adapters_timeout"} <= set(out["reasons"])


def test_recall_builtin_timeout_is_not_adapter() -> None:
    out = from_recall(_labels(degraded={"hybrid": "timeout"}), None, result_count=4)
    assert "adapters_timeout" not in out["reasons"]


@pytest.mark.parametrize(
    ("code", "reason"),
    [
        ("unavailable:no_index", "source_unavailable:qdrant"),
        ("error:x", "source_error:qdrant"),
        ("invalid", "source_error:qdrant"),
    ],
)
def test_recall_provider_status_codes(code: str, reason: str) -> None:
    out = from_recall(_labels(degraded={"qdrant": code}), None, result_count=4)
    assert reason in out["reasons"]
    assert out["mode"] == "degraded"


def test_recall_resolver_error() -> None:
    out = from_recall(_labels(resolvers_degraded={"notes": "timeout"}), None, result_count=4)
    assert out["reasons"] == ["resolver_error:notes"]
    assert out["mode"] == "degraded"


def test_recall_route_fallback() -> None:
    labels = _labels(fallback_reason="routed_providers_failed")
    out = from_recall(labels, None, result_count=4)
    assert (out["mode"], out["confidence"]) == ("fallback", "low")
    assert "route_fallback" in out["reasons"]


def test_recall_fail_closed_empty() -> None:
    labels = _labels(fail_closed=True, degraded={"fts": "error", "hybrid": "error"})
    out = from_recall(labels, None, result_count=0)
    _assert_shape(out)
    assert (out["mode"], out["confidence"]) == ("empty", "low")
    assert "all_sources_failed" in out["reasons"]


def test_recall_withheld_categories_stay_full() -> None:
    metrics = {"dropped": {"privacy": 2, "archived": 1, "superseded": 1, "tenant": 1, "source": 3}}
    out = from_recall(_labels(), metrics, result_count=2)
    _assert_shape(out)
    assert out["reasons"] == [
        "withheld:other:3",
        "withheld:privacy:2",
        "withheld:retired:2",
        "withheld:tenant:1",
    ]
    assert out["mode"] == "full"


def test_recall_withheld_ignores_zero_and_non_int() -> None:
    metrics = {"dropped": {"privacy": 0, "tenant": "2", "archived": 1}}
    out = from_recall(_labels(), metrics, result_count=2)
    assert out["reasons"] == ["withheld:retired:1"]


def test_recall_only_drops_withheld_only() -> None:
    out = from_recall(_labels(), {"dropped": {"privacy": 2}}, result_count=0)
    assert out["mode"] == "empty"
    assert "withheld_only" in out["reasons"]
    assert "no_match" not in out["reasons"]


def test_recall_semantic_not_configured() -> None:
    labels = _labels(providers=["fts"])
    out = from_recall(labels, None, result_count=3, semantic_configured=False)
    assert {"semantic_unavailable", "keyword_only"} <= set(out["reasons"])
    assert (out["mode"], out["confidence"]) == ("degraded", "medium")


def test_recall_unsafe_provider_name() -> None:
    out = from_recall(_labels(degraded={"bad name!": "timeout"}), None, result_count=1)
    assert "source_timeout:unknown" in out["reasons"]


# -- from_memory_search --------------------------------------------------------------


def _search(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "mode": "fts",
        "degraded": False,
        "errors": [],
        "model_versions": {},
        "results": [_hit()],
    }
    payload.update(overrides)
    return payload


def test_memory_fts_full() -> None:
    out = from_memory_search(_search(), requested_mode="fts")
    _assert_shape(out)
    assert (out["mode"], out["score_kind"]) == ("full", "bm25")


def test_memory_hybrid_no_embedder() -> None:
    out = from_memory_search(_search(degraded=True), requested_mode="hybrid")
    assert out["reasons"] == ["keyword_only", "semantic_unavailable"]
    assert (out["mode"], out["confidence"], out["score_kind"]) == ("degraded", "medium", "bm25")


def test_memory_hybrid_semantic_empty() -> None:
    payload = _search(degraded=True, model_versions={"embedding": "m"})
    out = from_memory_search(payload, requested_mode="hybrid")
    assert out["reasons"] == ["keyword_only", "semantic_empty"]
    assert (out["mode"], out["confidence"]) == ("degraded", "medium")


def test_memory_hybrid_raised_fallback() -> None:
    payload = _search(
        degraded=True, errors=["hybrid unavailable: boom"], model_versions={"embedding": "m"}
    )
    out = from_memory_search(payload, requested_mode="hybrid")
    assert (out["mode"], out["confidence"]) == ("fallback", "low")
    assert {"source_error:hybrid", "fts_fallback", "keyword_only"} <= set(out["reasons"])


def test_memory_semantic_off_and_partial() -> None:
    payload = _search(
        mode="hybrid",
        errors=["semantic_query_withheld", "semantic_partial"],
        model_versions={"embedding": "m"},
    )
    out = from_memory_search(payload, requested_mode="hybrid")
    # memory_hybrid's "semantic_partial" means memories without vectors.
    assert out["reasons"] == ["semantic_off", "vectors_partial"]
    assert (out["mode"], out["score_kind"]) == ("degraded", "rank")


def test_memory_hybrid_fts_semantic_off_no_semantic_unavailable() -> None:
    payload = _search(degraded=True, errors=["semantic_query_withheld"])
    out = from_memory_search(payload, requested_mode="hybrid")
    assert out["reasons"] == ["keyword_only", "semantic_off"]


def test_memory_hybrid_degraded_no_errors() -> None:
    payload = _search(mode="hybrid", degraded=True, model_versions={"embedding": "m"})
    out = from_memory_search(payload, requested_mode="hybrid")
    assert out["reasons"] == ["keyword_only", "semantic_empty"]
    assert (out["mode"], out["confidence"], out["score_kind"]) == ("degraded", "medium", "rank")


def test_memory_multihop_expansion_empty() -> None:
    out = from_memory_search(_search(mode="multihop", degraded=True), requested_mode="multihop")
    assert out["reasons"] == ["expansion_empty"]
    assert out["score_kind"] == "bm25"


def test_memory_vector_cosine() -> None:
    payload = _search(mode="vector", model_versions={"embedding": "m"})
    out = from_memory_search(payload, requested_mode="vector")
    assert (out["mode"], out["score_kind"]) == ("full", "cosine")


def test_memory_unknown_error() -> None:
    out = from_memory_search(_search(errors=["something odd happened"]), requested_mode="fts")
    assert out["reasons"] == ["source_error:unknown"]
    assert out["mode"] == "degraded"


def test_memory_empty_results() -> None:
    out = from_memory_search(_search(results=[]), requested_mode="fts")
    assert (out["mode"], out["reasons"]) == ("empty", ["no_match"])


def test_memory_two_stage_dispatches_to_recall() -> None:
    payload = {
        "mode": "two_stage",
        "degraded": False,
        "errors": ["access_not_recorded"],
        "model_versions": {"embedding": "m"},
        "results": [_hit(1), _hit(2)],
        "recall": _labels(),
        "recall_metrics": {"dropped": {"privacy": 1}},
    }
    out = from_memory_search(payload, requested_mode="two_stage")
    _assert_shape(out)
    assert out["score_kind"] == "rank"
    assert out["reasons"] == ["access_not_recorded", "withheld:privacy:1"]
    assert (out["mode"], out["confidence"]) == ("full", "high")


def test_memory_two_stage_recall_degraded() -> None:
    payload = {
        "mode": "two_stage",
        "errors": [],
        "model_versions": {"embedding": "m"},
        "results": [_hit()],
        "recall": _labels(providers=["fts"], degraded={"hybrid": "timeout"}),
        "recall_metrics": {"dropped": {}},
    }
    out = from_memory_search(payload, requested_mode="two_stage")
    assert out["reasons"] == ["keyword_only", "source_timeout:hybrid"]


# -- from_conversation_search --------------------------------------------------------


def test_conversation_default_full() -> None:
    payload = {"notice": "keyword turns", "results": [{"session_id": "s1"}], "next_offset": None}
    out = from_conversation_search(payload)
    _assert_shape(out)
    assert (out["mode"], out["score_kind"]) == ("full", "none")


def test_conversation_default_empty() -> None:
    out = from_conversation_search({"notice": "n", "results": [], "next_offset": None})
    assert (out["mode"], out["reasons"]) == ("empty", ["no_match"])


def _two_stage(degraded: list[str], tags: int = 0) -> dict[str, Any]:
    return {
        "mode": "two_stage",
        "degraded": degraded,
        "candidates": {"fts": 2, "semantic": 0, "tags": tags, "total": 2 + tags},
        "sessions": [{"session_id": "s1"}, {"session_id": "s2"}],
    }


def test_conversation_two_stage_no_embedder() -> None:
    out = from_conversation_search(_two_stage(["no_embedder"]))
    _assert_shape(out)
    assert out["reasons"] == ["keyword_only", "semantic_unavailable"]
    assert (out["mode"], out["confidence"], out["score_kind"]) == ("degraded", "medium", "rank")


def test_conversation_two_stage_tags_rescue() -> None:
    out = from_conversation_search(_two_stage(["no_embedder"], tags=1))
    assert out["reasons"] == ["semantic_unavailable"]
    assert out["confidence"] == "medium"


@pytest.mark.parametrize(
    ("code", "reason"),
    [
        ("no_summaries", "summaries_unavailable"),
        ("embedder_error", "source_error:embedder"),
        ("brand_new_code", "source_error:unknown"),
    ],
)
def test_conversation_two_stage_codes(code: str, reason: str) -> None:
    out = from_conversation_search(_two_stage([code], tags=1))
    assert reason in out["reasons"]
    assert out["mode"] == "degraded"


def test_conversation_two_stage_healthy() -> None:
    out = from_conversation_search(_two_stage([], tags=1))
    assert (out["mode"], out["score_kind"]) == ("full", "rank")


# -- from_context_pack ---------------------------------------------------------------


def _slot(name: str, entries: int = 1, degraded: str | None = None) -> dict[str, Any]:
    slot: dict[str, Any] = {
        "name": name,
        "entries": [{"id": f"{name}-{i}"} for i in range(entries)],
    }
    if degraded is not None:
        slot["degraded"] = degraded
    return slot


def test_context_pack_full() -> None:
    out = from_context_pack({"slots": [_slot("memories"), _slot("decisions")]})
    _assert_shape(out)
    assert (out["mode"], out["score_kind"]) == ("full", "none")


def test_context_pack_one_slot_timeout() -> None:
    pack = {"slots": [_slot("memories"), _slot("decisions", 0, "timeout")]}
    out = from_context_pack(pack)
    assert out["reasons"] == ["source_timeout:decisions"]
    assert out["mode"] == "degraded"


def test_context_pack_all_failed_empty() -> None:
    pack = {"slots": [_slot("memories", 0, "error"), _slot("decisions", 0, "timeout")]}
    out = from_context_pack(pack)
    assert out["mode"] == "empty"
    assert "all_sources_failed" in out["reasons"]


@pytest.mark.parametrize("code", ["unavailable", "not_found"])
def test_context_pack_unavailable(code: str) -> None:
    out = from_context_pack({"slots": [_slot("memories"), _slot("notes", 0, code)]})
    assert out["reasons"] == ["source_unavailable:notes"]


def test_context_pack_no_slots() -> None:
    out = from_context_pack({"slots": []})
    assert (out["mode"], out["reasons"]) == ("empty", ["no_match"])


# -- purity --------------------------------------------------------------------------


def test_inputs_not_mutated() -> None:
    labels = _labels(providers=["fts"], degraded={"hybrid": "timeout", "qdrant": "busy"})
    metrics = {"dropped": {"privacy": 2, "archived": 1}}
    search = _search(degraded=True, errors=["hybrid unavailable: x"])
    two_stage = {
        "mode": "two_stage",
        "errors": ["access_not_recorded"],
        "results": [_hit()],
        "recall": copy.deepcopy(labels),
        "recall_metrics": copy.deepcopy(metrics),
    }
    convo = _two_stage(["no_embedder", "no_summaries"])
    pack = {"slots": [_slot("memories"), _slot("notes", 0, "not_found")]}
    snapshot = copy.deepcopy([labels, metrics, search, two_stage, convo, pack])

    from_recall(labels, metrics, result_count=1)
    from_memory_search(search, requested_mode="hybrid")
    from_memory_search(two_stage, requested_mode="two_stage")
    from_conversation_search(convo)
    from_context_pack(pack)

    assert [labels, metrics, search, two_stage, convo, pack] == snapshot


def test_module_imports_no_memfw() -> None:
    tree = ast.parse(Path(health.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] != "memfw", alias.name
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "relative import"
            assert (node.module or "").split(".")[0] != "memfw", node.module


# -- docs agreement ------------------------------------------------------------------


@pytest.mark.parametrize("doc", ["retrieval-health.md", "retrieval-health.zh-CN.md"])
def test_docs_list_every_reason_code(doc: str) -> None:
    path = REPO_ROOT / "docs" / doc
    assert path.is_file(), f"missing {path.relative_to(REPO_ROOT)}"
    text = path.read_text(encoding="utf-8")
    missing = [code for code in REASON_CODES if not re.search(re.escape(f"`{code}`"), text)]
    assert not missing, f"{doc} lacks: {missing}"


# -- affects_mode ---------------------------------------------------------------------

_SAMPLES = {"<name>": "qdrant", "<n>": "3", "<category>": "privacy"}


def _sample(template: str) -> str:
    for key, value in _SAMPLES.items():
        template = template.replace(key, value)
    return template


def test_every_code_has_an_affects_mode_class() -> None:
    assert health.AFFECTS_MODE == ("yes", "no", "fallback", "empty")
    for template, reason in REASON_CODES.items():
        assert reason.affects_mode in health.AFFECTS_MODE, template
        assert reason.meaning
        assert health.affects_mode(_sample(template)) == reason.affects_mode


def test_owner_classification_of_key_codes() -> None:
    expected = {
        "keyword_only": "no",
        "adapters_shadow": "no",
        "access_not_recorded": "no",
        "withheld:<category>:<n>": "no",
        "semantic_unavailable": "yes",
        "semantic_empty": "yes",
        "source_timeout:<name>": "yes",
        "fts_fallback": "fallback",
        "route_fallback": "fallback",
        "no_match": "empty",
        "all_sources_failed": "empty",
        "withheld_only": "empty",
    }
    for template, kind in expected.items():
        assert REASON_CODES[template].affects_mode == kind, template


@pytest.mark.parametrize("template", list(REASON_CODES))
def test_affects_mode_drives_mode(template: str) -> None:
    code = _sample(template)
    kind = REASON_CODES[template].affects_mode
    out = derive(result_count=2, reasons=[code])
    want = {"yes": "degraded", "no": "full", "fallback": "fallback", "empty": "full"}[kind]
    assert out["mode"] == want
    want_confidence = {"degraded": "medium", "full": "high", "fallback": "low"}[want]
    assert out["confidence"] == want_confidence
    if kind == "empty":
        assert code not in out["reasons"]  # only explains an empty answer
    assert derive(result_count=0, reasons=[code])["mode"] == "empty"


def test_affects_mode_rejects_unknown() -> None:
    with pytest.raises(ValueError):
        health.affects_mode("nope")


@pytest.mark.parametrize("doc", ["docs/retrieval-health.md", "docs/retrieval-health.zh-CN.md"])
def test_docs_table_lists_affects_mode(doc: str) -> None:
    text = (Path(__file__).resolve().parents[1] / doc).read_text(encoding="utf-8")
    rows = [line for line in text.splitlines() if line.lstrip().startswith("|")]
    for template, reason in REASON_CODES.items():
        mine = [row for row in rows if f"| `{template}`" in row or f"|`{template}`" in row]
        assert mine, f"{doc}: no table row for {template}"
        cells = [cell.strip() for cell in mine[0].strip().strip("|").split("|")]
        assert f"`{reason.affects_mode}`" in cells, f"{doc}: {template} {cells}"


# -- review round 2: empty answers, canonical table, names ------------------------


@pytest.mark.parametrize("code", ["semantic_unavailable", "source_timeout:hybrid", "fts_fallback"])
def test_empty_with_a_degrading_reason_is_empty_degraded(code: str) -> None:
    out = derive(result_count=0, reasons=[code])
    assert out["mode"] == "empty" and out["confidence"] == "low"
    assert "empty_degraded" in out["reasons"]
    assert "no_match" not in out["reasons"] and "withheld_only" not in out["reasons"]
    assert out["note"] == "没有结果不代表不存在：检索路径有降级或回退。"


def test_empty_degraded_beats_withheld_only_and_informational_does_not_count() -> None:
    both = derive(result_count=0, reasons=["withheld:privacy:2", "semantic_off"])
    assert "empty_degraded" in both["reasons"] and "withheld_only" not in both["reasons"]
    info = derive(result_count=0, reasons=["keyword_only", "adapters_shadow"])
    assert "no_match" in info["reasons"]
    withheld = derive(result_count=0, reasons=["withheld:tenant:1", "keyword_only"])
    assert "withheld_only" in withheld["reasons"]


def test_empty_order_failed_then_page_then_degraded() -> None:
    assert health.export_codes()["empty_order"] == [
        "all_sources_failed",
        "page_empty",
        "empty_degraded",
        "withheld_only",
        "no_match",
    ]
    out = derive(result_count=0, reasons=["source_error:fts"], failed=True, page_empty=True)
    assert [code for code in out["reasons"] if health.affects_mode(code) == "empty"] == [
        "all_sources_failed"
    ]
    page = derive(result_count=0, reasons=["semantic_unavailable"], page_empty=True)
    assert "page_empty" in page["reasons"] and "empty_degraded" not in page["reasons"]
    for case in (out, page):  # exactly one empty code
        assert sum(health.affects_mode(code) == "empty" for code in case["reasons"]) == 1


def test_memory_search_every_source_failed_is_all_sources_failed() -> None:
    payload = _search(
        degraded=True, errors=["hybrid unavailable: boom", "fts index unavailable: x"]
    )
    payload["results"] = []
    out = from_memory_search(payload, requested_mode="hybrid")
    assert out["mode"] == "empty" and "all_sources_failed" in out["reasons"]
    only_hybrid = _search(degraded=True, errors=["hybrid unavailable: boom"])
    only_hybrid["results"] = []
    one = from_memory_search(only_hybrid, requested_mode="hybrid")
    assert "empty_degraded" in one["reasons"] and "all_sources_failed" not in one["reasons"]
    stale = {"mode": "fts", "degraded": True, "errors": ["index_stale"], "results": []}
    gone = from_memory_search(stale, requested_mode="fts")
    assert gone["reasons"] == ["all_sources_failed", "source_unavailable:fts"]


def test_conversation_page_beyond_range_is_page_empty() -> None:
    payload = _two_stage([])
    payload["sessions"] = []
    payload["candidates"] = {"fts": 3, "semantic": 0, "tags": 0, "total": 3}
    out = from_conversation_search(payload)
    assert out["mode"] == "empty" and "page_empty" in out["reasons"]
    assert "no_match" not in out["reasons"]
    payload["candidates"] = {"fts": 0, "semantic": 0, "tags": 0, "total": 0}
    assert "no_match" in from_conversation_search(payload)["reasons"]


def test_two_stage_unexplained_degraded_page_is_source_fallback() -> None:
    payload = {
        "mode": "two_stage",
        "degraded": True,
        "errors": [],
        "results": [{"id": "m1"}],
        "recall": _labels(providers=["fts", "hybrid"]),
        "recall_metrics": {"dropped": {}},
    }
    out = from_memory_search(payload, requested_mode="two_stage")
    assert out["reasons"] == ["source_fallback:unknown"]
    assert (out["mode"], out["confidence"]) == ("degraded", "medium")
    payload["degraded"] = False
    assert from_memory_search(payload, requested_mode="two_stage")["mode"] == "full"


def test_names_are_lowercase() -> None:
    assert health.NAME_PATTERN == "[a-z][a-z0-9_.-]{0,63}"
    labels = _labels(providers=["fts"], degraded={"QDrant": "timeout"})
    out = from_recall(labels, None, result_count=1)
    assert "source_timeout:qdrant" in out["reasons"]
    for bad in ("source_timeout:Qdrant", "source_timeout:9x", "source_timeout:_x"):
        with pytest.raises(ValueError):
            check_reason(bad)


def test_score_kinds_include_keyword_and_none() -> None:
    assert health.SCORE_KINDS == ("rank", "bm25", "cosine", "mixed", "keyword", "none")
    assert derive(result_count=1, score_kind="keyword")["score_kind"] == "keyword"


def test_client_codes_are_in_the_canonical_table() -> None:
    for template, kind in {
        "grep_fallback": "fallback",
        "meta_index_stale": "yes",
        "empty_degraded": "empty",
        "page_empty": "empty",
        "source_fallback:<name>": "yes",
        "vectors_partial": "yes",
        "semantic_partial": "yes",
    }.items():
        assert REASON_CODES[template].affects_mode == kind, template
    assert "partial semantic results" in REASON_CODES["semantic_partial"].meaning


def test_export_codes_matches_the_committed_json() -> None:
    import json

    data = health.export_codes()
    json.dumps(data)  # serialisable
    assert set(data["codes"]) == set(REASON_CODES)
    committed = Path(__file__).resolve().parents[1] / "docs" / "retrieval-health-codes.json"
    assert committed.read_text(encoding="utf-8") == health.export_codes_json(), (
        "regenerate: python -m memfw.retrieval.health > docs/retrieval-health-codes.json"
    )
    assert json.loads(committed.read_text(encoding="utf-8")) == data


def test_health_imports_no_heavy_memfw_modules() -> None:
    import subprocess
    import sys

    code = (
        "import sys, memfw.retrieval.health; "
        "print(sorted(m for m in sys.modules if m.startswith('memfw')))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    ).stdout
    assert out.strip() == "['memfw', 'memfw.retrieval', 'memfw.retrieval.health']"


def test_two_stage_access_error_alone_is_informational() -> None:
    payload = {
        "mode": "two_stage",
        "degraded": True,  # set by the access-event failure only
        "errors": ["access_not_recorded"],
        "results": [{"id": "m1"}],
        "recall": _labels(providers=["fts", "hybrid"]),
        "recall_metrics": {"dropped": {}},
    }
    out = from_memory_search(payload, requested_mode="two_stage")
    assert (out["mode"], out["confidence"]) == ("full", "high")
    assert out["reasons"] == ["access_not_recorded"]


def test_two_stage_degraded_sources_name_the_fallback() -> None:
    payload = {
        "mode": "two_stage",
        "degraded": True,
        "degraded_sources": ["Hybrid", "qdrant"],
        "errors": ["access_not_recorded"],
        "results": [{"id": "m1"}],
        "recall": _labels(providers=["fts", "hybrid", "qdrant"]),
        "recall_metrics": {"dropped": {}},
    }
    out = from_memory_search(payload, requested_mode="two_stage")
    assert out["reasons"] == [
        "access_not_recorded",
        "source_fallback:hybrid",
        "source_fallback:qdrant",
    ]
    assert (out["mode"], out["confidence"]) == ("degraded", "medium")
