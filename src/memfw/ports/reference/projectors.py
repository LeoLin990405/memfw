"""Two reference ``Projector`` implementations. Stdlib only.

- ``JsonlProjector`` (``jsonl-index``): one JSON line per memory with its id,
  metadata and a ``body_sha256`` — no body text. An index an external tool
  can join against, or diff between two exports to see what changed.
- ``MirrorProjector`` (``markdown-mirror``): the memory files copied
  byte-for-byte to ``<out>/memories/``, for a plain-file backend (a grep, a
  static site, another indexer) that reads Markdown.

Both read the vault only and apply core's privacy classes (see
``_vault.privacy_class``, never looser than core): a ``local-private`` memory
is exported only when ``policy.options["include_private"]`` is true, and an
``unknown`` one (``sensitivity:secret``, ``privacy:restricted``, ...) never.
Unreadable files (including front matter the reader cannot parse safely)
are skipped and counted. Both refuse an output inside the vault or reached
through a symlink, write each file through a temporary name (no symlink is
followed), and are reproducible: the same vault and policy give the same
``content_sha256``. ``generated_at`` comes from
an injected ``Clock`` (default ``SystemClock``).
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
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
        skipped = {"private": 0, "privacy_unknown": 0, "unreadable": 0}
        for _memory_id, memory in iter_memories(root):
            if memory is None:
                skipped["unreadable"] += 1
                continue
            privacy = memory.privacy_class()
            if privacy == "unknown":
                skipped["privacy_unknown"] += 1
            elif privacy == "local-private" and not include_private:
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


def _directory(path: Path) -> Path:
    """Create ``path`` if needed; refuse a symlink or a non-directory."""

    if path.is_symlink():
        raise ValueError(f"refusing to write through a symlink: {path.name}")
    path.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise ValueError(f"not a directory: {path.name}")
    return path


def _write(directory: Path, filename: str, data: bytes) -> None:
    """Write ``directory/filename`` via a temporary file and ``os.replace``.

    The temporary file is opened with ``O_EXCL | O_NOFOLLOW``; ``os.replace``
    swaps the name itself, so an existing symlink at the target is replaced,
    never followed.
    """

    temporary = directory / f".{filename}.{secrets.token_hex(8)}.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, directory / filename)
    finally:
        temporary.unlink(missing_ok=True)


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
        _write(_directory(out), INDEX_FILE, data)
        return self._manifest(policy, len(selected), skipped, [(INDEX_FILE, data)])


class MirrorProjector(_FileProjector):
    """``<out>/memories/<id>.md``: each selected memory file, byte-for-byte.

    ``<out>/memories/`` is made to match the selection: a ``.md`` file left
    from an earlier export (a memory since deleted, or since made private) is
    removed.
    """

    def __init__(self, *, name: str = "markdown-mirror", clock: Clock | None = None) -> None:
        super().__init__(name=name, clock=clock)

    def export(self, vault: Path, out: Path, policy: ProjectionPolicy) -> Manifest:
        out = Path(out).expanduser()
        selected, skipped = self._select(vault, out, policy)
        written: list[tuple[str, bytes]] = []
        directory = _directory(_directory(out) / MIRROR_DIR)
        keep = {f"{memory.memory_id}.md" for memory in selected}
        for stale in sorted(directory.glob("*.md")):
            if stale.name not in keep and (stale.is_symlink() or stale.is_file()):
                stale.unlink()
        for memory in selected:
            filename = f"{memory.memory_id}.md"
            _write(directory, filename, memory.raw)
            written.append((f"{MIRROR_DIR}/{filename}", memory.raw))
        return self._manifest(policy, len(selected), skipped, written)


__all__ = ["INDEX_FILE", "INDEX_KEYS", "MIRROR_DIR", "JsonlProjector", "MirrorProjector"]
