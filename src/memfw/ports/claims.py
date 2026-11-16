"""Claim ports: verify a claim against transcript text, stage verified claims. Stdlib only.

The conversation worker turns a session summary into short claims, keeps the
ones a transcript turn supports, and stages them as memory candidates for
review. It does both through these two protocols, so it does not import the
implementation (``memfw.dream``: its lexical verifier and its candidate
staging); the composition root injects one (architecture A2a). Any other
verifier (a model judge, say) or staging target that passes
``memfw.ports.testing.check_claim_verifier`` / ``check_claim_stager`` can be
injected instead.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Protocol, runtime_checkable

CLAIMS_PORT_VERSION: Final = 1

#: The polarities a verifier may return.
POLARITIES: Final[tuple[str, ...]] = ("support", "contradict", "neutral")


@runtime_checkable
class ClaimVerifier(Protocol):
    """Whether one piece of (redacted) transcript text supports a claim.

    ``polarity(claim, text)`` returns one of :data:`POLARITIES` and is a pure
    function of its arguments: the same pair always gives the same answer.
    """

    def polarity(self, claim: str, text: str) -> str: ...


@dataclass(frozen=True)
class ClaimEvidence:
    """One supporting turn: an opaque ref (``conv:APP:SESSION@OFFSET``), never text."""

    ref: str
    session_id: str = ""
    timestamp: str | None = None


@dataclass(frozen=True)
class StagedClaim:
    """A verified claim and the turns that support it."""

    claim: str
    oid: str
    evidence: tuple[ClaimEvidence, ...] = ()


@runtime_checkable
class ClaimStager(Protocol):
    """Stages one batch of verified claims as candidates for human review.

    ``stage_claims`` writes only under ``<vault>/_candidates/`` and never
    touches the vault's memories. ``batch_id`` names the batch: staging the
    same id again writes nothing and returns ``False`` (an interrupted run
    that is retried is idempotent); a first staging returns ``True``.
    ``report`` is copied into the batch's report; ``created_at`` is the
    caller's clock (ISO 8601).
    """

    def stage_claims(
        self,
        vault: Path,
        batch_id: str,
        claims: Sequence[StagedClaim],
        report: Mapping[str, Any],
        *,
        created_at: str,
    ) -> bool: ...


__all__ = [
    "CLAIMS_PORT_VERSION",
    "POLARITIES",
    "ClaimEvidence",
    "ClaimStager",
    "ClaimVerifier",
    "StagedClaim",
]
