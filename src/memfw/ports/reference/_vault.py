"""Read a vault's memory files without memfw internals. Stdlib only.

The reference adapters read ``<vault>/memories/<id>.md`` directly: a YAML
front matter block between ``---`` lines, then the body. Only what they need
is parsed: top-level scalar keys (``key: value``, quotes stripped) and
top-level lists of scalars (``key:`` followed by ``- item`` lines). Nested
mappings are ignored. A file that cannot be read or parsed is reported as
``None`` so the caller counts it as skipped, never guesses.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from ..types import MEMORY_ID_RE

MEMORIES_DIR = "memories"

_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):(?:\s+(.*))?$")
_PRIVATE_TAGS = frozenset({"sensitivity:private", "privacy:local-private", "privacy:private"})


@dataclass(frozen=True, slots=True)
class MemoryFile:
    """One memory file: its id, raw bytes, parsed front matter and body."""

    memory_id: str
    raw: bytes
    front: dict[str, str | list[str]] = field(default_factory=dict)
    body: str = ""

    def text(self, key: str, default: str = "") -> str:
        value = self.front.get(key, default)
        return value if isinstance(value, str) else default

    def tags(self) -> list[str]:
        value = self.front.get("tags", [])
        return list(value) if isinstance(value, list) else []

    def version(self) -> int | None:
        value = self.text("version")
        return int(value) if value.isdigit() else None

    def is_private(self) -> bool:
        """Tagged private (any case). The coordinator's policy is stricter; this is a floor."""

        return any(tag.strip().lower() in _PRIVATE_TAGS for tag in self.tags())


def _scalar(raw: str) -> str:
    value = raw.strip()
    if value in {"null", "~"}:
        return ""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        inner = value[1:-1]
        return inner.replace("''", "'") if value[0] == "'" else inner
    return value


def parse_memory(memory_id: str, raw: bytes) -> MemoryFile | None:
    """Parse one memory file; ``None`` when it has no well-formed front matter."""

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return None
    try:
        end = next(index for index in range(1, len(lines)) if lines[index].strip() == "---")
    except StopIteration:
        return None
    front: dict[str, str | list[str]] = {}
    current: str | None = None
    for line in lines[1:end]:
        if line.startswith("- ") and current is not None:
            items = front.setdefault(current, [])
            if isinstance(items, list):
                items.append(_scalar(line[2:]))
            continue
        if line.startswith((" ", "\t")):
            continue  # nested value: not needed here
        match = _KEY_RE.match(line)
        if not match:
            current = None
            continue
        key, value = match.group(1), match.group(2)
        if value is None or not value.strip():
            current = key
            front[key] = []
        else:
            current = None
            front[key] = [] if value.strip() == "[]" else _scalar(value)
    body = "\n".join(lines[end + 1 :]).strip("\n")
    return MemoryFile(memory_id, raw, front, body)


def memory_path(vault: Path, memory_id: str) -> Path | None:
    """``<vault>/memories/<id>.md`` for a valid id; ``None`` for anything else."""

    if not isinstance(memory_id, str) or not MEMORY_ID_RE.fullmatch(memory_id):
        return None
    return Path(vault) / MEMORIES_DIR / f"{memory_id}.md"


def read_memory(vault: Path, memory_id: str) -> MemoryFile | None:
    """One memory by id. Symlinks, missing and unparsable files are ``None``."""

    path = memory_path(vault, memory_id)
    if path is None or path.is_symlink() or not path.is_file():
        return None
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    return parse_memory(memory_id, raw)


def memory_ids(vault: Path) -> list[str]:
    """Ids of the regular ``*.md`` files in ``memories/``, sorted."""

    directory = Path(vault) / MEMORIES_DIR
    if directory.is_symlink() or not directory.is_dir():
        return []
    return sorted(
        path.stem
        for path in directory.glob("*.md")
        if not path.is_symlink() and path.is_file() and MEMORY_ID_RE.fullmatch(path.stem)
    )


def iter_memories(vault: Path) -> Iterator[tuple[str, MemoryFile | None]]:
    """``(id, file or None)`` for every memory file, in id order."""

    for memory_id in memory_ids(vault):
        yield memory_id, read_memory(vault, memory_id)


def is_vault(vault: Path) -> bool:
    return (Path(vault) / "vault.yaml").is_file()


def inside(path: Path, root: Path) -> bool:
    """Whether ``path`` is ``root`` or below it (after resolving both)."""

    resolved, base = Path(path).resolve(), Path(root).resolve()
    return resolved == base or base in resolved.parents
