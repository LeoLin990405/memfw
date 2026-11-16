"""Projection export, resolve, filter matrix and CLI contract tests (R6 PR-1).

Spec: memfw-qmd-fusion-design §3.2–3.8, §5.3, §5.4, §5.6, Appendix C. Every
vault here is synthetic (``tests/projection_fixtures.py``) and lives in tmp.
"""

from __future__ import annotations

import hashlib
import json
import re
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from projection_fixtures import (
    DOCS,
    EXTRA_DOCS,
    FIXTURE_DIR,
    SEARCH_QUERIES,
    build_search_vault,
    build_vault,
    patch_frontmatter,
    tree_digest,
    write_meta,
)
from typer.testing import CliRunner

from memfw.cli import app
from memfw.projection import (
    DEFAULT_CLEARANCE,
    MAX_RESOLVE_BATCH,
    FilterSettings,
    ProjectionError,
    ProjectionPolicy,
    export_projection,
    filter_decision,
    filter_resolved,
    load_policy,
    resolve,
)

NOW = datetime(2026, 11, 10, 1, 0, tzinfo=UTC)
NOW_ISO = "2026-11-10T01:00:00Z"
LATER = datetime(2026, 11, 10, 2, 0, tzinfo=UTC)
SETTINGS = FilterSettings(now=NOW)
POLICY_V2_FILE = FIXTURE_DIR / "policy_v2.json"
BASELINE_FILE = FIXTURE_DIR / "index_search_baseline.json"

ID = {name: spec["id"] for name, spec in {**DOCS, **EXTRA_DOCS}.items()}

#: Documents the default v1 policy projects from ``DOCS``.
PROJECTED = {
    "active",
    "private",
    "privacy_mismatch",
    "privacy_unknown",
    "eval_native",
    "expired_vault",
    "expired_meta",
    "expiry_unparsable",
    "superseded_vault",
    "superseded_meta",
    "tenant_other",
    "disputed",
    "anchor",
    "r3_hypothesis",
}
FAILED = {"native_no_tag", "orphan", "two_tags", "id_mismatch"}

MANIFEST_KEYS = {
    "schema_version",
    "projection_policy_version",
    "policy_sha256",
    "authority",
    "authority_head",
    "authority_content_head",
    "authority_sync_lag_seconds",
    "lifecycle_source",
    "lifecycle_source_generated_at",
    "lifecycle_source_status",
    "lifecycle_source_lag_seconds",
    "join",
    "generated_at_utc",
    "config",
    "counts",
    "projection_sha256",
    "tree",
    "documents",
    "previous_manifest_sha256",
}
MANIFEST_META_KEYS = {
    "status",
    "layer",
    "tier",
    "valid_to",
    "valid_until",
    "superseded_by",
    "tenant",
    "privacy",
    "privacy_class",
    "scope",
    "type",
    "lifecycle",
    "epistemic",
    "causal",
}
RESOLVE_KEYS = {
    "schema",
    "authority_head",
    "authority_content_head",
    "authority_sync_lag_seconds",
    "lifecycle_source_generated_at",
    "lifecycle_source_status",
    "projection_policy_version",
    "documents",
    "missing",
    "elapsed_ms",
}
RESOLVE_DOC_KEYS = {
    "id",
    "path",
    "fleet_path",
    "join_status",
    "title",
    "lifecycle",
    "superseded_by",
    "valid_to",
    "valid_until",
    "privacy_class",
    "privacy_raw",
    "tenant",
    "scope",
    "status",
    "layer",
    "tier",
    "is_stub",
    "type",
    "epistemic",
    "causal",
    "updated_at",
    "content_sha256",
    "body",
    "tags",
}
FRONTMATTER_V1 = [
    "id",
    "fleet_path",
    "title",
    "status",
    "layer",
    "tier",
    "valid_to",
    "superseded_by",
    "tenant",
    "type",
    "projection_policy_version",
]
FRONTMATTER_V2 = FRONTMATTER_V1[:3] + ["epistemic", "causal"] + FRONTMATTER_V1[3:]

runner = CliRunner()


# --------------------------------------------------------------------------- helpers


@pytest.fixture
def vault(tmp_path: Path) -> tuple[Path, Path]:
    return build_vault(tmp_path)


@pytest.fixture(scope="module")
def shared_vault(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    """Read-only vault shared by tests that never mutate it."""

    return build_vault(tmp_path_factory.mktemp("shared"))


@pytest.fixture(scope="module")
def shared_export(
    shared_vault: tuple[Path, Path], tmp_path_factory: pytest.TempPathFactory
) -> dict[str, Any]:
    root, meta = shared_vault
    return export_projection(
        root, tmp_path_factory.mktemp("out-v1"), lifecycle_source=meta, now=NOW
    )


@pytest.fixture(scope="module")
def shared_export_v2(
    shared_vault: tuple[Path, Path], tmp_path_factory: pytest.TempPathFactory
) -> dict[str, Any]:
    root, meta = shared_vault
    return export_projection(
        root,
        tmp_path_factory.mktemp("out-v2"),
        policy=load_policy(None, version=2),
        lifecycle_source=meta,
        now=NOW,
    )


@pytest.fixture(scope="module")
def resolved(shared_vault: tuple[Path, Path]) -> dict[str, dict[str, Any]]:
    root, meta = shared_vault
    payload = resolve(root, [ID[name] for name in DOCS], lifecycle_source=meta)
    return {name: _doc_by_id(payload, ID[name]) for name in DOCS}


def _doc_by_id(payload: dict[str, Any], memory_id: str) -> dict[str, Any]:
    return next(doc for doc in payload["documents"] if doc["id"] == memory_id)


def _manifest(result: dict[str, Any]) -> dict[str, Any]:
    return json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))


def _projection_file(result: dict[str, Any], name: str) -> Path:
    return Path(result["tree"]) / "files" / "memories" / f"{ID[name]}.md"


def _split(text: str) -> tuple[dict[str, Any], list[str], str]:
    """``(frontmatter, frontmatter key order, rest)`` of a projection file."""

    assert text.startswith("---\n")
    raw, rest = text[len("---\n") :].split("\n---\n", 1)
    data = yaml.safe_load(raw)
    keys = [line.split(":", 1)[0] for line in raw.splitlines() if re.match(r"^[a-z_]+:", line)]
    return data, keys, rest


def _label_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith("[memfw ")]


def _vault_snapshot(root: Path) -> tuple[dict[str, str], bytes]:
    return tree_digest(root), (root / "events.jsonl").read_bytes()


def _mem_path(root: Path, name: str) -> Path:
    return root / "memories" / f"{ID[name]}.md"


def _update_meta(meta_path: Path, name: str, **changes: Any) -> None:
    data = json.loads(meta_path.read_text(encoding="utf-8"))
    for item in data["items"]:
        if item["path"] == DOCS.get(name, EXTRA_DOCS.get(name))["rel"]:
            item.update(changes)
    write_meta(meta_path, data)


def _set_generated_at(meta_path: Path, value: str | None) -> None:
    data = json.loads(meta_path.read_text(encoding="utf-8"))
    if value is None:
        data.pop("generated_at", None)
    else:
        data["generated_at"] = value
    write_meta(meta_path, data)


def _record(**fields: Any) -> dict[str, Any]:
    """A minimal joined, keepable resolve record for direct filter_decision tests."""

    base: dict[str, Any] = {
        "join_status": "ok",
        "is_stub": False,
        "tier": "hot",
        "privacy_class": "none",
        "tenant": "",
        "lifecycle": "active",
        "valid_to": None,
        "valid_until": None,
        "superseded_by": None,
        "status": "unconfirmed",
        "layer": "current",
        "epistemic": None,
        "causal": None,
    }
    base.update(fields)
    return base


def _cli(args: list[str], stdin: str | None = None) -> Any:
    return runner.invoke(app, args, input=stdin)


def _cli_error(result: Any) -> None:
    assert result.exit_code == 2, (result.exit_code, result.stdout, result.stderr)
    assert result.stdout == ""
    lines = result.stderr.strip().splitlines()
    assert len(lines) == 1, result.stderr
    assert lines[0].startswith("error: ")


def _cli_has(args: list[str], needle: str) -> bool:
    result = runner.invoke(app, [*args, "--help"])
    return result.exit_code == 0 and needle in result.stdout


needs_projection_cli = pytest.mark.skipif(
    not _cli_has(["projection", "export"], "--lifecycle-source"),
    reason="memfw projection CLI not wired yet",
)
needs_resolve_cli = pytest.mark.skipif(
    not _cli_has(["resolve"], "--ids-stdin"), reason="memfw resolve CLI not wired yet"
)
needs_query_stdin = pytest.mark.skipif(
    not _cli_has(["index", "search"], "--query-stdin"),
    reason="index search --query-stdin not wired yet",
)


# ============================================================================ export


def test_export_counts_on_synthetic_vault(shared_export: dict[str, Any]) -> None:
    counts = shared_export["counts"]
    assert shared_export["status"] == "exported"
    assert shared_export["lifecycle_source_status"] == "ok"
    assert counts["vault_documents"] == len(DOCS) == 22
    assert counts["projected"] == len(PROJECTED) == 14
    assert counts["skipped"] == {
        "change_event": 1,
        "cold": 1,
        "stub": 1,
        "unjoined": 3,
        "id_mismatch": 1,
        "invalid": 0,
        "retired": 1,
    }
    assert counts["deleted_since_previous"] == 0
    assert shared_export["join"] == {"unjoined": 3, "id_mismatch": 1}


def test_export_privacy_counts_are_over_joined_documents(shared_export: dict[str, Any]) -> None:
    counts = shared_export["counts"]
    assert counts["privacy"] == {"local-private": 2, "none": 15, "unknown": 1}
    assert sum(counts["privacy"].values()) == len(DOCS) - len(FAILED)


