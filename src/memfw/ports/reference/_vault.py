"""Read a vault's memory files without memfw internals. Stdlib only.

The reference adapters read ``<vault>/memories/<id>.md`` directly: a YAML
front matter block between ``---`` lines, then the body. Only what they need
is parsed: top-level scalar keys (``key: value``, quotes and inline
``# comments`` stripped) and top-level block lists of scalars (``key:``
followed by ``- item`` lines at column 0, the layout the vault writes).

The parser fails closed. A file whose front matter cannot be read, or whose
privacy-relevant keys (``tags``, ``privacy``, ``sensitivity``) use any other
YAML form (a flow list, an indented list, a block scalar, a duplicate key),
is ``None``: the caller counts it as unreadable and skips it, never guesses.

``privacy_class`` mirrors core's rule (``memfw.projection.tag_privacy_class``
and ``memfw.recall.policy.classify_privacy`` as ``meta_from_document``
combines them) and may only be stricter: ``tests/test_swap_drills.py``
cross-checks it against core for a corpus of tag variants.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from ..types import MEMORY_ID_RE

MEMORIES_DIR = "memories"

PrivacyClass = Literal["none", "local-private", "unknown"]

_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):(?:[ \t]+(.*))?$")
_COMMENT_RE = re.compile(r"[ \t]#")
#: Front matter keys that decide visibility; any form the parser does not
#: understand for them makes the whole file unreadable.
PRIVACY_KEYS: frozenset[str] = frozenset({"tags", "privacy", "sensitivity"})
#: ``privacy:`` values core treats as public (``memfw.recall.policy``).
PRIVACY_NONE_VALUES: frozenset[str] = frozenset({"", "none", "null", "public", "fleet", "shared"})
_RANK: dict[str, int] = {"none": 0, "local-private": 1, "unknown": 2}


def _declared_class(value: str) -> PrivacyClass:
    cleaned = value.strip().lower()
    if cleaned in PRIVACY_NONE_VALUES:
        return "none"
    if cleaned == "local-private":
        return "local-private"
    return "unknown"


def _sensitivity_class(value: str) -> PrivacyClass:
    return "local-private" if value.strip().lower() == "private" else "unknown"


def strictest(classes: Iterable[PrivacyClass]) -> PrivacyClass:
    """The strictest class; ``none`` for no classes."""

    found: PrivacyClass = "none"
    for value in classes:
        if _RANK[value] > _RANK[found]:
            found = value
    return found


def tags_privacy_class(tags: Iterable[str]) -> PrivacyClass:
    """Class from ``privacy:*`` and ``sensitivity:*`` tags, any case, strictest wins.

    ``sensitivity:private`` is ``local-private``, any other ``sensitivity:``
    value ``unknown``; a ``privacy:`` value is ``none`` for the public values,
    ``local-private`` for ``local-private`` and ``unknown`` otherwise.
    """

    classes: list[PrivacyClass] = []
    for tag in tags:
        lowered = tag.lower()
        if lowered.startswith("privacy:"):
            classes.append(_declared_class(tag.split(":", 1)[1]))
        elif lowered.startswith("sensitivity:"):
            classes.append(_sensitivity_class(tag.split(":", 1)[1]))
    return strictest(classes)


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

    def privacy_class(self) -> PrivacyClass:
        """Core's class for this memory, or a stricter one (never looser).

        Tags as core reads them, plus top-level ``privacy`` / ``sensitivity``
        keys, which core does not read: counting them can only make a memory
        stricter.
        """

        classes = [tags_privacy_class(self.tags())]
        for key, classify in (("privacy", _declared_class), ("sensitivity", _sensitivity_class)):
            value = self.front.get(key)
            if isinstance(value, str):
                classes.append(classify(value) if value else "none")
            elif isinstance(value, list):
                classes.extend(classify(item) for item in value)
        return strictest(classes)


class _Unparsable(ValueError):
    pass


def _scalar(raw: str) -> str:
    """A plain or quoted scalar with its inline comment removed."""

    value = raw.strip()
    if value[:1] in {"'", '"'}:
        quote = value[0]
        index = 1
        out: list[str] = []
        while index < len(value):
            char = value[index]
            if quote == "'" and char == "'" and value[index + 1 : index + 2] == "'":
                out.append("'")
                index += 2
                continue
            if quote == '"' and char == "\\":
                decoded, index = _escape(value, index)
                out.append(decoded)
                continue
            if char == quote:
                rest = value[index + 1 :].strip()
                if rest and not rest.startswith("#"):
                    raise _Unparsable("text after a quoted scalar")
                return "".join(out)
            out.append(char)
            index += 1
        raise _Unparsable("unterminated quoted scalar")
    if value.startswith("#"):
        return ""
    match = _COMMENT_RE.search(value)
    if match:
        value = value[: match.start()].rstrip()
    return "" if value in {"null", "~"} else value


_ESCAPES = {
    "0": "\0", "a": "\a", "b": "\b", "t": "\t", "\t": "\t", "n": "\n", "v": "\v",
    "f": "\f", "r": "\r", "e": "\x1b", " ": " ", '"': '"', "/": "/", "\\": "\\",
    "N": "\x85", "_": "\xa0", "L": "\u2028", "P": "\u2029",
}  # fmt: skip
_HEX_ESCAPES = {"x": 2, "u": 4, "U": 8}


def _escape(value: str, index: int) -> tuple[str, int]:
    """Decode the YAML double-quoted escape at ``value[index]`` (a backslash)."""

    code = value[index + 1 : index + 2]
    if code in _ESCAPES:
        return _ESCAPES[code], index + 2
    width = _HEX_ESCAPES.get(code)
    digits = value[index + 2 : index + 2 + width] if width else ""
    if not width or len(digits) != width or not all(c in "0123456789abcdefABCDEF" for c in digits):
        raise _Unparsable("unknown escape in a double-quoted scalar")
    try:
        return chr(int(digits, 16)), index + 2 + width
    except ValueError as exc:
        raise _Unparsable("escape out of range") from exc


def _front(lines: list[str]) -> dict[str, str | list[str]]:
    front: dict[str, str | list[str]] = {}
    current: str | None = None
    for line in lines:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.startswith("- ") or line.rstrip() == "-":
            if current is None:
                raise _Unparsable("list item outside a list")
            items = front[current]
            assert isinstance(items, list)
            items.append(_scalar(line[1:]))
            continue
        if line.startswith((" ", "\t")):
            # A nested value (an indented list, a mapping, a continuation).
            if current in PRIVACY_KEYS or _last_key(front) in PRIVACY_KEYS:
                raise _Unparsable("nested form under a privacy key")
            continue
        match = _KEY_RE.match(line)
        if not match:
            raise _Unparsable("not a key line")
        key, raw = match.group(1), (match.group(2) or "")
        if key in front and key in PRIVACY_KEYS:
            raise _Unparsable("duplicate privacy key")
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            current = key
            front[key] = []
            continue
        current = None
        if stripped.startswith(("[", "{", "|", ">", "&", "*", "!")):
            if key in PRIVACY_KEYS and stripped not in {"[]", "{}"}:
                raise _Unparsable("flow or block form under a privacy key")
            front[key] = [] if stripped in {"[]", "{}"} else stripped
            continue
        front[key] = _scalar(raw)
    return front


def _last_key(front: dict[str, str | list[str]]) -> str | None:
    return next(reversed(front), None) if front else None


def parse_memory(memory_id: str, raw: bytes) -> MemoryFile | None:
    """Parse one memory file; ``None`` when it is not well-formed (fail closed)."""

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return None
    try:
        end = next(index for index in range(1, len(lines)) if lines[index].strip() == "---")
        front = _front(lines[1:end])
    except (StopIteration, _Unparsable):
        return None
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
