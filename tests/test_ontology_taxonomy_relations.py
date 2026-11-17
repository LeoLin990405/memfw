"""K1 (taxonomy ``broader``) and K2 (relation-type table) tests.

Both are additive: a vault without ``broader`` gets the same lint, search and
context output as before, and nothing expands a query along ``broader``.
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from memfw.core.repository import Repository
from memfw.ontology import (
    OntologyError,
    builtin_relation_table,
    import_seed,
    lint_concepts,
    lint_relations,
    load_concepts,
    merge_concepts,
    narrower_index,
)
from memfw.ontology.cli import app as concepts_app
from memfw.ontology.model import RELATIONS
from memfw.ontology.relations import check_relation_table, parse_relation_table

TABLE_PATH = Path(__file__).resolve().parents[1] / "src" / "memfw" / "ontology" / "relations.yaml"


def _concept(root: Path, slug: str, oid: str, **extra: Any) -> Path:
    meta: dict[str, Any] = {
        "oid": oid,
        "class": "Concept",
        "pref_label": slug.replace("-", " "),
        "alt_labels": [],
        "same_as": [],
        "status": "active",
        "merged_into": None,
    }
    meta.update(extra)
    path = root / "concepts" / "Concept" / f"{slug}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    dumped = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False).rstrip()
    path.write_text(f"---\n{dumped}\n---\n\n{slug}\n", encoding="utf-8")
    return path


def _memory(root: Path, memory_id: str, **meta: Any) -> Path:
    path = root / "memories" / f"{memory_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    front = {"id": memory_id, "title": memory_id, **meta}
    dumped = yaml.safe_dump(front, sort_keys=False).rstrip()
    path.write_text(f"---\n{dumped}\n---\n\nbody\n", encoding="utf-8")
    return path


def _kinds(items: list[dict[str, Any]]) -> list[str]:
    return [item["kind"] for item in items]


# --- K2: the relation-type table ---------------------------------------------------------


def test_builtin_table_loads_and_is_versioned() -> None:
    table = builtin_relation_table()
    assert table.version == 1
    expected = {
        "about",
        "supersedes",
        "superseded_by",
        "evidence",
        "cites",
        "observed_in",
        "treatment_of",
        "outcome_of",
        "experience_of",
        "related",
        "broader",
        "narrower",
    }
    assert expected <= table.names()


def test_table_covers_exactly_the_concept_relation_keys() -> None:
    # The concept-file relation keys lint already accepts, plus broader: the
    # table names no relation that is not already written somewhere.
    assert builtin_relation_table().written_on("concept") == RELATIONS | {"broader"}


def test_every_relation_is_filtered_by_privacy_and_tenant() -> None:
    for relation in builtin_relation_table().relations:
        assert relation.filter_privacy and relation.filter_tenant, relation.name


def test_table_rows_state_their_constraints() -> None:
    table = builtin_relation_table()
    about = table.get("about")
    assert about is not None and about.epistemic_max == "inference"
    assert about.targets == ("concept",)
    related = table.get("related")
    assert related is not None and related.derived and not related.typed
    narrower = table.get("narrower")
    assert narrower is not None and narrower.derived and narrower.fields == {}
    assert narrower.inverse == "broader"
    broader = table.get("broader")
    assert broader is not None and broader.cycles is False and broader.inverse == "narrower"
    supersedes = table.get("supersedes")
    assert supersedes is not None and supersedes.inverse == "superseded_by"


def _raw_table() -> dict[str, Any]:
    return yaml.safe_load(TABLE_PATH.read_text(encoding="utf-8"))


def _row(data: dict[str, Any], name: str) -> dict[str, Any]:
    return next(row for row in data["relations"] if row["name"] == name)


@pytest.mark.parametrize(
    ("mutate", "kind"),
    [
        (lambda d: d.update(version=2), "relation_table_version"),
        (lambda d: _row(d, "about")["filter"].update(tenant=False), "relation_unfiltered"),
        (lambda d: _row(d, "about")["filter"].update(privacy=False), "relation_unfiltered"),
        (lambda d: _row(d, "about").update(to=["planet"]), "relation_unknown_node_kind"),
        (lambda d: _row(d, "broader").update(inverse="smaller"), "relation_unknown_type"),
        (lambda d: _row(d, "about").update(epistemic_max="certain"), "relation_table_invalid"),
        (lambda d: _row(d, "about").update(colour="red"), "relation_table_invalid"),
        (
            lambda d: d["relations"].append(copy.deepcopy(_row(d, "about"))),
            "relation_table_duplicate",
        ),
        (lambda d: _row(d, "broader").update(inverse="about"), "relation_table_invalid"),
        (lambda d: _row(d, "about").update(fields={}), "relation_table_invalid"),
        (lambda d: d.update(extra=1), "relation_table_invalid"),
    ],
)
def test_table_validation_fails_closed(mutate: Any, kind: str) -> None:
    data = _raw_table()
    assert check_relation_table(data) == []
    mutate(data)
    assert kind in _kinds(check_relation_table(data))
    with pytest.raises(OntologyError):
        parse_relation_table(data)


def test_relations_command_prints_the_table() -> None:
    result = CliRunner().invoke(concepts_app, ["relations"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["version"] == 1
    assert {row["name"] for row in payload["relations"]} == builtin_relation_table().names()


# --- K1: broader / narrower ---------------------------------------------------------------


def test_broader_is_accepted_and_narrower_is_derived(tmp_path: Path) -> None:
    _concept(tmp_path, "food", "concept:food")
    _concept(tmp_path, "fruit", "concept:fruit", broader=["concept:food"])
    _concept(tmp_path, "apple", "concept:apple", broader="concept:fruit")
    _concept(tmp_path, "pear", "concept:pear", broader=["concept:fruit"])
    concepts = load_concepts(tmp_path)
    assert lint_concepts(concepts) == []
    assert narrower_index(concepts) == {
        "concept:food": ("concept:fruit",),
        "concept:fruit": ("concept:apple", "concept:pear"),
    }


def test_broader_lint_codes(tmp_path: Path) -> None:
    _concept(tmp_path, "a", "concept:a", broader=["concept:b"])
    _concept(tmp_path, "b", "concept:b", broader=["concept:c"])
    _concept(tmp_path, "c", "concept:c", broader=["concept:a"])
    _concept(tmp_path, "self", "concept:self", broader=["concept:self"])
    _concept(tmp_path, "lost", "concept:lost", broader=["concept:missing"])
    _concept(tmp_path, "bad", "concept:bad", broader=[{"oid": "concept:a"}])
    _concept(tmp_path, "old", "concept:old", merged_into="concept:new")
    _concept(tmp_path, "new", "concept:new")
    _concept(tmp_path, "child", "concept:child", broader=["concept:old"])
    review = lint_concepts(load_concepts(tmp_path))
    by_kind: dict[str, list[dict[str, Any]]] = {}
    for item in review:
        by_kind.setdefault(item["kind"], []).append(item)
    cycles = sorted(tuple(item["oids"]) for item in by_kind["broader_cycle"])
    assert cycles == [("concept:a", "concept:b", "concept:c"), ("concept:self",)]
    assert by_kind["broader_dangling"] == [
        {"kind": "broader_dangling", "oid": "concept:lost", "detail": "concept:missing"}
    ]
    assert by_kind["broader_invalid"] == [
        {"kind": "broader_invalid", "oid": "concept:bad", "detail": "broader"}
    ]
    assert by_kind["broader_merged"] == [
        {
            "kind": "broader_merged",
            "oid": "concept:child",
            "detail": "concept:old",
            "resolved": "concept:new",
        }
    ]
    # broader is owned by the taxonomy lint, not the generic relation check.
    assert "unknown_relation" not in by_kind
    assert "dangling_oid" not in by_kind
    assert narrower_index(load_concepts(tmp_path))["concept:new"] == ("concept:child",)


def test_cycle_through_a_merge_is_reported(tmp_path: Path) -> None:
    _concept(tmp_path, "x", "concept:x", broader=["concept:alias"])
    _concept(tmp_path, "alias", "concept:alias", merged_into="concept:y")
    _concept(tmp_path, "y", "concept:y", broader=["concept:x"])
    review = lint_concepts(load_concepts(tmp_path))
    cycles = [item for item in review if item["kind"] == "broader_cycle"]
    assert [item["oids"] for item in cycles] == [["concept:x", "concept:y"]]


def test_vault_without_broader_gets_the_old_review(tmp_path: Path) -> None:
    # Same review as before K1 for every pre-existing lint kind.
    _concept(tmp_path, "one", "concept:one", merged_into="concept:missing")
    _concept(tmp_path, "two", "concept:two", part_of=["concept:nowhere"], colour="red")
    _concept(tmp_path, "three", "concept:three", status="weird")
    review = lint_concepts(load_concepts(tmp_path))
    assert review == [
        {"kind": "dangling_oid", "oid": "concept:one", "detail": "concept:missing"},
        {"kind": "unknown_status", "oid": "concept:three", "detail": "weird"},
        {"kind": "dangling_oid", "oid": "concept:two", "detail": "concept:nowhere"},
        {"kind": "unknown_relation", "oid": "concept:two", "detail": "colour"},
        {"kind": "dangling_oid", "oid": "concept:two", "detail": "red"},
    ]


def test_handwritten_narrower_stays_an_unknown_relation(tmp_path: Path) -> None:
    _concept(tmp_path, "top", "concept:top", narrower=["concept:top"])
    review = lint_concepts(load_concepts(tmp_path))
    assert {"kind": "unknown_relation", "oid": "concept:top", "detail": "narrower"} in review


def test_merge_keeps_broader_and_old_files_keep_their_bytes(tmp_path: Path) -> None:
    plain = _concept(tmp_path, "plain", "concept:plain")
    before = plain.read_bytes()
    _concept(tmp_path, "target", "concept:target")
    _concept(tmp_path, "kid", "concept:kid", broader=["concept:target"])
    load_concepts(tmp_path)
    lint_concepts(load_concepts(tmp_path))
    lint_relations(tmp_path)
    assert plain.read_bytes() == before
    merge_concepts(tmp_path, "concept:kid", "concept:plain", apply=True)
    kid = load_concepts(tmp_path)["concept:kid"]
    assert kid.extras["broader"] == ["concept:target"]
    assert kid.merged_into == "concept:plain"


def test_seed_carries_broader(tmp_path: Path) -> None:
    seed = tmp_path / "seed.yaml"
    seed.write_text(
        yaml.safe_dump(
            {
                "entries": [
                    {"oid": "concept:food", "class": "Concept"},
                    {"oid": "concept:fruit", "class": "Concept", "broader": ["concept:food"]},
                ]
            }
        ),
        encoding="utf-8",
    )
    result = import_seed(seed, tmp_path / "out", apply=True)
    assert result["review"] == []
    concepts = load_concepts(tmp_path / "out")
    assert narrower_index(concepts) == {"concept:food": ("concept:fruit",)}
    plain = tmp_path / "plain.yaml"
    plain.write_text(yaml.safe_dump({"entries": [{"oid": "concept:x", "class": "Concept"}]}))
    import_seed(plain, tmp_path / "plain", apply=True)
    text = (tmp_path / "plain" / "concepts" / "Concept" / "x.md").read_text(encoding="utf-8")
    assert "broader" not in text


def test_show_adds_taxonomy_keys_only_when_used(tmp_path: Path) -> None:
    _concept(tmp_path, "food", "concept:food")
    _concept(tmp_path, "fruit", "concept:fruit", broader=["concept:food"])
    _concept(tmp_path, "rock", "concept:rock")
    runner = CliRunner()

    def show(oid: str) -> dict[str, Any]:
        result = runner.invoke(concepts_app, ["show", oid, "--vault", str(tmp_path)])
        assert result.exit_code == 0, result.output
        return json.loads(result.output)

    assert show("concept:fruit")["broader"] == ["concept:food"]
    assert show("concept:food")["narrower"] == ["concept:fruit"]
    rock = show("concept:rock")
    assert "broader" not in rock and "narrower" not in rock
    assert set(rock) == {
        "alt_labels",
        "class",
        "merged_into",
        "oid",
        "pref_label",
        "same_as",
        "status",
    }


# --- K2: lint-relations -------------------------------------------------------------------


def test_lint_relations_reports_dangling_cross_tenant_and_unknown(tmp_path: Path) -> None:
    _concept(tmp_path, "tool", "concept:tool", colour="red")
    _memory(tmp_path, "m-a", tenant="alpha", about=["concept:tool", "concept:ghost"])
    _memory(tmp_path, "m-b", tenant="beta", evidence=["m-a", "https://example.org/x"])
    _memory(tmp_path, "m-c", tenant="alpha", superseded_by="m-b")
    _memory(tmp_path, "m-d", tenant="alpha", superseded="m-gone")
    _memory(
        tmp_path,
        "m-e",
        tenant="alpha",
        evidence=["m-a", "m-e"],
        experience={"task": "t", "evidence": ["m-b"]},
    )
    review = lint_relations(tmp_path)
    assert review == [
        {
            "kind": "relation_unknown_type",
            "oid": "concept:tool",
            "relation": "colour",
            "detail": "colour",
        },
        {
            "kind": "relation_dangling",
            "memory_id": "m-a",
            "relation": "about",
            "detail": "concept:ghost",
        },
        {
            "kind": "relation_cross_tenant",
            "memory_id": "m-b",
            "relation": "evidence",
            "detail": "m-a",
        },
        {
            "kind": "relation_cross_tenant",
            "memory_id": "m-c",
            "relation": "superseded_by",
            "detail": "m-b",
        },
        {
            "kind": "relation_dangling",
            "memory_id": "m-d",
            "relation": "superseded_by",
            "detail": "m-gone",
        },
        {
            "kind": "relation_cross_tenant",
            "memory_id": "m-e",
            "relation": "experience_of",
            "detail": "m-b",
        },
    ]


def test_lint_relations_skips_about_without_concepts_and_writes_nothing(tmp_path: Path) -> None:
    path = _memory(tmp_path, "m-a", about=["concept:ghost"])
    before = path.read_bytes()
    assert lint_relations(tmp_path) == []
    assert path.read_bytes() == before
    assert not (tmp_path / "concepts").exists()


def test_lint_relations_includes_taxonomy_and_cli(tmp_path: Path) -> None:
    _concept(tmp_path, "a", "concept:a", broader=["concept:a"])
    result = CliRunner().invoke(concepts_app, ["lint-relations", "--vault", str(tmp_path)])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["table_version"] == 1
    assert _kinds(payload["review"]) == ["broader_cycle"]


# --- no change to search / context ----------------------------------------------------------


def test_search_and_topics_ignore_broader(tmp_path: Path) -> None:
    from memfw.ontology.expand import matching_memories
    from memfw.runtime.context_sources import concept_catalogue

    repository = Repository(tmp_path / "vault")
    repository.init()
    root = repository.root
    _concept(root, "food", "concept:food")
    _concept(root, "fruit", "concept:fruit", broader=["concept:food"], alt_labels=["fruit"])
    repository.add("Apple", "an apple", memory_id="mem-fruit", about=["concept:fruit"])
    repository.add("Bread", "bread", memory_id="mem-food", about=["concept:food"])
    hits, _ = matching_memories(root, "food")
    assert [memory_id for memory_id, _ in hits] == ["mem-food"]
    hits, _ = matching_memories(root, "fruit")
    assert [memory_id for memory_id, _ in hits] == ["mem-fruit"]
    catalogue = concept_catalogue(root)
    assert catalogue.canonical["concept:fruit"] == "concept:fruit"


# --- opt-in: byte-identical against a base ref ----------------------------------------------


@pytest.mark.skipif(
    not os.environ.get("MEMFW_ARCH_BASE_REF"),
    reason="opt-in (slow): set MEMFW_ARCH_BASE_REF to compare against that ref",
)
def test_byte_identical_against_base_ref() -> None:
    from tests.harness import byte_identical

    results = byte_identical.compare(
        "tests.harness.taxonomy_probe:taxonomy_probe",
        [{"broader": False}, {"broader": True}],
        old_ref=os.environ["MEMFW_ARCH_BASE_REF"],
    )
    for result in results:
        assert result.identical, result.diff
