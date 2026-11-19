"""The ``tailnet`` Ingestor: host-fact candidates under ``_candidates/tailnet/``.

Staging goes through the reference ``stage_candidates``, so the ingest
contract holds: a proposal id and a source are required, nothing outside
``<vault>/_candidates/`` is written, and staging the same item twice writes
nothing new. Candidates are reviewed like every other staged candidate;
nothing here writes authoritative memory.

Privacy: every candidate carries a ``privacy`` level, ``local-private`` unless
the operator configures another. A machine inventory (names, tailnet IPs,
operating systems, when each machine was last online) maps the operator's
network and habits, so by default it stays on the machine that holds it. An
item that already carries a level keeps it, except that the configured level
is the loosest the ingestor will stamp: an item may be stricter, never looser.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ._ports import (
    PRIVACY_LEVELS,
    Availability,
    ExtraValue,
    IngestItem,
    StageResult,
    StagingError,
    privacy_value,
    stage_candidates,
)

NAME = "tailnet"
DEFAULT_PRIVACY = "local-private"
#: The ``source`` a scan stages under.
SOURCE = "tailscale-status"
_RANK = {level: rank for rank, level in enumerate(PRIVACY_LEVELS)}


@dataclass(frozen=True, slots=True)
class Settings:
    """Adapter settings (``adapters.tailnet``): ``privacy`` and ``include_shared``.

    ``include_shared`` (default ``False``) keeps peers shared in from other
    tailnets (``ShareeNode``); they may be the owner's own machines elsewhere or
    a third party's, so the owner opts in.
    """

    privacy: str = DEFAULT_PRIVACY
    include_shared: bool = False

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> Settings:
        data = dict(raw or {})
        unknown = set(data) - {"privacy", "include_shared"}
        if unknown:
            raise ValueError("unknown tailnet setting(s): " + ", ".join(sorted(unknown)))
        level = privacy_value(data.get("privacy", DEFAULT_PRIVACY))
        include_shared = data.get("include_shared", False)
        if not isinstance(include_shared, bool):
            raise ValueError("include_shared must be true or false")
        return cls(privacy=level or DEFAULT_PRIVACY, include_shared=include_shared)


def _stricter(a: str, b: str) -> str:
    return a if _RANK.get(a, len(_RANK)) >= _RANK.get(b, len(_RANK)) else b


class TailnetIngestor:
    """Stages items with a ``privacy`` level no looser than the configured one."""

    name = NAME

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or Settings()

    def available(self) -> Availability:
        return Availability.ok()

    def _with_privacy(self, item: IngestItem) -> IngestItem:
        raw = item.metadata.get("privacy")
        try:
            level = privacy_value(raw) if isinstance(raw, str) else None
        except ValueError:
            level = None
        if raw is not None and level is None:
            level = DEFAULT_PRIVACY  # an unknown level fails closed
        level = _stricter(level, self.settings.privacy) if level else self.settings.privacy
        if level == raw:
            return item
        metadata: dict[str, ExtraValue] = {**item.metadata, "privacy": level}
        return IngestItem(item.title, item.body, item.external_ref, metadata)

    def stage(
        self,
        vault: Path,
        items: Sequence[IngestItem],
        *,
        proposal_id: str,
        source: str,
    ) -> StageResult:
        prepared = []
        for item in items:
            if not isinstance(item, IngestItem):
                raise StagingError("items must be IngestItem values")
            prepared.append(self._with_privacy(item))
        return stage_candidates(vault, NAME, prepared, proposal_id=proposal_id, source=source)


def factory(settings: Mapping[str, Any] | None = None) -> TailnetIngestor:
    """Registry factory: ``adapters: {enabled: [tailnet], tailnet: {privacy: ...}}``."""

    return TailnetIngestor(Settings.from_mapping(settings))


__all__ = ["DEFAULT_PRIVACY", "NAME", "SOURCE", "Settings", "TailnetIngestor", "factory"]
