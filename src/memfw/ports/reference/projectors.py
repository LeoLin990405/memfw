"""Two reference ``Projector`` implementations. Stdlib only.

- ``JsonlProjector`` (``jsonl-index``): one JSON line per memory with its id,
  metadata and a ``body_sha256`` — no body text. An index an external tool
  can join against, or diff between two exports to see what changed.
- ``MirrorProjector`` (``markdown-mirror``): the memory files copied
  byte-for-byte to ``<out>/memories/``, for a plain-file backend (a grep, a
  static site, another indexer) that reads Markdown.

Both read the vault only, skip memories tagged private unless
``policy.options["include_private"]`` is true, count unreadable files as
skipped, refuse an output inside the vault, and are reproducible: the same
vault and policy give the same ``content_sha256``. ``generated_at`` comes from
an injected ``Clock`` (default ``SystemClock``).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ..clock import Clock, SystemClock
from ..types import Availability, Manifest, ProjectionPolicy, check_name
from ._vault import MemoryFile, inside, is_vault, iter_memories

INDEX_FILE = "memories.jsonl"
MIRROR_DIR = "memories"

#: Front matter keys ``JsonlProjector`` copies (when present), in this order.
INDEX_KEYS: tuple[str, ...] = (
    "title",
    "tenant",
    "source",
    "layer",
    "provenance",
    "created_at",
    "updated_at",
    "valid_from",
    "valid_to",
    "superseded_by",
    "version",
)


class _FileProjector:
    name: str

    def __init__(self, *, name: str, clock: Clock | None = None) -> None:
        self.name = check_name(name, what="projector")
        self.clock: Clock = clock or SystemClock()

    def available(self) -> Availability:
        return Availability.ok()

    def _select(
        self, vault: Path, out: Path, policy: ProjectionPolicy
    ) -> tuple[list[MemoryFile], dict[str, int]]:
        root = Path(vault).expanduser()
        if not is_vault(root):
            raise ValueError("not a vault: vault.yaml is missing")
        if inside(out, root):
            raise ValueError("the output must not be inside the vault")
        include_private = policy.options.get("include_private") is True
        selected: list[MemoryFile] = []
        skipped = {"private": 0, "unreadable": 0}
        for _memory_id, memory in iter_memories(root):
            if memory is None:
                skipped["unreadable"] += 1
            elif memory.is_private() and not include_private:
                skipped["private"] += 1
            else:
                selected.append(memory)
        return selected, skipped

    def _manifest(
        self,
        policy: ProjectionPolicy,
        projected: int,
        skipped: dict[str, int],
        written: list[tuple[str, bytes]],
    ) -> Manifest:
        digest = hashlib.sha256()
        for relative, data in written:
            digest.update(relative.encode("utf-8") + b"\x00")
            digest.update(hashlib.sha256(data).digest())
        return Manifest(
            projector=self.name,
            policy_version=policy.version,
            projected=projected,
            skipped={key: value for key, value in skipped.items() if value},
            content_sha256=digest.hexdigest(),
            generated_at=self.clock.now(),
            files=tuple(relative for relative, _data in written),
        )


def _write(out: Path, relative: str, data: bytes) -> None:
    target = out / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink():
        raise ValueError(f"refusing to write through a symlink: {relative}")
    target.write_bytes(data)


class JsonlProjector(_FileProjector):
    """``<out>/memories.jsonl``: id, metadata, tags and ``body_sha256``; never the body."""

    def __init__(self, *, name: str = "jsonl-index", clock: Clock | None = None) -> None:
        super().__init__(name=name, clock=clock)

    def export(self, vault: Path, out: Path, policy: ProjectionPolicy) -> Manifest:
        out = Path(out).expanduser()
        selected, skipped = self._select(vault, out, policy)
        lines = []
        for memory in selected:
            record: dict[str, object] = {"id": memory.memory_id}
            for key in INDEX_KEYS:
                if key in memory.front and isinstance(memory.front[key], str):
                    record[key] = memory.front[key]
            record["tags"] = memory.tags()
            record["body_sha256"] = hashlib.sha256(memory.body.encode("utf-8")).hexdigest()
            lines.append(json.dumps(record, ensure_ascii=False, sort_keys=True))
        data = ("\n".join(lines) + "\n").encode("utf-8") if lines else b""
        out.mkdir(parents=True, exist_ok=True)
        _write(out, INDEX_FILE, data)
        return self._manifest(policy, len(selected), skipped, [(INDEX_FILE, data)])


class MirrorProjector(_FileProjector):
    """``<out>/memories/<id>.md``: each selected memory file, byte-for-byte."""

    def __init__(self, *, name: str = "markdown-mirror", clock: Clock | None = None) -> None:
        super().__init__(name=name, clock=clock)

    def export(self, vault: Path, out: Path, policy: ProjectionPolicy) -> Manifest:
        out = Path(out).expanduser()
        selected, skipped = self._select(vault, out, policy)
        written: list[tuple[str, bytes]] = []
        out.mkdir(parents=True, exist_ok=True)
        for memory in selected:
            relative = f"{MIRROR_DIR}/{memory.memory_id}.md"
            _write(out, relative, memory.raw)
            written.append((relative, memory.raw))
        return self._manifest(policy, len(selected), skipped, written)


__all__ = ["INDEX_FILE", "INDEX_KEYS", "MIRROR_DIR", "JsonlProjector", "MirrorProjector"]
