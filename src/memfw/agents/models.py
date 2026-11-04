"""Deterministic, metadata-only proposal schemas shared by agents and review."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

ProposalKind: TypeAlias = Literal["salience", "merge", "layer", "forget", "anchor"]
ProposalStatus: TypeAlias = Literal["pending", "approved", "rejected", "applied"]

MOCK_EPOCH = datetime(2000, 1, 1, tzinfo=UTC)


def canonical_json(value: object) -> str:
    """Serialize JSON values in the one stable representation used for hashes."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def canonical_payload_hash(payload: object) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def mock_signature(
    proposal_id: str,
    input_hash: str,
    payload_hash: str,
    timestamp: datetime,
    target_hashes: dict[str, str] | None = None,
) -> str:
    """Return the stable opaque mock digest; this is not identity authentication."""
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("signature timestamp must include a timezone")
    normalized = timestamp.astimezone(UTC)
    return "mock:" + canonical_payload_hash(
        [proposal_id, input_hash, payload_hash, normalized.isoformat(), target_hashes or {}]
    )


class Proposal(BaseModel):
    """A deterministic proposal. Signature is a mock digest, not authentication."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    proposal_id: str
    kind: ProposalKind
    target_ids: tuple[str, ...]
    # Canonical MemoryDocument JSON snapshots supplied by judge-created proposals.
    # Empty remains valid for compatibility with older/manual review proposals;
    # the repository maintains its own authoritative raw-Markdown bindings.
    target_hashes: dict[str, str] = Field(default_factory=dict)
    agent_name: str
    model_name: str
    input_hash: str
    output: dict[str, JsonValue]
    reason: str
    payload_hash: str
    actor: str
    signature: str
    status: ProposalStatus = "pending"
    created_at: datetime = MOCK_EPOCH
    updated_at: datetime = MOCK_EPOCH

    @field_validator("created_at", "updated_at")
    @classmethod
    def timestamps_are_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamps must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def payload_hash_matches(self) -> Proposal:
        if canonical_payload_hash(self.output) != self.payload_hash:
            raise ValueError("payload_hash does not match canonical output")
        if self.target_hashes and set(self.target_hashes) != set(self.target_ids):
            raise ValueError("target_hashes keys must match target_ids")
        if any(
            len(value) != 64 or any(char not in "0123456789abcdef" for char in value)
            for value in self.target_hashes.values()
        ):
            raise ValueError("target_hashes values must be lowercase SHA-256 hex digests")
        if not all(
            (
                self.proposal_id,
                self.agent_name,
                self.model_name,
                self.input_hash,
                self.actor,
                self.signature,
            )
        ):
            raise ValueError("proposal identity and hashes are required")
        return self

    @property
    def payload(self) -> dict[str, JsonValue]:
        """Compatibility alias for review clients that call output a payload."""
        return self.output


class MergeProposal(BaseModel):
    """A bounded, mechanical near-duplicate suggestion; it never edits memory."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    representative_id: str
    duplicate_ids: tuple[str, ...]
    match_basis: str
    disposition: Literal["delete", "retain"] = "retain"
    confidence: float = Field(ge=0.0, le=1.0)
    proposal: Proposal

    @model_validator(mode="after")
    def links_match(self) -> MergeProposal:
        if (
            not self.duplicate_ids
            or len(set(self.duplicate_ids)) != len(self.duplicate_ids)
            or self.representative_id in self.duplicate_ids
        ):
            raise ValueError("merge proposal needs distinct representative and duplicate ids")
        if self.proposal.kind != "merge":
            raise ValueError("nested proposal kind must be merge")
        if self.proposal.target_ids != (self.representative_id, *self.duplicate_ids):
            raise ValueError("proposal target_ids must match merge links")
        return self

    def to_proposal(self) -> Proposal:
        return self.proposal


def canonical_proposal_payload(proposal: Proposal) -> dict[str, JsonValue]:
    """Return the stable JSON-safe representation used for proposal hashing."""
    return proposal.model_dump(mode="json")


__all__ = [
    "JsonValue",
    "MergeProposal",
    "Proposal",
    "ProposalKind",
    "ProposalStatus",
    "canonical_json",
    "canonical_payload_hash",
    "canonical_proposal_payload",
    "mock_signature",
]
