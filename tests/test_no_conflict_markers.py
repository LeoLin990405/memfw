"""No merge-conflict markers in tracked text files.

A rebase that leaves a diff3 ``|||||||`` block (or any other conflict marker)
in a committed file fails here instead of in review. Checked: lines that are
seven ``<``, ``|`` or ``>`` alone or followed by a space, and a line that is
exactly ``=======``, except a Markdown setext heading underline (``=======``
directly under a non-empty line in a ``.md`` file). Binary files (a NUL in the
first 8 KiB) and files over ``SIZE_CAP`` are skipped. The file list is ``git
ls-files``; without git, outside a checkout, or when it lists nothing, the
tree is walked instead (skipping environments, caches and the ignored data
and vault directories).
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Iterable, Iterator
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
# Built, not written out: this file must not trip its own check.
PREFIXES = tuple(char * 7 + " " for char in "<|>")
MARKER = re.compile(r"^([<|>])\1{6}( |$)")
SEPARATOR = "=" * 7
MARKDOWN = {".md", ".markdown"}
HEAD_BYTES = 8192
#: Larger files are not text a person edits (fixtures, dumps): skipped.
SIZE_CAP = 5 * 1024 * 1024
#: Skipped when walking the tree without git: environments, caches, and the
#: data/vault directories ``.gitignore`` keeps out of the repo.
WALK_SKIP = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".cache",
    ".codegraph",
    "build",
    "dist",
    "htmlcov",
    "localdata",
    "logs",
    "data",
    "datasets",
    "private",
    "vault",
    "vaults",
    "models",
    "model_cache",
    "pilot-vault",
    "pilot_vault",
    "memfw-data",
    "memories",
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
    if not names:
        return sorted(_walk(root))
    return [root / name for name in names]


def _walk(root: Path) -> Iterator[Path]:
    for directory, subdirs, files in os.walk(root):
        subdirs[:] = [name for name in subdirs if name not in WALK_SKIP]
        for name in files:
            yield Path(directory) / name


def _is_binary(data: bytes) -> bool:
    return b"\0" in data[:HEAD_BYTES]


def _read_text(path: Path) -> bytes | None:
    """The file's bytes, or None for a link, a non-file, an oversized or binary file."""

    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > SIZE_CAP:
            return None
        with path.open("rb") as handle:
            head = handle.read(HEAD_BYTES)
            if _is_binary(head):
                return None
            return head + handle.read()
    except OSError:
        return None


def conflict_lines(path: Path, data: bytes) -> list[int]:
    """1-based numbers of the lines in ``data`` that look like conflict markers."""

    if _is_binary(data):
        return []
    markdown = path.suffix.lower() in MARKDOWN
    found: list[int] = []
    previous = ""
    for number, raw in enumerate(data.split(b"\n"), start=1):
        line = raw.rstrip(b"\r").decode("utf-8", "replace")
        if MARKER.match(line):
            found.append(number)
        elif line == SEPARATOR and not (markdown and previous.strip()):
            found.append(number)
        previous = line
    return found


def scan(paths: Iterable[Path], root: Path) -> list[str]:
    problems: list[str] = []
    for path in paths:
        data = _read_text(path)  # None: deleted, a submodule, a link, binary or huge
        if data is None:
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
        # Bare markers (no label after them) count too.
        ("a.py", "x\n" + "<" * 7 + "\n" + "|" * 7 + "\n" + ">" * 7 + "\n", [2, 3, 4]),
        ("a.txt", "<" * 8 + "\n" + "|" * 6 + "\n", []),
    ],
)
def test_conflict_lines(tmp_path: Path, name: str, text: str, lines: list[int]) -> None:
    assert conflict_lines(tmp_path / name, text.encode()) == lines


def test_binary_files_are_skipped(tmp_path: Path) -> None:
    assert conflict_lines(tmp_path / "a.bin", f"\0\n{SEPARATOR}\n".encode()) == []


def test_bare_marker_block_in_markdown_is_caught(tmp_path: Path) -> None:
    # The separator sits under "ours" (it reads as a setext underline), but the
    # bare opening and closing markers still catch the block.
    path = tmp_path / "notes.md"
    path.write_text(
        "# Notes\n\n" + "<" * 7 + "\nours\n" + SEPARATOR + "\ntheirs\n" + ">" * 7 + "\n"
    )
    assert scan([path], tmp_path) == ["notes.md:3", "notes.md:7"]


def test_scan_skips_binary_and_oversized_files(tmp_path: Path) -> None:
    binary = tmp_path / "blob.dat"
    binary.write_bytes(b"\0" + f"\n{PREFIXES[0]}x\n".encode())
    huge = tmp_path / "dump.txt"
    with huge.open("wb") as handle:
        handle.write(f"{PREFIXES[0]}x\n".encode())
        handle.truncate(SIZE_CAP + 1)
    late = tmp_path / "late.txt"  # a marker after the first 8 KiB is still read
    late.write_text("a\n" * HEAD_BYTES + f"{PREFIXES[2]}x\n")
    assert scan([binary, huge, late], tmp_path) == [f"late.txt:{HEAD_BYTES + 1}"]


def test_walk_fallback_without_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "bad.py").write_text(f"a\n{PREFIXES[1]}base\n")
    for skipped in (".venv", "vault", "data"):
        (tmp_path / skipped).mkdir()
        (tmp_path / skipped / "ignored.py").write_text(f"{PREFIXES[0]}x\n")

    def no_git(*args: object, **kwargs: object) -> None:
        raise FileNotFoundError("git")

    monkeypatch.setattr(subprocess, "run", no_git)
    files = tracked_files(tmp_path)
    assert files == [tmp_path / "src" / "bad.py"]
    assert scan(files, tmp_path) == ["src/bad.py:2"]


def test_empty_git_listing_falls_back_to_the_walk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "bad.py").write_text(f"{PREFIXES[2]}x\n")

    def empty(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(args, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", empty)
    assert tracked_files(tmp_path) == [tmp_path / "bad.py"]
