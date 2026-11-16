"""CLI surfacing of the okf export, schedule and candidates fixes."""

from __future__ import annotations

import json
import plistlib
from pathlib import Path

from typer.testing import CliRunner

from memfw.cli import app
from memfw.core.repository import Repository


def _vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    repository = Repository(root)
    repository.init()
    repository.add(title="Greenhouse", body="The sensor reports 18 degrees.", tags=["type:note"])
    return root


def _files(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_okf_export_force_onto_vault_is_a_usage_error(tmp_path: Path) -> None:
    root = _vault(tmp_path)
    before = _files(root)
    result = CliRunner().invoke(app, ["okf", "export", str(root), "--root", str(root), "--force"])
    assert result.exit_code == 2, result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "Traceback" not in result.output
    assert _files(root) == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["vault"]


def test_okf_export_force_onto_parent_of_vault_is_a_usage_error(tmp_path: Path) -> None:
    root = _vault(tmp_path)
    result = CliRunner().invoke(
        app, ["okf", "export", str(tmp_path), "--root", str(root), "--force"]
    )
    assert result.exit_code == 2, result.output
    assert root.is_dir() and (root / "vault.yaml").exists()


def test_okf_export_existing_output_without_force_names_the_flag(tmp_path: Path) -> None:
    root = _vault(tmp_path)
    out = tmp_path / "bundle"
    runner = CliRunner()
    assert runner.invoke(app, ["okf", "export", str(out), "--root", str(root)]).exit_code == 0
    result = runner.invoke(app, ["okf", "export", str(out), "--root", str(root)])
    assert result.exit_code == 2
    assert "--force" in result.output and "force=True" not in result.output
    again = runner.invoke(app, ["okf", "export", str(out), "--root", str(root), "--force"])
    assert again.exit_code == 0, again.output
    again = runner.invoke(app, ["okf", "export", str(out), "--root", str(root), "--force"])
    assert again.exit_code == 0, again.output
    assert not (tmp_path / "bundle.tmp").exists()


def test_schedule_print_tells_the_user_to_create_the_log_dir(tmp_path: Path) -> None:
    root = _vault(tmp_path)
    result = CliRunner().invoke(
        app,
        ["conversations", "schedule", "--print", "--root", str(root), "--bin", "/opt/memfw"],
    )
    assert result.exit_code == 0, result.output
    plist = plistlib.loads(result.stdout.encode())
    log_dir = str(Path(plist["StandardOutPath"]).parent)
    assert f"mkdir -p {log_dir}" in result.stderr


def test_candidates_lists_skipped_tampered_file(tmp_path: Path) -> None:
    root = _vault(tmp_path)
    broken = root / "_candidates" / "remember" / "broken.md"
    broken.parent.mkdir(parents=True)
    broken.write_text("---\nname: [broken\n---\n\nbody\n", encoding="utf-8")
    result = CliRunner().invoke(app, ["candidates", "--root", str(root)])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"candidates": [], "skipped": ["broken"]}
