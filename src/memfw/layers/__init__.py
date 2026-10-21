"""Memory layer classification and permission-checked transitions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from memfw.core.errors import IntegrityError
from memfw.core.models import Layer


class LayerPolicy(Protocol):
    def classify(self, content: str, *, sources: tuple[str, ...]) -> Layer: ...


@dataclass(frozen=True, slots=True)
class LayerProposal:
    memory_id: str
    before: Layer
    proposed: Layer
    reason: str
    requires_human_approval: bool


def propose_layer(repository: Any, memory_id: str, layer: Layer, *, reason: str) -> LayerProposal:
    """Build a reviewable transition proposal without changing vault files."""

    current = repository.get(memory_id)
    if not reason.strip():
        raise ValueError("reason must not be empty")
    selected = Layer(layer)
    return LayerProposal(memory_id, current.layer, selected, reason, selected == Layer.ANCHOR)


def set_layer(
    repository: Any,
    memory_id: str,
    layer: Layer,
    *,
    actor: str = "human",
    agent_id: str | None = None,
    signature: str | None = None,
    proposal_id: str | None = None,
    approved_by: str | None = None,
) -> Any:
    """Apply a layer transition, keeping anchor changes human-controlled."""

    selected = Layer(layer)
    if (
        selected == Layer.ANCHOR
        and actor != "human"
        and not (proposal_id and approved_by == "human" and agent_id and signature)
    ):
        # The repository permission check is authoritative. This early check makes
        # the human approval boundary explicit in the layer API as well.
        raise IntegrityError("Setting an anchor requires a signed, human-approved proposal")
    return repository.update(
        memory_id,
        actor=actor,
        agent_id=agent_id,
        signature=signature,
        proposal_id=proposal_id,
        approved_by=approved_by,
        layer=selected,
    )


__all__ = ["LayerPolicy", "LayerProposal", "propose_layer", "set_layer"]
