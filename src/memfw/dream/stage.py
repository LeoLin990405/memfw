"""Stage dream candidates under ``_candidates/dream/<dream_id>/``.

One file per concept (``<oid>.md``), plus ``report.json``. A dream's files
are written into a private temporary directory and published with a single
rename, so concurrent dreams never overwrite each other and a half-written
dream is never visible. The authoritative vault is never touched.

Every concept file and ``report.json`` carry ``recorded_at``: the server-clock
instant memfw wrote them (transaction time, as for staged memory candidates).
``created_at`` stays the caller's (possibly injected) clock.
"""

from __future__ import annotations

import json
import os
import re
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from .model import Candidate, candidate_filename

DREAM_DIR = Path("_candidates") / "dream"
_DREAM_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$")


class DreamExistsError(FileExistsError):
    """A dream with this id was already staged."""


def check_dream_id(dream_id: str) -> str:
    if not _DREAM_ID.match(dream_id):
        raise ValueError("dream id must be 1-96 characters of letters, digits, '.', '_' or '-'")
    return dream_id


def group_by_concept(candidates: list[Candidate]) -> dict[str, list[Candidate]]:
    grouped: dict[str, list[Candidate]] = defaultdict(list)
    for candidate in candidates:
        grouped[candidate.oid or "unlinked"].append(candidate)
    return dict(sorted(grouped.items()))


def render_concept(
    dream_id: str,
    oid: str,
    candidates: list[Candidate],
    created_at: str,
    recorded_at: str | None = None,
) -> str:
    """Markdown for one concept: YAML frontmatter with the candidates, then a summary."""

    front: dict[str, Any] = {
        "dream_id": dream_id,
        "oid": oid,
        "created_at": created_at,
    }
    if recorded_at:
        front["recorded_at"] = recorded_at
    front |= {
        "authority": False,
        "candidates": [candidate.as_dict() for candidate in candidates],
    }
    lines = [f"# Dream candidates for {oid}", ""]
    for candidate in candidates:
        targets = ", ".join(candidate.memory_ids) or "-"
        lines.append(
            f"- **{candidate.action}** ({candidate.operation}) {targets}: {candidate.reason}"
        )
        if candidate.evidence:
            refs = ", ".join(f"{item.ref} ({item.polarity})" for item in candidate.evidence)
            lines.append(f"  - evidence: {refs}")
    lines += [
        "",
        "These are proposals. Nothing here has changed the vault; a human decides.",
        "Evidence refs point into the transcript index (`memfw history read REF`).",
    ]
    dumped = yaml.safe_dump(front, allow_unicode=True, sort_keys=False).rstrip()
    return f"---\n{dumped}\n---\n\n" + "\n".join(lines) + "\n"


class DreamStageError(ValueError):
    """The staging area is unusable (a symlink, a file, or not writable)."""


_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)


def _child_dir(parent: int, name: str, *, create: bool) -> int:
    """Open (creating if asked) directory ``name`` under the open ``parent``, never
    following a symlink."""

    if create:
        try:
            os.mkdir(name, 0o700, dir_fd=parent)
        except FileExistsError:
            pass
    try:
        return os.open(name, _DIR_FLAGS, dir_fd=parent)
    except OSError as exc:
        raise DreamStageError(
            f"refusing to stage dreams: {name} is not a plain directory ({exc.strerror})"
        ) from exc


def _write_new(directory: int, name: str, text: str) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(name, flags, 0o600, dir_fd=directory)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())


def _exists(directory: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def stage(
    vault: Path,
    dream_id: str,
    candidates: list[Candidate],
    report: dict[str, Any],
    *,
    created_at: str,
) -> Path:
    """Publish one dream; return its directory (relative to the vault).

    Every step works on directory file descriptors (``openat``-style), so a
    symlink swapped into the staging path is refused, not followed.
    """

    check_dream_id(dream_id)
    # Transaction time: the server clock, never the caller's injected one.
    recorded_at = datetime.now(UTC).isoformat()
    opened: list[int] = []
    try:
        root = os.open(Path(vault), _DIR_FLAGS & ~getattr(os, "O_NOFOLLOW", 0))
        opened.append(root)
        candidates_dir = _child_dir(root, DREAM_DIR.parts[0], create=True)
        opened.append(candidates_dir)
        base = _child_dir(candidates_dir, DREAM_DIR.parts[1], create=True)
        opened.append(base)
        if _exists(base, dream_id):
            raise DreamExistsError(f"dream {dream_id} is already staged")
        temporary = f".tmp-{dream_id}-{os.getpid()}-{os.urandom(4).hex()}"
        try:
            os.mkdir(temporary, 0o700, dir_fd=base)
        except OSError as exc:
            raise DreamStageError(f"cannot create a staging directory: {exc.strerror}") from exc
        work = _child_dir(base, temporary, create=False)
        opened.append(work)
        written: list[str] = []
        try:
            names: dict[str, str] = {}
            for oid, items in group_by_concept(candidates).items():
                name = candidate_filename(oid)
                if name in names.values():
                    stem = name[:-3]
                    counter = 2
                    while f"{stem}-{counter}.md" in names.values():
                        counter += 1
                    name = f"{stem}-{counter}.md"
                names[oid] = name
                _write_new(
                    work, name, render_concept(dream_id, oid, items, created_at, recorded_at)
                )
                written.append(name)
            full_report = {**report, "files": names, "recorded_at": recorded_at}
            _write_new(
                work,
                "report.json",
                json.dumps(full_report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            )
            written.append("report.json")
            # One rename publishes the whole dream; it fails if another dream won.
            if _exists(base, dream_id):
                raise DreamExistsError(f"dream {dream_id} is already staged")
            try:
                os.rename(temporary, dream_id, src_dir_fd=base, dst_dir_fd=base)
            except OSError as exc:
                raise DreamExistsError(f"dream {dream_id} is already staged") from exc
        except BaseException:
            for name in written:
                try:
                    os.unlink(name, dir_fd=work)
                except OSError:
                    pass
            try:
                os.rmdir(temporary, dir_fd=base)
            except OSError:
                pass
            raise
    except NotADirectoryError as exc:
        raise DreamStageError(f"refusing to stage dreams: {exc.strerror}") from exc
    finally:
        for descriptor in reversed(opened):
            os.close(descriptor)
    return DREAM_DIR / dream_id
