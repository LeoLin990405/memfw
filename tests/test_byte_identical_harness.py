"""Self-test of ``tests/harness/byte_identical.py`` (old/new output comparison)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.harness import byte_identical

pytestmark = pytest.mark.skipif(
    not byte_identical.git_available(), reason="git or the repository is unavailable"
)

PAYLOADS = [
    {"query": "backup", "mode": "fts"},
    {"query": "backup", "limit": 3},
    {"query": "迁移 备份", "mode": "fts", "source": "manual"},
]


def test_mcp_memory_search_is_identical_on_head_and_head() -> None:
    results = byte_identical.compare(
        "tests.harness.probes:mcp_memory_search", PAYLOADS, old_ref="HEAD", new_ref="HEAD~0"
    )
    assert len(results) == len(PAYLOADS)
    for result in results:
        assert result.identical, result.diff
        response = json.loads(result.new)
        assert response["id"] == 1
        assert response["result"]["isError"] is False, response
        assert "results" in response["result"]["content"][0]["text"]
    first = json.loads(results[0].new)["result"]["content"][0]["text"]
    assert "m-backup" in [row["id"] for row in first["results"]]
    assert len(results[0].new.splitlines()) > 10  # pretty-printed for line diffs


def test_each_side_loads_its_own_tree() -> None:
    """Old runs from the unpacked ref, new from the working tree: a real diff shows."""

    (result,) = byte_identical.compare("tests.harness.probes:loaded_from", [{}], old_ref="HEAD")
    assert not result.identical
    assert result.new == str(byte_identical.REPO / "src")
    assert result.old.endswith("/old/src")


def test_comparison_reports_a_diff() -> None:
    same = byte_identical.Comparison(payload={}, old="a\nb\n", new="a\nb\n")
    changed = byte_identical.Comparison(payload={}, old="a\nb\n", new="a\nc\n")
    assert same.identical and same.diff == ""
    assert not changed.identical
    assert "-b" in changed.diff and "+c" in changed.diff


def test_materialize_unpacks_src_from_a_ref(tmp_path: Path) -> None:
    tree = byte_identical.materialize("HEAD", tmp_path / "tree")
    assert (tree / "src" / "memfw" / "__init__.py").is_file()
    with pytest.raises(byte_identical.UnknownRef):
        byte_identical.materialize("no-such-ref-for-memfw-harness", tmp_path / "missing")
    assert not issubclass(byte_identical.UnknownRef, byte_identical.HarnessUnavailable)


def test_materialize_rejects_a_ref_without_src_or_scripts(tmp_path: Path) -> None:
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    subprocess.run([*git, "init", "-q"], check=True)
    (repo / "README").write_text("x\n", encoding="utf-8")
    subprocess.run([*git, "add", "README"], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "x"], check=True)
    with pytest.raises(byte_identical.UnknownRef):
        byte_identical.materialize("HEAD", tmp_path / "tree", repo=repo)
