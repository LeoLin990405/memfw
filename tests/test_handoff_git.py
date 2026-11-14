"""Read-only git state for the handoff pack (temporary repositories only)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from memfw.handoff.gitinfo import git_snapshot

GIT = shutil.which("git")
needs_git = pytest.mark.skipif(GIT is None, reason="git not installed")


@pytest.fixture(autouse=True)
def _isolated_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(home / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


def _git(repo: Path, *args: str) -> None:
    email = "demo" + "@" + "example.invalid"
    subprocess.run(
        ["git", "-c", "user.name=Demo", "-c", f"user.email={email}", "-C", str(repo), *args],
        check=True,
        capture_output=True,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "-q")
    (path / "app.py").write_text("print('a')\n")
    _git(path, "add", "app.py")
    _git(path, "commit", "-q", "-m", "init")
    (path / "app.py").write_text("print('b')\nprint('c')\n")
    (path / "new.txt").write_text("x\n")
    return path


@needs_git
def test_status_and_diff_stat(repo: Path) -> None:
    snapshot = git_snapshot(repo)
    assert snapshot is not None
    assert " M app.py" in snapshot["status"] and "?? new.txt" in snapshot["status"]
    assert any("app.py" in line for line in snapshot["diff_stat"])
    assert any("changed" in line for line in snapshot["diff_stat"])


@needs_git
def test_not_a_work_tree_missing_or_none(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    assert git_snapshot(plain) is None
    assert git_snapshot(tmp_path / "missing") is None
    assert git_snapshot(None) is None
    assert git_snapshot("") is None


@needs_git
def test_max_lines_marks_the_cut(repo: Path) -> None:
    for index in range(6):
        (repo / f"extra{index}.txt").write_text("x\n")
    snapshot = git_snapshot(repo, max_lines=3)
    assert snapshot is not None
    assert len(snapshot["status"]) == 4 and snapshot["status"][-1].startswith("… (")


def test_timeout_and_failure_give_none(tmp_path: Path) -> None:
    def slow(argv: list[str], **kwargs: Any) -> Any:
        raise subprocess.TimeoutExpired(argv, kwargs.get("timeout", 0))

    assert git_snapshot(tmp_path, runner=slow) is None

    def failing(argv: list[str], **kwargs: Any) -> Any:
        return subprocess.CompletedProcess(argv, 128, "", "fatal")

    assert git_snapshot(tmp_path, runner=failing) is None


def test_commands_are_read_only(tmp_path: Path) -> None:
    seen: list[tuple[list[str], dict[str, Any]]] = []

    def runner(argv: list[str], **kwargs: Any) -> Any:
        seen.append((argv, kwargs))
        out = "true\n" if "--is-inside-work-tree" in argv else ""
        return subprocess.CompletedProcess(argv, 0, out, "")

    assert git_snapshot(tmp_path, runner=runner) == {"status": [], "diff_stat": []}
    verbs = {argv[argv.index("color.ui=false") + 1] for argv, _ in seen}
    assert verbs <= {"rev-parse", "status", "diff"}
    for argv, kwargs in seen:
        assert "core.fsmonitor=false" in argv
        assert not {"fetch", "pull", "push", "commit", "checkout"} & set(argv)
        assert kwargs["env"]["GIT_OPTIONAL_LOCKS"] == "0"
        assert kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0"
        assert kwargs["stdin"] == subprocess.DEVNULL and kwargs["timeout"] == 5.0


def test_secret_lines_are_dropped(tmp_path: Path) -> None:
    secret = "pass" + "word=correct-horse-battery-9"

    def runner(argv: list[str], **kwargs: Any) -> Any:
        if "--is-inside-work-tree" in argv:
            return subprocess.CompletedProcess(argv, 0, "true\n", "")
        return subprocess.CompletedProcess(argv, 0, f"?? {secret}.txt\n M ok.py\n", "")

    snapshot = git_snapshot(tmp_path, runner=runner)
    assert snapshot is not None and snapshot["status"] == [" M ok.py"]
