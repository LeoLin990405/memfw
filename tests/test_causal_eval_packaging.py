"""The causal eval fixtures ship inside the package, not in ``tests/``."""

from __future__ import annotations

from importlib import resources
from pathlib import Path

import pytest
from typer.testing import CliRunner

from memfw.cli import app
from memfw.memory_eval import CAUSAL_FIXTURE_DIR, load_causal_materials

REPO_TESTS = Path(__file__).resolve().parent


def test_default_fixtures_are_package_data() -> None:
    packaged = resources.files("memfw") / "eval_data" / "causal_eval"
    assert packaged.is_dir()
    names = sorted(item.name for item in packaged.iterdir() if item.name.endswith(".json"))
    assert len(names) == 8
    assert str(CAUSAL_FIXTURE_DIR) == str(packaged)
    with resources.as_file(CAUSAL_FIXTURE_DIR) as on_disk:
        assert REPO_TESTS not in Path(on_disk).resolve().parents


def test_default_load_uses_packaged_fixtures() -> None:
    materials = load_causal_materials()
    assert len(materials) == 8
    assert {material.group for material in materials} == {
        "identified",
        "confounded",
        "narrative",
        "malformed",
    }


def test_eval_causal_runs_from_outside_the_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(app, ["eval", "causal", "--workdir", str(tmp_path / "w")])
    assert result.exit_code == 0, result.output
    assert "materials: 8" in result.output


def test_explicit_string_directory_still_works(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_causal_materials(str(tmp_path / "missing"))
