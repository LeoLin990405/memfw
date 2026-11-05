"""Tests for the generic OKF v0.2 projection."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from memfw.core.repository import Repository
from memfw.okf import export_okf, validate_okf_bundle


def _vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    repository = Repository(root)
    repository.init()
    repository.add(
        title="Greenhouse notes",
        body="The greenhouse sensor reports 18 degrees.",
        tags=["type:note", "topic:greenhouse"],
    )
    return root


def test_export_creates_conformant_bundle(tmp_path: Path) -> None:
    root = _vault(tmp_path)
    bundle = tmp_path / "okf"
    result = export_okf(root, bundle)
    assert result.valid
    assert result.concepts == 1
    assert (bundle / "index.md").is_file()
    assert (bundle / "log.md").is_file()
    manifest = json.loads((bundle / "manifest.json").read_text())
    assert manifest["okf_version"] == "0.2"
    assert manifest["valid"] is True
    concept = next(bundle.glob("*.md"))
    text = concept.read_text()
    assert text.startswith("---\n")
    assert "type: note" in text


def test_export_refuses_overwrite_without_force(tmp_path: Path) -> None:
    root = _vault(tmp_path)
    bundle = tmp_path / "okf"
    export_okf(root, bundle)
    with pytest.raises(FileExistsError):
        export_okf(root, bundle)
    result = export_okf(root, bundle, force=True)
    assert result.valid


def test_validation_rejects_missing_type(tmp_path: Path) -> None:
    bundle = tmp_path / "bad"
    bundle.mkdir()
    (bundle / "x.md").write_text("---\ntitle: no type\n---\n\nbody\n")
    result = validate_okf_bundle(bundle)
    assert not result.valid
    assert any("missing non-empty type" in error for error in result.errors)
