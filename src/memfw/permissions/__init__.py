"""Local permission workflow policy. This is metadata enforcement, not a sandbox."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from ..core.models import Layer


class PermissionMode(StrEnum):
    NONE = "none"
    PROPOSE_THEN_CONFIRM = "propose_then_confirm"
    FULL = "full"


@dataclass(frozen=True, slots=True)
class PermissionPolicy:
    mode: PermissionMode = PermissionMode.PROPOSE_THEN_CONFIRM

    def can_propose(self, actor: str) -> bool:
        return actor.startswith("agent:")

    def can_mutate(
        self,
        actor: str,
        *,
        signature: str | None = None,
        agent_id: str | None = None,
        approved: bool = False,
        layer: Layer | None = None,
        anchor: str | None = None,
    ) -> bool:
        if actor == "human":
            return True
        if actor == "mechanism":
            return layer != Layer.ANCHOR and anchor is None
        if actor == "simulated_approver":
            return approved and layer != Layer.ANCHOR and anchor is None
        if not actor.startswith("agent:") or not signature or not agent_id:
            return False
        if actor != f"agent:{agent_id}":
            return False
        is_anchor = layer == Layer.ANCHOR or anchor is not None
        if is_anchor and not approved:
            return False
        if self.mode == PermissionMode.FULL:
            return True
        return self.mode == PermissionMode.PROPOSE_THEN_CONFIRM and approved

    def may_set_anchor(self, actor: str) -> bool:
        return actor == "human"


__all__ = ["PermissionMode", "PermissionPolicy"]