def test_export_counts_privacy_source_mismatch(shared_export: dict[str, Any]) -> None:
    # privacy_mismatch (meta private, no tag) and privacy_unknown (meta unknown, no tag)
    assert shared_export["counts"]["privacy_source_mismatch"] == 2


def test_export_projects_exactly_the_expected_documents(shared_export: dict[str, Any]) -> None:
    files = {path.stem for path in (Path(shared_export["tree"]) / "files" / "memories").iterdir()}
    assert files == {ID[name] for name in PROJECTED}
    assert {doc["id"] for doc in _manifest(shared_export)["documents"]} == files


def test_export_failures_list_every_unjoined_and_id_mismatch_doc(
    shared_export: dict[str, Any],
) -> None:
    lines = Path(shared_export["failures_path"]).read_text(encoding="utf-8").splitlines()
    failures = {item["id"]: item for item in map(json.loads, lines)}
    assert set(failures) == {ID[name] for name in FAILED}
    assert failures[ID["id_mismatch"]]["reason"] == "id_mismatch"
    assert failures[ID["id_mismatch"]]["fleet_path"] == DOCS["id_mismatch"]["rel"]
    for name in ("native_no_tag", "orphan", "two_tags"):
        assert failures[ID[name]]["reason"] == "unjoined"
    tree = Path(shared_export["tree"]) / "files" / "memories"
    for name in FAILED:
        assert not (tree / f"{ID[name]}.md").exists()


def test_export_policy_v1_files_have_no_epistemic_or_causal_keys(
    shared_export: dict[str, Any],
) -> None:
    for name in PROJECTED:
        frontmatter, keys, _ = _split(_projection_file(shared_export, name).read_text("utf-8"))
        assert keys == FRONTMATTER_V1, name
        assert "epistemic" not in frontmatter and "causal" not in frontmatter
    for doc in _manifest(shared_export)["documents"]:
        assert doc["meta"]["epistemic"] is None
        assert doc["meta"]["causal"] is None


def test_export_policy_v1_label_line_is_unspecified_even_for_r3_doc(
    shared_export: dict[str, Any],
) -> None:
    text = _projection_file(shared_export, "r3_hypothesis").read_text("utf-8")
    first = _label_lines(text)[0]
    assert "epistemic=unspecified" in first
    assert "认知=未声明" in first
    assert "causal=none" in first
    assert "## Causal claim" not in text


def test_export_policy_v2_includes_epistemic_and_causal(shared_export_v2: dict[str, Any]) -> None:
    assert shared_export_v2["projection_policy_version"] == 2
    text = _projection_file(shared_export_v2, "r3_hypothesis").read_text("utf-8")
    frontmatter, keys, _ = _split(text)
    assert keys == FRONTMATTER_V2
    assert frontmatter["epistemic"] == "inference"
    assert frontmatter["causal"] == "hypothesis"
    assert frontmatter["projection_policy_version"] == 2
    first = _label_lines(text)[0]
    assert "epistemic=inference" in first and "认知=推断" in first
    assert "causal=hypothesis" in first and "因果=假设" in first
    meta = _doc_by_id(_manifest(shared_export_v2), ID["r3_hypothesis"])["meta"]
    assert meta["epistemic"] == "inference"
    assert meta["causal"] == "hypothesis"


def test_export_policy_v2_causal_claim_section(shared_export_v2: dict[str, Any]) -> None:
    text = _projection_file(shared_export_v2, "r3_hypothesis").read_text("utf-8")
    section = text.split("## Causal claim\n", 1)[1]
    assert text.index("## Body") < text.index("## Causal claim")
    assert "- treatment: pause cron" in section
    assert "- outcome: migration not interrupted" in section
    assert "- estimand: ATE" in section
    assert "- identification: " in section and "no confounding" in section
    assert "- robustness: " in section


def test_export_policy_v2_doc_without_epistemic_renders_unspecified(
    shared_export_v2: dict[str, Any],
) -> None:
    text = _projection_file(shared_export_v2, "active").read_text("utf-8")
    frontmatter, keys, _ = _split(text)
    assert keys == FRONTMATTER_V2
    assert frontmatter["epistemic"] is None
    assert frontmatter["causal"] == "none"
    first = _label_lines(text)[0]
    assert "epistemic=unspecified" in first and "causal=none" in first
    assert "## Causal claim" not in text


def test_policy_v2_file_equals_policy_version_override() -> None:
    from_file = load_policy(POLICY_V2_FILE)
    assert from_file.projection_policy_version == 2
    assert from_file == load_policy(None, version=2)
    assert from_file.sha256 == load_policy(None, version=2).sha256
    assert from_file.sha256 != ProjectionPolicy().sha256


def test_export_policy_v2_file_matches_version_override(
    vault: tuple[Path, Path], tmp_path: Path, shared_export_v2: dict[str, Any]
) -> None:
    root, meta = vault
    result = export_projection(
        root,
        tmp_path / "out",
        policy=load_policy(POLICY_V2_FILE),
        lifecycle_source=meta,
        now=NOW,
    )
    manifest = _manifest(result)
    assert manifest["projection_policy_version"] == 2
    assert manifest["policy_sha256"] == _manifest(shared_export_v2)["policy_sha256"]
    assert manifest["projection_sha256"] == _manifest(shared_export_v2)["projection_sha256"]


def test_export_template_structure(shared_export: dict[str, Any]) -> None:
    text = _projection_file(shared_export, "active").read_text("utf-8")
    frontmatter, keys, rest = _split(text)
    assert keys == FRONTMATTER_V1
    assert frontmatter == {
        "id": ID["active"],
        "fleet_path": DOCS["active"]["rel"],
        "title": DOCS["active"]["title"],
        "status": "unconfirmed",
        "layer": "current",
        "tier": "hot",
        "valid_to": None,
        "superseded_by": None,
        "tenant": "",
        "type": "fact",
        "projection_policy_version": 1,
    }
    lines = rest.splitlines()
    body_lines = [line for line in lines if line]
    assert body_lines[0].startswith("<!-- memfw-stable-labels v1;")
    assert body_lines[1].startswith("[memfw ") and body_lines[2].startswith("[memfw ")
    assert len(_label_lines(text)) == 2
    assert body_lines[1] == (
        "[memfw 认知=未声明 epistemic=unspecified | 因果=none causal=none | type=fact]"
    )
    assert body_lines[2] == (
        "[memfw tags=nas1,cron,migration | about=hd-migration | applies_to=nas1 | anchor=none]"
    )
    h1 = [line for line in lines if line.startswith("# ")]
    assert h1 == [f"# {DOCS['active']['title']}"]
    assert "## Summary\nPause cron first.\n" in rest
    assert "## Body\n" + DOCS["active"]["body"] + "\n" in rest
    assert rest.index("# ") < rest.index("## Summary") < rest.index("## Body")


def test_export_template_summary_only_when_non_empty(shared_export: dict[str, Any]) -> None:
    text = _projection_file(shared_export, "privacy_mismatch").read_text("utf-8")
    assert "## Summary" not in text
    assert "## Body\n" in text


def test_export_template_tags_strip_fleet_path_and_sensitivity(
    shared_export: dict[str, Any],
) -> None:
    text = _projection_file(shared_export, "private").read_text("utf-8")
    assert _label_lines(text)[1].startswith("[memfw tags=none |")
    for name in PROJECTED:
        text = _projection_file(shared_export, name).read_text("utf-8")
        second = _label_lines(text)[1]
        assert "fleet_path:" not in second and "sensitivity:" not in second


def test_export_template_never_writes_lifecycle_privacy_or_valid_until(
    shared_export: dict[str, Any],
) -> None:
    for name in PROJECTED:
        text = _projection_file(shared_export, name).read_text("utf-8")
        frontmatter, _, rest = _split(text)
        for key in ("lifecycle", "privacy", "privacy_class", "valid_until", "scope"):
            assert key not in frontmatter
            assert f"{key}:" not in text
            assert f"{key}=" not in text
        assert "local-private" not in "\n".join(_label_lines(text))
        assert "team-secret" not in text
    active = _projection_file(shared_export, "active").read_text("utf-8")
    for word in ("lifecycle", "privacy", "valid_until", "active", "retired"):
        assert word not in active


def test_export_include_change_events_projects_change_event_type(
    vault: tuple[Path, Path], tmp_path: Path
) -> None:
    root, meta = vault
    data = ProjectionPolicy().to_json() | {"include_change_events": True}
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps(data), encoding="utf-8")
    result = export_projection(
        root, tmp_path / "out", policy=load_policy(policy_path), lifecycle_source=meta, now=NOW
    )
    assert result["counts"]["skipped"]["change_event"] == 0
    assert result["counts"]["projected"] == len(PROJECTED) + 1
    text = _projection_file(result, "change_event").read_text("utf-8")
    frontmatter, _, _ = _split(text)
    assert frontmatter["type"] == "change-event"
    assert "type=change-event]" in _label_lines(text)[0]
    manifest = _manifest(result)
    assert manifest["config"]["include_change_events"] is True
    assert _doc_by_id(manifest, ID["change_event"])["meta"]["type"] == "change-event"


