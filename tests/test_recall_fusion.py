"""Weighted RRF is deterministic, and routing sends exact queries to FTS."""

from __future__ import annotations

import itertools
import random

import pytest

from memfw.recall.fusion import rrf_fuse
from memfw.recall.routing import AllProvidersRouter, DefaultRouter, classify_query


def test_single_list_keeps_its_order() -> None:
    fused = rrf_fuse([("fts", ["c", "a", "b"])])
    assert [hit.memory_id for hit in fused] == ["c", "a", "b"]
    assert fused[0].ranks == {"fts": 1}
    assert fused[0].score == pytest.approx(1 / 61)


def test_overlap_outranks_single_hits() -> None:
    fused = rrf_fuse([("fts", ["a", "b"]), ("qmd", ["b", "c"])])
    assert [hit.memory_id for hit in fused] == ["b", "a", "c"]
    assert fused[0].providers == ("fts", "qmd")
    assert fused[0].ranks == {"fts": 2, "qmd": 1}


def test_weights_change_the_winner() -> None:
    lists = [("fts", ["a"]), ("qmd", ["b"])]
    assert rrf_fuse(lists, weights={"fts": 1.0, "qmd": 3.0})[0].memory_id == "b"
    assert rrf_fuse(lists, weights={"fts": 3.0, "qmd": 1.0})[0].memory_id == "a"


def test_zero_weight_keeps_provider_label_without_score() -> None:
    fused = rrf_fuse([("fts", ["a"]), ("qmd", ["a"])], weights={"qmd": 0.0})
    assert fused[0].providers == ("fts", "qmd")
    assert fused[0].score == pytest.approx(1 / 61)


def test_item_weights_downrank() -> None:
    fused = rrf_fuse([("fts", ["a", "b"])], item_weights={"a": 0.1})
    assert [hit.memory_id for hit in fused] == ["b", "a"]


def test_ties_break_by_rank_then_provider_order_then_id() -> None:
    # a and b both score 1/61: rank equal, so provider order decides.
    fused = rrf_fuse([("fts", ["a"]), ("qmd", ["b"])])
    assert [hit.memory_id for hit in fused] == ["a", "b"]
    fused = rrf_fuse([("qmd", ["b"]), ("fts", ["a"])])
    assert [hit.memory_id for hit in fused] == ["b", "a"]
    # Equal score, equal best rank, both first seen in "p": the id decides.
    fused = rrf_fuse([("p", ["y", "x"]), ("q", ["x", "y"])])
    assert [hit.memory_id for hit in fused] == ["x", "y"]


def test_result_is_independent_of_input_mapping_order() -> None:
    rng = random.Random(7)
    ids = [f"m{index:02d}" for index in range(30)]
    lists = []
    for name in ("fts", "hybrid", "multihop"):
        shuffled = ids[:]
        rng.shuffle(shuffled)
        lists.append((name, shuffled[:20]))
    weights = {"fts": 1.0, "hybrid": 0.7, "multihop": 0.4}
    baseline = [(h.memory_id, h.score) for h in rrf_fuse(lists, weights=weights)]
    for _ in range(5):
        shuffled_weights = dict(rng.sample(sorted(weights.items()), k=3))
        again = [(h.memory_id, h.score) for h in rrf_fuse(lists, weights=shuffled_weights)]
        assert again == baseline


def test_permuting_equal_lists_only_moves_ties_by_provider_order() -> None:
    lists = [("a", ["1", "2"]), ("b", ["2", "1"]), ("c", ["3"])]
    outputs = set()
    for perm in itertools.permutations(lists):
        outputs.add(tuple(hit.memory_id for hit in rrf_fuse(list(perm))))
    # "1" and "2" always tie on score and best rank; "3" is always last.
    assert all(order[2] == "3" for order in outputs)
    assert outputs <= {("1", "2", "3"), ("2", "1", "3")}


def test_duplicates_within_a_list_keep_first_position_and_limit() -> None:
    fused = rrf_fuse([("fts", ["a", "a", "b"])], limit=1)
    assert [hit.memory_id for hit in fused] == ["a"]
    assert rrf_fuse([("fts", ["a", "a", "b"])])[1].ranks == {"fts": 2}


def test_bad_arguments() -> None:
    with pytest.raises(ValueError):
        rrf_fuse([("fts", ["a"])], k=0)
    with pytest.raises(ValueError):
        rrf_fuse([("fts", ["a"])], weights={"fts": -1.0})
    assert rrf_fuse([]) == []


@pytest.mark.parametrize(
    "query",
    [
        '"NAS1 cron pause"',
        "「迁移」",
        "sha 9c99bc2a1f00",
        "ticket 1234567",
        "memfw v0.2.0",
        "SRV100 status",
        "https://example.invalid/x",
        "src/memfw/cli.py",
        "notes.md",
        "2026 11 16",
        "Leo",
        "Ada Lovelace",
        "nas1",
    ],
)
def test_exact_queries(query: str) -> None:
    assert classify_query(query) == "exact"


@pytest.mark.parametrize(
    "query",
    [
        "how do we pause the backup job before moving disks",
        "memory retrieval design notes",
        "为什么迁移之前要先暂停定时任务",
        "",
    ],
)
def test_semantic_queries(query: str) -> None:
    assert classify_query(query) == "semantic"


def test_router_prefers_lexical_for_exact() -> None:
    router = DefaultRouter()
    names = ("hybrid", "fts", "multihop")
    exact = router.route('"x y"', names, lexical=frozenset({"fts"}))
    assert exact.kind == "exact"
    assert exact.providers == ("fts",)
    semantic = router.route("what changed in the backup plan", names, lexical=frozenset({"fts"}))
    assert semantic.providers == names
    # No lexical provider configured: exact goes everywhere.
    assert router.route('"x"', names, lexical=frozenset()).providers == names


def test_all_providers_router() -> None:
    decision = AllProvidersRouter().route('"x"', ("a", "b"), lexical=frozenset({"a"}))
    assert decision.providers == ("a", "b")
    assert decision.kind == "exact"
