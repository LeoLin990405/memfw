"""Concept anchors, alias projection, and read-path expansion."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from eval.retrieval_r0.alias_eval import measure
from memfw.cli import app
from memfw.core.repository import Repository
from memfw.ontology.model import (
    Concept,
    OntologyError,
    import_seed,
    lint_concepts,
    load_concepts,
    merge_concepts,
    project_aliases,
    propose_concept,
    save_concept,
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


def test_normalise_folds_open_cc_forms_and_records_collisions() -> None:
    assert normalise("服務") == normalise("服务") == "服务"
    assert normalise("裡") == normalise("裏") == normalise("里")
    # Distinct traditional characters share OpenCC's first simplified form.
    assert normalise("髮") == normalise("發") == normalise("发")
    assert normalise("乾") == "干"


def _channels(
    root: Path,
    query: str,
    *,
    limit: int = 10,
    include_history: bool = False,
    as_of: str | None = None,
) -> dict[str, dict[str, list[str]]]:
    from memfw.index import MemoryIndex
    from memfw.mcp.server import Gateway

    MemoryIndex(root / "index.sqlite3").rebuild(Repository(root))
    gateway = Gateway(root, tenant="alpha", entity_enabled=False, allowed_sources=())
    found: dict[str, dict[str, list[str]]] = {}
    http = search_vault(
        root,
        query,
        tenant="alpha",
        limit=limit,
        include_history=include_history,
        as_of=as_of,
    )
    found["http"] = {
        "ids": [item["id"] for item in http["results"]],
        "errors": [str(item) for item in http["errors"]],
    }
    for mode in ("fts", "hybrid", "multihop"):
        payload = gateway.search(
            query,
            limit=limit,
            mode=mode,
            include_history=include_history,
            as_of=as_of,
        )
        found[mode] = {
            "ids": [item["id"] for item in payload["results"]],
            "errors": [str(item) for item in payload["errors"]],
        }
    return found


def test_alias_hits_use_the_search_temporal_gate(tmp_path: Path) -> None:
    from eval.retrieval_r0.alias_eval import _recall_vault, _tag

    root = _recall_vault(tmp_path / "temporal")
    _tag(
        root / "memories/mem-about.md",
        {
            "created_at": "2020-01-01T00:00:00Z",
            "valid_from": "2020-01-01T00:00:00Z",
            "valid_to": "2021-01-01T00:00:00Z",
            "superseded_by": "mem-applies",
        },
    )
    _tag(
        root / "memories/mem-about-2.md",
        {
            "created_at": "2020-01-01T00:00:00Z",
            "valid_from": "2030-01-01T00:00:00Z",
        },
    )
    _tag(
        root / "memories/mem-applies.md",
        {
            "created_at": "2020-01-01T00:00:00Z",
            "valid_from": "2020-01-01T00:00:00Z",
        },
    )
    current = _channels(root, "hammer", limit=10)
    for name, payload in current.items():
        assert "mem-about" not in payload["ids"], name
        assert payload["ids"] == ["mem-about-2", "mem-applies"], name
    dated = _channels(root, "hammer", limit=10, as_of="2026-01-01T00:00:00+00:00")
    for name, payload in dated.items():
        assert payload["ids"] == ["mem-applies"], name
    history = _channels(root, "hammer", limit=10, include_history=True)
    for name, payload in history.items():
        assert payload["ids"] == ["mem-about-2", "mem-about", "mem-applies"], name


def test_alias_channel_is_fused_when_lexical_fills_the_limit(tmp_path: Path) -> None:
    from eval.retrieval_r0.alias_eval import _recall_vault

    root = _recall_vault(tmp_path / "saturation")
    repository = Repository(root)
    for number in range(3):
        repository.add(
            "Hammer advertisement",
            "hammer advertisement",
            memory_id=f"advert-{number}",
            tenant="alpha",
            source="manual",
        )
    page = _channels(root, "hammer", limit=3)
    wide = _channels(root, "hammer", limit=6)
    targets = {"mem-about", "mem-about-2", "mem-applies"}
    for name, payload in page.items():
        assert set(payload["ids"]) != {f"advert-{number}" for number in range(3)}, name
        assert set(payload["ids"]) & targets, name
    for name, payload in wide.items():
        assert targets <= set(payload["ids"]), name


def test_malformed_concept_file_keeps_fts_results(tmp_path: Path) -> None:
    from eval.retrieval_r0.alias_eval import _recall_vault

    root = _recall_vault(tmp_path / "yaml")
    bad = root / "concepts" / "Concept" / "bad.md"
    bad.write_text("---\noid: [unterminated\n---\n", encoding="utf-8")
    found = _channels(root, "crate", limit=10)
    for name, payload in found.items():
        assert payload["ids"], name
        assert payload["errors"], name
        assert "mem-about" in payload["ids"], name

    bad.write_bytes(b"---\noid: concept:bad\npref_label: \xff\n---\n")
    broken = _channels(root, "crate", limit=10)
    for name, payload in broken.items():
        assert payload["ids"], name
        assert payload["errors"], name

    bad.write_text(
        "---\noid: concept:blocked\nclass: Concept\npref_label: Blocked\n"
        "alt_labels: []\nsame_as: []\nstatus: active\nmerged_into: null\n---\n",
        encoding="utf-8",
    )
    os.chmod(bad, 0)
    try:
        denied = _channels(root, "crate", limit=10)
    finally:
        os.chmod(bad, 0o644)
    for name, payload in denied.items():
        assert payload["ids"], name
        assert payload["errors"], name


def test_candidate_cannot_shadow_an_existing_oid(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    save_concept(
        vault,
        Concept("concept:shared", "Concept", "Original", status="active"),
        apply=True,
    )
    with pytest.raises(OntologyError, match="already exists"):
        save_concept(
            vault,
            Concept("concept:shared", "Project", "Replacement", status="active"),
            apply=True,
        )
    proposed = runner_propose(vault)
    assert proposed.exit_code != 0
    concepts = load_concepts(vault)
    assert concepts["concept:shared"].pref_label == "Original"
    assert concepts["concept:shared"].status == "active"
    assert normalise("Original") in project_aliases(concepts).by_alias
    assert len(list(vault.glob("concepts/*/*.md"))) == 1

    shadow = vault / "concepts" / "Project" / "shared.md"
    shadow.parent.mkdir(parents=True)
    shadow.write_text(
        "---\noid: concept:shared\nclass: Project\npref_label: Replacement\n"
        "alt_labels: []\nsame_as: []\nstatus: candidate\nmerged_into: null\n---\n",
        encoding="utf-8",
    )
    loaded = load_concepts(vault)
    assert loaded["concept:shared"].pref_label == "Original"
    assert loaded["concept:shared"].status == "active"
    assert any(item["kind"] == "duplicate_oid" for item in lint_concepts(loaded))
    assert normalise("Original") in project_aliases(loaded).by_alias


def runner_propose(vault: Path):
    runner = CliRunner()
    return runner.invoke(
        app,
        [
            "concepts",
            "propose",
            "--vault",
            str(vault),
            "--oid",
            "concept:shared",
            "--class",
            "Project",
            "--pref-label",
            "Replacement",
            "--apply",
        ],
    )


def test_merge_rejects_cycles_and_lint_reports_them(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    for oid, label in (
        ("concept:alpha", "Alpha"),
        ("concept:beta", "Beta"),
        ("concept:gamma", "Gamma"),
    ):
        save_concept(
            vault,
            Concept(oid, "Concept", label, status="active"),
            apply=True,
        )
    merge_concepts(vault, "concept:alpha", "concept:beta", apply=True)
    with pytest.raises(OntologyError, match="cycle"):
        merge_concepts(vault, "concept:beta", "concept:alpha", apply=True)
    assert load_concepts(vault)["concept:beta"].merged_into is None
    merge_concepts(vault, "concept:beta", "concept:gamma", apply=True)
    with pytest.raises(OntologyError, match="cycle"):
        merge_concepts(vault, "concept:gamma", "concept:alpha", apply=True)
    concepts = load_concepts(vault)
    assert project_aliases(concepts).by_alias[normalise("Alpha")] == "concept:gamma"
    assert not any(item["kind"] == "merge_cycle" for item in lint_concepts(concepts))

    alpha = concepts["concept:alpha"]
    beta = concepts["concept:beta"]
    assert alpha.path is not None and beta.path is not None
    alpha.path.write_text(
        "---\noid: concept:alpha\nclass: Concept\npref_label: Alpha\n"
        "alt_labels: []\nsame_as: []\nstatus: active\nmerged_into: concept:beta\n---\n",
        encoding="utf-8",
    )
    beta.path.write_text(
        "---\noid: concept:beta\nclass: Concept\npref_label: Beta\n"
        "alt_labels: []\nsame_as: []\nstatus: active\nmerged_into: concept:alpha\n---\n",
        encoding="utf-8",
    )
    cycled = load_concepts(vault)
    assert any(item["kind"] == "merge_cycle" for item in lint_concepts(cycled))


def test_merge_rejects_a_redirect_deeper_than_sixteen(tmp_path: Path) -> None:
    vault = tmp_path / "chain"
    for number in range(18):
        save_concept(
            vault,
            Concept(f"concept:n{number}", "Concept", f"N{number}", status="active"),
            apply=True,
        )
    for number in range(16):
        merge_concepts(vault, f"concept:n{number}", f"concept:n{number + 1}", apply=True)
    with pytest.raises(OntologyError, match="too deep"):
        merge_concepts(vault, "concept:n16", "concept:n17", apply=True)
    chain = load_concepts(vault)
    assert not any(item["kind"] == "redirect_depth" for item in lint_concepts(chain))
    # A hand-written 17th hop is reported and not repaired.
    path = chain["concept:n16"].path
    assert path is not None
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace("merged_into: null", "merged_into: concept:n17"), encoding="utf-8")
    reported = lint_concepts(load_concepts(vault))
    assert any(item["kind"] == "redirect_depth" for item in reported)
    assert "concept:n17" in path.read_text(encoding="utf-8")


def test_merge_keeps_relations_and_lint_reports_bad_ones(tmp_path: Path) -> None:
    vault = tmp_path / "relations"
    save_concept(
        vault,
        Concept(
            "concept:alpha",
            "Concept",
            "Alpha",
            status="active",
            extras={"part_of": ["concept:beta"], "runs_onn": ["concept:missing"]},
        ),
        apply=True,
    )
    save_concept(
        vault,
        Concept("concept:beta", "Concept", "Beta", status="active"),
        apply=True,
    )
    before = (vault / "concepts" / "Concept" / "alpha.md").read_text(encoding="utf-8")
    assert "part_of" in before
    assert "runs_onn" in before
    merge_concepts(vault, "concept:alpha", "concept:beta", apply=True)
    text = (vault / "concepts" / "Concept" / "alpha.md").read_text(encoding="utf-8")
    loaded = yaml.safe_load(text.split("---\n", 2)[1])
    assert loaded["part_of"] == ["concept:beta"]
    assert loaded["runs_onn"] == ["concept:missing"]
    assert loaded["merged_into"] == "concept:beta"
    review = lint_concepts(load_concepts(vault))
    assert any(
        item["kind"] == "unknown_relation" and item["detail"] == "runs_onn" for item in review
    )
    assert any(
        item["kind"] == "dangling_oid" and item["detail"] == "concept:missing" for item in review
    )


def _mutate_about(root: Path, field: str, value: str) -> None:
    path = root / "memories/mem-about.md"
    text = path.read_text(encoding="utf-8")
    _, header, body = text.split("---", 2)
    metadata = yaml.safe_load(header) or {}
    metadata.pop("valid_to", None)
    metadata.pop("superseded_by", None)
    metadata[field] = value
    dumped = yaml.safe_dump(metadata, sort_keys=False)
    path.write_text("---\n" + dumped + "---" + body, encoding="utf-8")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("valid_until", "2021-01-01T00:00:00Z"),
        ("superseded", "mem-applies"),
    ],
)
@pytest.mark.parametrize("channel", ["http", "fts", "hybrid", "multihop"])
def test_legacy_temporal_alias_hides_mem_about_on_each_channel(
    tmp_path: Path,
    field: str,
    value: str,
    channel: str,
) -> None:
    from eval.retrieval_r0.alias_eval import _recall_vault

    root = _recall_vault(tmp_path / f"{field}-{channel}")
    _mutate_about(root, field, value)
    found = _channels(root, "hammer")
    assert "mem-about" not in found[channel]["ids"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("valid_until", "2021-01-01T00:00:00Z"),
        ("superseded", "mem-applies"),
    ],
)
def test_matching_memories_history_returns_legacy_temporal_alias(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    from eval.retrieval_r0.alias_eval import _recall_vault
    from memfw.ontology.expand import matching_memories

    root = _recall_vault(tmp_path / field)
    _mutate_about(root, field, value)
    hits = matching_memories(
        root,
        "hammer",
        tenant="alpha",
        include_history=True,
    )[0]
    assert "mem-about" in {row[0] for row in hits}


def test_matching_memories_as_of_follows_legacy_valid_until(tmp_path: Path) -> None:
    from eval.retrieval_r0.alias_eval import _recall_vault
    from memfw.ontology.expand import matching_memories

    root = _recall_vault(tmp_path / "as-of")
    _mutate_about(root, "valid_until", "2021-01-01T00:00:00Z")
    _mutate_about(root, "valid_from", "2020-01-01T00:00:00Z")
    inside = matching_memories(
        root,
        "hammer",
        tenant="alpha",
        as_of="2020-06-01T00:00:00Z",
    )[0]
    outside = matching_memories(
        root,
        "hammer",
        tenant="alpha",
        as_of="2021-06-01T00:00:00Z",
    )[0]
    assert "mem-about" in {row[0] for row in inside}
    assert "mem-about" not in {row[0] for row in outside}


def test_http_as_of_hides_legacy_valid_until(tmp_path: Path) -> None:
    from eval.retrieval_r0.alias_eval import _recall_vault
    from memfw.index import MemoryIndex

    root = _recall_vault(tmp_path / "http-as-of")
    _mutate_about(root, "valid_until", "2021-01-01T00:00:00Z")
    MemoryIndex(root / "index.sqlite3").rebuild(Repository(root))
    found = search_vault(
        root,
        "hammer",
        tenant="alpha",
        limit=10,
        as_of="2021-06-01T00:00:00Z",
    )
    assert "mem-about" not in [item["id"] for item in found["results"]]


def test_alias_expansion_reads_memories_once_and_refreshes_on_concept_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from eval.retrieval_r0.alias_eval import _recall_vault, _tag
    from memfw.ontology import expand, model

    root = _recall_vault(tmp_path / "cache")
    reads: list[str] = []
    concept_reads: list[str] = []
    real_frontmatter = expand._frontmatter
    real_read_concept = model._read_concept

    def counting_frontmatter(path: Path) -> dict[str, object]:
        reads.append(Path(path).name)
        return real_frontmatter(path)

    def counting_read_concept(path: Path) -> Concept:
        concept_reads.append(Path(path).name)
        return real_read_concept(path)

    monkeypatch.setattr(expand, "_frontmatter", counting_frontmatter)
    monkeypatch.setattr(model, "_read_concept", counting_read_concept)
    # Query well after the files were written, so their stamps are settled.
    later = time.time_ns() + 60_000_000_000
    monkeypatch.setattr(expand, "_clock_ns", lambda: later)

    first = expand.matching_memories(root, "hammer", tenant="alpha")[0]
    assert {row[0] for row in first} == {"mem-about", "mem-about-2", "mem-applies"}
    assert reads and concept_reads
    reads.clear()
    concept_reads.clear()

    for query in ("hammer", "mallet", "pounder", "unrelated words"):
        expand.matching_memories(root, query, tenant="alpha")
    assert reads == [], "memory files were scanned again for an unchanged vault"
    assert concept_reads == [], "concept files were parsed again without a change"

    concept_path = root / "concepts" / "Concept" / "hammer.md"
    text = concept_path.read_text(encoding="utf-8")
    assert "sledge" not in text
    edited = text.replace("- mallet\n", "- mallet\n- sledge\n", 1)
    concept_path.write_text(edited, encoding="utf-8")
    refreshed = expand.matching_memories(root, "sledge", tenant="alpha")[0]
    assert {row[0] for row in refreshed} == {"mem-about", "mem-about-2", "mem-applies"}
    assert concept_reads, "an edited concept file must rebuild the alias map"
    assert reads == [], "a concept edit must not rescan memory files"

    _tag(root / "memories/mem-about.md", {"about": "concept:tool:other"})
    after = expand.matching_memories(root, "hammer", tenant="alpha")[0]
    assert "mem-about" not in {row[0] for row in after}
    assert reads == ["mem-about.md"], "only the edited memory is parsed again"


def _scoped_vault(root: Path) -> Path:
    """Recall vault plus scoped labels.

    ``forge-tool`` is a scoped label of wedge in ``project:smithy`` and of
    hammer in ``project:workshop``. ``mallet`` is unscoped on hammer and
    scoped on wedge in ``project:smithy``.
    """
    from eval.retrieval_r0.alias_eval import _recall_vault
    from memfw.ontology.model import Label, _render

    _recall_vault(root)
    concepts = load_concepts(root)
    hammer = concepts["concept:tool:hammer"]
    hammer.alt_labels = (*hammer.alt_labels, Label("forge-tool", "project:workshop"))
    wedge = concepts["concept:tool:other"]
    wedge.alt_labels = (
        Label("forge-tool", "project:smithy"),
        Label("mallet", "project:smithy"),
    )
    for concept in (hammer, wedge):
        assert concept.path is not None
        concept.path.write_text(_render(concept), encoding="utf-8")
    return root


_HAMMER = {"mem-about", "mem-about-2", "mem-applies"}


def test_unscoped_aliases_resolve_in_every_scope(tmp_path: Path) -> None:
    from memfw.ontology.expand import matching_memories

    root = _scoped_vault(tmp_path / "unscoped")
    for scope in ("", "project:smithy", "project:workshop", "project:none"):
        hits, ambiguities = matching_memories(root, "wedge", tenant="alpha", scope=scope)
        assert [row[0] for row in hits] == ["mem-other"], scope
        assert ambiguities == [], scope
        hits, _ = matching_memories(root, "pounder", tenant="alpha", scope=scope)
        assert {row[0] for row in hits} == _HAMMER, scope
    index = project_aliases(load_concepts(root))
    assert normalise("forge-tool") not in index.by_alias
    assert normalise("forge-tool") not in index.ambiguous


def test_scoped_alias_resolves_only_in_its_scope(tmp_path: Path) -> None:
    from memfw.index import MemoryIndex
    from memfw.mcp.server import Gateway
    from memfw.ontology.expand import matching_memories

    root = _scoped_vault(tmp_path / "scoped")
    smithy, _ = matching_memories(root, "forge-tool", tenant="alpha", scope="project:smithy")
    assert smithy == [("mem-other", "alias(forge-tool→concept:tool:other)")]
    workshop, _ = matching_memories(root, "forge-tool", tenant="alpha", scope="project:workshop")
    assert {row[0] for row in workshop} == _HAMMER
    for scope in ("", "project:none", "project:smith", " "):
        hits, ambiguities = matching_memories(root, "forge-tool", tenant="alpha", scope=scope)
        assert hits == [] and ambiguities == [], scope

    MemoryIndex(root / "index.sqlite3").rebuild(Repository(root))
    unscoped = search_vault(root, "forge-tool", tenant="alpha", limit=10)
    assert unscoped["results"] == []
    scoped = search_vault(root, "forge-tool", tenant="alpha", limit=10, scope="project:smithy")
    assert [item["id"] for item in scoped["results"]] == ["mem-other"]
    from scripts.memfw_readonly_service import ServiceError

    with pytest.raises(ServiceError):
        search_vault(root, "forge-tool", tenant="alpha", scope=7)  # type: ignore[arg-type]
    gateway = Gateway(root, tenant="alpha", entity_enabled=False, allowed_sources=())
    for mode in ("fts", "hybrid", "multihop"):
        plain = gateway.search("forge-tool", limit=10, mode=mode)
        assert plain["results"] == [], mode
        handler = gateway.handlers()["memory_search"]
        scoped_payload = handler({"query": "forge-tool", "mode": mode, "scope": "project:smithy"})
        assert [item["id"] for item in scoped_payload["results"]] == ["mem-other"], mode


def test_scoped_label_conflicting_with_unscoped_label_is_ambiguous_only_in_scope(
    tmp_path: Path,
) -> None:
    from memfw.ontology.expand import matching_memories

    root = _scoped_vault(tmp_path / "conflict")
    outside, ambiguities = matching_memories(root, "mallet", tenant="alpha")
    assert {row[0] for row in outside} == _HAMMER
    assert ambiguities == []
    elsewhere, _ = matching_memories(root, "mallet", tenant="alpha", scope="project:workshop")
    assert {row[0] for row in elsewhere} == _HAMMER
    inside, ambiguities = matching_memories(
        root, "mallet", tenant="alpha", scope="project:smithy"
    )
    assert inside == []
    assert ambiguities == [
        {
            "kind": "duplicate_alias",
            "alias": "mallet",
            "detail": "mallet",
            "oids": ["concept:tool:hammer", "concept:tool:other"],
        }
    ]
    review = [
        item for item in lint_concepts(load_concepts(root)) if item["kind"] == "duplicate_alias"
    ]
    assert review == [
        {
            "kind": "duplicate_alias",
            "oid": "concept:tool:hammer",
            "detail": "mallet",
            "scope": "project:smithy",
            "oids": ["concept:tool:hammer", "concept:tool:other"],
        }
    ]


def _replace_same_size(path: Path, old: str, new: str) -> None:
    """Rewrite in place with the same size and put the old mtime back."""
    assert len(old.encode()) == len(new.encode())
    before = path.stat()
    text = path.read_text(encoding="utf-8")
    assert old in text
    path.write_text(text.replace(old, new, 1), encoding="utf-8")
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert path.stat().st_mtime_ns == before.st_mtime_ns
    assert path.stat().st_size == before.st_size


def _settled_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    from memfw.ontology import expand

    later = time.time_ns() + 60_000_000_000
    monkeypatch.setattr(expand, "_clock_ns", lambda: later)


def test_alias_cache_sees_same_size_edit_with_restored_mtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from eval.retrieval_r0.alias_eval import _recall_vault
    from memfw.ontology.expand import matching_memories

    root = _recall_vault(tmp_path / "same-size")
    _settled_clock(monkeypatch)
    assert {row[0] for row in matching_memories(root, "hammer", tenant="alpha")[0]} == _HAMMER

    _replace_same_size(root / "memories/mem-about.md", "tenant: alpha", "tenant: bravo")
    alpha = {row[0] for row in matching_memories(root, "hammer", tenant="alpha")[0]}
    bravo = {row[0] for row in matching_memories(root, "hammer", tenant="bravo")[0]}
    assert alpha == _HAMMER - {"mem-about"}
    assert bravo == {"mem-about"}

    concept = root / "concepts" / "Concept" / "hammer.md"
    _replace_same_size(concept, "- mallet\n", "- maulet\n")
    assert matching_memories(root, "mallet", tenant="alpha")[0] == []
    assert {row[0] for row in matching_memories(root, "maulet", tenant="alpha")[0]} == (
        _HAMMER - {"mem-about"}
    )


def test_alias_cache_drops_a_deleted_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from eval.retrieval_r0.alias_eval import _recall_vault
    from memfw.ontology.expand import matching_memories

    root = _recall_vault(tmp_path / "deleted")
    _settled_clock(monkeypatch)
    assert {row[0] for row in matching_memories(root, "hammer", tenant="alpha")[0]} == _HAMMER
    (root / "memories/mem-about-2.md").unlink()
    after = {row[0] for row in matching_memories(root, "hammer", tenant="alpha")[0]}
    assert after == _HAMMER - {"mem-about-2"}


def test_alias_cache_reparses_files_changed_within_the_racy_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A coarse-timestamp filesystem: every stamp field stays identical."""
    from eval.retrieval_r0.alias_eval import _recall_vault
    from memfw.ontology import expand

    root = _recall_vault(tmp_path / "racy")
    tick = (time.time_ns() // 1_000_000_000) * 1_000_000_000
    frozen: dict[str, int] = {}

    def coarse_row(path: Path) -> tuple[str, int, int, int, int]:
        size = frozen.setdefault(str(path), path.stat().st_size)
        return (str(path), tick, tick, size, 1)

    monkeypatch.setattr(expand, "_file_row", coarse_row)
    monkeypatch.setattr(expand, "_clock_ns", lambda: tick + 500_000_000)
    reads: list[str] = []
    real_frontmatter = expand._frontmatter

    def counting_frontmatter(path: Path) -> dict[str, object]:
        reads.append(Path(path).name)
        return real_frontmatter(path)

    monkeypatch.setattr(expand, "_frontmatter", counting_frontmatter)
    hits = {row[0] for row in expand.matching_memories(root, "hammer", tenant="alpha")[0]}
    assert hits == _HAMMER
    path = root / "memories/mem-applies.md"
    path.write_text(
        path.read_text(encoding="utf-8").replace("tenant: alpha", "tenant: bravo", 1),
        encoding="utf-8",
    )
    concept = root / "concepts" / "Concept" / "hammer.md"
    concept.write_text(
        concept.read_text(encoding="utf-8").replace("- mallet\n", "- maulet\n", 1),
        encoding="utf-8",
    )
    reads.clear()
    alpha = {row[0] for row in expand.matching_memories(root, "maulet", tenant="alpha")[0]}
    assert "mem-applies.md" in reads, "a file edited just now must be parsed again"
    assert alpha == _HAMMER - {"mem-applies"}
    assert expand.matching_memories(root, "mallet", tenant="alpha")[0] == []
