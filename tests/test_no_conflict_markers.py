"""No merge-conflict markers in tracked text files.

A rebase that leaves a diff3 ``|||||||`` block (or any other conflict marker)
in a committed file fails here instead of in review. Checked: lines starting
with ``<<<<<<< ``, ``||||||| `` or ``>>>>>>> ``, and a line that is exactly
``=======``, except a Markdown setext heading underline (``=======`` directly
under a non-empty line in a ``.md`` file). Binary files are skipped. The file
list is ``git ls-files``; without git (or outside a checkout) the tree is
walked instead.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterable, Iterator
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
# Built, not written out: this file must not trip its own check.
PREFIXES = tuple(char * 7 + " " for char in "<|>")
SEPARATOR = "=" * 7
MARKDOWN = {".md", ".markdown"}
#: Skipped when walking the tree without git (never tracked in this repo).
WALK_SKIP = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".codegraph",
    "build",
    "dist",
}


def tracked_files(root: Path) -> list[Path]:
    """``git ls-files`` under ``root``, or every file under it when git is unavailable."""

    try:
        result = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z"],
            capture_output=True,
            check=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return sorted(_walk(root))
    names = [name for name in result.stdout.decode("utf-8", "surrogateescape").split("\0") if name]
    return [root / name for name in names]


def _walk(root: Path) -> Iterator[Path]:
    for directory, subdirs, files in os.walk(root):
        subdirs[:] = [name for name in subdirs if name not in WALK_SKIP]
        for name in files:
            yield Path(directory) / name


def _is_binary(data: bytes) -> bool:
    return b"\0" in data[:8192]


def conflict_lines(path: Path, data: bytes) -> list[int]:
    """1-based numbers of the lines in ``data`` that look like conflict markers."""

    if _is_binary(data):
        return []
    markdown = path.suffix.lower() in MARKDOWN
    found: list[int] = []
    previous = ""
    for number, raw in enumerate(data.split(b"\n"), start=1):
        line = raw.rstrip(b"\r").decode("utf-8", "replace")
        if line.startswith(PREFIXES):
            found.append(number)
        elif line == SEPARATOR and not (markdown and previous.strip()):
            found.append(number)
        previous = line
    return found


def scan(paths: Iterable[Path], root: Path) -> list[str]:
    problems: list[str] = []
    for path in paths:
        if path.is_symlink() or not path.is_file():
            continue  # deleted in the worktree, a submodule, or a link
        try:
            data = path.read_bytes()
        except OSError:
            continue
        for number in conflict_lines(path, data):
            problems.append(f"{path.relative_to(root).as_posix()}:{number}")
    return problems


def test_tracked_files_have_no_conflict_markers() -> None:
    files = tracked_files(ROOT)
    assert files, "no files found to check"
    problems = scan(files, ROOT)
    assert problems == [], "conflict markers left in: " + ", ".join(problems)


@pytest.mark.parametrize(
    ("name", "text", "lines"),
    [
        ("a.py", f"x = 1\n{PREFIXES[0]}HEAD\ny\n", [2]),
        ("a.py", f"x\n{PREFIXES[1]}merged common ancestors\ny\n", [2]),
        ("a.py", f"x\n{PREFIXES[2]}topic\n", [2]),
        ("a.py", f"x\n{SEPARATOR}\ny\n", [2]),
        ("a.py", f"x\r\n{SEPARATOR}\r\n", [2]),
        # Setext heading in Markdown: fine. Without a title line above it: a marker.
        ("a.md", f"Title\n{SEPARATOR}\n", []),
        ("a.md", f"\n{SEPARATOR}\n", [2]),
        ("a.md", f"{SEPARATOR}\n", [1]),
        # Only an exact separator line, only markers at the line start.
        ("a.txt", f"{SEPARATOR}=\n {SEPARATOR}\n  {PREFIXES[0]}x\n", []),
        ("a.txt", "<<<<<<<no space\n", []),
    ],
)
def test_conflict_lines(tmp_path: Path, name: str, text: str, lines: list[int]) -> None:
    assert conflict_lines(tmp_path / name, text.encode()) == lines


def test_binary_files_are_skipped(tmp_path: Path) -> None:
    assert conflict_lines(tmp_path / "a.bin", f"\0\n{SEPARATOR}\n".encode()) == []


def test_walk_fallback_without_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "bad.py").write_text(f"a\n{PREFIXES[1]}base\n")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "ignored.py").write_text(f"{PREFIXES[0]}x\n")

    def no_git(*args: object, **kwargs: object) -> None:
        raise FileNotFoundError("git")

    monkeypatch.setattr(subprocess, "run", no_git)
    files = tracked_files(tmp_path)
    assert files == [tmp_path / "src" / "bad.py"]
    assert scan(files, tmp_path) == ["src/bad.py:2"]