def test_export_manifest_schema(shared_export: dict[str, Any], shared_vault: Any) -> None:
    raw = Path(shared_export["manifest_path"]).read_bytes()
    manifest = json.loads(raw)
    assert set(manifest) == MANIFEST_KEYS
    assert manifest["schema_version"] == 1
    assert manifest["projection_policy_version"] == 1
    assert manifest["policy_sha256"] == ProjectionPolicy().sha256
    assert manifest["authority"] == "files"
    assert manifest["authority_content_head"] == shared_export["authority_content_head"]
    assert re.fullmatch(r"[0-9a-f]{64}", manifest["authority_content_head"])
    assert manifest["lifecycle_source"] == "status/memory-meta-index.json"
    assert manifest["lifecycle_source_generated_at"] == "2026-11-10T00:05:00Z"
    assert manifest["lifecycle_source_status"] == "ok"
    assert manifest["generated_at_utc"] == NOW_ISO
    assert manifest["join"] == {
        "key": "fleet_path",
        "id_rule": "f+sha256(rel)[:12]",
        "unjoined": 3,
        "id_mismatch": 1,
    }
    assert manifest["config"] == {
        "include_change_events": False,
        "exclude_tiers": ["cold"],
        "exclude_stubbed": True,
        "exclude_lifecycle": ["retired"],
        "tenant_map": "identity",
        "skip_privacy": [],
    }
    assert manifest["counts"] == shared_export["counts"]
    assert manifest["previous_manifest_sha256"] is None
    assert manifest["tree"] == Path(shared_export["tree"]).name
    assert shared_export["manifest_sha256"] == hashlib.sha256(raw).hexdigest()


def test_export_manifest_documents_sorted_with_meta_keys(shared_export: dict[str, Any]) -> None:
    documents = _manifest(shared_export)["documents"]
    ids = [doc["id"] for doc in documents]
    assert ids == sorted(ids)
    for doc in documents:
        assert set(doc) == {"id", "path", "fleet_path", "content_sha256", "meta"}
        assert doc["path"] == f"memories/{doc['id']}.md"
        assert set(doc["meta"]) == MANIFEST_META_KEYS
    active = _doc_by_id(_manifest(shared_export), ID["active"])
    assert active["fleet_path"] == DOCS["active"]["rel"]
    assert active["meta"] == {
        "status": "unconfirmed",
        "layer": "current",
        "tier": "hot",
        "valid_to": None,
        "valid_until": None,
        "superseded_by": None,
        "tenant": "",
        "privacy": None,
        "privacy_class": "none",
        "scope": "fleet",
        "type": "fact",
        "lifecycle": "active",
        "epistemic": None,
        "causal": None,
    }


def test_export_manifest_content_sha256_matches_written_file(
    shared_export: dict[str, Any],
) -> None:
    files = Path(shared_export["tree"]) / "files"
    for doc in _manifest(shared_export)["documents"]:
        data = (files / doc["path"]).read_bytes()
        assert doc["content_sha256"] == hashlib.sha256(data).hexdigest()


def test_export_manifest_meta_carries_lifecycle_privacy_validity(
    shared_export: dict[str, Any],
) -> None:
    manifest = _manifest(shared_export)
    assert _doc_by_id(manifest, ID["private"])["meta"]["privacy"] == "local-private"
    assert _doc_by_id(manifest, ID["private"])["meta"]["privacy_class"] == "local-private"
    assert _doc_by_id(manifest, ID["privacy_unknown"])["meta"]["privacy_class"] == "unknown"
    assert _doc_by_id(manifest, ID["expired_meta"])["meta"]["valid_until"] == "2026-02-01"
    assert _doc_by_id(manifest, ID["expired_vault"])["meta"]["valid_to"] == "2026-01-01T00:00:00Z"
    assert _doc_by_id(manifest, ID["superseded_meta"])["meta"]["lifecycle"] == "superseded"
    assert _doc_by_id(manifest, ID["superseded_vault"])["meta"]["superseded_by"] == ID["active"]
    assert _doc_by_id(manifest, ID["tenant_other"])["meta"]["tenant"] == "other-team"


def test_export_file_modes_are_private(shared_export: dict[str, Any]) -> None:
    tree = Path(shared_export["tree"])
    files = [path for path in tree.rglob("*") if path.is_file()]
    assert {path.name for path in files} >= {"manifest.json", "delta.json", "export-failures.jsonl"}
    for path in files:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600, path
    for path in [tree, *(p for p in tree.rglob("*") if p.is_dir())]:
        assert stat.S_IMODE(path.stat().st_mode) & 0o007 == 0, path


def test_export_no_change_fast_path_writes_nothing(
    vault: tuple[Path, Path], tmp_path: Path
) -> None:
    root, meta = vault
    out = tmp_path / "out"
    first = export_projection(root, out, lifecycle_source=meta, now=NOW)
    before = tree_digest(out)
    for previous in (Path(first["tree"]), Path(first["manifest_path"])):
        again = export_projection(root, out, lifecycle_source=meta, previous=previous, now=LATER)
        assert again["status"] == "no_change_fast_path"
        assert again["manifest_sha256"] == first["manifest_sha256"]
        assert again["authority_content_head"] == first["authority_content_head"]
        assert again["counts"] == first["counts"]
        assert tree_digest(out) == before
        assert sorted(p.name for p in out.iterdir()) == [Path(first["tree"]).name]


def test_export_reexports_when_vault_file_changes(vault: tuple[Path, Path], tmp_path: Path) -> None:
    root, meta = vault
    out = tmp_path / "out"
    first = export_projection(root, out, lifecycle_source=meta, now=NOW)
    patch_frontmatter(_mem_path(root, "disputed"), {"summary": "now with a summary"})
    again = export_projection(
        root, out, lifecycle_source=meta, previous=Path(first["tree"]), now=LATER
    )
    assert again["status"] == "exported"
    assert again["authority_content_head"] != first["authority_content_head"]
    delta = json.loads(Path(again["delta_path"]).read_text("utf-8"))
    assert delta["changed"] == [ID["disputed"]]


def test_export_reexports_when_meta_item_changes(vault: tuple[Path, Path], tmp_path: Path) -> None:
    root, meta = vault
    out = tmp_path / "out"
    first = export_projection(root, out, lifecycle_source=meta, now=NOW)
    _update_meta(meta, "active", scope="personal")
    again = export_projection(
        root, out, lifecycle_source=meta, previous=Path(first["tree"]), now=LATER
    )
    assert again["status"] == "exported"
    assert again["authority_content_head"] != first["authority_content_head"]
    assert _doc_by_id(_manifest(again), ID["active"])["meta"]["scope"] == "personal"
    delta = json.loads(Path(again["delta_path"]).read_text("utf-8"))
    assert delta == {
        "schema_version": 1,
        "previous_manifest_sha256": first["manifest_sha256"],
        "added": [],
        "changed": [],
        "deleted": [],
    }


def test_export_reexports_when_policy_changes(vault: tuple[Path, Path], tmp_path: Path) -> None:
    root, meta = vault
    out = tmp_path / "out"
    first = export_projection(root, out, lifecycle_source=meta, now=NOW)
    again = export_projection(
        root,
        out,
        policy=load_policy(None, version=2),
        lifecycle_source=meta,
        previous=Path(first["tree"]),
        now=LATER,
    )
    assert again["status"] == "exported"
    assert again["authority_content_head"] == first["authority_content_head"]
    delta = json.loads(Path(again["delta_path"]).read_text("utf-8"))
    assert delta["changed"] == sorted(ID[name] for name in PROJECTED)


def test_export_reexports_when_lifecycle_status_changes(
    vault: tuple[Path, Path], tmp_path: Path
) -> None:
    root, meta = vault
    out = tmp_path / "out"
    first = export_projection(root, out, lifecycle_source=meta, now=NOW)
    _set_generated_at(meta, "2026-11-09T00:00:00Z")
    again = export_projection(
        root, out, lifecycle_source=meta, previous=Path(first["tree"]), now=LATER
    )
    assert again["status"] == "exported"
    assert again["lifecycle_source_status"] == "stale"
    assert again["authority_content_head"] == first["authority_content_head"]


def test_export_meta_generated_at_change_alone_keeps_fast_path(
    vault: tuple[Path, Path], tmp_path: Path
) -> None:
    root, meta = vault
    out = tmp_path / "out"
    first = export_projection(root, out, lifecycle_source=meta, now=NOW)
    _set_generated_at(meta, "2026-11-10T00:30:00Z")
    again = export_projection(
        root, out, lifecycle_source=meta, previous=Path(first["tree"]), now=LATER
    )
    assert again["status"] == "no_change_fast_path"
    assert again["authority_content_head"] == first["authority_content_head"]


def test_export_delta_added_changed_deleted(vault: tuple[Path, Path], tmp_path: Path) -> None:
    root, meta = vault
    out = tmp_path / "out"
    first = export_projection(root, out, lifecycle_source=meta, now=NOW)
    _mem_path(root, "anchor").unlink()
    patch_frontmatter(_mem_path(root, "disputed"), {"summary": "changed"})
    _update_meta(meta, "retired", lifecycle="active")
    again = export_projection(
        root, out, lifecycle_source=meta, previous=Path(first["tree"]), now=LATER
    )
    delta = json.loads(Path(again["delta_path"]).read_text("utf-8"))
    assert delta["added"] == [ID["retired"]]
    assert delta["changed"] == [ID["disputed"]]
    assert delta["deleted"] == [ID["anchor"]]
    assert delta["previous_manifest_sha256"] == first["manifest_sha256"]
    assert again["counts"]["deleted_since_previous"] == 1
    assert _manifest(again)["previous_manifest_sha256"] == first["manifest_sha256"]


def test_export_without_previous_delta_adds_everything(shared_export: dict[str, Any]) -> None:
    delta = json.loads(Path(shared_export["delta_path"]).read_text("utf-8"))
    assert delta["previous_manifest_sha256"] is None
    assert delta["added"] == sorted(ID[name] for name in PROJECTED)
    assert delta["changed"] == [] and delta["deleted"] == []


