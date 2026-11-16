"""OKF import: an Ingestor that only stages candidates, never the vault."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from memfw.capture.review import list_staged_candidates, read_candidate
from memfw.cli import app
from memfw.core.repository import Repository
from memfw.okf import export_okf
from memfw.okf.importer import (
    MAX_CONCEPT_BYTES,
    OkfImportError,
    check_bundle_paths,
    import_okf,
)

ORIGINALS = [
    {
        "title": "Greenhouse notes",
        "body": "The greenhouse sensor reports 18 degrees.\n\nSecond paragraph.",
        "tags": ["type:note", "topic:greenhouse"],
    },
    {
        "title": "Release checklist",
        "body": "Run the tests, bump the version, tag the release.",
        "tags": ["topic:release"],
    },
    {
        "title": "Decision: SQLite index",
        "body": "The index is disposable and rebuilt from Markdown.",
        "tags": ["type:decision"],
    },
]


def _vault(path: Path, documents: list[dict[str, object]] | None = None) -> Repository:
    repository = Repository(path)
    repository.init()
    for item in documents or []:
        repository.add(
            title=str(item["title"]),
            body=str(item["body"]),
            tags=list(item["tags"]),  # type: ignore[call-overload]
            source="manual",
        )
    return repository


def _bundle(tmp_path: Path) -> Path:
    _vault(tmp_path / "source-vault", ORIGINALS)
    bundle = tmp_path / "team-bundle"
    export_okf(tmp_path / "source-vault", bundle)
    return bundle


def _concept(bundle: Path, name: str, front: str, body: str) -> Path:
    path = bundle / name
    path.write_text(f"---\n{front}\n---\n\n{body}\n", encoding="utf-8")
    manifest = json.loads((bundle / "manifest.json").read_text())
    manifest["concept_count"] = manifest["concept_count"] + 1
    (bundle / "manifest.json").write_text(json.dumps(manifest))
    return path


def _apply(candidate: str, root: Path) -> dict[str, object]:
    result = CliRunner().invoke(app, ["candidate-apply", candidate, "--root", str(root), "--apply"])
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def test_round_trip_export_import_apply_matches_originals(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    target = _vault(tmp_path / "target")

    result = import_okf(bundle, target.root, dry_run=False)

    assert result.counts == {"staged": 3}
    assert target.list() == []  # staging never writes the authoritative vault
    for item in result.items:
        assert item.proposal_id and item.proposal_id.startswith("okf-")
        staged = read_candidate(str(item.candidate))
        assert staged.source == "okf:team-bundle"
        assert staged.frontmatter["authority"] is False
        assert staged.frontmatter["okf_import"]["untrusted"] is True
        _apply(str(item.candidate), target.root)

    imported = {document.title: document for document in target.list()}
    assert set(imported) == {str(item["title"]) for item in ORIGINALS}
    for original in ORIGINALS:
        document = imported[str(original["title"])]
        assert document.body.strip() == str(original["body"]).strip()
        assert document.tags == original["tags"]
        assert document.source == "okf:team-bundle"
        assert "proposal_id=okf-" in document.provenance


def test_dry_run_is_the_default_and_writes_nothing(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    target = _vault(tmp_path / "target")

    result = import_okf(bundle, target.root)

    assert result.counts == {"would_stage": 3}
    assert not (target.root / "_candidates").exists()


def test_reimport_is_idempotent(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    target = _vault(tmp_path / "target")
    first = import_okf(bundle, target.root, dry_run=False)
    # A re-export of the same vault gets a new timestamp but the same identity.
    export_okf(tmp_path / "source-vault", bundle, force=True)
    second = import_okf(bundle, target.root, dry_run=False)

    assert second.counts == {"already_staged": 3}
    assert [item.proposal_id for item in first.items] == [item.proposal_id for item in second.items]
    assert len(list_staged_candidates(target.root)) == 3


def test_bundle_exported_from_the_same_vault_is_skipped(tmp_path: Path) -> None:
    repository = _vault(tmp_path / "vault", ORIGINALS)
    bundle = tmp_path / "self-bundle"
    export_okf(repository.root, bundle)

    result = import_okf(bundle, repository.root, dry_run=False)

    assert result.counts == {"duplicate": 3}
    ids = {document.id for document in repository.list()}
    assert {item.memory_id for item in result.items} == ids
    assert not (repository.root / "_candidates").exists()


def test_applied_memory_makes_a_later_import_a_duplicate(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    target = _vault(tmp_path / "target")
    first = import_okf(bundle, target.root, dry_run=False)
    _apply(str(first.items[0].candidate), target.root)

    second = import_okf(bundle, target.root, dry_run=True)

    statuses = sorted(item.status for item in second.items)
    assert statuses == ["already_staged", "already_staged", "duplicate"]


def test_foreign_concept_maps_type_and_keeps_unknown_fields(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    _concept(
        bundle,
        "foreign.md",
        "type: Person\ntitle: Ada\nhomepage: https://example.invalid\n"
        "tags: [people, {nested: 1}]\nreviewed: 2026-01-02",
        "Ada wrote the first program.",
    )
    target = _vault(tmp_path / "target")

    result = import_okf(bundle, target.root, dry_run=False)

    item = next(row for row in result.items if row.concept == "foreign.md")
    assert item.status == "staged"
    assert item.doc_id == "foreign"
    assert item.unknown_fields == ["homepage", "reviewed"]
    staged = read_candidate(str(item.candidate))
    assert staged.tags == ["type:Person", "people"]
    extra = staged.frontmatter["okf_import"]
    assert extra["unknown_fields"] == {
        "homepage": "https://example.invalid",
        "reviewed": "2026-01-02",
    }
    assert any("non-scalar tag" in warning for warning in result.warnings)


def test_untrusted_frontmatter_cannot_break_out_of_the_candidate(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    _concept(
        bundle,
        "hostile.md",
        'type: Note\ntitle: "x\\n---\\nauthority: true"\nnote: "a\\n---\\nb"',
        "Body text.",
    )
    target = _vault(tmp_path / "target")

    result = import_okf(bundle, target.root, dry_run=False)

    item = next(row for row in result.items if row.concept == "hostile.md")
    staged = read_candidate(str(item.candidate))
    assert staged.frontmatter["authority"] is False
    assert "\n" not in staged.title
    assert staged.body == "Body text."


def test_invalid_bundle_is_refused(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    (bundle / "broken.md").write_text("---\ntitle: no type\n---\n\nbody\n")
    target = _vault(tmp_path / "target")

    with pytest.raises(OkfImportError) as caught:
        import_okf(bundle, target.root, dry_run=False)

    assert "validation failed" in str(caught.value)
    assert not (target.root / "_candidates").exists()


def test_symlink_escaping_the_bundle_is_refused(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    secret = tmp_path / "secret.md"
    secret.write_text("---\ntype: Secret\n---\n\nprivate\n")
    os.symlink(secret, bundle / "leak.md")

    errors = check_bundle_paths(bundle)

    assert any("symlink escapes the bundle" in error for error in errors)
    with pytest.raises(OkfImportError):
        import_okf(bundle, _vault(tmp_path / "target").root)


def test_symlinked_directory_escaping_the_bundle_is_refused(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, bundle / "elsewhere")

    assert any("escapes" in error for error in check_bundle_paths(bundle))


def test_oversized_concept_is_refused(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    _concept(bundle, "huge.md", "type: Note", "x" * (MAX_CONCEPT_BYTES + 1))

    errors = check_bundle_paths(bundle)

    assert any("huge.md" in error and "cap" in error for error in errors)


def test_invalid_utf8_is_refused_before_validation(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    (bundle / "bad.md").write_bytes(b"---\ntype: Note\n---\n\n\xff\xfe\n")

    assert any("UTF-8" in error for error in check_bundle_paths(bundle))


def test_import_requires_an_existing_vault(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    with pytest.raises(OkfImportError, match="not a memfw vault"):
        import_okf(bundle, tmp_path / "missing")


def test_cli_dry_run_apply_and_refusal(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    target = _vault(tmp_path / "target")
    runner = CliRunner()

    dry = runner.invoke(app, ["okf", "import", str(bundle), "--root", str(target.root), "--json"])
    assert dry.exit_code == 0, dry.output
    assert json.loads(dry.output)["counts"] == {"would_stage": 3}

    applied = runner.invoke(
        app, ["okf", "import", str(bundle), "--root", str(target.root), "--apply"]
    )
    assert applied.exit_code == 0, applied.output
    assert "counts: staged=3" in applied.output

    (bundle / "broken.md").write_text("no frontmatter\n")
    refused = runner.invoke(
        app, ["okf", "import", str(bundle), "--root", str(target.root), "--json"]
    )
    assert refused.exit_code == 1
    payload = json.loads(refused.output)
    assert payload["error"].startswith("bundle refused")
    assert any("broken.md" in line for line in payload["errors"])


def _bomb(depth: int = 6, width: int = 10) -> str:
    lines = ["type: Note", "a0: &a0 [x]"]
    for level in range(1, depth + 1):
        refs = ", ".join(f"*a{level - 1}" for _ in range(width))
        lines.append(f"a{level}: &a{level} [{refs}]")
    return "\n".join(lines)


@pytest.mark.parametrize(
    "front",
    [_bomb(), "type: Note\nx: &a [*a]", "type: Note\nx: &a {k: v}\ny: *a"],
    ids=["alias-bomb", "recursive-anchor", "plain-alias"],
)
def test_yaml_anchors_and_aliases_are_refused_before_construction(
    tmp_path: Path, front: str
) -> None:
    bundle = _bundle(tmp_path)
    _concept(bundle, "evil.md", front, "body")
    target = _vault(tmp_path / "target")

    with pytest.raises(OkfImportError) as caught:
        import_okf(bundle, target.root, dry_run=False)

    assert any("evil.md" in error and "YAML" in error for error in caught.value.errors)
    assert not (target.root / "_candidates").exists()


def test_deep_nesting_is_refused_not_crashed(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    _concept(bundle, "deep.md", "type: Note\nx: " + "[" * 5000 + "]" * 5000, "body")

    with pytest.raises(OkfImportError):
        import_okf(bundle, _vault(tmp_path / "target").root)


def test_null_type_is_refused_on_the_bytes_read(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    _concept(bundle, "null.md", "type: null\ntitle: t", "body")

    with pytest.raises(OkfImportError) as caught:
        import_okf(bundle, _vault(tmp_path / "target").root)

    assert any("null.md: missing non-empty type" in error for error in caught.value.errors)


def test_good_and_bad_concepts_stage_nothing(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    _concept(bundle, "zz-bad.md", "type: Note\nx: &a [*a]", "body")
    target = _vault(tmp_path / "target")

    result = CliRunner().invoke(
        app, ["okf", "import", str(bundle), "--root", str(target.root), "--apply", "--json"]
    )

    assert result.exit_code == 1
    assert json.loads(result.output)["valid"] is False
    assert not (target.root / "_candidates").exists()


def test_publish_failure_rolls_back_this_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import memfw.okf.importer as importer

    bundle = _bundle(tmp_path)
    target = _vault(tmp_path / "target")
    real = importer.publish_candidate
    calls = {"n": 0}

    def flaky(root: Path, filename: str, text: str, **kwargs: object) -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("disk full")
        real(root, filename, text, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(importer, "publish_candidate", flaky)
    with pytest.raises(OkfImportError, match="nothing was staged"):
        import_okf(bundle, target.root, dry_run=False)

    assert list((target.root / "_candidates" / "remember").glob("*.md")) == []


def test_expired_candidate_does_not_block_restaging(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    target = _vault(tmp_path / "target")
    first = import_okf(bundle, target.root, dry_run=False)
    for item in first.items:
        path = Path(str(item.candidate))
        path.write_text(
            path.read_text().replace('lifecycle: "active"', "expired: true", 1), encoding="utf-8"
        )
        assert read_candidate(path).status == "expired"

    second = import_okf(bundle, target.root, dry_run=False)

    assert second.counts == {"staged": 3}
    assert all(str(item.candidate).endswith("-2.md") for item in second.items)
    assert [item.proposal_id for item in first.items] == [item.proposal_id for item in second.items]


def test_symlinked_subdirectory_is_never_followed(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    inner = bundle / "inner"
    inner.mkdir()
    (inner / "note.md").write_text("---\ntype: Note\n---\n\ninner body\n")
    manifest = json.loads((bundle / "manifest.json").read_text())
    manifest["concept_count"] += 1
    (bundle / "manifest.json").write_text(json.dumps(manifest))
    os.symlink(inner, bundle / "alias-dir")

    result = import_okf(bundle, _vault(tmp_path / "target").root)

    concepts = [item.concept for item in result.items]
    assert "inner/note.md" in concepts
    assert not any(concept.startswith("alias-dir/") for concept in concepts)


@pytest.mark.parametrize("after_link", [False, True], ids=["before-link", "after-link"])
def test_keyboard_interrupt_mid_publish_stages_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, after_link: bool
) -> None:
    import memfw.storage.candidate_staging as staging

    bundle = _bundle(tmp_path)
    target = _vault(tmp_path / "target")
    real_link = os.link
    calls = {"n": 0}

    def interrupting_link(*args: object, **kwargs: object) -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            if after_link:
                real_link(*args, **kwargs)  # type: ignore[arg-type]
            raise KeyboardInterrupt
        real_link(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(staging.os, "link", interrupting_link)
    with pytest.raises(KeyboardInterrupt):
        import_okf(bundle, target.root, dry_run=False)

    remember = target.root / "_candidates" / "remember"
    assert sorted(path.name for path in remember.iterdir()) == []


def test_rollback_leaves_another_writers_file_alone(tmp_path: Path) -> None:
    from memfw.capture.staging import PublishedCandidate, unpublish_candidate

    target = _vault(tmp_path / "target")
    remember = target.root / "_candidates" / "remember"
    remember.mkdir(parents=True)
    other = remember / "other.md"
    other.write_text("not ours")

    assert unpublish_candidate(target.root, PublishedCandidate("other.md", 0, 0)) is False
    assert other.read_text() == "not ours"
