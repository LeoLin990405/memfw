"""Read-only review view over staged memory candidates.

The candidate file reader lives in :mod:`memfw.storage.candidate_files` (A2b)
so the OKF importer can share it without depending on capture; its names are
re-exported here unchanged.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..core.errors import IntegrityError
from ..storage.candidate_files import (
    CANDIDATE_TTL_DAYS,
    Candidate,
    InvalidProposalIdError,
    candidate_content_hash,
    candidate_dict,
    candidate_inside_vault,
    candidate_is_expired,
    candidate_provenance,
    candidate_recorded_instant,
    parse_candidate_provenance,
    read_candidate,
    staged_candidate_paths,
    validate_candidate_hash,
)

_log = logging.getLogger(__name__)


def list_staged_candidates(
    root: Path | str,
    *,
    include_expired: bool = False,
    now: datetime | None = None,
    skipped: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Staged candidates. Expired ones are hidden unless asked for.

    One bad file does not hide the rest: a symlink that leaves the vault, a
    file that cannot be read or parsed, or one whose body no longer matches
    ``content_hash`` is skipped with a warning. Pass ``skipped`` to collect
    the skipped candidate ids, the same shape ``candidates-expire`` reports.
    """

    current = now or datetime.now(UTC)
    vault = Path(root).expanduser().resolve()
    output: list[dict[str, Any]] = []
    for path in staged_candidate_paths(root):
        reason: str | None = None
        # Every path, not only a symlinked file: a symlinked ``_candidates``
        # or ``remember`` directory leads outside the vault just as well.
        if not candidate_inside_vault(path, vault):
            reason = "resolves outside the vault"
        elif not path.is_file():
            reason = "not a regular file"
        if reason is None:
            try:
                candidate = read_candidate(path)
                validate_candidate_hash(candidate)
            except (OSError, IntegrityError) as exc:
                reason = str(exc) or type(exc).__name__
        if reason is not None:
            _log.warning("skipped candidate %s: %s", path.name, reason)
            if skipped is not None:
                skipped.append(path.stem)
            continue
        expired = candidate_is_expired(candidate, now=current)
        if expired and not include_expired:
            continue
        row = candidate_dict(candidate)
        if expired:
            row["status"] = "expired"
        output.append(row)
    return output


__all__ = [
    "CANDIDATE_TTL_DAYS",
    "Candidate",
    "InvalidProposalIdError",
    "candidate_is_expired",
    "candidate_recorded_instant",
    "candidate_content_hash",
    "candidate_dict",
    "candidate_inside_vault",
    "candidate_provenance",
    "list_staged_candidates",
    "parse_candidate_provenance",
    "read_candidate",
    "staged_candidate_paths",
    "validate_candidate_hash",
]
