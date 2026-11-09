"""Concept anchors, alias projection, and read-path expansion."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from eval.retrieval_r0.alias_eval import measure
from memfw.cli import app
from memfw.core.repository import Repository
from memfw.ontology.model import (
    OntologyError,
    import_seed,
    lint_concepts,
    load_concepts,
    propose_concept,
)
from memfw.ontology.normalise import normalise
from scripts.memfw_readonly_service import read_path, search_vault

_SEED = """
entries:
  - oid: concept:demo:one
    class: Concept
    pref_label: Demo
    alt_labels:
      - demo-alias
    status: active
  - oid: concept:demo:old
    class: Concept
    pref_label: Old Demo
    status: active
    merged_into: concept:demo:missing
"""


def test_normalise_folds_case_separators_traditional_and_host() -> None:
    assert normalise("Fi") == normalise("ﬁ")
    assert normalise("Foo-Bar_baz") == "foo bar baz"
    assert normalise("記憶@vault") == normalise("记忆")
    assert normalise("  Hammer  ") == "hammer"


def test_alias_query_class_recalls_every_memory_and_keeps_the_gates() -> None:
    report = measure()
    assert report["in_case_types"] is False
    assert report["fts_recall"] == 0
    assert report["alias_recall"] == 1
    assert report["mcp_fts_recall"] == 1
    assert report["mcp_hybrid_recall"] == 1
    assert report["recall_gain"] == 1
    assert report["distractor_excluded"] is True
    assert report["ambiguity_rate"] == 1
    assert report["merged_redirect"] is True


def test_alias_why_survives_annotate_and_unknown_text_does_not() -> None:
    kept: dict[str, object] = {}
    read_path.annotate(
        kept,
        provenance="human-authored",
        source="manual",
        index_version=1,
        sot_version=1,
        why="alias(hammer→concept:tool:hammer)",
    )
    assert kept["why"] == "alias(hammer→concept:tool:hammer)"
    dropped: dict[str, object] = {}
    read_path.annotate(
        dropped,
        provenance="",
        source="manual",
        index_version=1,
        sot_version=1,
        why="alias(no-arrow)",
    )
    assert dropped["why"] == "lexical"


def test_lint_reports_and_does_not_rewrite(tmp_path: Path) -> None:
    path = tmp_path / "concepts" / "Widget" / "one.md"
    path.parent.mkdir(parents=True)
    original = (
        "---\noid: concept:demo:one\nclass: Widget\npref_label: Demo\n"
        "alt_labels: []\nsame_as: []\nstatus: active\nmerged_into: concept:missing\n---\n\n\n"
    )
    path.write_text(original, encoding="utf-8")
    before = path.read_bytes()
    review = lint_concepts(load_concepts(tmp_path))
    assert path.read_bytes() == before
    kinds = {item["kind"] for item in review}
    assert {"unknown_class", "dangling_oid"} <= kinds


def test_bad_concept_file_is_not_repaired_and_search_stays_up(tmp_path: Path) -> None:
    repository = Repository(tmp_path / "vault")
    repository.init()
    repository.add("Alpha", "corpus keyword alpha", memory_id="mem-alpha", tenant="alpha")
    from memfw.index import MemoryIndex

    MemoryIndex(repository.index_path).rebuild(repository)
    path = repository.root / "concepts" / "Concept" / "bad.md"
    path.parent.mkdir(parents=True)
    path.write_text("not a concept\n", encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(OntologyError):
        load_concepts(repository.root)
    found = search_vault(repository.root, "corpus keyword", limit=5, tenant="alpha")
    assert [item["id"] for item in found["results"]] == ["mem-alpha"]
    assert found["errors"]
    assert "ambiguity" not in found
    assert path.read_bytes() == before


def test_import_seed_dry_run_writes_nothing_and_apply_refuses_a_live_vault(tmp_path: Path) -> None:
    seed = tmp_path / "seed.yaml"
    seed.write_text(_SEED, encoding="utf-8")
    empty = tmp_path / "empty"
    empty.mkdir()
    preview = import_seed(seed, empty, apply=False)
    assert preview["applied"] is False
    assert list(empty.iterdir()) == []
    applied = import_seed(seed, empty, apply=True)
    assert applied["written"] == ["concept:demo:one", "concept:demo:old"]
    assert any(item["kind"] == "dangling_oid" for item in applied["review"])
    again = import_seed(seed, empty, apply=True)
    assert again["written"] == []
    assert again["skipped"]
    live = Repository(tmp_path / "live")
    live.init()
    with pytest.raises(OntologyError, match="live vault"):
        import_seed(seed, live.root, apply=True)
    assert not (live.root / "concepts").exists()


def test_concepts_cli_list_show_propose_and_merge(tmp_path: Path) -> None:
    runner = CliRunner()
    vault = tmp_path / "vault"
    dry = runner.invoke(
        app,
        [
            "concepts",
            "propose",
            "--vault",
            str(vault),
            "--oid",
            "concept:demo:one",
            "--class",
            "Concept",
            "--pref-label",
            "Demo",
            "--alt-label",
            "demo-alias",
        ],
    )
    assert dry.exit_code == 0, dry.stdout
    assert json.loads(dry.stdout)["applied"] is False
    assert not vault.exists() or not any(vault.rglob("*.md"))
    proposed = runner.invoke(
        app,
        [
            "concepts",
            "propose",
            "--vault",
            str(vault),
            "--oid",
            "concept:demo:one",
            "--class",
            "Concept",
            "--pref-label",
            "Demo",
            "--apply",
        ],
    )
    assert proposed.exit_code == 0, proposed.stdout
    second = runner.invoke(
        app,
        [
            "concepts",
            "propose",
            "--vault",
            str(vault),
            "--oid",
            "concept:demo:two",
            "--class",
            "Concept",
            "--pref-label",
            "Two",
            "--apply",
        ],
    )
    assert second.exit_code == 0, second.stdout
    listed = runner.invoke(app, ["concepts", "list", "--vault", str(vault)])
    payload = json.loads(listed.stdout)
    assert {row["oid"] for row in payload["concepts"]} == {
        "concept:demo:one",
        "concept:demo:two",
    }
    shown = runner.invoke(
        app, ["concepts", "show", "concept:demo:one", "--vault", str(vault)]
    )
    assert json.loads(shown.stdout)["status"] == "candidate"
    both = runner.invoke(
        app,
        [
            "concepts",
            "merge",
            "concept:demo:one",
            "concept:demo:two",
            "--vault",
            str(vault),
            "--apply",
            "--dry-run",
        ],
    )
    assert both.exit_code == 2
    merged = runner.invoke(
        app,
        [
            "concepts",
            "merge",
            "concept:demo:one",
            "concept:demo:two",
            "--vault",
            str(vault),
            "--apply",
        ],
    )
    assert merged.exit_code == 0, merged.stdout
    assert load_concepts(vault)["concept:demo:one"].merged_into == "concept:demo:two"
    seed = tmp_path / "seed.yaml"
    seed.write_text(_SEED, encoding="utf-8")
    imported = runner.invoke(
        app,
        ["concepts", "import-seed", "--seed", str(seed), "--vault", str(tmp_path / "seeded")],
    )
    assert imported.exit_code == 0, imported.stdout
    assert json.loads(imported.stdout)["applied"] is False
    assert not (tmp_path / "seeded").exists()


def test_propose_dry_run_does_not_write(tmp_path: Path) -> None:
    preview = propose_concept(
        tmp_path,
        oid="concept:demo:one",
        class_name="Concept",
        pref_label="Demo",
        apply=False,
    )
    assert preview["applied"] is False
    assert not (tmp_path / "concepts").exists()


def test_about_round_trips_and_empty_targets_stay_off_the_file(tmp_path: Path) -> None:
    repository = Repository(tmp_path / "vault")
    repository.init()
    repository.add("Plain", "plain body", memory_id="mem-plain", tenant="alpha")
    plain = (repository.root / "memories" / "mem-plain.md").read_text(encoding="utf-8")
    assert "about:" not in plain
    assert "applies_to:" not in plain
    repository.add("Tagged", "lexical needle", memory_id="mem-tagged", tenant="alpha")
    path = repository.root / "memories" / "mem-tagged.md"
    text = path.read_text(encoding="utf-8")
    parts = text.split("---\n", 2)
    meta = yaml.safe_load(parts[1])
    meta["about"] = "concept:tool:hammer"
    dumped = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False).rstrip()
    path.write_text(f"---\n{dumped}\n---\n{parts[2]}", encoding="utf-8")
    loaded = repository.get("mem-tagged")
    assert loaded.about == ["concept:tool:hammer"]
    assert loaded.applies_to == []
    from memfw.index import MemoryIndex
    from memfw.mcp.server import Gateway

    MemoryIndex(repository.index_path).rebuild(repository)
    gateway = Gateway(
        repository.root, tenant="alpha", entity_enabled=False, allowed_sources=()
    )
    found = gateway.search("lexical needle", limit=5, mode="hybrid")
    assert [item["id"] for item in found["results"]] == ["mem-tagged"]


def test_search_without_concepts_has_no_ambiguity_field(tmp_path: Path) -> None:
    repository = Repository(tmp_path / "vault")
    repository.init()
    repository.add("Alpha", "corpus keyword alpha", memory_id="mem-alpha", tenant="alpha")
    from memfw.index import MemoryIndex

    MemoryIndex(repository.index_path).rebuild(repository)
    found = search_vault(repository.root, "corpus keyword", limit=5, tenant="alpha")
    assert "ambiguity" not in found
    assert found["errors"] == []
    linked = repository.root / "concepts"
    target = tmp_path / "outside"
    target.mkdir()
    linked.symlink_to(target, target_is_directory=True)
    (target / "Concept").mkdir()
    (target / "Concept" / "hammer.md").write_text("ignored\n", encoding="utf-8")
    again = search_vault(repository.root, "corpus keyword", limit=5, tenant="alpha")
    assert again["errors"] == []
    assert "ambiguity" not in again


def test_seed_yaml_round_trip_shape() -> None:
    loaded = yaml.safe_load(_SEED)
    assert isinstance(loaded["entries"], list)
