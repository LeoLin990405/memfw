"""Turn one transcript line into an entry record.

Supported shapes (detected per line, no format flag needed):

* Claude Code style JSONL: ``sessionId``, ``uuid``, ``parentUuid``,
  ``timestamp``, ``message.content`` (string or blocks); a
  ``system``/``compact_boundary`` record starts a new context window.
* Codex style rollout JSONL: ``{"timestamp", "type", "payload"}`` with
  ``session_meta`` (session id, ``forked_from_id``), ``response_item`` and
  ``event_msg`` records; a ``compacted`` record starts a new window.
* Generic / Conductor JSONL: ``session_id`` or ``ticket_id`` / ``run_id``,
  ``worker_id``, ``handoff_from`` / ``parent_id``, ``ts`` / ``timestamp`` and a
  ``message`` / ``text`` / ``content`` field.
* Plain text logs (``.log``): one line per entry, session = file stem.

Explicit window ids (``window_id``, ``context_window_id``) and parent window
ids (``parent_window_id``, ``previous_window_id``) are used when present.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

_TEXT_KEYS = ("message", "text", "content", "msg", "summary", "output", "body")
_SESSION_KEYS = ("session_id", "sessionId", "ticket_id", "ticketId", "run_id", "conversation_id")
_PARENT_SESSION_KEYS = (
    "parent_session_id",
    "parentSessionId",
    "forked_from_id",
    "handoff_from",
    "handoff_id",
    "parent_ticket_id",
)
_WINDOW_KEYS = ("context_window_id", "window_id", "windowId")
_PARENT_WINDOW_KEYS = ("parent_window_id", "previous_window_id", "parentWindowId")
_TIME_KEYS = ("timestamp", "ts", "time", "created_at", "createdAt")
_MAX_BLOCK_CHARS = 4000
_MAX_DEPTH = 8
_MAX_STRINGS = 100_000


class TooComplex(ValueError):
    """A line too deeply nested or with too many strings to scan completely."""


def iter_strings(value: Any, depth: int = 0) -> list[str]:
    """Every decoded string in ``value``: keys, values, and JSON nested in strings.

    The secret scan runs on each of them, so a credential that only appears
    JSON-escaped in the raw line (``{\\"password\\": ...}``) is still seen in
    its decoded form. JSON inside strings is decoded up to ``_MAX_DEPTH``
    levels; past that the string stays opaque, and a copy with backslashes
    removed is scanned as well so multiply-escaped pairs still match.
    Raises :class:`TooComplex` when the strings cannot all be collected.
    """

    out: list[str] = []
    stack: list[tuple[Any, int]] = [(value, depth)]
    while stack:
        if len(out) >= _MAX_STRINGS:
            raise TooComplex("too many strings to scan")
        item, level = stack.pop()
        if isinstance(item, str):
            out.append(item)
            stripped = item.strip()
            if stripped[:1] not in {"{", "["} or len(stripped) >= 1_048_576:
                continue
            if level >= _MAX_DEPTH:
                if "\\" in item:
                    out.append(item.replace("\\", ""))
                continue
            try:
                nested = json.loads(stripped)
            except (ValueError, RecursionError):
                if "\\" in item:
                    out.append(item.replace("\\", ""))
                continue
            stack.append((nested, level + 1))
        elif isinstance(item, dict):
            for key, child in item.items():
                out.append(str(key))
                stack.append((child, level + 1))
        elif isinstance(item, list):
            stack.extend((child, level + 1) for child in item)
    return out


_RENDER_DEPTH = 16


def render(value: Any, depth: int = 0) -> str:
    """Readable text for a tool input: decoded ``key: value`` pairs, not escaped JSON.

    Nesting deeper than ``_RENDER_DEPTH`` is shown as an ellipsis.
    """

    if depth >= _RENDER_DEPTH:
        return "\u2026"
    if isinstance(value, str):
        stripped = value.strip()
        if stripped[:1] in {"{", "["}:
            try:
                decoded = json.loads(stripped)
            except (ValueError, RecursionError):
                return value
            return render(decoded, depth + 1)
        return value
    if isinstance(value, dict):
        return "; ".join(f"{key}: {render(child, depth + 1)}" for key, child in value.items())
    if isinstance(value, list):
        return "; ".join(render(child, depth + 1) for child in value)
    return "" if value is None else str(value)


@dataclass
class Record:
    """Fields pulled from one line. ``text`` is unredacted and never stored."""

    text: str
    role: str = ""
    kind: str = ""
    session_id: str = ""
    parent_session_id: str = ""
    entry_id: str = ""
    parent_entry_id: str = ""
    window_id: str = ""
    parent_window_id: str = ""
    new_window: bool = False
    timestamp: str = ""
    agent: str = ""
    strings: list[str] = field(default_factory=list)

    def metadata(self) -> list[str]:
        """Fields that are returned to callers besides the text."""

        return [
            self.role,
            self.kind,
            self.session_id,
            self.parent_session_id,
            self.entry_id,
            self.parent_entry_id,
            self.window_id,
            self.parent_window_id,
            self.timestamp,
            self.agent,
        ]


def _str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _first(mapping: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, (str, int)) and not isinstance(value, bool) and str(value):
            return str(value)
    return ""


def _timestamp(mapping: dict[str, Any]) -> str:
    for key in _TIME_KEYS:
        value = mapping.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            seconds = value / 1000 if value > 10_000_000_000 else value
            try:
                return datetime.fromtimestamp(seconds, UTC).isoformat().replace("+00:00", "Z")
            except (OverflowError, OSError, ValueError):
                continue
        if isinstance(value, str) and value:
            return value
    return ""


def _blocks(content: Any, depth: int = 0) -> str:
    """Text of a message content value: a string or a list of typed blocks."""

    if depth >= _RENDER_DEPTH:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        content = [content]
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
            continue
        if not isinstance(block, dict):
            continue
        kind = _str(block.get("type"))
        if kind in {"thinking", "redacted_thinking", "reasoning", "image"}:
            continue
        if kind in {"tool_use", "function_call"}:
            name = _str(block.get("name"))
            payload = block.get("input", block.get("arguments"))
            parts.append(f"[tool {name}] {render(payload)[:_MAX_BLOCK_CHARS]}")
            continue
        if kind == "tool_result":
            parts.append(_blocks(block.get("content"), depth + 1)[:_MAX_BLOCK_CHARS])
            continue
        text = block.get("text")
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(part for part in parts if part)


def _codex(raw: dict[str, Any], payload: dict[str, Any]) -> Record:
    kind = _str(raw.get("type"))
    item_type = _str(payload.get("type"))
    record = Record(text="", kind=item_type or kind, agent="codex", timestamp=_timestamp(raw))
    record.window_id = _first(payload, _WINDOW_KEYS) or _first(raw, _WINDOW_KEYS)
    record.parent_window_id = _first(payload, _PARENT_WINDOW_KEYS)
    record.entry_id = _first(payload, ("id", "call_id"))
    if kind == "session_meta":
        record.session_id = _first(payload, ("id", "session_id"))
        record.parent_session_id = _first(payload, _PARENT_SESSION_KEYS)
        record.kind = "session_meta"
        record.timestamp = _timestamp(payload) or record.timestamp
        return record
    if kind == "compacted":
        record.new_window = True
        record.kind = "compacted"
        return record
    if item_type == "message":
        record.role = _str(payload.get("role"))
        record.text = _blocks(payload.get("content"))
    elif item_type in {"function_call", "custom_tool_call", "local_shell_call"}:
        record.role = "tool"
        arguments = payload.get("arguments", payload.get("input", payload.get("action")))
        record.text = f"[tool {_str(payload.get('name'))}] {render(arguments)[:_MAX_BLOCK_CHARS]}"
    elif item_type in {"function_call_output", "custom_tool_call_output"}:
        record.role = "tool"
        output = payload.get("output")
        if isinstance(output, dict):
            output = output.get("content", output.get("output"))
        record.text = _blocks(output)[:_MAX_BLOCK_CHARS]
    elif kind == "event_msg":
        record.role = "user" if item_type == "user_message" else "assistant"
        record.text = _str(payload.get("message"))
    return record


def _claude(raw: dict[str, Any]) -> Record:
    kind = _str(raw.get("type"))
    message = raw.get("message")
    record = Record(
        text="",
        kind=_str(raw.get("subtype")) or kind,
        agent="claude",
        session_id=_str(raw.get("sessionId")),
        parent_session_id=_first(raw, _PARENT_SESSION_KEYS),
        entry_id=_str(raw.get("uuid")),
        parent_entry_id=_str(raw.get("parentUuid")) or _str(raw.get("logicalParentUuid")),
        timestamp=_timestamp(raw),
        window_id=_first(raw, _WINDOW_KEYS),
        parent_window_id=_first(raw, _PARENT_WINDOW_KEYS),
    )
    if kind == "system" and _str(raw.get("subtype")) == "compact_boundary":
        record.new_window = True
    if isinstance(message, dict):
        record.role = _str(message.get("role")) or kind
        record.text = _blocks(message.get("content"))
    elif kind == "summary":
        record.role = "summary"
        record.text = _str(raw.get("summary"))
    else:
        record.role = kind
        record.text = _str(raw.get("content"))
    if raw.get("isSidechain") is True:
        record.kind = record.kind or "sidechain"
    return record


def _generic(raw: dict[str, Any]) -> Record:
    text = ""
    for key in _TEXT_KEYS:
        value = raw.get(key)
        if isinstance(value, dict):
            value = value.get("content", value.get("text"))
        text = _blocks(value)
        if text:
            break
    return Record(
        text=text,
        role=_first(raw, ("role", "level", "actor", "speaker")),
        kind=_first(raw, ("event", "type", "kind")),
        agent=_first(raw, ("agent", "worker", "worker_id")) or "generic",
        session_id=_first(raw, _SESSION_KEYS),
        parent_session_id=_first(raw, _PARENT_SESSION_KEYS + ("parent_id",)),
        entry_id=_first(raw, ("id", "uuid", "event_id", "entry_id")),
        parent_entry_id=_first(raw, ("parent_entry_id", "parentUuid", "reply_to")),
        window_id=_first(raw, _WINDOW_KEYS),
        parent_window_id=_first(raw, _PARENT_WINDOW_KEYS),
        new_window=raw.get("new_window") is True,
        timestamp=_timestamp(raw),
    )


def parse_line(line: str, *, jsonl: bool) -> Record | None:
    """Parse one line. ``None`` means the line is blank or not a JSON object."""

    stripped = line.strip()
    if not stripped:
        return None
    if not jsonl:
        return Record(text=stripped, kind="line", agent="log", strings=[stripped])
    try:
        raw = json.loads(stripped)
    except (ValueError, RecursionError):
        # Not JSON, or nested past the interpreter's recursion limit.
        return None
    if not isinstance(raw, dict):
        return None
    try:
        payload = raw.get("payload")
        if isinstance(payload, dict) and "type" in raw:
            record = _codex(raw, payload)
        elif "sessionId" in raw or ("message" in raw and "uuid" in raw):
            record = _claude(raw)
        else:
            record = _generic(raw)
        record.strings = iter_strings(raw)
    except (RecursionError, TooComplex):
        return None
    return record
