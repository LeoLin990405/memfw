"""The four port protocols.

An adapter implements one of these. The kernel only ever talks to an adapter
through its protocol, so an external project stays native: memfw never
imports its internals, and the adapter module holds the only coupling.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

from .types import (
    Availability,
    Candidate,
    DerivedView,
    DeriveRequest,
    IngestItem,
    Manifest,
    ProjectionPolicy,
    RecallBudget,
    StageResult,
)


@runtime_checkable
class RecallProvider(Protocol):
    """Query → candidates. Never returns body text.

    ``search`` returns at most ``limit`` candidates, each with
    ``provider == self.name`` and ranks 1..n in order. It must honour the
    tenant (exact match, the empty tenant matches only the empty tenant) when
    it can; the coordinator filters again either way. It may raise
    ``ProviderUnavailable`` (degraded, reason ``unavailable``) or any other
    exception (degraded, reason ``error``).
    """

    name: str

    def available(self) -> Availability: ...

    def search(
        self,
        query: str,
        *,
        limit: int,
        budget: RecallBudget,
        tenant: str,
        as_of: datetime | None,
    ) -> Sequence[Candidate]: ...


@runtime_checkable
class Projector(Protocol):
    """Vault → external format. Read-only on the vault; output is rebuildable."""

    name: str

    def available(self) -> Availability: ...

    def export(self, vault: Path, out: Path, policy: ProjectionPolicy) -> Manifest: ...


@runtime_checkable
class Ingestor(Protocol):
    """External records → staged candidates.

    Writes only under ``<vault>/_candidates/``, never authoritative memory.
    Requires a ``proposal_id`` and a ``source``. Staging the same item twice
    yields the same candidate id and writes nothing new (idempotent).
    ``memfw.ports.staging.stage_candidates`` is the reference implementation.
    """

    name: str

    def available(self) -> Availability: ...

    def stage(
        self,
        vault: Path,
        items: Sequence[IngestItem],
        *,
        proposal_id: str,
        source: str,
    ) -> StageResult: ...


@runtime_checkable
class Deriver(Protocol):
    """Vault (plus external data) → a read-only view with citations.

    The view carries an epistemic cap and never writes back; anything worth
    keeping is offered to an ``Ingestor`` as a candidate.
    """

    name: str

    def available(self) -> Availability: ...

    def derive(self, vault: Path, request: DeriveRequest) -> DerivedView: ...


__all__ = ["Deriver", "Ingestor", "Projector", "RecallProvider"]
