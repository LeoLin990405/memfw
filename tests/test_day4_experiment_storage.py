from __future__ import annotations

import sys
from pathlib import Path
from zipfile import ZipFile

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from day4_storage_matrix import _validate_archive_members


def test_baseline_archive_members_are_safe_and_snapshot_is_present() -> None:
    archive_path = Path(__file__).parents[1] / "reports" / "day4_baseline_snapshot.zip"
    if not archive_path.exists():
        pytest.skip("day4_baseline_snapshot.zip is a release asset; see reports/LARGE_FILES.md")
    with ZipFile(archive_path) as archive:
        _validate_archive_members(archive)
        assert "BASELINE_SHA256.txt" in archive.namelist()
        assert "src/memfw/core/repository.py" in archive.namelist()


@pytest.mark.parametrize("member", ["../outside.txt", "C:/outside.txt"])
def test_baseline_archive_rejects_unsafe_paths(tmp_path: Path, member: str) -> None:
    archive_path = tmp_path / "unsafe.zip"
    with ZipFile(archive_path, "w") as archive:
        archive.writestr(member, "unsafe")
    with ZipFile(archive_path) as archive, pytest.raises(ValueError, match="unsafe member path"):
        _validate_archive_members(archive)
