"""Read-only git state of a session's working directory for the handoff pack.

Only ``git rev-parse``, ``git status --porcelain`` and ``git diff --stat``
run: no fetch, no optional locks (``GIT_OPTIONAL_LOCKS=0``), no fsmonitor,
no prompt, a timeout per command. Every line is redacted; a line that still
looks like it holds a secret is dropped.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from memfw.conversations.store import safe_text

LINE_CHARS = 300
Runner = Callable[..., "subprocess.CompletedProcess[str]"]


def _git(cwd: Path, *args: str, timeout: float, runner: Runner) -> str | None:
    argv = [
        "git",
        "-C",
        str(cwd),
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.untrackedCache=false",
        "-c",
        "color.ui=false",
        *args,
    ]
    env = dict(os.environ)
    env.update({"GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"})
    try:
        result: Any = runner(
            argv,
            cwd=str(cwd),
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if getattr(result, "returncode", 1) != 0:
        return None
    return str(result.stdout or "")


def _lines(output: str, max_lines: int) -> list[str]:
    kept: list[str] = []
    for raw in output.splitlines():
        line = raw.rstrip()
        if not line:
            continue
        text, withheld = safe_text(line, LINE_CHARS)
        if not withheld:
            kept.append(text)
    if len(kept) > max_lines:
        extra = len(kept) - max_lines
        kept = [*kept[:max_lines], f"… ({extra} more)"]
    return kept


def git_snapshot(
    cwd: str | Path | None,
    *,
    timeout: float = 5.0,
    max_lines: int = 100,
    runner: Runner = subprocess.run,
) -> dict[str, list[str]] | None:
    """``{"status", "diff_stat"}`` of ``cwd``'s work tree, or ``None`` when unavailable.

    ``diff_stat`` compares the work tree with ``HEAD`` (staged and unstaged
    changes together) when there is a commit, else with the index.
    """

    if not cwd:
        return None
    try:
        directory = Path(cwd).expanduser()
        if not directory.is_dir():
            return None
    except (OSError, RuntimeError):
        return None
    inside = _git(directory, "rev-parse", "--is-inside-work-tree", timeout=timeout, runner=runner)
    if inside is None or inside.strip() != "true":
        return None
    status = _git(
        directory,
        "status",
        "--porcelain=v1",
        "--untracked-files=normal",
        timeout=timeout,
        runner=runner,
    )
    if status is None:
        return None
    has_head = (
        _git(directory, "rev-parse", "--verify", "--quiet", "HEAD", timeout=timeout, runner=runner)
        is not None
    )
    diff_args = ("diff", "--stat", "--no-color", *(("HEAD",) if has_head else ()))
    diff = _git(directory, *diff_args, timeout=timeout, runner=runner)
    if diff is None:
        return None
    return {"status": _lines(status, max_lines), "diff_stat": _lines(diff, max_lines)}
