"""K3 (typed-edge projection) and K4 (bounded path patterns) tests, plus the K3 prerequisites.

The projection is rebuilt from vault fields, stored behind the
``ProjectionStore`` port and read only through ``memfw.runtime.graph``,
which applies tenant, clearance, allowed sources and a live re-check of
every memory. Nothing here may leak a hidden id.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from memfw.cli import app as cli_app
from memfw.core.repository import Repository
from memfw.edges import (
    FILENAME,
    SqliteProjectionStore,
    collect_edges,
    projection_path,
    vault_store,
)
from memfw.ontology import builtin_relation_table, import_seed, lint_concepts, load_concepts
from memfw.ontology.model import OntologyError
from memfw.ontology.relations import FieldMap, parse_relation_table
from memfw.ports import Edge, EdgeScope, ProjectionStore
from memfw.ports.patterns import (
    MAX_DEPTH,
    MAX_FANOUT,
    MAX_LIMIT,
    PatternError,
    match,
    parse_path,
)
from memfw.ports.reference import MemoryProjectionStore
from memfw.ports.testing import tree_digest
from memfw.runtime.graph import GraphError, graph_match, rebuild_edge_projection

TABLE_PATH = Path(__file__).resolve().parents[1] / "src" / "memfw" / "ontology" / "relations.yaml"
EVERY = frozenset({"public", "shared", "local-private"})
PUBLIC = frozenset({"public"})


@pytest.fixture(autouse=True)
def _home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """The projection lives under the home directory: give every test its own."""

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    return home


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


def _vault(tmp_path: Path) -> Repository:
    repository = Repository(tmp_path / "vault")
    repository.init()
    return repository


def _add(repository: Repository, memory_id: str, body: str = "body", **fields: Any) -> None:
    fields.setdefault("privacy", "public")
    repository.add(memory_id, body, memory_id=memory_id, **fields)


def _rewrite(repository: Repository, memory_id: str, **front: Any) -> None:
    """Hand-edit a memory's front matter (keys set to ``None`` are removed)."""

    path = repository.memories_dir / f"{memory_id}.md"
    text = path.read_text(encoding="utf-8")
    _, head, body = text.split("---\n", 2)
    meta = yaml.safe_load(head)
    for key, value in front.items():
        if value is None:
            meta.pop(key, None)
        else:
            meta[key] = value
    path.write_text("---\n" + yaml.safe_dump(meta, sort_keys=False) + "---\n" + body, "utf-8")


def _keys(edges: list[Edge]) -> set[tuple[str, str, str]]:
    return {edge.key for edge in edges}


def _ids(payload: dict[str, Any]) -> list[list[str]]:
    return [[node["id"] for node in path["nodes"]] for path in payload["paths"]]


@pytest.fixture
def food(tmp_path: Path) -> Repository:
    """Concepts food ⊃ fruit, memories about them at every privacy level and two tenants."""

    repository = _vault(tmp_path)
    root = repository.root
    _concept(root, "food", "concept:food")
    _concept(root, "fruit", "concept:fruit", broader=["concept:food"], alt_labels=["fruits"])
    _add(repository, "m-apple", "apple, see kh:notes/fruit#apple.", about=["concept:fruit"])
    _add(repository, "m-bread", about=["concept:food"])
    _add(repository, "m-shared", about=["concept:food"], privacy="shared")
    _add(repository, "m-secret", about=["concept:food"], privacy="local-private")
    _add(repository, "m-other", about=["concept:food"], tenant="other")
    return repository


# --- K3 prerequisites (K1/K2 review items) -------------------------------------------


def test_relation_types_are_hashable_and_immutable() -> None:
    table = builtin_relation_table()
    rows = set(table.relations)
    assert len(rows) == len(table.relations)
    about = table.get("about")
    assert about is not None and hash(about) == hash(table.get("about"))
    assert isinstance(about.fields, FieldMap) and about.fields == {
        "memory": ("about",),
        "concept": ("about",),
    }
    with pytest.raises(TypeError):
        about.fields["memory"] = ("x",)  # type: ignore[index]
    assert {table: 1}[table] == 1  # the cached table is hashable too
    assert table is builtin_relation_table()


def test_table_names_the_open_question_relations() -> None:
    table = builtin_relation_table()
    conflicts = table.get("conflicts")
    resolved = table.get("resolved_by")
    assert conflicts is not None and conflicts.fields == {"memory": ("open_question.conflicts",)}
    assert resolved is not None and resolved.fields == {"memory": ("open_question.resolved_by",)}
    assert conflicts.sources == conflicts.targets == ("memory",)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(version=True),
        lambda d: d.update(version=1.0),
        lambda d: d["relations"][0].update(to=[["concept"]]),
        lambda d: d["relations"][0].update({"from": [{"memory": 1}]}),
    ],
)
def test_loader_refuses_bool_versions_and_nested_kinds(mutate: Any) -> None:
    data = yaml.safe_load(TABLE_PATH.read_text(encoding="utf-8"))
    mutate(data)
    with pytest.raises(OntologyError):
        parse_relation_table(data)


