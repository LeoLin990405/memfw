"""Two reference ``Deriver`` implementations. Stdlib only.

- ``ConcatDeriver`` (``concat``): a citations-only listing — one line per
  requested memory, ``- <title> [<id>]``, in request order.
- ``TimelineDeriver`` (``timeline``): the same memories ordered by
  ``created_at`` (then id), one line per memory, ``<date> <title> [<id>]``.

Neither reads or repeats a body, so the view says nothing the titles do not.
Both cite only requested memories that exist in the vault (with their
version), skip the rest, and raise ``ValueError`` when none exists (a view
must cite something). The caps are the weakest useful ones: ``inference`` and
no causal claim.
"""

from __future__ import annotations

from pathlib import Path

from ..types import Availability, Citation, DerivedView, DeriveRequest, check_name
from ._vault import MemoryFile, is_vault, read_memory

#: Longest title carried into a view line.
MAX_TITLE_CHARS = 200


def _title(memory: MemoryFile) -> str:
    title = " ".join(memory.text("title").split()) or memory.memory_id
    return title if len(title) <= MAX_TITLE_CHARS else title[: MAX_TITLE_CHARS - 1] + "…"


class _ListingDeriver:
    name: str

    def __init__(self, *, name: str) -> None:
        self.name = check_name(name, what="deriver")

    def available(self) -> Availability:
        return Availability.ok()

    def _memories(self, vault: Path, request: DeriveRequest) -> list[MemoryFile]:
        root = Path(vault).expanduser()
        if not is_vault(root):
            raise ValueError("not a vault: vault.yaml is missing")
        found: list[MemoryFile] = []
        seen: set[str] = set()
        for memory_id in request.memory_ids:
            if memory_id in seen:
                continue
            seen.add(memory_id)
            memory = read_memory(root, memory_id)
            if memory is not None:
                found.append(memory)
        if not found:
            raise ValueError("none of the requested memories exists")
        return found

    def _view(
        self, request: DeriveRequest, lines: list[str], cited: list[MemoryFile]
    ) -> DerivedView:
        return DerivedView(
            deriver=self.name,
            kind=request.kind,
            content="\n".join(lines),
            citations=tuple(Citation(memory.memory_id, memory.version()) for memory in cited),
            epistemic_cap="inference",
            causal_cap="none",
        )


class ConcatDeriver(_ListingDeriver):
    def __init__(self, *, name: str = "concat") -> None:
        super().__init__(name=name)

    def derive(self, vault: Path, request: DeriveRequest) -> DerivedView:
        memories = self._memories(vault, request)
        lines = [f"- {_title(memory)} [{memory.memory_id}]" for memory in memories]
        return self._view(request, lines, memories)


class TimelineDeriver(_ListingDeriver):
    def __init__(self, *, name: str = "timeline") -> None:
        super().__init__(name=name)

    def derive(self, vault: Path, request: DeriveRequest) -> DerivedView:
        memories = sorted(
            self._memories(vault, request),
            key=lambda memory: (memory.text("created_at"), memory.memory_id),
        )
        lines = [
            f"{memory.text('created_at')[:10] or '????-??-??'} {_title(memory)} "
            f"[{memory.memory_id}]"
            for memory in memories
        ]
        return self._view(request, lines, memories)


__all__ = ["MAX_TITLE_CHARS", "ConcatDeriver", "TimelineDeriver"]
