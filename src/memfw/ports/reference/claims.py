"""Reference ``ClaimVerifier`` and ``ClaimStager``. Stdlib only.

- ``KeywordClaimVerifier``: token overlap. A text that contains at least
  ``min_overlap`` of the claim's content words supports it, unless it
  negates where the claim does not (or the other way round), which
  contradicts it; anything else is neutral. No model call, no memfw import:
  the second verifier next to ``memfw.dream``'s lexical classifier.
- ``FileClaimStager``: one JSON file per batch,
  ``<vault>/_candidates/<name>/<batch_id>.json``, created atomically; a batch
  id that already exists writes nothing and returns ``False``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ..claims import StagedClaim
from ..staging import StagingError, candidate_directory, create_exclusive
from ..types import check_name
from .recall import tokenize

#: Words that carry no claim content.
STOPWORDS: frozenset[str] = frozenset(
    {
        "a", "an", "the", "is", "are", "was", "were", "be", "been", "to", "of", "in", "on",
        "at", "for", "and", "or", "it", "its", "this", "that", "with", "as", "by", "uses",
        "use", "used", "does", "do", "did", "has", "have", "had",
        "的", "了", "是", "在", "和",
    }
)  # fmt: skip

#: Negation markers (after tokenizing: ``don't`` → ``don``, ``t``).
NEGATIONS: frozenset[str] = frozenset(
    {"not", "no", "never", "none", "without", "t", "cannot", "不", "没", "无", "非"}
)

_BATCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class KeywordClaimVerifier:
    """``support`` / ``contradict`` / ``neutral`` from content-word overlap."""

    def __init__(self, *, min_overlap: float = 0.6) -> None:
        if not 0.0 < min_overlap <= 1.0:
            raise ValueError("min_overlap must be within (0, 1]")
        self.min_overlap = min_overlap

    def polarity(self, claim: str, text: str) -> str:
        claim_tokens = tokenize(claim)
        text_tokens = tokenize(text)
        content = {token for token in claim_tokens if token not in STOPWORDS | NEGATIONS}
        if not content:
            return "neutral"
        present = set(text_tokens)
        overlap = len(content & present) / len(content)
        if overlap < self.min_overlap:
            return "neutral"
        claim_negated = any(token in NEGATIONS for token in claim_tokens)
        text_negated = any(token in NEGATIONS for token in text_tokens)
        return "contradict" if claim_negated != text_negated else "support"


class FileClaimStager:
    """Stages a batch as ``_candidates/<name>/<batch_id>.json``."""

    def __init__(self, *, name: str = "claims") -> None:
        self.name = check_name(name, what="stager")

    def stage_claims(
        self,
        vault: Path,
        batch_id: str,
        claims: Sequence[StagedClaim],
        report: Mapping[str, Any],
        *,
        created_at: str,
    ) -> bool:
        if not isinstance(batch_id, str) or not _BATCH_RE.fullmatch(batch_id):
            raise StagingError("batch_id must be a short ASCII name")
        if not isinstance(created_at, str) or not created_at:
            raise StagingError("created_at required")
        records = []
        for item in claims:
            if not isinstance(item, StagedClaim):
                raise StagingError("claims must be StagedClaim values")
            records.append(
                {
                    "claim": item.claim,
                    "oid": item.oid,
                    "evidence": [
                        {
                            "ref": evidence.ref,
                            "session_id": evidence.session_id,
                            "timestamp": evidence.timestamp,
                        }
                        for evidence in item.evidence
                    ],
                }
            )
        try:
            data = json.dumps(
                {
                    "batch_id": batch_id,
                    "stager": self.name,
                    "created_at": created_at,
                    "status": "staged",
                    "claims": records,
                    "report": dict(report),
                },
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise StagingError("batch is not serialisable") from exc
        directory = candidate_directory(vault, self.name)
        return create_exclusive(directory, batch_id, ".json", data + b"\n")


__all__ = ["NEGATIONS", "STOPWORDS", "FileClaimStager", "KeywordClaimVerifier"]