@pytest.mark.parametrize("broader", [None, {}, [], [1], ["concept:food", None], ""])
def test_import_seed_copies_only_well_formed_broader(tmp_path: Path, broader: Any) -> None:
    seed = tmp_path / "seed.yaml"
    seed.write_text(
        yaml.safe_dump(
            {
                "entries": [
                    {"oid": "concept:food", "class": "Concept"},
                    {"oid": "concept:fruit", "class": "Concept", "broader": broader},
                ]
            }
        ),
        encoding="utf-8",
    )
    import_seed(seed, tmp_path / "out", apply=True)
    text = (tmp_path / "out" / "concepts" / "Concept" / "fruit.md").read_text(encoding="utf-8")
    assert "broader" not in text


def test_broader_on_a_merged_concept_is_reported_and_its_cycle_found(tmp_path: Path) -> None:
    # old (merged into top) says broader: mid; mid says broader: top -> top ⊂ mid ⊂ top.
    _concept(tmp_path, "top", "concept:top")
    _concept(tmp_path, "mid", "concept:mid", broader=["concept:top"])
    _concept(tmp_path, "old", "concept:old", merged_into="concept:top", broader=["concept:mid"])
    review = lint_concepts(load_concepts(tmp_path))
    assert {
        "kind": "broader_on_merged",
        "oid": "concept:old",
        "detail": "broader",
        "resolved": "concept:top",
    } in review
    cycles = [item["oids"] for item in review if item["kind"] == "broader_cycle"]
    assert cycles == [["concept:mid", "concept:top"]]


def test_lint_docstring_no_longer_promises_every_exit_hides_cross_tenant() -> None:
    from memfw.ontology import relations_lint

    doc = relations_lint.__doc__ or ""
    assert "would never be returned" not in doc
    assert '``""`` and ``fleet``' in doc


# --- K3: what the projection holds ------------------------------------------------------


