"""Stage dream candidates under ``_candidates/dream/<dream_id>/``.

One file per concept (``<oid>.md``), plus ``report.json``. A dream's files
are written into a private temporary directory and published with a single
rename, so concurrent dreams never overwrite each other and a half-written
dream is never visible. The authoritative vault is never touched.
"""

from __future__ import annotations

import json
import os
import re
from collections import defaultdict
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


def render_concept(dream_id: str, oid: str, candidates: list[Candidate], created_at: str) -> str:
    """Markdown for one concept: YAML frontmatter with the candidates, then a summary."""

    front = {
        "dream_id": dream_id,
        "oid": oid,
        "created_at": created_at,
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


def _write_new(path: Path, text: str) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())


def stage(
    vault: Path,
    dream_id: str,
    candidates: list[Candidate],
    report: dict[str, Any],
    *,
    created_at: str,
) -> Path:
    """Publish one dream; return its directory (relative to the vault)."""

    check_dream_id(dream_id)
    base = Path(vault) / DREAM_DIR
    for part in (Path(vault) / "_candidates", base):
        if part.is_symlink():
            raise ValueError(f"refusing to stage through a symlink: {part.name}")
    base.mkdir(parents=True, exist_ok=True)
    final = base / dream_id
    if final.exists() or final.is_symlink():
        raise DreamExistsError(f"dream {dream_id} is already staged")
    temporary = base / f".tmp-{dream_id}-{os.getpid()}"
    temporary.mkdir(mode=0o700)
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
            _write_new(temporary / name, render_concept(dream_id, oid, items, created_at))
        report = {**report, "files": names}
        _write_new(
            temporary / "report.json",
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
        # One rename publishes the whole dream; it fails if another dream won.
        try:
            os.rename(temporary, final)
        except OSError as exc:
            raise DreamExistsError(f"dream {dream_id} is already staged") from exc
    except BaseException:
        for leftover in sorted(temporary.glob("*")) if temporary.exists() else []:
            leftover.unlink()
        if temporary.exists():
            temporary.rmdir()
        raise
    return final.relative_to(Path(vault))
