"""Curation interfaces. TODO: ranking and human confirmation arrive on day two."""

from typing import Protocol

from ..core.models import Layer, Tier


class Curator(Protocol):
    def propose(self, content: str, *, sources: tuple[str, ...]) -> tuple[Layer, Tier]: ...
