"""Read-only git state of a session's working directory for the handoff pack.

Only ``git rev-parse``, ``git config --get-regexp``, ``git status
--porcelain`` and ``git diff --stat`` run: no fetch, no optional locks
(``GIT_OPTIONAL_LOCKS=0``), no fsmonitor, no hooks, no external diff or
textconv, no submodules, no prompt, a timeout per command, and no inherited
``GIT_*`` variable that could redirect or inject config. A repository whose
own config (local or worktree, with its includes) defines a ``filter.*``
driver gets no snapshot: clean/smudge filters are commands. The user's
global and system config are trusted, so a git-lfs filter there is fine;
textconv and external diff never run, whichever scope sets them.

Every line is redacted; a line that still
looks like it holds a secret is dropped.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from memfw.history.transcripts import safe_text

LINE_CHARS = 300
Runner = Callable[..., "subprocess.CompletedProcess[str]"]


_SAFE_CONFIG = (
    "core.fsmonitor=false",
    "core.untrackedCache=false",
    "core.hooksPath=/dev/null",
    "color.ui=false",
)


_USER_CONFIG_VARS = frozenset({"GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_NOSYSTEM"})
"""Kept: they only choose which of the user's own (trusted) config files git reads."""


def _env() -> dict[str, str]:
    """The environment without ``GIT_*`` variables that would override ``-C`` or inject config.

    ``GIT_DIR``, ``GIT_WORK_TREE``, ``GIT_CONFIG_PARAMETERS``, ``GIT_CONFIG_COUNT``
    and the rest are dropped; only :data:`_USER_CONFIG_VARS` stay.
    """

    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("GIT_") or key in _USER_CONFIG_VARS
    }
    env.update({"GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"})
    return env


def _run(cwd: Path, *args: str, timeout: float, runner: Runner) -> tuple[int, str] | None:
    """``(exit code, stdout)``, or ``None`` when git could not run or timed out."""

    argv = ["git", "-C", str(cwd)]
    for setting in _SAFE_CONFIG:
        argv += ["-c", setting]
    argv += list(args)
    try:
        result: Any = runner(
            argv,
            cwd=str(cwd),
            env=_env(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return int(getattr(result, "returncode", 1)), str(result.stdout or "")


def _git(cwd: Path, *args: str, timeout: float, runner: Runner) -> str | None:
    outcome = _run(cwd, *args, timeout=timeout, runner=runner)
    if outcome is None or outcome[0] != 0:
        return None
    return outcome[1]


def _has_filters(cwd: Path, *, timeout: float, runner: Runner) -> bool:
    """True when the repository itself defines a ``filter.*`` driver (or the check fails).

    ``status`` and ``diff`` run clean/smudge filters, so a filter set in the
    repository's own config is a command chosen by whoever wrote that repo.
    Only the repo-controlled scopes are checked: ``--local`` and
    ``--worktree``, with ``--includes`` so ``include`` / ``includeIf`` files
    they pull in count too. Global and system config belong to the user and
    are trusted (git-lfs puts ``filter.lfs.*`` there on most machines).
    """

    for scope in ("--local", "--worktree"):
        outcome = _run(
            cwd,
            "config",
            scope,
            "--includes",
            "--get-regexp",
            r"^filter\.",
            timeout=timeout,
            runner=runner,
        )
        # Exit 1 means "no such key"; anything else (0 = found, errors) is unsafe.
        if outcome is None or outcome[0] != 1:
            return True
    return False


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
    if _has_filters(directory, timeout=timeout, runner=runner):
        return None
    status = _git(
        directory,
        "status",
        "--porcelain=v1",
        "--untracked-files=normal",
        "--ignore-submodules=all",
        timeout=timeout,
        runner=runner,
    )
    if status is None:
        return None
    has_head = (
        _git(directory, "rev-parse", "--verify", "--quiet", "HEAD", timeout=timeout, runner=runner)
        is not None
    )
    diff_args = (
        "diff",
        "--stat",
        "--no-color",
        "--no-ext-diff",
        "--no-textconv",
        "--ignore-submodules=all",
        *(("HEAD",) if has_head else ()),
    )
    diff = _git(directory, *diff_args, timeout=timeout, runner=runner)
    if diff is None:
        return None
    return {"status": _lines(status, max_lines), "diff_stat": _lines(diff, max_lines)}
