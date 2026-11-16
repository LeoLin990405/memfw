"""Byte-identical old/new comparison harness (memfw-architecture section 7, A0).

A refactor step (A1..A5) must leave the default output unchanged. This module
checks that by running the same *probe* against two source trees and diffing
the text it returns (all payloads for one tree run in one subprocess):

* the old tree is ``src/`` and ``scripts/`` from a git ref, unpacked with
  ``git archive`` into a temp directory;
* the new tree is another ref, or the working tree (``new_ref=None``).

Each tree runs in a fresh subprocess with the tree's ``src`` first on ``sys.path``
(the runner checks that ``memfw`` really loaded from there), ``HOME`` pointed
at an empty temp directory and every ``MEMFW_*`` variable removed. Both trees
share this interpreter and its venv (third-party packages, and the probe
module from this checkout); only memfw's own source differs.

An unknown ref, or a ref without ``src/`` and ``scripts/``, raises
``UnknownRef`` (a hard error, never a skip). Only a missing git raises
``HarnessUnavailable``. CI checkouts are shallow by default: a refactor PR
that compares against ``origin/main`` needs ``fetch-depth: 0`` (or a
``git fetch origin main``) first.

A probe is ``"module:function"``. The module is imported from this checkout
(so it may live under ``tests/``) and must only use memfw APIs that exist in
both trees. ``function(payload, workdir)`` returns a ``str`` (compared as is)
or a JSON value (dumped with sorted keys). ``workdir`` is a fresh empty
directory per payload; replace its path in the output if it can appear there.

    results = compare("tests.harness.probes:mcp_memory_search", payloads, old_ref="main")
    assert all(result.identical for result in results), results[0].diff
"""

from __future__ import annotations

import difflib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
TREE_PATHS = ("src", "scripts")

_RUNNER = r"""
import importlib, json, sys, tempfile
from pathlib import Path

spec = json.loads(sys.argv[1])
sys.path[:0] = [spec["src"], spec["repo"]]
import memfw
loaded = Path(memfw.__file__).resolve()
if Path(spec["src"]).resolve() not in loaded.parents:
    raise SystemExit(f"memfw loaded from {loaded}, not from {spec['src']}")
module_name, _, function_name = spec["probe"].partition(":")
function = getattr(importlib.import_module(module_name), function_name)
outputs = []
for payload in spec["payloads"]:
    workdir = Path(tempfile.mkdtemp(prefix="work-", dir=spec["scratch"]))
    result = function(payload, workdir)
    if not isinstance(result, str):
        result = json.dumps(result, ensure_ascii=False, sort_keys=True, indent=1)
    outputs.append(result)
Path(spec["out"]).write_text(json.dumps(outputs), encoding="utf-8")
"""


class UnknownRef(ValueError):
    """The ref does not exist, or holds neither ``src/`` nor ``scripts/``: a hard error."""


class HarnessUnavailable(RuntimeError):
    """git (or the requested ref) is not available; callers usually skip."""


@dataclass(frozen=True, slots=True)
class Comparison:
    """One payload run against both trees."""

    payload: Any
    old: str
    new: str

    @property
    def identical(self) -> bool:
        return self.old == self.new

    @property
    def diff(self) -> str:
        return "".join(
            difflib.unified_diff(
                self.old.splitlines(keepends=True),
                self.new.splitlines(keepends=True),
                fromfile="old",
                tofile="new",
            )
        )


def git_available(repo: Path = REPO) -> bool:
    """``git`` is installed and ``repo`` is a work tree with at least one commit."""

    if shutil.which("git") is None:
        return False
    completed = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", "HEAD^{commit}"],
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.returncode == 0


def materialize(ref: str, dest: Path, *, repo: Path = REPO) -> Path:
    """Unpack ``src/`` and ``scripts/`` at ``ref`` into ``dest``; return ``dest``."""

    if not git_available(repo):
        raise HarnessUnavailable("git or the repository is unavailable")
    listed = subprocess.run(
        ["git", "-C", str(repo), "ls-tree", "--name-only", ref, "--", *TREE_PATHS],
        capture_output=True,
        text=True,
        check=False,
    )
    if listed.returncode != 0:
        raise UnknownRef(f"unknown ref {ref!r}: {listed.stderr.strip()}")
    paths = [name for name in listed.stdout.split() if name]
    if not paths:
        raise UnknownRef(f"ref {ref!r} has neither src/ nor scripts/")
    archive = subprocess.run(
        ["git", "-C", str(repo), "archive", "--format=tar", ref, "--", *paths],
        capture_output=True,
        check=True,
    ).stdout
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(dest, filter="data")
    return dest


def _environment(home: Path) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("MEMFW_")}
    env.pop("PYTHONPATH", None)
    env.update(
        HOME=str(home),
        XDG_DATA_HOME=str(home / ".local" / "share"),
        XDG_CONFIG_HOME=str(home / ".config"),
        XDG_CACHE_HOME=str(home / ".cache"),
        PYTHONHASHSEED="0",
        PYTHONDONTWRITEBYTECODE="1",
        TZ="UTC",
    )
    return env


def run_probe(
    tree: Path,
    probe: str,
    payloads: Sequence[Any],
    *,
    scratch: Path,
    timeout: float = 120.0,
) -> list[str]:
    """Run ``probe(payload, workdir)`` for each payload in one subprocess on ``tree/src``."""

    scratch.mkdir(parents=True, exist_ok=True)
    home = Path(tempfile.mkdtemp(prefix="home-", dir=scratch))
    out = scratch / "outputs.json"
    spec = {
        "src": str(tree / "src"),
        "repo": str(REPO),
        "probe": probe,
        "payloads": list(payloads),
        "scratch": str(scratch),
        "out": str(out),
    }
    completed = subprocess.run(
        [sys.executable, "-c", _RUNNER, json.dumps(spec)],
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=scratch,
        env=_environment(home),
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"probe {probe} failed in {tree}:\n{completed.stderr[-4000:]}")
    outputs = json.loads(out.read_text(encoding="utf-8"))
    if not isinstance(outputs, list) or len(outputs) != len(payloads):
        raise RuntimeError(f"probe {probe} returned {len(outputs)} outputs")
    return [str(item) for item in outputs]


def compare(
    probe: str,
    payloads: Sequence[Any],
    *,
    old_ref: str,
    new_ref: str | None = None,
    repo: Path = REPO,
) -> list[Comparison]:
    """Run each payload on ``old_ref`` and on ``new_ref`` (``None``: working tree)."""

    with tempfile.TemporaryDirectory(prefix="memfw-byte-identical-") as tmp:
        base = Path(tmp)
        old_tree = materialize(old_ref, base / "old", repo=repo)
        new_tree = repo if new_ref is None else materialize(new_ref, base / "new", repo=repo)
        old = run_probe(old_tree, probe, payloads, scratch=base / "runs-old")
        new = run_probe(new_tree, probe, payloads, scratch=base / "runs-new")
    return [
        Comparison(payload=payload, old=before, new=after)
        for payload, before, after in zip(payloads, old, new, strict=True)
    ]


__all__ = [
    "Comparison",
    "HarnessUnavailable",
    "UnknownRef",
    "compare",
    "git_available",
    "materialize",
    "run_probe",
]
