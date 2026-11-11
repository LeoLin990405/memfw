"""Dream operation ④ — the injected-layer budget (proposal only).

Injected files (``MEMORY.md`` and agent start-up files such as ``CLAUDE.md`` /
``AGENTS.md``) are loaded at the start of every session, so they have a hard
budget: :data:`MAX_LINES` lines or :data:`MAX_BYTES` bytes. ``check_budget``
measures them and, for a file over budget, proposes which sections to demote
(move to a detail page, leaving a one-line pointer). Nothing is edited.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from memfw.dream.model import Candidate
from memfw.history.redact import has_secret, redact

MAX_LINES = 200
MAX_BYTES = 25 * 1024
_READ_LIMIT = MAX_BYTES * 8

_HEADING = re.compile(r"^(#{1,6})[ \t]+(.*?)[ \t#]*$")
_FENCE = re.compile(r"^\s*(```|~~~)")
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True)
class BudgetReport:
    """Size of one injected file against the budget, plus demotion proposals."""

    path: str
    lines: int
    bytes: int
    over: bool
    proposals: list[dict[str, Any]] = field(default_factory=list)
    skipped: str | None = None
    """Why the file was not measured (``symlink``, ``not a regular file``), if so."""


def _is_regular_file(path: Path) -> bool:
    """A regular file that is not itself a symlink (symlinks are never followed)."""

    try:
        return not path.is_symlink() and path.is_file()
    except OSError:
        return False


def _through_symlink(vault: Path, path: Path) -> bool:
    """Whether a path inside the vault passes through a symlinked directory."""

    try:
        relative = path.relative_to(vault)
    except ValueError:
        return False
    current = vault
    for part in relative.parts[:-1]:
        current = current / part
        if current.is_symlink():
            return True
    return False


def _entries(vault: Path, config: dict[str, Any]) -> list[tuple[Path, str | None]]:
    """Every configured injected path with why it is skipped (``None``: measure it).

    Missing files are dropped silently; a symlink or a non-regular file is
    reported, never followed.
    """

    entries = config.get("injected_files")
    if entries is None:
        entries = []
    if not isinstance(entries, list) or not all(isinstance(item, str) for item in entries):
        raise ValueError("injected_files must be a list of strings")
    candidates = [vault / "MEMORY.md"]
    for entry in entries:
        path = Path(entry).expanduser()
        candidates.append(path if path.is_absolute() else vault / path)
    seen: set[str] = set()
    found: list[tuple[Path, str | None]] = []
    for path in candidates:
        key = os.path.normcase(os.path.normpath(os.path.abspath(path)))
        if key in seen:
            continue
        seen.add(key)
        if path.is_symlink() or _through_symlink(vault, path):
            found.append((path, "symlink"))
        elif _is_regular_file(path):
            found.append((path, None))
        elif path.exists():
            found.append((path, "not a regular file"))
    return found


def injected_files(vault: Path, config: dict[str, Any]) -> list[Path]:
    """``<vault>/MEMORY.md`` (when present) and the configured ``injected_files``.

    Relative entries resolve against the vault; ``~`` is expanded. Missing files,
    directories and symlinks are left out (``check_budget`` reports the latter).
    Order is kept, duplicates dropped.
    """

    return [path for path, skipped in _entries(vault, config) if skipped is None]


def _display_path(vault: Path, path: Path) -> str:
    """Path relative to the vault when inside it, else only the file name."""

    for base, target in ((vault.absolute(), path.absolute()), (vault.resolve(), path.resolve())):
        try:
            return safe_heading(target.relative_to(base).as_posix())
        except ValueError:
            continue
    return safe_heading(path.name)


def _count_lines(data: bytes) -> int:
    if not data:
        return 0
    return data.count(b"\n") + (0 if data.endswith(b"\n") else 1)


def _within(lines: int, size: int) -> bool:
    return lines <= MAX_LINES and size <= MAX_BYTES


def _sections(lines: list[str]) -> list[tuple[str, int, int]]:
    """``(heading text, start, end)`` for each Markdown heading section (flat, disjoint)."""

    starts: list[tuple[int, str]] = []
    in_fence = False
    for index, line in enumerate(lines):
        if _FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = _HEADING.match(line.rstrip("\r\n"))
        if match is not None:
            starts.append((index, safe_heading(match.group(2).strip() or match.group(1))))
    result: list[tuple[str, int, int]] = []
    for position, (start, heading) in enumerate(starts):
        end = starts[position + 1][0] if position + 1 < len(starts) else len(lines)
        result.append((heading, start, end))
    return result


WITHHELD_HEADING = "[withheld heading: secret detected]"
MAX_PROPOSALS = 20
"""Most demotion proposals per file (the largest sections first)."""


def safe_heading(text: str) -> str:
    """A heading as it may appear in a candidate: withheld if it holds a secret,
    otherwise redacted (e-mail, IP, user names) and capped."""

    if has_secret(text):
        return WITHHELD_HEADING
    return redact(text)[:200]


def _pointer(heading: str) -> str:
    return f"{heading}: details demoted to a linked page (one-line pointer)"


def _proposals(text: str, total_lines: int, total_bytes: int) -> list[dict[str, Any]]:
    lines = text.splitlines(keepends=True)
    sizes = [len(line.encode("utf-8")) for line in lines]
    sections = _sections(lines)

    if sections:
        measured = [
            (heading, end - start, sum(sizes[start:end]), start) for heading, start, end in sections
        ]
        measured.sort(key=lambda item: (-item[2], -item[1], item[3]))
        remaining_lines, remaining_bytes = total_lines, total_bytes
        proposals: list[dict[str, Any]] = []
        for heading, count, size, _start in measured:
            if _within(remaining_lines, remaining_bytes) or len(proposals) >= MAX_PROPOSALS:
                break
            keep = _pointer(heading)
            keep_bytes = len(keep.encode("utf-8")) + 1
            if count <= 1 and size <= keep_bytes:
                continue  # demoting a bare heading saves nothing
            remaining_lines += 1 - count
            remaining_bytes += keep_bytes - size
            proposals.append(
                {
                    "section": heading,
                    "lines": count,
                    "bytes": size,
                    "action": "demote",
                    "keep": keep,
                }
            )
        return proposals

    # No headings: demote the tail, keeping as much of the head as fits with a pointer.
    keep = _pointer("tail")
    keep_bytes = len(keep.encode("utf-8")) + 1
    cut = 0
    head_bytes = 0
    for size in sizes:
        if cut + 1 >= MAX_LINES or head_bytes + size + keep_bytes > MAX_BYTES:
            break
        cut += 1
        head_bytes += size
    return [
        {
            "section": f"lines {cut + 1}-{total_lines}",
            "lines": total_lines - cut,
            "bytes": total_bytes - head_bytes,
            "action": "demote",
            "keep": keep,
        }
    ]


def check_budget(vault: Path, config: dict[str, Any]) -> list[BudgetReport]:
    """Measure each injected file; over-budget files get demotion proposals."""

    reports: list[BudgetReport] = []
    for path, skipped in _entries(vault, config):
        if skipped is not None:
            reports.append(
                BudgetReport(
                    path=_display_path(vault, path), lines=0, bytes=0, over=False, skipped=skipped
                )
            )
            continue
        with path.open("rb") as handle:
            data = handle.read(_READ_LIMIT)
        size = max(len(data), path.stat().st_size)
        lines = _count_lines(data)
        over = not _within(lines, size)
        proposals = _proposals(data.decode("utf-8", errors="replace"), lines, size) if over else []
        reports.append(
            BudgetReport(
                path=_display_path(vault, path),
                lines=lines,
                bytes=size,
                over=over,
                proposals=proposals,
            )
        )
    return reports


def budget_candidates(reports: list[BudgetReport]) -> list[Candidate]:
    """One ``promote``/``demote`` candidate per over-budget file."""

    candidates: list[Candidate] = []
    for report in reports:
        if not report.over:
            continue
        safe = _UNSAFE.sub("_", report.path).strip("._") or "unnamed"
        sections = ", ".join(str(item["section"]) for item in report.proposals) or "none"
        candidates.append(
            Candidate(
                operation="promote",
                action="demote",
                memory_ids=[],
                oid=f"injected-{safe[:100]}",
                reason=(
                    f"{report.path} is over the injected budget: {report.lines} lines "
                    f"(limit {MAX_LINES}), {report.bytes} bytes (limit {MAX_BYTES}). "
                    f"Proposal: demote {sections} and keep a one-line pointer each. "
                    "A human decides; the file is not edited."
                ),
                changes={"file": report.path, "proposals": [dict(p) for p in report.proposals]},
            )
        )
    candidates.sort(key=lambda item: item.oid)
    return candidates
