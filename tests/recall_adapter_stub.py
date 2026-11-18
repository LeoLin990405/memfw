"""A recall adapter for the ``runtime.recall.adapters`` tests: stdlib only, no extra install.

``factory`` builds the reference ``InMemoryRecallProvider`` over the vault the
runtime passes in (unknown options are refused, as real adapters do);
``down_factory`` builds one that is never available. Not collected by pytest.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from memfw.ports import Availability, ProviderUnavailable
from memfw.ports.reference import InMemoryRecallProvider

NAME = "stub"
#: Every settings mapping a factory here was called with (keys only).
CALLS: list[frozenset[str]] = []


def factory(settings: Mapping[str, Any]) -> InMemoryRecallProvider:
    CALLS.append(frozenset(settings))
    unknown = set(settings) - {"vault", "embedder", "name"}
    if unknown:
        raise ValueError("unknown stub settings")
    return InMemoryRecallProvider.from_vault(settings["vault"], name=settings.get("name", NAME))


class Down:
    name = "down"
    lexical = False
    projection_version = ""

    def available(self) -> Availability:
        return Availability.missing("unreachable")

    def search(self, query: str, **kwargs: Any) -> list[Any]:
        raise ProviderUnavailable("unreachable")


def down_factory(settings: Mapping[str, Any]) -> Down:
    CALLS.append(frozenset(settings))
    return Down()


class FixedRefs:
    """Returns the ``refs`` it was built with as ``path`` candidates (``kh:`` notes)."""

    lexical = False
    projection_version = ""

    def __init__(self, name: str, refs: list[str]) -> None:
        self.name = name
        self.refs = refs

    def available(self) -> Availability:
        return Availability.ok()

    def search(self, query: str, *, limit: int, **kwargs: Any) -> list[Any]:
        from memfw.ports import Candidate

        return [
            Candidate(ref=ref, ref_kind="path", score=1.0 / rank, provider=self.name, rank=rank)
            for rank, ref in enumerate(self.refs[:limit], start=1)
        ]


def refs_factory(settings: Mapping[str, Any]) -> FixedRefs:
    CALLS.append(frozenset(settings))
    refs = settings.get("refs")
    if not isinstance(refs, list) or not all(isinstance(ref, str) for ref in refs):
        raise ValueError("refs must be a list of strings")
    return FixedRefs(str(settings.get("name", "refs")), refs)
