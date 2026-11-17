"""The dream side of the claim ports: the lexical verifier and candidate staging.

The conversation worker verifies and stages claims through
:class:`memfw.ports.claims.ClaimVerifier` and :class:`~memfw.ports.claims.ClaimStager`
and does not import this package (architecture A2a). ``memfw conversations
work`` injects these two; staged claims become ``distill`` candidates of one
dream under ``_candidates/dream/<batch_id>/``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from memfw.ports.claims import StagedClaim

from .model import Candidate, EvidenceRef
from .stage import DreamExistsError, stage
from .verify import classify


class DreamClaimVerifier:
    """:func:`memfw.dream.verify.classify`'s polarity (no model call)."""

    def polarity(self, claim: str, text: str) -> str:
        polarity, _values = classify(claim, text)
        return polarity


class DreamClaimStager:
    """Stages a batch of claims as one dream of ``distill`` candidates.

    Each candidate proposes ``privacy: local-private`` in ``changes`` (MM0):
    a claim comes from a transcript and must not reach a remote exit by way
    of the vault's defaults.
    """

    def stage_claims(
        self,
        vault: Path,
        batch_id: str,
        claims: Sequence[StagedClaim],
        report: Mapping[str, Any],
        *,
        created_at: str,
    ) -> bool:
        candidates = [
            Candidate(
                operation="distill",
                action="distill",
                memory_ids=[],
                reason=item.claim,
                oid=item.oid,
                evidence=[
                    EvidenceRef(
                        ref=evidence.ref,
                        polarity="support",
                        session_id=evidence.session_id,
                        timestamp=evidence.timestamp,
                    )
                    for evidence in item.evidence
                ],
                # Conversation-derived (MM0): transcripts are local-private, so
                # the proposed memory is too, whatever the vault defaults say.
                changes={"privacy": "local-private"},
            )
            for item in claims
        ]
        try:
            stage(vault, batch_id, candidates, dict(report), created_at=created_at)
        except DreamExistsError:
            return False  # staged by an earlier, interrupted run: idempotent
        return True


__all__ = ["DreamClaimStager", "DreamClaimVerifier"]
