"""``memfw init --dry-run`` reports whether init would set up a vault."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from memfw.cli import app

runner = CliRunner()


def _dry_run(path: Path) -> dict[str, object]:
    result = runner.invoke(app, ["init", str(path), "--dry-run"])
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def test_missing_directory_would_initialize(tmp_path: Path) -> None:
    report = _dry_run(tmp_path / "vault")
    assert report["would_initialize"] is True
    assert report["exists"] is False
    assert not (tmp_path / "vault").exists()


def test_existing_empty_directory_would_initialize(tmp_path: Path) -> None:
    target = tmp_path / "vault"
    target.mkdir()
    report = _dry_run(target)
    assert report["would_initialize"] is True
    assert report["exists"] is True
    assert report["initialized"] is False
    assert list(target.iterdir()) == []


def test_initialized_vault_would_not_initialize(tmp_path: Path) -> None:
    target = tmp_path / "vault"
    assert runner.invoke(app, ["init", str(target)]).exit_code == 0
    report = _dry_run(target)
    assert report["would_initialize"] is False
    assert report["initialized"] is True


def test_file_path_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "file"
    target.write_text("x", encoding="utf-8")
    result = runner.invoke(app, ["init", str(target), "--dry-run"])
    assert result.exit_code != 0
