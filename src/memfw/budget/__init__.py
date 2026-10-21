"""Deterministic byte and record-count budget accounting."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


@dataclass(frozen=True, slots=True)
class BudgetLimits:
    """Limits for stored memory bodies.

    Byte usage is the sum of each body's UTF-8 encoded byte length. Frontmatter,
    event logs, indexes, and filesystem allocation are outside this content budget.
    A ``None`` limit is unbounded; zero is a valid strict limit.
    """

    max_bytes: int | None = None
    max_count: int | None = None

    def __post_init__(self) -> None:
        if self.max_bytes is not None and self.max_bytes < 0:
            raise ValueError("max_bytes must be non-negative or None")
        if self.max_count is not None and self.max_count < 0:
            raise ValueError("max_count must be non-negative or None")


class BudgetProfile(StrEnum):
    MB_1 = "1MB"
    MB_10 = "10MB"
    MB_100 = "100MB"


@dataclass(frozen=True, slots=True)
class BudgetUsage:
    count: int
    bytes: int


@dataclass(frozen=True, slots=True)
class BudgetEvaluation:
    limits: BudgetLimits
    usage: BudgetUsage
    within_budget: bool
    exceeded_bytes: int
    exceeded_count: int


BYTE_BUDGETS: dict[BudgetProfile, BudgetLimits] = {
    BudgetProfile.MB_1: BudgetLimits(max_bytes=1_000_000),
    BudgetProfile.MB_10: BudgetLimits(max_bytes=10_000_000),
    BudgetProfile.MB_100: BudgetLimits(max_bytes=100_000_000),
}


class SizedMemory(Protocol):
    @property
    def id(self) -> str: ...

    @property
    def body(self) -> str: ...


def body_bytes(memory: SizedMemory) -> int:
    """Return deterministic UTF-8 body bytes for one candidate."""

    return len(memory.body.encode("utf-8"))


def usage_for(memories: Iterable[SizedMemory]) -> BudgetUsage:
    items = tuple(memories)
    return BudgetUsage(count=len(items), bytes=sum(body_bytes(item) for item in items))


def evaluate_budget(memories: Iterable[SizedMemory], limits: BudgetLimits) -> BudgetEvaluation:
    usage = usage_for(memories)
    exceeded_bytes = max(0, usage.bytes - limits.max_bytes) if limits.max_bytes is not None else 0
    exceeded_count = max(0, usage.count - limits.max_count) if limits.max_count is not None else 0
    return BudgetEvaluation(
        limits=limits,
        usage=usage,
        within_budget=exceeded_bytes == 0 and exceeded_count == 0,
        exceeded_bytes=exceeded_bytes,
        exceeded_count=exceeded_count,
    )
