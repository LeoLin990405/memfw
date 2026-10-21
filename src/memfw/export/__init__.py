"""Export interfaces. TODO: add audience- and permission-aware export."""

from typing import Protocol


class Exporter(Protocol):
    def export(self, memory_ids: tuple[str, ...], *, format: str) -> bytes: ...
