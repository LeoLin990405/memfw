"""``okf export --force`` must never replace the vault or anything memfw did not write."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from memfw.core.repository import Repository
from memfw.okf import export_okf

FROZEN = datetime(2026, 11, 16, 12, 0, 0, tzinfo=UTC)


def _vault(tmp_path: Path, name: str = "vault") -> Path:
    root = tmp_path / name
    repository = Repository(root)
    repository.init()
    repository.add(title="Greenhouse", body="The sensor reports 18 degrees.", tags=["type:note"])
    return root


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_vault_as_output_is_refused_and_vault_intact(tmp_path: Path) -> None:
    root = _vault(tmp_path)
    before = _snapshot(root)
    with pytest.raises(ValueError, match="vault"):
        export_okf(root, root, force=True)
    assert _snapshot(root) == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["vault"]


def test_vault_via_symlink_is_refused(tmp_path: Path) -> None:
    root = _vault(tmp_path)
    link = tmp_path / "link"
    link.symlink_to(root, target_is_directory=True)
    with pytest.raises(ValueError, match="vault"):
        export_okf(root, link, force=True)
    assert root.is_dir() and link.is_symlink()


def test_parent_of_vault_is_refused(tmp_path: Path) -> None:
    parent = tmp_path / "parent"
    root = _vault(parent)
    before = _snapshot(root)
    with pytest.raises(ValueError, match="contains the vault"):
        export_okf(root, parent, force=True)
    assert _snapshot(root) == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["parent"]


def test_output_inside_vault_is_refused(tmp_path: Path) -> None:
    root = _vault(tmp_path)
    with pytest.raises(ValueError, match="inside the vault"):
        export_okf(root, root / "okf")
    assert not (root / "okf").exists()


def test_force_refuses_directory_not_written_by_memfw(tmp_path: Path) -> None:
    root = _vault(tmp_path)
    other = tmp_path / "photos"
    other.mkdir()
    (other / "keep.txt").write_text("precious")
    with pytest.raises(FileExistsError, match="memfw-okf-export"):
        export_okf(root, other, force=True)
    assert (other / "keep.txt").read_text() == "precious"
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / "manifest.json").write_text(json.dumps({"generator": "someone-else"}))
    with pytest.raises(FileExistsError):
        export_okf(root, foreign, force=True)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["foreign", "photos", "vault"]


def test_force_refuses_existing_file(tmp_path: Path) -> None:
    root = _vault(tmp_path)
    target = tmp_path / "okf"
    target.write_text("not a bundle")
    with pytest.raises(FileExistsError):
        export_okf(root, target, force=True)
    assert target.read_text() == "not a bundle"


def test_back_to_back_force_exports_in_same_second(tmp_path: Path) -> None:
    root = _vault(tmp_path)
    bundle = tmp_path / "okf"
    export_okf(root, bundle, now=FROZEN)
    export_okf(root, bundle, force=True, now=FROZEN)
    result = export_okf(root, bundle, force=True, now=FROZEN)
    assert result.valid
    names = sorted(p.name for p in tmp_path.iterdir())
    assert not any(name.endswith(".tmp") for name in names)
    backups = [name for name in names if name.startswith("okf.bak-")]
    assert len(backups) == 2 and len(set(backups)) == 2


def test_failed_export_leaves_no_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import memfw.okf.export as export_module

    root = _vault(tmp_path)

    def boom(path: Path) -> object:
        raise RuntimeError("validation exploded")

    monkeypatch.setattr(export_module, "validate_okf_bundle", boom)
    with pytest.raises(RuntimeError):
        export_okf(root, tmp_path / "okf")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["vault"]


def test_dot_output_works(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _vault(tmp_path)
    out = tmp_path / "out"
    export_okf(root, out)
    monkeypatch.chdir(out)
    result = export_okf(root, ".", force=True)
    assert result.valid
    assert result.bundle == out.resolve()
    assert (out / "manifest.json").is_file()


def test_exists_message_mentions_cli_flag(tmp_path: Path) -> None:
    root = _vault(tmp_path)
    bundle = tmp_path / "okf"
    export_okf(root, bundle)
    with pytest.raises(FileExistsError) as excinfo:
        export_okf(root, bundle)
    assert "--force" in str(excinfo.value)
    assert "force=True" not in str(excinfo.value)


def _case_insensitive(tmp_path: Path) -> bool:
    probe = tmp_path / "case-probe"
    probe.mkdir()
    try:
        return (tmp_path / "CASE-PROBE").exists()
    finally:
        probe.rmdir()


def test_scratch_dir_differing_only_in_case_from_vault_is_refused(tmp_path: Path) -> None:
    if not _case_insensitive(tmp_path):
        pytest.skip("file system is case-sensitive")
    root = _vault(tmp_path, "b.tmp")
    before = _snapshot(root)
    with pytest.raises(ValueError, match="vault"):
        export_okf(root, tmp_path / "B", force=True)
    assert _snapshot(root) == before


def test_output_inside_vault_spelled_with_other_case_is_refused(tmp_path: Path) -> None:
    if not _case_insensitive(tmp_path):
        pytest.skip("file system is case-sensitive")
    root = _vault(tmp_path, "v")
    before = _snapshot(root)
    with pytest.raises(ValueError, match="inside the vault"):
        export_okf(root, tmp_path / "V" / "exp")
    with pytest.raises(ValueError, match="vault"):
        export_okf(root, tmp_path / "V", force=True)
    assert _snapshot(root) == before


def test_output_through_symlink_into_vault_is_refused(tmp_path: Path) -> None:
    root = _vault(tmp_path)
    (tmp_path / "alias").symlink_to(root, target_is_directory=True)
    with pytest.raises(ValueError, match="inside the vault"):
        export_okf(root, tmp_path / "alias" / "exp")


def test_result_keeps_the_callers_relative_spelling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _vault(tmp_path)
    monkeypatch.chdir(tmp_path)
    result = export_okf(root, Path("rel") / "out", now=FROZEN)
    assert result.bundle == Path("rel/out")
    assert result.manifest == Path("rel/out/manifest.json")
    assert (tmp_path / "rel" / "out" / "manifest.json").is_file()