def test_export_reexport_same_second_after_meta_only_change(
    vault: tuple[Path, Path], tmp_path: Path
) -> None:
    root, meta = vault
    out = tmp_path / "out"
    first = export_projection(root, out, lifecycle_source=meta, now=NOW)
    _update_meta(meta, "active", scope="personal")
    again = export_projection(
        root, out, lifecycle_source=meta, previous=Path(first["tree"]), now=NOW
    )
    assert again["status"] == "exported"


def test_export_out_inside_vault_is_error(vault: tuple[Path, Path]) -> None:
    root, meta = vault
    before = _vault_snapshot(root)
    with pytest.raises(ProjectionError, match="inside the vault"):
        export_projection(root, root / "projection", lifecycle_source=meta, now=NOW)
    assert _vault_snapshot(root) == before
    assert not (root / "projection").exists()


def test_export_unreadable_lifecycle_source_is_error(
    vault: tuple[Path, Path], tmp_path: Path
) -> None:
    root, _ = vault
    with pytest.raises(ProjectionError, match="lifecycle source unreadable"):
        export_projection(root, tmp_path / "out", lifecycle_source=tmp_path / "nope.json", now=NOW)
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    with pytest.raises(ProjectionError, match="lifecycle source unreadable"):
        export_projection(root, tmp_path / "out", lifecycle_source=broken, now=NOW)
    broken.write_text('{"items": "x"}', encoding="utf-8")
    with pytest.raises(ProjectionError):
        export_projection(root, tmp_path / "out", lifecycle_source=broken, now=NOW)
    assert not (tmp_path / "out").exists()


def test_export_not_a_vault_is_error(tmp_path: Path) -> None:
    (tmp_path / "empty").mkdir()
    with pytest.raises(ProjectionError, match="not a memfw vault"):
        export_projection(tmp_path / "empty", tmp_path / "out", now=NOW)
    with pytest.raises(ProjectionError, match="unreadable"):
        export_projection(tmp_path / "missing", tmp_path / "out", now=NOW)


@pytest.mark.parametrize(
    ("generated_at", "status", "lag"),
    [
        ("2026-11-09T23:49:59Z", "stale", 601),
        ("2026-11-09T23:50:00Z", "ok", 600),
        ("2026-11-10T00:05:00Z", "ok", 0),
        (None, "stale", None),
        ("not a date", "stale", None),
    ],
)
def test_export_lifecycle_source_status(
    vault: tuple[Path, Path], tmp_path: Path, generated_at: str | None, status: str, lag: Any
) -> None:
    root, meta = vault
    _set_generated_at(meta, generated_at)
    result = export_projection(root, tmp_path / "out", lifecycle_source=meta, now=NOW)
    assert result["lifecycle_source_status"] == status
    manifest = _manifest(result)
    assert manifest["lifecycle_source_status"] == status
    assert manifest["lifecycle_source_lag_seconds"] == lag


def test_export_without_lifecycle_source_is_unavailable_and_projects_nothing(
    vault: tuple[Path, Path], tmp_path: Path
) -> None:
    root, _ = vault
    result = export_projection(root, tmp_path / "out", now=NOW)
    assert result["lifecycle_source_status"] == "unavailable"
    assert result["counts"]["projected"] == 0
    assert result["join"]["id_mismatch"] == 1
    assert result["join"]["unjoined"] == len(DOCS) - 1
    assert result["counts"]["privacy"] == {}
    manifest = _manifest(result)
    assert manifest["lifecycle_source_generated_at"] is None
    assert manifest["documents"] == []


@pytest.mark.parametrize("marker", [".pending.json", ".event-append.pending.json"])
def test_export_pending_transaction_is_error(
    vault: tuple[Path, Path], tmp_path: Path, marker: str
) -> None:
    root, meta = vault
    (root / marker).write_text("{}", encoding="utf-8")
    with pytest.raises(ProjectionError, match="pending transaction"):
        export_projection(root, tmp_path / "out", lifecycle_source=meta, now=NOW)
    with pytest.raises(ProjectionError, match="pending transaction"):
        resolve(root, [ID["active"]], lifecycle_source=meta)


@pytest.mark.parametrize(
    "overrides",
    [
        {"unknown_key": 1},
        {"schema_version": 2},
        {"projection_policy_version": 3},
        {"projection_policy_version": 0},
        {"join_key": "id"},
        {"tenant_map": "scope"},
        {"label_line_version": 2},
        {"lifecycle_max_lag_seconds": -1},
        {"lifecycle_max_lag_seconds": "600"},
        {"lifecycle_max_lag_seconds": True},
        {"include_change_events": "yes"},
        {"exclude_stubbed": 1},
        {"change_event_marker": {"tag_prefix": "x", "type": "y", "extra": 1}},
        {"change_event_marker": "fleet_path:"},
        {"exclude_tiers": "cold"},
        {"exclude_lifecycle": [1]},
    ],
)
def test_bad_policy_is_error(tmp_path: Path, overrides: dict[str, Any]) -> None:
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(ProjectionPolicy().to_json() | overrides), encoding="utf-8")
    with pytest.raises(ProjectionError):
        load_policy(path)


def test_bad_policy_file_and_version_override_are_errors(tmp_path: Path) -> None:
    with pytest.raises(ProjectionError, match="unreadable"):
        load_policy(tmp_path / "missing.json")
    path = tmp_path / "policy.json"
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(ProjectionError, match="JSON object"):
        load_policy(path)
    path.write_text("{bad", encoding="utf-8")
    with pytest.raises(ProjectionError):
        load_policy(path)
    with pytest.raises(ProjectionError):
        load_policy(None, version=3)


def test_policy_defaults_match_spec_policy_file() -> None:
    spec = json.loads(POLICY_V2_FILE.read_text("utf-8")) | {"projection_policy_version": 1}
    assert ProjectionPolicy.from_mapping(spec) == ProjectionPolicy()
    assert load_policy(None) == ProjectionPolicy()


# =========================================================================== resolve


def test_resolve_output_schema(shared_vault: tuple[Path, Path]) -> None:
    root, meta = shared_vault
    payload = resolve(root, [ID["active"]], lifecycle_source=meta)
    assert RESOLVE_KEYS <= set(payload)
    assert payload["schema"] == "memfw-resolve-v1"
    assert payload["lifecycle_source_generated_at"] == "2026-11-10T00:05:00Z"
    assert payload["lifecycle_source_status"] == "ok"
    assert payload["projection_policy_version"] == 1
    assert payload["missing"] == []
    assert isinstance(payload["elapsed_ms"], int)
    (doc,) = payload["documents"]
    assert RESOLVE_DOC_KEYS <= set(doc)
    assert doc["id"] == ID["active"]
    assert doc["path"] == f"memories/{ID['active']}.md"
    assert doc["fleet_path"] == DOCS["active"]["rel"]
    assert doc["join_status"] == "ok"
    assert doc["title"] == DOCS["active"]["title"]
    assert doc["lifecycle"] == "active"
    assert doc["privacy_class"] == "none"
    assert doc["privacy_raw"] is None
    assert doc["tenant"] == ""
    assert doc["scope"] == "fleet"
    assert (doc["status"], doc["layer"], doc["tier"], doc["is_stub"]) == (
        "unconfirmed",
        "current",
        "hot",
        False,
    )
    assert doc["type"] == "fact"
    assert doc["updated_at"] == "2026-11-10T00:00:00Z"
    assert doc["body"] == DOCS["active"]["body"]
    assert doc["tags"] == ["nas1", "cron", "migration"]
    raw = _mem_path(root, "active").read_bytes()
    assert doc["content_sha256"] == hashlib.sha256(raw).hexdigest()


def test_resolve_authority_content_head_equals_export_head(
    shared_vault: tuple[Path, Path], shared_export: dict[str, Any]
) -> None:
    root, meta = shared_vault
    payload = resolve(root, [ID["active"]], lifecycle_source=meta)
    assert payload["authority_content_head"] == shared_export["authority_content_head"]
    assert payload["authority_head"] == _manifest(shared_export)["authority_head"]


def test_resolve_keeps_input_order_and_removes_duplicates(
    shared_vault: tuple[Path, Path],
) -> None:
    root, meta = shared_vault
    hits = [
        ID["disputed"],
        ID["active"],
        ID["disputed"],
        f"memories/{ID['active']}.md",
        ID["anchor"],
    ]
    payload = resolve(root, hits, lifecycle_source=meta)
    assert [doc["id"] for doc in payload["documents"]] == [
        ID["disputed"],
        ID["active"],
        ID["anchor"],
    ]


def test_resolve_missing_ids(shared_vault: tuple[Path, Path]) -> None:
    root, meta = shared_vault
    payload = resolve(root, ["f999999999999", ID["active"], "zeval-9999"], lifecycle_source=meta)
    assert payload["missing"] == ["f999999999999", "zeval-9999"]
    assert [doc["id"] for doc in payload["documents"]] == [ID["active"]]


def test_resolve_unresolved_hits(shared_vault: tuple[Path, Path]) -> None:
    root, meta = shared_vault
    hits = ["files/example_gen8_cron.md", "hello world", ID["active"]]
    payload = resolve(root, hits, lifecycle_source=meta)
    assert payload["unresolved"] == ["files/example_gen8_cron.md", "hello world"]
    assert payload["missing"] == []
    assert [doc["id"] for doc in payload["documents"]] == [ID["active"]]


@pytest.mark.parametrize(
    "template",
    [
        "memories/{id}.md",
        "fleet-memory/memories/{id}.md",
        "qmd://fleet-memory/memories/{id}.md",
        "{id}\tsome title",
    ],
)
def test_resolve_path_form_hits(shared_vault: tuple[Path, Path], template: str) -> None:
    root, meta = shared_vault
    hit = template.format(id=ID["active"])
    payload = resolve(root, [hit], lifecycle_source=meta)
    (doc,) = payload["documents"]
    assert doc["id"] == ID["active"]
    assert doc["hit"] == hit


