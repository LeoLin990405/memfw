"""Protocol definitions for deterministic framework policy boundaries."""

from dataclasses import dataclass
from typing import Protocol

from ..permissions import PermissionMode, PermissionPolicy
from .models import Layer, Tier


@dataclass(frozen=True)
class CapturePolicy:
    enabled: bool = False
    source_allowlist: tuple[str, ...] = ()


class CurationPolicy(Protocol):
    def choose_layer(self, text: str) -> Layer: ...

    def choose_tier(self, text: str) -> Tier: ...


class RetrievalPolicy(Protocol):
    def allow(self, *, actor: str, memory_id: str) -> bool: ...


class ExportPolicy(Protocol):
    def allow_export(self, *, actor: str, memory_id: str) -> bool: ...


@dataclass(frozen=True)
class BudgetPolicy:
    max_tokens: int | None = None


__all__ = [
    "BudgetPolicy",
    "CapturePolicy",
    "CurationPolicy",
    "ExportPolicy",
    "PermissionMode",
    "PermissionPolicy",
    "RetrievalPolicy",
]
