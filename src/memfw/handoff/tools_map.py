"""Which tool calls edit files, per agent, and how to pull their paths out.

Stored tool calls are ``{"name", "summary"}`` where the summary is the
reader's :func:`memfw.history.parse.render` of the arguments (``key: value;
key: value``), redacted and cut to 200 characters by the store. A value cut
short there (or by the reader, which marks it with ``…``) is not trusted as a
path.

The per-agent table lists the public edit-tool names of each CLI; a tool not
in the table still counts when its name looks like an edit and one of the
common path keys is present (:data:`GENERIC_KEYS`).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from memfw.history.redact import has_secret, redact

PATH_CHARS = 300
STORED_SUMMARY_CHARS = 200
"""The store cuts summaries here; a value running to that end may be cut short."""
GENERIC_KEYS = ("path", "file_path", "filePath", "target_file", "filename", "notebook_path")
_PATCH_TOOLS = frozenset({"apply_patch", "patch", "applypatch"})

_COMMON: dict[str, tuple[str, ...]] = {
    "edit": GENERIC_KEYS,
    "write": GENERIC_KEYS,
}

EDIT_TOOLS: dict[str, dict[str, tuple[str, ...]]] = {
    "claude": {
        "edit": ("file_path",),
        "write": ("file_path",),
        "multiedit": ("file_path",),
        "notebookedit": ("notebook_path",),
    },
    "codex": {"apply_patch": ("input", "patch"), "write_file": ("path",), **_COMMON},
    "kimi": {
        "writefile": ("path",),
        "strreplacefile": ("path",),
        "write_file": ("path",),
        "str_replace_file": ("path",),
    },
    "kimi-desktop": {"writefile": ("path",), "strreplacefile": ("path",), **_COMMON},
    "grok": {"edit_file": GENERIC_KEYS, "write_file": GENERIC_KEYS, **_COMMON},
    "cursor": {
        "edit_file": ("target_file", "path"),
        "search_replace": ("file_path", "path"),
        "write": ("file_path", "path"),
        "edit": ("file_path", "path"),
    },
    "cursor-ide": {"edit_file": ("target_file", "path"), "search_replace": ("file_path",)},
    "deepseek": {"edit_file": GENERIC_KEYS, "write_file": GENERIC_KEYS, **_COMMON},
    "minimax": {"edit_file": GENERIC_KEYS, "write_file": GENERIC_KEYS, **_COMMON},
    "glm": {"edit_file": GENERIC_KEYS, "write_file": GENERIC_KEYS, **_COMMON},
    "stepfun": {"edit_file": GENERIC_KEYS, "write_file": GENERIC_KEYS, **_COMMON},
    "opencode": {"edit": ("filePath",), "write": ("filePath",), "patch": ("patchText", "patch")},
    "gemini": {"write_file": ("file_path",), "replace": ("file_path",)},
    "antigravity": {
        "write_to_file": ("TargetFile", "path"),
        "replace_file_content": ("TargetFile",),
    },
    "qwen": {
        "write_file": ("file_path",),
        "edit": ("file_path",),
        "notebook_edit": ("notebook_path",),
    },
}

_EDIT_LIKE = re.compile(r"(?i)edit|write|patch|str_?replace|create_?file|multiedit|apply|replace")
# A pair starts at the beginning or after "; " and is "key: value".
_PAIR = re.compile(r"(?:^|;\s)([A-Za-z_][\w.-]{0,63}):\s")
_PATCH_HEADER = re.compile(
    r"\*\*\* (?:Update|Add|Delete) File: ([^\n*;]+)|\*\*\* Move to: ([^\n*;]+)"
)


def canonical_agent(name: str) -> str:
    """The registry name for ``name`` (aliases resolved), else ``name`` lowercased."""

    lowered = (name or "").strip().lower()
    try:
        from memfw.history.sources import UnknownAgent, get_reader
    except ImportError:  # pragma: no cover - the reader package ships with memfw
        return lowered
    try:
        return str(get_reader(lowered).agent)
    except (UnknownAgent, KeyError):
        return lowered


def pairs(summary: str) -> dict[str, str]:
    """``key: value`` pairs of a rendered summary (last one wins for a repeated key)."""

    out: dict[str, str] = {}
    matches = list(_PAIR.finditer(summary or ""))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(summary)
        out[match.group(1)] = summary[match.end() : end]
    return out


def _clean(value: str) -> str | None:
    value = value.strip()
    if not value or len(value) > PATH_CHARS or "\n" in value or value.endswith("…"):
        return None
    # A secret-shaped path is dropped, so check before redacting (redact()
    # would mask the secret and hide it from has_secret).
    if has_secret(value):
        return None
    # Paths come from conversation text: full redaction, not only user names.
    return redact(value)


def _keys_for(agent: str, tool: str) -> tuple[str, ...]:
    table = EDIT_TOOLS.get(agent, {})
    if tool in table:
        return table[tool]
    return GENERIC_KEYS if _EDIT_LIKE.search(tool) else ()


def _paths_of(agent: str, call: Mapping[str, Any]) -> list[str]:
    tool = str(call.get("name") or "").strip().lower()
    summary = str(call.get("summary") or "")
    keys = _keys_for(agent, tool)
    if not keys or not summary:
        return []
    cut = len(summary) >= STORED_SUMMARY_CHARS
    found: list[str] = []
    if tool in _PATCH_TOOLS or "*** " in summary:
        for match in _PATCH_HEADER.finditer(summary):
            found.append(match.group(1) or match.group(2))
        if found:
            if cut and summary.rstrip().endswith(found[-1].rstrip()):
                found.pop()  # the last header ran into the store's cut
            return found
    values = pairs(summary)
    last = next(reversed(values), None)
    for key in keys:
        if key in values and not (cut and key == last):
            found.append(values[key])
    return found


def files_from_calls(
    agent: str, calls: Iterable[Mapping[str, Any]], *, limit: int = 50
) -> list[str]:
    """Paths of files edited by ``calls``, first seen first, deduplicated, at most ``limit``."""

    name = canonical_agent(agent)
    seen: list[str] = []
    for call in calls:
        if not isinstance(call, Mapping):
            continue
        for raw in _paths_of(name, call):
            cleaned = _clean(raw)
            if cleaned is None or cleaned in seen:
                continue
            seen.append(cleaned)
            if len(seen) >= limit:
                return seen
    return seen