@pytest.mark.parametrize("name", sorted(FAILED))
def test_resolve_non_ok_join_nulls_lifecycle_privacy_body(
    resolved: dict[str, dict[str, Any]], name: str
) -> None:
    doc = resolved[name]
    assert doc["join_status"] == ("id_mismatch" if name == "id_mismatch" else "unjoined")
    assert doc["lifecycle"] is None
    assert doc["privacy_class"] is None
    assert doc["body"] is None


def test_resolve_superseded_by_from_vault(resolved: dict[str, dict[str, Any]]) -> None:
    assert resolved["superseded_vault"]["superseded_by"] == ID["active"]
    assert resolved["superseded_meta"]["superseded_by"] is None
    assert resolved["superseded_meta"]["lifecycle"] == "superseded"


def test_resolve_superseded_by_from_meta(tmp_path: Path) -> None:
    root, meta = build_vault(tmp_path, {**DOCS, **EXTRA_DOCS})
    payload = resolve(root, [ID["superseded_meta_by"]], lifecycle_source=meta)
    (doc,) = payload["documents"]
    assert doc["superseded_by"] == ID["active"]
    assert doc["lifecycle"] == "active"


def test_resolve_reports_valid_to_and_valid_until_separately(
    resolved: dict[str, dict[str, Any]],
) -> None:
    assert resolved["expired_vault"]["valid_to"] == "2026-01-01T00:00:00Z"
    assert resolved["expired_vault"]["valid_until"] is None
    assert resolved["expired_meta"]["valid_to"] is None
    assert resolved["expired_meta"]["valid_until"] == "2026-02-01"
    assert resolved["expiry_unparsable"]["valid_until"] == "next tuesday"


def test_resolve_privacy_fields(resolved: dict[str, dict[str, Any]]) -> None:
    assert resolved["private"]["privacy_class"] == "local-private"
    assert resolved["private"]["privacy_raw"] == "local-private"
    assert resolved["private"]["tags"] == []
    assert resolved["privacy_mismatch"]["privacy_class"] == "local-private"
    assert resolved["privacy_unknown"]["privacy_class"] == "unknown"
    assert resolved["privacy_unknown"]["privacy_raw"] == "team-secret"


def test_resolve_policy_v1_epistemic_causal_null(resolved: dict[str, dict[str, Any]]) -> None:
    assert resolved["r3_hypothesis"]["epistemic"] is None
    assert resolved["r3_hypothesis"]["causal"] is None


def test_resolve_policy_v2_epistemic_causal_actual(shared_vault: tuple[Path, Path]) -> None:
    root, meta = shared_vault
    payload = resolve(
        root,
        [ID["r3_hypothesis"], ID["active"]],
        lifecycle_source=meta,
        policy=load_policy(None, version=2),
    )
    assert payload["projection_policy_version"] == 2
    r3, active = payload["documents"]
    assert (r3["epistemic"], r3["causal"]) == ("inference", "hypothesis")
    assert (active["epistemic"], active["causal"]) == (None, "none")


def test_resolve_max_lines_truncation(tmp_path: Path) -> None:
    root, meta = build_vault(tmp_path, {**DOCS, **EXTRA_DOCS})
    hits = [ID["multiline"]]
    (full,) = resolve(root, hits, lifecycle_source=meta)["documents"]
    assert full["body"] == EXTRA_DOCS["multiline"]["body"]
    assert full["body_truncated"] is False
    (cut,) = resolve(root, hits, lifecycle_source=meta, max_lines=2)["documents"]
    assert cut["body"] == "line one\nline two"
    assert cut["body_truncated"] is True
    (exact,) = resolve(root, hits, lifecycle_source=meta, max_lines=5)["documents"]
    assert exact["body_truncated"] is False
    (none,) = resolve(root, hits, lifecycle_source=meta, max_lines=0)["documents"]
    assert none["body"] is None
    assert none["title"] == EXTRA_DOCS["multiline"]["title"]


def test_resolve_more_than_50_is_error(shared_vault: tuple[Path, Path]) -> None:
    root, meta = shared_vault
    hits = [f"f{0x100 + index:012x}" for index in range(MAX_RESOLVE_BATCH + 1)]
    with pytest.raises(ProjectionError, match="at most 50"):
        resolve(root, hits, lifecycle_source=meta)
    payload = resolve(root, hits[:MAX_RESOLVE_BATCH], lifecycle_source=meta)
    assert len(payload["missing"]) == MAX_RESOLVE_BATCH


def test_resolve_retired_doc_is_resolvable(resolved: dict[str, dict[str, Any]]) -> None:
    doc = resolved["retired"]
    assert doc["join_status"] == "ok"
    assert doc["lifecycle"] == "retired"
    assert doc["body"] == DOCS["retired"]["body"]


def test_resolve_does_not_filter(shared_vault: tuple[Path, Path]) -> None:
    root, meta = shared_vault
    payload = resolve(root, [ID[name] for name in DOCS], lifecycle_source=meta)
    assert [doc["id"] for doc in payload["documents"]] == [ID[name] for name in DOCS]


def test_resolve_unreadable_lifecycle_source_or_root_is_error(
    shared_vault: tuple[Path, Path], tmp_path: Path
) -> None:
    root, _ = shared_vault
    with pytest.raises(ProjectionError, match="lifecycle source unreadable"):
        resolve(root, [ID["active"]], lifecycle_source=tmp_path / "nope.json")
    with pytest.raises(ProjectionError):
        resolve(tmp_path / "nope", [ID["active"]])


@pytest.mark.parametrize(
    ("generated_at", "status"),
    [("2026-11-09T23:49:59Z", "stale"), (None, "stale"), ("2026-11-10T00:05:00Z", "ok")],
)
def test_resolve_lifecycle_source_status(
    vault: tuple[Path, Path], generated_at: str | None, status: str
) -> None:
    root, meta = vault
    _set_generated_at(meta, generated_at)
    assert resolve(root, [], lifecycle_source=meta)["lifecycle_source_status"] == status


def test_resolve_without_lifecycle_source_is_unavailable(
    shared_vault: tuple[Path, Path],
) -> None:
    root, _ = shared_vault
    payload = resolve(root, [ID["active"]])
    assert payload["lifecycle_source_status"] == "unavailable"
    assert payload["lifecycle_source_generated_at"] is None
    (doc,) = payload["documents"]
    assert doc["join_status"] == "unjoined"
    assert doc["lifecycle"] is None and doc["body"] is None


# ============================================================================ filter


def _decide(record: dict[str, Any], **settings: Any) -> tuple[str | None, list[str]]:
    return filter_decision(record, FilterSettings(now=NOW, **settings))


@pytest.mark.parametrize("name", sorted(FAILED))
def test_filter_unjoined_dropped(resolved: dict[str, dict[str, Any]], name: str) -> None:
    assert _decide(resolved[name])[0] == "filtered_unjoined"
    assert _decide(resolved[name], include_retired=True)[0] == "filtered_unjoined"


def test_filter_retired_dropped(resolved: dict[str, dict[str, Any]]) -> None:
    assert _decide(resolved["retired"]) == ("filtered_retired", [])


def test_filter_retired_kept_and_labelled_with_include_retired(
    resolved: dict[str, dict[str, Any]],
) -> None:
    counter, labels = _decide(resolved["retired"], include_retired=True)
    assert counter is None
    assert "retired" in labels


@pytest.mark.parametrize("name", ["superseded_vault", "superseded_meta"])
def test_filter_superseded_dropped(resolved: dict[str, dict[str, Any]], name: str) -> None:
    assert _decide(resolved[name])[0] == "filtered_superseded"


@pytest.mark.parametrize("name", ["superseded_vault", "superseded_meta"])
def test_filter_superseded_kept_and_labelled_with_include_retired(
    resolved: dict[str, dict[str, Any]], name: str
) -> None:
    counter, labels = _decide(resolved[name], include_retired=True)
    assert counter is None
    assert "superseded" in labels


def test_filter_superseded_by_from_meta_dropped(tmp_path: Path) -> None:
    root, meta = build_vault(tmp_path, {**DOCS, **EXTRA_DOCS})
    (doc,) = resolve(root, [ID["superseded_meta_by"]], lifecycle_source=meta)["documents"]
    assert _decide(doc)[0] == "filtered_superseded"


def test_filter_expired_via_valid_to(resolved: dict[str, dict[str, Any]]) -> None:
    assert _decide(resolved["expired_vault"])[0] == "filtered_expired"


def test_filter_expired_via_valid_until(resolved: dict[str, dict[str, Any]]) -> None:
    assert _decide(resolved["expired_meta"])[0] == "filtered_expired"


def test_filter_expired_kept_and_labelled_with_include_retired(
    resolved: dict[str, dict[str, Any]],
) -> None:
    # §5.4 note: include_retired also lets expired through, labelled.
    counter, labels = _decide(resolved["expired_meta"], include_retired=True)
    assert counter is None
    assert "expired" in labels


@pytest.mark.parametrize(
    ("valid_to", "valid_until", "expected"),
    [
        ("2027-01-01T00:00:00Z", "2026-02-01", "filtered_expired"),
        ("2026-02-01T00:00:00Z", "2027-01-01", "filtered_expired"),
        ("2027-01-01T00:00:00Z", "2027-02-01", None),
        (None, "2026-11-10T00:59:59Z", "filtered_expired"),
        (None, "2026-11-10T01:00:01Z", None),
        ("2026-11-10", None, "filtered_expired"),
    ],
)
def test_filter_expiry_earliest_wins(valid_to: Any, valid_until: Any, expected: str | None) -> None:
    record = _record(valid_to=valid_to, valid_until=valid_until)
    assert _decide(record)[0] == expected


