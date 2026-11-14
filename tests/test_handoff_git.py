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


def _fake(argv: list[str], out: str = "") -> Any:
    if "--is-inside-work-tree" in argv:
        return subprocess.CompletedProcess(argv, 0, "true\n", "")
    if "--get-regexp" in argv:
        return subprocess.CompletedProcess(argv, 1, "", "")  # no filter.* key
    return subprocess.CompletedProcess(argv, 0, out, "")


def test_commands_are_read_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[list[str], dict[str, Any]]] = []
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "elsewhere"))
    monkeypatch.setenv("GIT_WORK_TREE", str(tmp_path / "elsewhere"))
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", "'filter.x.clean'='touch y'")

    def runner(argv: list[str], **kwargs: Any) -> Any:
        seen.append((argv, kwargs))
        return _fake(argv)

    assert git_snapshot(tmp_path, runner=runner) == {"status": [], "diff_stat": []}
    verbs = {argv[argv.index("color.ui=false") + 1] for argv, _ in seen}
    assert verbs <= {"rev-parse", "config", "status", "diff"}
    for argv, kwargs in seen:
        assert "core.fsmonitor=false" in argv and "core.hooksPath=/dev/null" in argv
        assert not {"fetch", "pull", "push", "commit", "checkout"} & set(argv)
        env = kwargs["env"]
        assert env["GIT_OPTIONAL_LOCKS"] == "0" and env["GIT_TERMINAL_PROMPT"] == "0"
        assert {key for key in env if key.startswith("GIT_")} == {
            "GIT_OPTIONAL_LOCKS",
            "GIT_TERMINAL_PROMPT",
        }
        assert kwargs["stdin"] == subprocess.DEVNULL and kwargs["timeout"] == 5.0
    diff = next(argv for argv, _ in seen if "diff" in argv)
    assert {"--no-ext-diff", "--no-textconv"} <= set(diff)


def test_a_configured_filter_or_a_failed_check_skips_the_snapshot(tmp_path: Path) -> None:
    def found(argv: list[str], **kwargs: Any) -> Any:
        if "--get-regexp" in argv:
            return subprocess.CompletedProcess(argv, 0, "filter.x.clean cmd\n", "")
        assert "status" not in argv and "diff" not in argv
        return _fake(argv)

    assert git_snapshot(tmp_path, runner=found) is None

    def broken(argv: list[str], **kwargs: Any) -> Any:
        if "--get-regexp" in argv:
            return subprocess.CompletedProcess(argv, 128, "", "bad config")
        return _fake(argv)

    assert git_snapshot(tmp_path, runner=broken) is None


@needs_git
@pytest.mark.parametrize("command", ["clean", "smudge", "process"])
def test_filter_in_repo_config_never_runs(repo: Path, tmp_path: Path, command: str) -> None:
    marker = tmp_path / f"marker-{command}"
    script = tmp_path / "filter.sh"
    script.write_text(f"#!/bin/sh\ntouch '{marker}'\ncat\n")
    script.chmod(0o755)
    (repo / ".gitattributes").write_text("*.py filter=evil diff=evil\n")
    _git(repo, "config", f"filter.evil.{command}", str(script))
    _git(repo, "config", "diff.evil.textconv", str(script))
    _git(repo, "config", "diff.external", str(script))
    (repo / "app.py").write_text("print('changed again')\n")
    assert git_snapshot(repo) is None
    assert not marker.exists()


@needs_git
def test_textconv_and_external_diff_never_run(repo: Path, tmp_path: Path) -> None:
    marker = tmp_path / "marker-diff"
    script = tmp_path / "diff.sh"
    script.write_text(f"#!/bin/sh\ntouch '{marker}'\ncat \"$1\"\n")
    script.chmod(0o755)
    (repo / ".gitattributes").write_text("*.py diff=evil\n")
    _git(repo, "config", "diff.evil.textconv", str(script))
    _git(repo, "config", "diff.external", str(script))
    snapshot = git_snapshot(repo)
    assert snapshot is not None and any("app.py" in line for line in snapshot["diff_stat"])
    assert not marker.exists()


@needs_git
def test_inherited_git_dir_does_not_redirect(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = tmp_path / "other"
    other.mkdir()
    _git(other, "init", "-q")
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(other))
    snapshot = git_snapshot(repo)
    assert snapshot is not None and " M app.py" in snapshot["status"]


def test_secret_lines_are_dropped(tmp_path: Path) -> None:
    secret = "pass" + "word=correct-horse-battery-9"

    def runner(argv: list[str], **kwargs: Any) -> Any:
        return _fake(argv, f"?? {secret}.txt\n M ok.py\n")

    snapshot = git_snapshot(tmp_path, runner=runner)
    assert snapshot is not None and snapshot["status"] == [" M ok.py"]