def test_every_written_relation_becomes_a_typed_edge(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    root = repository.root
    _concept(root, "food", "concept:food")
    _concept(root, "fruit", "concept:fruit", broader=["concept:food"], part_of=["concept:food"])
    _concept(root, "old-fruit", "concept:old-fruit", merged_into="concept:fruit")
    _concept(root, "plan", "concept:plan", supersedes=["concept:old-plan"])
    _concept(root, "old-plan", "concept:old-plan")
    _add(repository, "m-a", "body kh:notes/a#one", about=["concept:old-fruit", "fruits-label"])
    _add(repository, "m-b", applies_to=["concept:food"], related=["m-a"])
    _add(repository, "m-c", evidence=["m-a", "att:photo.png#p1", "kh:notes/b", "https://x.test"])
    _add(repository, "m-d")
    _rewrite(repository, "m-d", superseded_by="m-c")
    _add(
        repository,
        "q-1",
        memory_type="open-question",
        open_question={"question": "which?", "conflicts": ["m-a", "m-b"]},
    )
    _rewrite(
        repository,
        "q-1",
        open_question={"question": "which?", "conflicts": ["m-a", "m-b"], "resolved_by": "m-c"},
    )
    edges = collect_edges(repository)
    keys = _keys(edges)
    assert {
        ("about", "m-a", "concept:fruit"),  # merged concept counts for its target
        ("about", "m-a", "fruits-label"),  # not a concept: a concept node named by the value
        ("applies_to", "m-b", "concept:food"),
        ("related", "m-b", "m-a"),
        ("evidence", "m-c", "m-a"),
        ("evidence", "m-c", "att:photo.png#p1"),
        ("observed_in", "m-c", "att:photo.png#p1"),
        ("evidence", "m-c", "kh:notes/b"),
        ("cites", "m-c", "kh:notes/b"),
        ("evidence", "m-c", "https://x.test"),
        ("cites", "m-a", "kh:notes/a#one"),
        ("superseded_by", "m-d", "m-c"),
        ("conflicts", "q-1", "m-a"),
        ("conflicts", "q-1", "m-b"),
        ("resolved_by", "q-1", "m-c"),
        ("broader", "concept:fruit", "concept:food"),
        ("part_of", "concept:fruit", "concept:food"),
        ("superseded_by", "concept:old-plan", "concept:plan"),  # supersedes read backwards
    } <= keys
    names = builtin_relation_table().names()
    assert {edge.relation for edge in edges} <= names
    assert "supersedes" not in {edge.relation for edge in edges}
    kinds = {edge.key: (edge.source_kind, edge.target_kind) for edge in edges}
    assert kinds[("observed_in", "m-c", "att:photo.png#p1")] == ("memory", "attachment")
    assert kinds[("evidence", "m-c", "https://x.test")] == ("memory", "external")
    by_key = {edge.key: edge for edge in edges}
    assert by_key[("cites", "m-a", "kh:notes/a#one")].field == "body"
    assert by_key[("conflicts", "q-1", "m-a")].field == "open_question.conflicts"
    assert by_key[("about", "m-a", "concept:fruit")].epistemic_max == "inference"


def test_edges_carry_both_ends_tenant_privacy_and_validity(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    _add(repository, "m-a", privacy="shared", tenant="t1")
    _add(repository, "m-b", privacy="public", tenant="t1")
    _rewrite(repository, "m-a", evidence=["m-b"], valid_from="2026-01-01T00:00:00Z")
    _rewrite(repository, "m-b", valid_to="2026-06-01T00:00:00Z")
    (edge,) = [e for e in collect_edges(repository) if e.relation == "evidence"]
    assert (edge.source_tenant, edge.target_tenant) == ("t1", "t1")
    assert (edge.source_privacy, edge.target_privacy) == ("shared", "public")
    assert edge.privacy == "shared"
    assert edge.valid_from is not None and edge.valid_from.startswith("2026-01-01")
    assert edge.valid_to is not None and edge.valid_to.startswith("2026-06-01")


def test_privacy_follows_the_mm0_rule(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    _concept(repository.root, "c", "concept:c")
    _add(repository, "m-legacy", about=["concept:c"])
    _rewrite(repository, "m-legacy", privacy=None)  # no field: the legacy default
    _add(repository, "m-tag", about=["concept:c"], tags=["sensitivity:private"])
    _add(repository, "m-key", about=["concept:c"])
    _rewrite(repository, "m-key", sensitivity="private")
    _add(repository, "m-odd", about=["concept:c"])
    _rewrite(repository, "m-odd", privacy="secret-ish")
    levels = {e.source: e.source_privacy for e in collect_edges(repository)}
    assert levels == {
        "m-legacy": "local-private",
        "m-tag": "local-private",
        "m-key": "local-private",
        "m-odd": "unknown",
    }
    config = yaml.safe_load(repository.config_path.read_text(encoding="utf-8"))
    config["legacy_privacy_default"] = "public"
    repository.config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    levels = {e.source: e.source_privacy for e in collect_edges(repository)}
    assert levels["m-legacy"] == "public"


def test_cross_tenant_and_dangling_edges_are_never_built(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    _add(repository, "m-a", tenant="t1")
    _add(repository, "m-b", tenant="t2")
    _rewrite(repository, "m-a", evidence=["m-b"], related=["m-b", "m-gone"], superseded_by="m-gone")
    keys = _keys(collect_edges(repository))
    assert not {key for key in keys if "m-b" in key}
    assert ("related", "m-a", "m-gone") not in keys
    assert ("superseded_by", "m-a", "m-gone") not in keys


def test_concept_privacy_only_when_the_file_states_it(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    _concept(repository.root, "open", "concept:open")
    _concept(repository.root, "hush", "concept:hush", privacy="local-private")
    _add(repository, "m-a", about=["concept:open", "concept:hush"])
    edges = {e.target: e for e in collect_edges(repository)}
    assert edges["concept:open"].target_privacy is None
    assert edges["concept:hush"].target_privacy == "local-private"
    assert edges["concept:open"].target_tenant is None


# --- K3: rebuild ---------------------------------------------------------------------------


def test_rebuild_is_deterministic_private_and_never_writes_the_vault(food: Repository) -> None:
    root = food.root
    before = tree_digest(root, exclude=(".memfw.lock",))
    first = rebuild_edge_projection(food)
    path = projection_path(root)
    data = path.read_bytes()
    second = rebuild_edge_projection(food)
    assert first == second and first["path"] == str(path)
    assert path.read_bytes() == data
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert tree_digest(root, exclude=(".memfw.lock",)) == before
    assert vault_store(food).version() == first["version"]
    assert not list(path.parent.glob(f".{path.name}.*.tmp"))
    assert not any("edges" in item.name for item in root.rglob("*"))


def test_rebuild_deletes_edges_whose_fields_are_gone(food: Repository) -> None:
    rebuild_edge_projection(food)
    store = vault_store(food)
    scope = EdgeScope("", EVERY)
    assert store.query(scope, node="m-apple", relations=("about",))
    _rewrite(food, "m-apple", about=[])
    old = store.version()
    rebuild_edge_projection(food)
    assert store.query(scope, node="m-apple", relations=("about",)) == ()
    assert store.version() != old
    food.delete("m-bread")
    rebuild_edge_projection(food)
    assert store.query(scope, node="m-bread") == ()
    assert "m-bread" not in {
        e.source for e in store.query(scope, node="concept:food", direction="in")
    }


def test_rebuild_refuses_memories_dir_and_symlinks(food: Repository, tmp_path: Path) -> None:
    from memfw.edges import EdgeError

    inside = SqliteProjectionStore(food.memories_dir / "x.sqlite3", forbid=food.memories_dir)
    with pytest.raises(EdgeError):
        inside.rebuild([])
    in_vault = SqliteProjectionStore(food.root / "edges.sqlite3", forbid=food.root)
    with pytest.raises(EdgeError):
        in_vault.rebuild([])
    link = tmp_path / "link.sqlite3"
    link.symlink_to(tmp_path / "elsewhere.sqlite3")
    with pytest.raises(EdgeError):
        SqliteProjectionStore(link).rebuild([])


# --- K4: syntax and caps ---------------------------------------------------------------------


def test_path_syntax_and_inverse_names() -> None:
    pattern = parse_path("about> broader*1..2> <about")
    assert pattern.as_text() == "about>broader*1..2><about"
    assert pattern.max_hops == 4
    aliases = {"supersedes": "superseded_by", "narrower": "broader"}
    assert parse_path("supersedes*3>", aliases=aliases).as_text() == "<superseded_by*3"
    assert parse_path("<narrower", aliases=aliases).as_text() == "broader>"


@pytest.mark.parametrize(
    "text",
    [
        "",
        "about",  # no direction
        "about>>",
        "broader*>",  # unbounded
        "broader*0..2>",
        "broader*3..2>",
        "broader*5>",  # deeper than MAX_DEPTH
        "about>about>about>about>about>",
        "about>broader*1..3><about",  # 5 hops
        "About>",
        "about>" * 60,
        "ghost>",
    ],
)
def test_bad_paths_are_refused(text: str) -> None:
    with pytest.raises(PatternError):
        parse_path(text, relations=builtin_relation_table().names())


def test_caps_are_hard(food: Repository) -> None:
    rebuild_edge_projection(food)
    for kwargs in ({"limit": 0}, {"limit": MAX_LIMIT + 1}, {"fanout": MAX_FANOUT + 1}):
        with pytest.raises(GraphError):
            graph_match(food, start="m-apple", path="about>", **kwargs)
    assert MAX_DEPTH == 4


def test_fanout_and_limit_truncate_deterministically(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    _concept(repository.root, "hub", "concept:hub")
    for index in range(12):
        _add(repository, f"m-{index:02d}", about=["concept:hub"])
    rebuild_edge_projection(repository)
    full = graph_match(repository, start="concept:hub", path="<about", limit=100)
    assert len(full["paths"]) == 12 and full["truncated"] is False
    assert [ids[1] for ids in _ids(full)] == [f"m-{i:02d}" for i in range(12)]
    narrow = graph_match(repository, start="concept:hub", path="<about", fanout=5)
    assert [ids[1] for ids in _ids(narrow)] == [f"m-{i:02d}" for i in range(5)]
    assert narrow["truncated"] is True
    few = graph_match(repository, start="concept:hub", path="<about", limit=3)
    assert [ids[1] for ids in _ids(few)] == ["m-00", "m-01", "m-02"] and few["truncated"]
    again = graph_match(repository, start="concept:hub", path="<about", limit=3)
    assert again == few


def test_expansion_cap_truncates(tmp_path: Path) -> None:
    store = MemoryProjectionStore()
    edges = [
        Edge("related", f"n{i}", f"n{j}", "concept", "concept", "related")
        for i in range(6)
        for j in range(6)
        if i != j
    ]
    store.rebuild(edges)
    scope = EdgeScope("", EVERY)
    result = match(store, scope, "n0", parse_path("related*4>"), start_kind="concept", limit=100)
    assert result.truncated  # 5*4*... paths, more than the limit
    small = match(
        store, scope, "n0", parse_path("related*4>"), start_kind="concept", max_expansions=3
    )
    assert small.truncated


# --- K4: semantics -------------------------------------------------------------------------


def test_memories_about_a_broader_concept(food: Repository) -> None:
    rebuild_edge_projection(food)
    payload = graph_match(
        food, start="m-apple", path="about>broader*1..2><about", clearance=["public"]
    )
    assert payload["path"] == "about>broader*1..2><about"
    assert _ids(payload) == [["m-apple", "concept:fruit", "concept:food", "m-bread"]]
    shared = graph_match(
        food, start="m-apple", path="about>broader><about", clearance=["public", "shared"]
    )
    assert _ids(shared) == [
        ["m-apple", "concept:fruit", "concept:food", "m-bread"],
        ["m-apple", "concept:fruit", "concept:food", "m-shared"],
    ]
    by_label = graph_match(food, start="fruits", path="<broader", clearance=["public"])
    assert by_label["paths"] == []
    down = graph_match(food, start="fruits", path="broader>")
    assert _ids(down) == [["concept:fruit", "concept:food"]]
    assert down["paths"][0]["nodes"][0]["kind"] == "concept"


def test_supersedes_chain_walks_superseded_by_backwards(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    for name in ("m-v1", "m-v2", "m-v3"):
        _add(repository, name)
    _rewrite(repository, "m-v1", superseded_by="m-v2")
    _rewrite(repository, "m-v2", superseded_by="m-v3")
    rebuild_edge_projection(repository)
    payload = graph_match(repository, start="m-v3", path="supersedes*1..3>")
    assert payload["path"] == "<superseded_by*1..3"
    assert _ids(payload) == [["m-v3", "m-v2"], ["m-v3", "m-v2", "m-v1"]]


def test_cycles_via_broader_and_merges_terminate(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    root = repository.root
    _concept(root, "a", "concept:a", broader=["concept:b"])
    _concept(root, "b", "concept:b", broader=["concept:alias"])
    _concept(root, "alias", "concept:alias", merged_into="concept:a")
    rebuild_edge_projection(repository)
    keys = _keys(collect_edges(repository))
    assert keys == {("broader", "concept:a", "concept:b"), ("broader", "concept:b", "concept:a")}
    loops = graph_match(repository, start="concept:a", path="broader*1..4>", limit=100)
    # Each edge once per path: a -> b -> a, and no further.
    assert _ids(loops) == [["concept:a", "concept:b"], ["concept:a", "concept:b", "concept:a"]]
    acyclic = graph_match(repository, start="concept:a", path="broader*1..4>", acyclic=True)
    assert _ids(acyclic) == [["concept:a", "concept:b"]]


def test_both_stores_give_the_same_paths(food: Repository) -> None:
    edges = collect_edges(food)
    sqlite = SqliteProjectionStore(food.root.parent / "edges.sqlite3")
    memory = MemoryProjectionStore()
    stores: list[ProjectionStore] = [sqlite, memory]
    for store in stores:
        store.rebuild(edges)
    patterns = ["about>", "about>broader*1..2><about", "<about", "about><about", "cites>"]
    starts = ["m-apple", "m-bread", "m-secret", "concept:food", "concept:fruit", "m-other"]
    for clearance in (PUBLIC, frozenset({"public", "shared"}), EVERY):
        for tenant in ("", "other"):
            for text in patterns:
                for start in starts:
                    answers = [
                        graph_match(
                            food,
                            start=start,
                            path=text,
                            tenant=tenant,
                            clearance=clearance,
                            store=store,
                        )
                        for store in stores
                    ]
                    assert answers[0] == answers[1], (clearance, tenant, text, start)


# --- leaks: hidden ids never show ----------------------------------------------------------


def test_private_endpoint_hides_the_edge_and_its_id(food: Repository) -> None:
    rebuild_edge_projection(food)
    for clearance in (PUBLIC, frozenset({"public", "shared"})):
        for text in ("<about", "broader><about", "<broader*1..2<about"):
            for start in ("concept:food", "concept:fruit", "fruits"):
                payload = graph_match(food, start=start, path=text, clearance=clearance)
                assert "m-secret" not in json.dumps(payload)
                assert "m-other" not in json.dumps(payload)
    # A hidden start answers exactly like one that does not exist.
    hidden = graph_match(food, start="m-secret", path="about><about")
    missing = graph_match(food, start="m-nothing", path="about><about")
    assert hidden["paths"] == missing["paths"] == [] and hidden["truncated"] is missing["truncated"]
    assert {k: v for k, v in hidden.items() if k != "from"} == {
        k: v for k, v in missing.items() if k != "from"
    }
    # The operator's full clearance sees it, the other tenant never does.
    full = graph_match(food, start="concept:food", path="<about", clearance=EVERY)
    assert "m-secret" in {ids[1] for ids in _ids(full)}
    assert "m-other" not in json.dumps(full)
    other = graph_match(food, start="concept:food", path="<about", tenant="other", clearance=EVERY)
    assert _ids(other) == [["concept:food", "m-other"]]


def test_unknown_privacy_is_never_returned(food: Repository) -> None:
    _rewrite(food, "m-bread", privacy="weird")
    rebuild_edge_projection(food)
    payload = graph_match(food, start="concept:food", path="<about", clearance=EVERY)
    assert "m-bread" not in json.dumps(payload)


def test_live_vault_wins_over_a_stale_projection(food: Repository) -> None:
    rebuild_edge_projection(food)
    _rewrite(food, "m-bread", privacy="local-private")
    payload = graph_match(food, start="concept:food", path="<about")
    assert "m-bread" not in json.dumps(payload)
    as_start = graph_match(food, start="m-bread", path="about>")
    assert as_start["paths"] == [] and not as_start["truncated"]
    food.delete("m-apple")
    gone = graph_match(food, start="m-apple", path="about>")
    assert gone["paths"] == [] and not gone["truncated"]
    via = graph_match(food, start="concept:fruit", path="<about")
    assert "m-apple" not in json.dumps(via)


def test_allowed_sources_hide_other_sources(food: Repository) -> None:
    _rewrite(food, "m-bread", source="agent-x")
    rebuild_edge_projection(food)
    every = graph_match(food, start="concept:food", path="<about")
    assert "m-bread" in json.dumps(every)
    only = graph_match(food, start="concept:food", path="<about", allowed_sources=["manual"])
    assert "m-bread" not in json.dumps(only)


def test_hidden_neighbours_do_not_use_up_the_fanout(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    _concept(repository.root, "hub", "concept:hub")
    for index in range(5):
        _add(repository, f"m-a{index}", about=["concept:hub"], privacy="local-private")
    _add(repository, "m-z", about=["concept:hub"])
    rebuild_edge_projection(repository)
    payload = graph_match(repository, start="concept:hub", path="<about", fanout=1)
    assert _ids(payload) == [["concept:hub", "m-z"]] and payload["truncated"] is False


def _hub_edges(hidden: int, visible: int) -> list[Edge]:
    """``hidden`` memories (sorting first) and ``visible`` ones, all ``about`` one hub."""

    def about(memory_id: str) -> Edge:
        return Edge(
            "about",
            memory_id,
            "concept:hub",
            "memory",
            "concept",
            "about",
            source_tenant="",
            source_privacy="public",
        )

    return [about(f"m-a{i:05d}") for i in range(hidden)] + [
        about(f"m-z{i:02d}") for i in range(visible)
    ]


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
def test_live_hidden_rows_beyond_one_page_never_hide_visible_neighbours(
    tmp_path: Path, kind: str
) -> None:
    """1500 store-visible but live-hidden neighbours sort before 3 visible ones."""

    store: ProjectionStore = (
        MemoryProjectionStore()
        if kind == "memory"
        else SqliteProjectionStore(tmp_path / "out" / "edges.sqlite3")
    )
    store.rebuild(_hub_edges(1500, 3))
    scope = EdgeScope("", EVERY)

    def live(node: str, node_kind: str) -> bool:
        return not node.startswith("m-a")  # deleted or made private since the rebuild

    result = match(
        store,
        scope,
        "concept:hub",
        parse_path("<about"),
        start_kind="concept",
        node_ok=live,
    )
    assert [path.nodes[1][0] for path in result.paths] == ["m-z00", "m-z01", "m-z02"]
    assert result.truncated is False  # hidden rows never set it
    cut = match(
        store,
        scope,
        "concept:hub",
        parse_path("<about"),
        start_kind="concept",
        node_ok=live,
        fanout=2,
    )
    assert [path.nodes[1][0] for path in cut.paths] == ["m-z00", "m-z01"]
    assert cut.truncated is True  # a visible neighbour was cut
    starved = match(
        store,
        scope,
        "concept:hub",
        parse_path("<about"),
        start_kind="concept",
        node_ok=live,
        max_expansions=2,
    )
    assert starved.paths == () and starved.truncated is True  # the query cap ran out


def test_store_hidden_rows_beyond_one_page_never_set_truncated(tmp_path: Path) -> None:
    """More than MAX_QUERY_LIMIT private edges before the visible ones."""

    edges = [
        Edge(
            "about",
            f"m-a{i:05d}",
            "concept:hub",
            "memory",
            "concept",
            "about",
            source_tenant="",
            source_privacy="local-private",
        )
        for i in range(1200)
    ] + _hub_edges(0, 2)
    for store in (MemoryProjectionStore(), SqliteProjectionStore(tmp_path / "s" / "e.sqlite3")):
        store.rebuild(edges)
        result = match(
            store,
            EdgeScope("", frozenset({"public"})),
            "concept:hub",
            parse_path("<about"),
            start_kind="concept",
        )
        assert [path.nodes[1][0] for path in result.paths] == ["m-z00", "m-z01"]
        assert result.truncated is False


def test_one_projection_connection_per_match(
    food: Repository, monkeypatch: pytest.MonkeyPatch
) -> None:
    rebuild_edge_projection(food)
    opened: list[int] = []
    real = SqliteProjectionStore._connect

    def counting(self: SqliteProjectionStore) -> Any:
        opened.append(1)
        return real(self)

    monkeypatch.setattr(SqliteProjectionStore, "_connect", counting)
    for start, path in (
        ("m-apple", "about>broader*1..2><about"),
        ("concept:food", "<about"),
        ("concept:fruit", "broader*1..2>"),
        ("m-missing", "about>"),
    ):
        opened.clear()
        graph_match(food, start=start, path=path, clearance=list(EVERY))
        assert len(opened) == 1, (start, path, len(opened))
    opened.clear()
    vault_store(food).query(EdgeScope("", EVERY), node="m-apple")
    assert len(opened) == 1  # outside a match each call still opens (and closes) its own


def test_a_node_is_read_from_the_store_once_per_match() -> None:
    store = MemoryProjectionStore()
    store.rebuild(
        [
            Edge("related", f"n{i}", f"n{j}", "concept", "concept", "related")
            for i in range(4)
            for j in range(4)
            if i != j
        ]
    )
    calls: list[tuple[str, str]] = []
    real = store.query

    def counting(scope: EdgeScope, **kwargs: Any) -> tuple[Edge, ...]:
        calls.append((kwargs["node"], kwargs["direction"]))
        return real(scope, **kwargs)

    store.query = counting  # type: ignore[method-assign]
    result = match(
        store,
        EdgeScope("", EVERY),
        "n0",
        parse_path("related*1..3>"),
        start_kind="concept",
        limit=100,
    )
    assert result.paths
    assert len(calls) == len(set(calls)) == 4  # n0..n3, each once


def test_sqlite_rows_push_visibility_and_limit_into_sql(tmp_path: Path) -> None:
    store = SqliteProjectionStore(tmp_path / "p" / "edges.sqlite3")
    private = [
        Edge(
            "about",
            f"m-a{i:05d}",
            "concept:hub",
            "memory",
            "concept",
            "about",
            source_tenant="",
            source_privacy="local-private",
        )
        for i in range(1200)
    ]
    other = [
        Edge(
            "about",
            f"m-b{i:05d}",
            "concept:hub",
            "memory",
            "concept",
            "about",
            source_tenant="other",
            source_privacy="public",
        )
        for i in range(300)
    ]
    store.rebuild([*private, *other, *_hub_edges(0, 3)])
    scope = EdgeScope("", frozenset({"public"}))
    fetched = list(store._rows(scope, "concept:hub", "in", None, 2, None))
    assert [edge.source for edge in fetched] == ["m-z00", "m-z01"]  # SQL returned only these
    rest = list(store._rows(scope, "concept:hub", "in", None, 10, fetched[-1].key))
    assert [edge.source for edge in rest] == ["m-z02"]
    none = EdgeScope("", frozenset())
    assert list(store._rows(none, "concept:hub", "in", None, 10, None)) == []


def test_sqlite_cursor_matches_the_reference_for_any_after_key(tmp_path: Path) -> None:
    """The index-friendly cursor rewrite equals the full key comparison."""

    from memfw.ports.projection_store import select

    names = ["a", "b", "c"]
    edges = [
        Edge(relation, source, target, "concept", "concept", relation)
        for relation in ("broader", "related")
        for source in names
        for target in names
        if source != target
    ]
    sqlite = SqliteProjectionStore(tmp_path / "c" / "edges.sqlite3")
    sqlite.rebuild(edges)
    scope = EdgeScope("", EVERY)
    probes = ["", "a", "b", "bb", "c", "d"]
    for direction in ("out", "in"):
        for node in names:
            for after in [
                (r, s_, t)
                for r in ("", "broader", "c", "related", "z")
                for s_ in probes
                for t in probes
            ]:
                for relations in (None, ("related",), ("broader", "related")):
                    names_set = frozenset(relations) if relations is not None else None
                    for limit in (1, 100):
                        want = select(
                            edges,
                            scope,
                            node=node,
                            direction=direction,
                            relations=names_set,
                            limit=limit,
                            after=after,
                        )
                        got = sqlite.query(
                            scope,
                            node=node,
                            direction=direction,
                            relations=relations,
                            limit=limit,
                            after=after,
                        )
                        assert got == want, (direction, node, after, relations, limit)


def test_the_same_edges_split_two_ways_are_one_path() -> None:
    store = MemoryProjectionStore()
    store.rebuild(
        [
            Edge("related", source, target, "concept", "concept", "related")
            for source, target in (("a", "b"), ("b", "c"), ("c", "d"))
        ]
    )
    result = match(
        store,
        EdgeScope("", EVERY),
        "a",
        parse_path("related*1..2>related*1..2>"),
        start_kind="concept",
        limit=100,
    )
    ids = [[node for node, _kind in path.nodes] for path in result.paths]
    assert ids == [["a", "b", "c"], ["a", "b", "c", "d"]]  # a,b,c,d once, not twice
    assert len({tuple(e.key for e in p.edges) for p in result.paths}) == len(result.paths)
    assert result.truncated is False


def test_leak_sweep_over_every_start_and_pattern(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    root = repository.root
    _concept(root, "a", "concept:a", broader=["concept:b"])
    _concept(root, "b", "concept:b")
    _concept(root, "c", "concept:c", privacy="local-private", broader=["concept:b"])
    _add(repository, "pub-1", about=["concept:a"], related=["pub-2"])
    _add(repository, "pub-2", about=["concept:b", "concept:c"], evidence=["priv-1", "att:x"])
    _add(repository, "priv-1", about=["concept:a"], privacy="local-private", related=["pub-1"])
    _add(repository, "odd-1", about=["concept:b"], related=["pub-1"])
    _rewrite(repository, "odd-1", privacy="nope")
    _add(repository, "ten-1", about=["concept:a"], tenant="t9", related=["pub-1"])
    rebuild_edge_projection(repository)
    hidden = {"priv-1", "odd-1", "ten-1", "concept:c"}
    starts = ["pub-1", "pub-2", "concept:a", "concept:b", "att:x", *sorted(hidden)]
    patterns = [
        "about>",
        "<about",
        "about><about",
        "about>broader><about",
        "related>",
        "<related",
        "related*1..3>",
        "<related*1..2about>",
        "evidence>",
        "about>broader*1..2>",
        "<broader",
    ]
    for start in starts:
        for text in patterns:
            payload = graph_match(repository, start=start, path=text, clearance=PUBLIC)
            shown = {node["id"] for path in payload["paths"] for node in path["nodes"]}
            assert not shown & hidden, (start, text, shown & hidden)
            if start in hidden:
                assert payload["paths"] == [] and payload["truncated"] is False


# --- interfaces --------------------------------------------------------------------------------


def test_cli_rebuild_and_match(food: Repository) -> None:
    runner = CliRunner()
    root = str(food.root)
    missing = runner.invoke(
        cli_app, ["graph", "match", "-r", root, "--from", "m-apple", "--path", "about>"]
    )
    assert missing.exit_code == 2 and "memfw graph rebuild" in missing.output
    built = runner.invoke(cli_app, ["graph", "rebuild", "-r", root])
    assert built.exit_code == 0, built.output
    assert json.loads(built.output)["path"] == str(projection_path(food.root))
    result = runner.invoke(
        cli_app,
        [
            "graph",
            "match",
            "-r",
            root,
            "--from",
            "concept:food",
            "--path",
            "<about",
            "--limit",
            "5",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert _ids(payload) == [["concept:food", "m-bread"], ["concept:food", "m-shared"]]
    full = runner.invoke(
        cli_app,
        [
            "graph", "match", "-r", root, "--from", "concept:food", "--path", "<about",
            "--clearance", "public,shared,local-private",
        ],
    )  # fmt: skip
    assert "m-secret" in full.output
    for bad in (["--clearance", "everything"], ["--path", "about"], ["--limit", "1000"]):
        args = ["graph", "match", "-r", root, "--from", "m-apple", "--path", "about>", *bad]
        assert runner.invoke(cli_app, args).exit_code != 0


def test_existing_outputs_ignore_the_projection(food: Repository) -> None:
    """Building edges.sqlite3 changes no other command's output."""

    from memfw.index import MemoryIndex
    from memfw.index.readonly import search_vault
    from memfw.ontology import lint_relations
    from memfw.runtime.context_sources import concept_catalogue

    def outputs() -> str:
        MemoryIndex(food.index_path).rebuild(food)
        runner = CliRunner()
        out = {
            "search": [search_vault(food.root, term, limit=5) for term in ("apple", "food")],
            "lint": lint_relations(food.root),
            "topics": concept_catalogue(food.root).canonical,
            "list": runner.invoke(cli_app, ["list", "--root", str(food.root)]).output,
        }
        return json.dumps(out, sort_keys=True, default=str)

    before = outputs()
    rebuild_edge_projection(food)
    assert outputs() == before


def test_mcp_tools_list_is_unchanged() -> None:
    """K4 has no MCP tool: the server source never names the graph runtime."""

    mcp = Path(__file__).resolve().parents[1] / "src" / "memfw" / "mcp"
    for path in mcp.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "runtime.graph" not in text and "graph_match" not in text, path.name


# --- the projection lives outside the vault (budget, fingerprint) ------------------------


def test_projection_path_is_outside_the_vault_and_keyed_like_usage(
    food: Repository, _home: Path
) -> None:
    from memfw.core.local_data import projection_file, vault_key
    from memfw.usage import projection_path as usage_path

    path = projection_path(food.root)
    assert (
        path == _home / ".local" / "share" / "memfw" / "edges" / f"{vault_key(food.root)}.sqlite3"
    )
    assert path == projection_file(food.root, "edges")
    assert path.name == usage_path(food.root).name  # one key per vault, one rule
    assert food.root.resolve() not in path.resolve().parents
    other = Path("/elsewhere-home")
    assert projection_path(food.root, home=other).parent == other / ".local/share/memfw/edges"


def test_a_large_projection_never_drives_total_budget_forgetting(tmp_path: Path) -> None:
    """Reviewer scenario: 20 memories, budget_mode=total, max_bytes = vault + 64 KB,
    hard forget, a projection far larger than the headroom: nothing is forgotten."""

    from datetime import UTC, datetime

    from memfw.curation.engine import enforce_budget
    from memfw.stats import measure_physical_disk_bytes

    repository = _vault(tmp_path)
    _concept(repository.root, "hub", "concept:hub")
    for number in range(20):
        _add(repository, f"m-{number:02d}", f"memory {number}", about=["concept:hub"])
    physical = measure_physical_disk_bytes(repository.root)
    config = repository.read_config()
    config.update(
        budget_mode="total",
        max_bytes=physical + 64 * 1024,
        forget_action="hard",
        budget_scope="all",
    )
    repository.configure(config)
    physical = measure_physical_disk_bytes(repository.root)
    fingerprint = repository._tree_fingerprint()
    big = [
        Edge(
            "related",
            f"m-{number % 20:02d}",
            f"x-{number:06d}-" + "p" * 200,
            "memory",
            "external",
            "related",
            source_tenant="",
            source_privacy="public",
        )
        for number in range(40_000)
    ]
    vault_store(repository).rebuild(big)
    assert projection_path(repository.root).stat().st_size > 4 * 1024 * 1024
    assert measure_physical_disk_bytes(repository.root) == physical
    assert repository._tree_fingerprint() == fingerprint
    result = enforce_budget(repository, now=datetime(2026, 11, 17, 9, 0, tzinfo=UTC))
    assert result.applied_ids == ()
    assert len(repository.list()) == 20


def test_a_legacy_vault_root_file_is_ignored_noted_and_kept(food: Repository) -> None:
    legacy = food.root / FILENAME
    legacy.write_bytes(b"not a projection")
    runner = CliRunner()
    built = runner.invoke(cli_app, ["graph", "rebuild", "-r", str(food.root)])
    assert built.exit_code == 0, built.output
    assert "no longer used" in built.stderr and FILENAME in built.stderr
    assert json.loads(built.stdout)["legacy_file"] == FILENAME
    assert legacy.read_bytes() == b"not a projection"  # never deleted or rewritten
    payload = graph_match(food, start="concept:food", path="<about")
    assert ["concept:food", "m-bread"] in _ids(payload)
    legacy.unlink()
    assert "legacy_file" not in rebuild_edge_projection(food)


def test_edges_module_is_a_projection_layer() -> None:
    from tests.architecture.layers import UNITS

    assert UNITS["memfw.edges"] == "L1-projection"
    assert os.path.basename(FILENAME) == "edges.sqlite3"