def test_filter_expiry_unparsable_dropped_even_with_include_retired(
    resolved: dict[str, dict[str, Any]],
) -> None:
    assert _decide(resolved["expiry_unparsable"])[0] == "filtered_expiry_unparsable"
    assert (
        _decide(resolved["expiry_unparsable"], include_retired=True)[0]
        == "filtered_expiry_unparsable"
    )
    assert _decide(_record(valid_to="garbage"))[0] == "filtered_expiry_unparsable"


def test_filter_privacy_default_clearance_drops_private_and_unknown(
    resolved: dict[str, dict[str, Any]],
) -> None:
    assert SETTINGS.clearance == DEFAULT_CLEARANCE == frozenset({"none"})
    for name in ("private", "privacy_mismatch", "privacy_unknown"):
        assert _decide(resolved[name])[0] == "filtered_privacy", name
    assert _decide(resolved["active"])[0] is None


def test_filter_privacy_explicit_clearance_keeps_private_but_drops_unknown(
    resolved: dict[str, dict[str, Any]],
) -> None:
    clearance = frozenset({"none", "local-private"})
    assert _decide(resolved["private"], clearance=clearance)[0] is None
    assert _decide(resolved["privacy_mismatch"], clearance=clearance)[0] is None
    assert _decide(resolved["privacy_unknown"], clearance=clearance)[0] == "filtered_privacy"


def test_filter_privacy_null_class_dropped() -> None:
    assert _decide(_record(privacy_class=None))[0] == "filtered_privacy"


def test_filter_include_retired_never_bypasses_privacy_tenant_unjoined(
    resolved: dict[str, dict[str, Any]],
) -> None:
    assert _decide(resolved["private"], include_retired=True)[0] == "filtered_privacy"
    assert _decide(resolved["privacy_unknown"], include_retired=True)[0] == "filtered_privacy"
    assert _decide(resolved["tenant_other"], include_retired=True)[0] == "filtered_tenant"
    assert _decide(resolved["orphan"], include_retired=True)[0] == "filtered_unjoined"
    retired_private = _record(lifecycle="retired", privacy_class="local-private")
    assert _decide(retired_private, include_retired=True)[0] == "filtered_privacy"
    retired_other = _record(lifecycle="retired", tenant="other-team")
    assert _decide(retired_other, include_retired=True)[0] == "filtered_tenant"


def test_filter_tenant_default_allows_empty_and_fleet(
    resolved: dict[str, dict[str, Any]],
) -> None:
    assert _decide(_record(tenant=""))[0] is None
    assert _decide(_record(tenant="fleet"))[0] is None
    assert _decide(resolved["tenant_other"])[0] == "filtered_tenant"


def test_filter_tenant_explicit_set(resolved: dict[str, dict[str, Any]]) -> None:
    tenants = frozenset({"other-team"})
    assert _decide(resolved["tenant_other"], tenants=tenants)[0] is None
    assert _decide(resolved["active"], tenants=tenants)[0] == "filtered_tenant"


@pytest.mark.parametrize("name", ["cold", "stub"])
def test_filter_cold_and_stub_dropped(resolved: dict[str, dict[str, Any]], name: str) -> None:
    assert _decide(resolved[name])[0] == "filtered_archived"
    assert _decide(resolved[name], include_retired=True)[0] == "filtered_archived"


def test_filter_unknown_lifecycle_dropped() -> None:
    assert _decide(_record(lifecycle="weird"))[0] == "filtered_lifecycle_unknown"
    assert _decide(_record(lifecycle=None))[0] == "filtered_lifecycle_unknown"


def test_filter_disputed_kept_with_label_and_negative_rank_adjust(
    shared_vault: tuple[Path, Path],
) -> None:
    root, meta = shared_vault
    payload = resolve(root, [ID["disputed"], ID["active"]], lifecycle_source=meta)
    kept = filter_resolved(payload, SETTINGS)["documents"]
    disputed, active = kept
    assert "disputed" in disputed["labels"]
    assert disputed["rank_adjust"] < 0
    assert active["rank_adjust"] == 0
    assert active["labels"] == []


def test_filter_anchor_kept_with_positive_rank_adjust(shared_vault: tuple[Path, Path]) -> None:
    root, meta = shared_vault
    payload = resolve(root, [ID["anchor"]], lifecycle_source=meta)
    (anchor,) = filter_resolved(payload, SETTINGS)["documents"]
    assert "anchor" in anchor["labels"]
    assert anchor["rank_adjust"] > 0


def test_filter_consolidated_layer_labelled() -> None:
    counter, labels = _decide(_record(layer="consolidated"))
    assert counter is None
    assert "consolidated" in labels


def test_filter_epistemic_causal_labels(shared_vault: tuple[Path, Path]) -> None:
    root, meta = shared_vault
    hits = [ID["r3_hypothesis"]]
    v2 = resolve(root, hits, lifecycle_source=meta, policy=load_policy(None, version=2))
    (doc,) = filter_resolved(v2, SETTINGS)["documents"]
    assert "epistemic:inference" in doc["labels"]
    assert "causal:hypothesis" in doc["labels"]
    v1 = resolve(root, hits, lifecycle_source=meta)
    (doc,) = filter_resolved(v1, SETTINGS)["documents"]
    assert not any(label.startswith(("epistemic:", "causal:")) for label in doc["labels"])


@pytest.mark.parametrize("status", ["stale", "unavailable", None])
def test_filter_fail_closed_when_lifecycle_source_not_ok(
    resolved: dict[str, dict[str, Any]], status: str | None
) -> None:
    payload = {
        "lifecycle_source_status": status,
        "documents": [resolved["active"]],
        "missing": ["f999999999999"],
        "unresolved": [],
    }
    result = filter_resolved(payload, SETTINGS)
    assert result["documents"] == []
    assert result["output_meta"]["fail_closed"] is True
    assert result["output_meta"]["fallback_reason"] == f"lifecycle_source_{status or 'unavailable'}"
    assert result["output_meta"]["filtered_missing"] == 1


def test_filter_fail_closed_on_real_stale_resolve(vault: tuple[Path, Path]) -> None:
    root, meta = vault
    _set_generated_at(meta, "2026-11-09T00:00:00Z")
    payload = resolve(root, [ID["active"]], lifecycle_source=meta)
    result = filter_resolved(payload, SETTINGS)
    assert result["documents"] == []
    assert result["output_meta"]["fail_closed"] is True
    assert result["output_meta"]["fallback_reason"] == "lifecycle_source_stale"


def test_filter_counters_over_full_vault(shared_vault: tuple[Path, Path]) -> None:
    root, meta = shared_vault
    hits = [ID[name] for name in DOCS] + ["f999999999999", "files/x.md"]
    payload = resolve(root, hits, lifecycle_source=meta)
    result = filter_resolved(payload, SETTINGS)
    output = result["output_meta"]
    assert output["fail_closed"] is False
    assert "fallback_reason" not in output
    assert output == {
        "filtered_unjoined": 4,
        "filtered_archived": 2,
        "filtered_privacy": 3,
        "filtered_tenant": 1,
        "filtered_lifecycle_unknown": 0,
        "filtered_expiry_unparsable": 1,
        "filtered_retired": 1,
        "filtered_superseded": 2,
        "filtered_expired": 2,
        "filtered_missing": 1,
        "filtered_unresolved": 1,
        "fail_closed": False,
    }
    kept = {doc["id"] for doc in result["documents"]}
    expected = {"active", "change_event", "disputed", "anchor", "r3_hypothesis", "eval_native"}
    assert kept == {ID[name] for name in expected}


# ========================================================================= read-only


def test_export_is_read_only_on_vault(vault: tuple[Path, Path], tmp_path: Path) -> None:
    root, meta = vault
    before = _vault_snapshot(root)
    export_projection(root, tmp_path / "out", lifecycle_source=meta, now=NOW)
    export_projection(
        root, tmp_path / "out2", policy=load_policy(None, version=2), lifecycle_source=meta, now=NOW
    )
    assert _vault_snapshot(root) == before


def test_resolve_is_read_only_on_vault(vault: tuple[Path, Path]) -> None:
    root, meta = vault
    before = _vault_snapshot(root)
    resolve(root, [ID[name] for name in DOCS], lifecycle_source=meta)
    resolve(root, ["files/x.md", "f999999999999"], lifecycle_source=meta, max_lines=0)
    assert _vault_snapshot(root) == before


@needs_projection_cli
def test_cli_export_is_read_only_on_vault(vault: tuple[Path, Path], tmp_path: Path) -> None:
    root, meta = vault
    before = _vault_snapshot(root)
    result = _cli(_export_args(root, tmp_path / "out", meta))
    assert result.exit_code == 0, result.stderr
    assert _vault_snapshot(root) == before


@needs_resolve_cli
def test_cli_resolve_is_read_only_on_vault(vault: tuple[Path, Path]) -> None:
    root, meta = vault
    before = _vault_snapshot(root)
    stdin = "\n".join(ID[name] for name in DOCS) + "\n"
    result = _cli(_resolve_args(root, meta), stdin)
    assert result.exit_code == 0, result.stderr
    assert _vault_snapshot(root) == before


@needs_query_stdin
def test_cli_index_search_dry_run_query_stdin_is_read_only(tmp_path: Path) -> None:
    root = build_search_vault(tmp_path)
    args = ["--now", NOW_ISO, "index", "search", "--query-stdin", "--root", str(root), "--dry-run"]
    before = _vault_snapshot(root)
    unbuilt = _cli(args, "cron\n")
    assert unbuilt.exit_code == 0, unbuilt.stderr
    assert _vault_snapshot(root) == before
    assert _cli(["index", "rebuild", "--root", str(root)]).exit_code == 0
    before = _vault_snapshot(root)
    built = _cli(args, "cron\n")
    assert built.exit_code == 0, built.stderr
    assert json.loads(built.stdout)["writes"] is False
    assert _vault_snapshot(root) == before


# ============================================================= index search identity


def _normalise(text: str) -> str:
    return re.sub(r'"elapsed_seconds": [0-9.e-]+', '"elapsed_seconds": 0.0', text)


def _search_outputs(tmp: Path, query: str) -> dict[str, list[Any]]:
    root = str(build_search_vault(tmp))
    dry = ["--now", NOW_ISO, "index", "search", query, "--root", root, "--dry-run"]
    r0 = _cli(dry)
    assert _cli(["index", "rebuild", "--root", root]).exit_code == 0
    r1 = _cli(dry)
    apply = ["--now", NOW_ISO, "--id-seed", "baseline", "index", "search", query, "--root", root]
    r2 = _cli(apply)
    return {
        "dry_run_unbuilt": [r0.exit_code, _normalise(r0.stdout)],
        "dry_run": [r1.exit_code, _normalise(r1.stdout)],
        "apply": [r2.exit_code, _normalise(r2.stdout)],
    }


@pytest.fixture(scope="module")
def search_baseline() -> dict[str, Any]:
    return json.loads(BASELINE_FILE.read_text(encoding="utf-8"))["outputs"]


@pytest.mark.parametrize("query", SEARCH_QUERIES)
def test_index_search_output_byte_identical_to_baseline(
    tmp_path: Path, search_baseline: dict[str, Any], query: str
) -> None:
    assert _search_outputs(tmp_path, query) == search_baseline[query]


@needs_query_stdin
@pytest.mark.parametrize("query", SEARCH_QUERIES)
def test_index_search_query_stdin_matches_positional(tmp_path: Path, query: str) -> None:
    root = str(build_search_vault(tmp_path / "a"))
    assert _cli(["index", "rebuild", "--root", root]).exit_code == 0
    for mode in (["--dry-run"], ["--apply"]):
        pos = _cli(
            ["--now", NOW_ISO, "--id-seed", "s", "index", "search", query, "--root", root, *mode]
        )
        via_stdin = _cli(
            ["--now", NOW_ISO, "--id-seed", "s", "index", "search", "--query-stdin", "--root", root]
            + mode,
            f"{query}\n",
        )
        assert pos.exit_code == via_stdin.exit_code == 0, (pos.stderr, via_stdin.stderr)
        assert _normalise(via_stdin.stdout) == _normalise(pos.stdout)


@needs_query_stdin
@pytest.mark.parametrize("query", SEARCH_QUERIES)
def test_index_search_query_stdin_matches_baseline_default_mode(
    tmp_path: Path, search_baseline: dict[str, Any], query: str
) -> None:
    root = str(build_search_vault(tmp_path))
    args = ["--now", NOW_ISO, "index", "search", "--query-stdin", "--root", root, "--dry-run"]
    r0 = _cli(args, query)
    assert [r0.exit_code, _normalise(r0.stdout)] == search_baseline[query]["dry_run_unbuilt"]
    assert _cli(["index", "rebuild", "--root", root]).exit_code == 0
    r1 = _cli(args, query)
    assert [r1.exit_code, _normalise(r1.stdout)] == search_baseline[query]["dry_run"]
    r2 = _cli(
        ["--now", NOW_ISO, "--id-seed", "baseline", "index", "search", "--query-stdin"]
        + ["--root", root],
        query,
    )
    assert [r2.exit_code, _normalise(r2.stdout)] == search_baseline[query]["apply"]


@needs_query_stdin
@pytest.mark.parametrize(
    ("args", "stdin"),
    [
        (["cron", "--query-stdin"], "cron\n"),
        ([], None),
        (["--query-stdin"], ""),
        (["--query-stdin"], "   \n"),
    ],
)
def test_index_search_query_source_errors(
    tmp_path: Path, args: list[str], stdin: str | None
) -> None:
    root = str(build_search_vault(tmp_path))
    result = _cli(["index", "search", *args, "--root", root, "--dry-run"], stdin)
    assert result.exit_code == 2, (result.stdout, result.stderr)


# ======================================================================= CLI: export


def _export_args(root: Path, out: Path, meta: Path | None, *extra: str) -> list[str]:
    args = ["--now", NOW_ISO, "projection", "export", "--root", str(root), "--out", str(out)]
    if meta is not None:
        args += ["--lifecycle-source", str(meta)]
    return [*args, *extra, "--json"]


def _one_line_json(result: Any) -> dict[str, Any]:
    assert result.exit_code == 0, (result.stdout, result.stderr)
    lines = result.stdout.splitlines()
    assert len(lines) == 1, result.stdout
    return json.loads(lines[0])


def _strip_volatile(payload: dict[str, Any]) -> dict[str, Any]:
    drop = {"elapsed_ms", "tree", "manifest_path", "delta_path", "failures_path"}
    return {key: value for key, value in payload.items() if key not in drop}


@needs_projection_cli
def test_cli_export_matches_library(vault: tuple[Path, Path], tmp_path: Path) -> None:
    root, meta = vault
    payload = _one_line_json(_cli(_export_args(root, tmp_path / "cli", meta)))
    library = export_projection(root, tmp_path / "lib", lifecycle_source=meta, now=NOW)
    assert _strip_volatile(payload) == _strip_volatile(library)
    assert set(payload) == set(library)
    assert Path(payload["tree"]).parent == tmp_path / "cli"
    assert _manifest(payload)["generated_at_utc"] == NOW_ISO
    assert Path(payload["tree"]).name == Path(library["tree"]).name


@needs_projection_cli
def test_cli_export_policy_version_and_policy_file(
    vault: tuple[Path, Path], tmp_path: Path
) -> None:
    root, meta = vault
    by_version = _one_line_json(
        _cli(_export_args(root, tmp_path / "a", meta, "--policy-version", "2"))
    )
    by_file = _one_line_json(
        _cli(_export_args(root, tmp_path / "b", meta, "--policy", str(POLICY_V2_FILE)))
    )
    assert by_version["projection_policy_version"] == by_file["projection_policy_version"] == 2
    assert by_version["manifest_sha256"] == by_file["manifest_sha256"]


@needs_projection_cli
def test_cli_export_previous_fast_path(vault: tuple[Path, Path], tmp_path: Path) -> None:
    root, meta = vault
    out = tmp_path / "out"
    first = _one_line_json(_cli(_export_args(root, out, meta)))
    before = tree_digest(out)
    again = _one_line_json(_cli(_export_args(root, out, meta, "--previous", first["tree"])))
    assert again["status"] == "no_change_fast_path"
    assert again["manifest_sha256"] == first["manifest_sha256"]
    assert tree_digest(out) == before


@needs_projection_cli
def test_cli_export_errors(vault: tuple[Path, Path], tmp_path: Path) -> None:
    root, meta = vault
    bad_policy = tmp_path / "bad-policy.json"
    bad_policy.write_text('{"unknown_key": 1}', encoding="utf-8")
    (tmp_path / "empty").mkdir()
    cases = [
        _export_args(root, tmp_path / "o1", tmp_path / "missing.json"),
        _export_args(root, tmp_path / "o2", meta, "--policy", str(bad_policy)),
        _export_args(root, tmp_path / "o3", meta, "--policy-version", "3"),
        _export_args(root, root / "inside", meta),
        _export_args(tmp_path / "empty", tmp_path / "o4", meta),
    ]
    for args in cases:
        _cli_error(_cli(args))
    for name in ("o1", "o2", "o3", "o4"):
        assert not (tmp_path / name).exists()
    assert not (root / "inside").exists()


# ====================================================================== CLI: resolve


def _resolve_args(root: Path, meta: Path | None, *extra: str) -> list[str]:
    args = ["resolve", "--root", str(root)]
    if meta is not None:
        args += ["--lifecycle-source", str(meta)]
    return [*args, *extra, "--ids-stdin", "--json"]


@needs_resolve_cli
def test_cli_resolve_matches_library(shared_vault: tuple[Path, Path]) -> None:
    root, meta = shared_vault
    hits = [ID["active"], "", ID["active"], "f999999999999", f"memories/{ID['retired']}.md"]
    payload = _one_line_json(_cli(_resolve_args(root, meta), "\n".join(hits) + "\n"))
    library = resolve(root, [ID["active"], "f999999999999", hits[-1]], lifecycle_source=meta)
    payload.pop("elapsed_ms")
    library.pop("elapsed_ms")
    assert payload == library
    assert RESOLVE_KEYS - {"elapsed_ms"} <= set(payload)


@needs_resolve_cli
def test_cli_resolve_policy_and_max_lines(tmp_path: Path) -> None:
    root, meta = build_vault(tmp_path, {**DOCS, **EXTRA_DOCS})
    stdin = f"{ID['multiline']}\n{ID['r3_hypothesis']}\n"
    payload = _one_line_json(
        _cli(_resolve_args(root, meta, "--policy-version", "2", "--max-lines", "2"), stdin)
    )
    multiline, r3 = payload["documents"]
    assert multiline["body"] == "line one\nline two"
    assert (r3["epistemic"], r3["causal"]) == ("inference", "hypothesis")
    by_file = _one_line_json(
        _cli(_resolve_args(root, meta, "--policy", str(POLICY_V2_FILE), "--max-lines", "0"), stdin)
    )
    assert by_file["projection_policy_version"] == 2
    assert by_file["documents"][0]["body"] is None


@needs_resolve_cli
def test_cli_resolve_without_ids_stdin_is_error(shared_vault: tuple[Path, Path]) -> None:
    root, meta = shared_vault
    result = _cli(["resolve", "--root", str(root), "--lifecycle-source", str(meta), "--json"])
    assert result.exit_code == 2
    assert result.stdout == ""


@needs_resolve_cli
def test_cli_resolve_more_than_50_distinct_lines_is_error(shared_vault: tuple[Path, Path]) -> None:
    root, meta = shared_vault
    lines = [f"f{0x100 + index:012x}" for index in range(MAX_RESOLVE_BATCH + 1)]
    _cli_error(_cli(_resolve_args(root, meta), "\n".join(lines) + "\n"))
    repeated = lines[:MAX_RESOLVE_BATCH] + lines[:10] + ["", "  "]
    payload = _one_line_json(_cli(_resolve_args(root, meta), "\n".join(repeated) + "\n"))
    assert len(payload["missing"]) == MAX_RESOLVE_BATCH


@needs_resolve_cli
def test_cli_resolve_unreadable_root_or_lifecycle_source_is_error(
    shared_vault: tuple[Path, Path], tmp_path: Path
) -> None:
    root, meta = shared_vault
    _cli_error(_cli(_resolve_args(tmp_path / "nope", meta), f"{ID['active']}\n"))
    _cli_error(_cli(_resolve_args(root, tmp_path / "nope.json"), f"{ID['active']}\n"))


def test_invalid_vault_file_is_reported_not_projected(
    vault: tuple[Path, Path], tmp_path: Path
) -> None:
    root, meta = vault
    (root / "memories" / "broken-0001.md").write_text("no frontmatter here\n", encoding="utf-8")
    result = export_projection(root, tmp_path / "out", lifecycle_source=meta, now=NOW)
    assert result["counts"]["skipped"]["invalid"] == 1
    assert result["counts"]["vault_documents"] == len(DOCS) + 1
    assert result["counts"]["projected"] == len(PROJECTED)
    lines = Path(result["failures_path"]).read_text("utf-8").splitlines()
    assert {"id": "broken-0001", "reason": "invalid"}.items() <= json.loads(
        next(line for line in lines if "broken-0001" in line)
    ).items()
    payload = resolve(root, ["broken-0001", ID["active"]], lifecycle_source=meta)
    assert payload["invalid"] == ["broken-0001"]
    assert filter_resolved(payload, SETTINGS)["output_meta"]["filtered_unresolved"] == 1


# ------------------------------------------------------------- review fixes (nits 1-7)


def _resolve_ok(root: Path, meta: Path, names: list[str]) -> dict[str, Any]:
    return resolve(root, [ID[name] for name in names], lifecycle_source=meta)


@pytest.mark.parametrize(
    "line",
    [
        "updated_at: '2026-11-10T03:00:00Z' # c",
        "'updated_at': '2026-11-10T03:00:00Z'",
        '"updated_at": 2026-11-10T03:00:00Z  # trailing comment',
    ],
)
def test_resolve_staleness_uses_parsed_updated_at(vault: tuple[Path, Path], line: str) -> None:
    root, meta = vault
    path = _mem_path(root, "active")
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"^updated_at: .*$", line, text, count=1, flags=re.MULTILINE)
    path.write_text(text, encoding="utf-8")
    payload = _resolve_ok(root, meta, ["active"])
    exported = export_projection(root, root.parent / "out", lifecycle_source=meta, now=NOW)
    assert payload["lifecycle_source_status"] == "stale"
    assert exported["lifecycle_source_status"] == "stale"


def test_resolve_staleness_unparseable_file_is_stale(vault: tuple[Path, Path]) -> None:
    root, meta = vault
    (root / "memories" / "broken-0001.md").write_text("no frontmatter\n", encoding="utf-8")
    payload = _resolve_ok(root, meta, ["active"])
    assert payload["lifecycle_source_status"] == "stale"
    assert payload["lifecycle_source_lag_seconds"] is None
    assert filter_resolved(payload, FilterSettings(now=NOW))["output_meta"]["fail_closed"]


@pytest.mark.parametrize(
    ("tags", "expected"),
    [
        (["sensitivity:private"], "local-private"),
        (["sensitivity:Private"], "local-private"),
        (["Sensitivity:PRIVATE"], "local-private"),
        (["sensitivity:secret"], "unknown"),
        (["sensitivity:local-private"], "unknown"),
        (["sensitivity:private", "sensitivity:secret"], "unknown"),
        ([], "none"),
    ],
)
def test_sensitivity_tags_fail_closed(
    vault: tuple[Path, Path], tags: list[str], expected: str
) -> None:
    root, meta = vault
    path = _mem_path(root, "active")
    text = path.read_text(encoding="utf-8")
    data = yaml.safe_load(text.split("---\n", 2)[1])
    patch_frontmatter(
        path, {"tags": [t for t in data["tags"] if not t.startswith("sensitivity")] + tags}
    )
    doc = _resolve_ok(root, meta, ["active"])["documents"][0]
    assert doc["privacy_class"] == expected
    assert not any(tag.lower().startswith("sensitivity:") for tag in doc["tags"])


def test_export_skip_privacy_opt_in(vault: tuple[Path, Path], tmp_path: Path) -> None:
    root, meta = vault
    default = export_projection(root, tmp_path / "a", lifecycle_source=meta, now=NOW)
    skipped = export_projection(
        root,
        tmp_path / "b",
        policy=load_policy(None, skip_privacy=["unknown", "local-private"]),
        lifecycle_source=meta,
        now=NOW,
    )
    assert "privacy" not in default["counts"]["skipped"]
    # private + privacy_mismatch (meta says local-private) + privacy_unknown
    assert skipped["counts"]["skipped"]["privacy"] == 3
    assert skipped["counts"]["projected"] == default["counts"]["projected"] - 3
    files = {p.stem for p in (Path(skipped["tree"]) / "files" / "memories").iterdir()}
    assert ID["private"] not in files and ID["privacy_unknown"] not in files
    assert ID["private"] in {
        p.stem for p in (Path(default["tree"]) / "files" / "memories").iterdir()
    }
    cli = _cli(
        [
            "--now",
            NOW_ISO,
            "projection",
            "export",
            "--root",
            str(root),
            "--out",
            str(tmp_path / "c"),
            "--lifecycle-source",
            str(meta),
            "--skip-privacy",
            "unknown,local-private",
            "--json",
        ]
    )
    assert cli.exit_code == 0, cli.stderr
    assert json.loads(cli.stdout)["counts"]["skipped"]["privacy"] == 3
    bad = _cli(
        [
            "projection",
            "export",
            "--root",
            str(root),
            "--out",
            str(tmp_path / "d"),
            "--lifecycle-source",
            str(meta),
            "--skip-privacy",
            "secret",
        ]
    )
    _cli_error(bad)


def test_export_failure_removes_staging(
    vault: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import memfw.projection as projection

    root, meta = vault
    out = tmp_path / "new" / "parent" / "out"
    real = projection._write_private

    def failing(path: Path, data: bytes) -> None:
        if path.name == "manifest.json":
            raise OSError("injected write failure")
        real(path, data)

    monkeypatch.setattr(projection, "_write_private", failing)
    before = _vault_snapshot(root)
    with pytest.raises(OSError, match="injected"):
        export_projection(root, out, lifecycle_source=meta, now=NOW)
    assert list(out.iterdir()) == []
    for directory in (tmp_path / "new", tmp_path / "new" / "parent", out):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert _vault_snapshot(root) == before
    monkeypatch.setattr(projection, "_write_private", real)
    done = export_projection(root, out, lifecycle_source=meta, now=NOW)
    assert stat.S_IMODE(Path(done["tree"]).stat().st_mode) == 0o750


@pytest.mark.parametrize("name", ["id_mismatch", "orphan", "native_no_tag", "two_tags"])
def test_resolve_withholds_unjoined_metadata(vault: tuple[Path, Path], name: str) -> None:
    root, meta = vault
    payload = _resolve_ok(root, meta, ["active", name])
    ok, doc = payload["documents"]
    assert set(doc) == set(ok)
    assert doc["id"] == ID[name] and doc["join_status"] in {"unjoined", "id_mismatch"}
    assert doc["join_reason"]
    for key in (
        "title",
        "updated_at",
        "fleet_path",
        "lifecycle",
        "privacy_class",
        "body",
        "content_sha256",
        "tenant",
        "type",
        "status",
    ):
        assert doc[key] is None, key
    assert doc["tags"] == []


def test_resolve_stdin_caps(vault: tuple[Path, Path]) -> None:
    root, meta = vault
    base = ["resolve", "--root", str(root), "--lifecycle-source", str(meta), "--ids-stdin"]
    _cli_error(_cli(base, stdin="x" * (1024 * 1024 + 1)))
    long_hit = "memories/" + "a" * 5000 + f"/memories/{ID['active']}.md"
    result = _cli(base, stdin=f"{long_hit}\n{'z' * 300}/nope\n{ID['active']}\n")
    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert [doc["id"] for doc in payload["documents"]] == [ID["active"]]
    assert len(payload["unresolved"]) == 2
    assert all(len(item) <= 200 for item in payload["unresolved"])


def test_projection_uses_core_parser_only() -> None:
    source = (Path(__file__).parents[1] / "src" / "memfw" / "projection.py").read_text()
    imports = [line for line in source.splitlines() if line.startswith(("from .", "import "))]
    assert all(line.startswith(("from .core.", "import ")) for line in imports), imports
    assert "qmd" not in "\n".join(imports).lower()
