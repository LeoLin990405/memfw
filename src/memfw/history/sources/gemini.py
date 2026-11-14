"""Gemini CLI conversation reader.

Gemini CLI keeps one directory per project under ``~/.gemini/tmp``::

    <project slug or hash>/.project_root            the project's absolute path
    <project slug or hash>/chats/session-<time>-<id8>.jsonl
    <project slug or hash>/chats/<parent session id>/<subagent id>.jsonl
    <project slug or hash>/chats/session-<time>-<id8>.json      (older CLIs)
    <project slug or hash>/logs.json, checkpoint-*.json, shell_history ...

Only ``chats/`` is read: ``logs.json`` repeats the user prompts and
checkpoints repeat the history. Credential files (``oauth_creds.json``,
``google_accounts.json`` ...) live one level up, in ``~/.gemini``, and are
never opened.

A ``.jsonl`` chat is a record log replayed in order, the way the CLI loads it:

* ``{"sessionId", "projectHash", "startTime", "lastUpdated", "kind"}`` opens
  the session (``kind`` is ``main`` or ``subagent``; it may carry
  ``messages`` and ``directories``);
* a record with a string ``id`` is a message; re-recording the same ``id``
  replaces it (tool calls and token counts are added that way);
* ``{"$set": {...}}`` updates metadata (``messages`` there replaces the list);
* ``{"$patch": {"id" | "updates" | "removeIds" | "orderIds"}}`` edits messages;
* ``{"$rewindTo": id}`` drops that message and everything after it.

A message is ``{"id", "timestamp", "type", "content", "displayContent"}`` with
``type`` ``user``, ``gemini`` (plus ``model``, ``thoughts``, ``tokens`` and
``toolCalls`` of ``{id, name, args, result, resultDisplay, status}``),
``info``, ``warning`` or ``error``. ``content`` is a string or a list of
``{"text"}`` parts. The older ``.json`` form is one object with the same
metadata and a ``messages`` list.

Each surviving message ``i`` maps to offset ``2 * position`` where
``position`` is the byte offset of the line that first recorded it (the list
index for a ``.json`` file); the results of its tool calls become a ``tool``
record at ``2 * position + 1``. The log is replayed in full on every change
(``append_only`` is false) because later lines rewrite earlier messages.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .base import (
    TOOL_OUTPUT_CHARS,
    ConversationRecord,
    SessionLocator,
    ToolCall,
    content_text,
    iso_timestamp,
    iter_jsonl,
    read_bytes,
    read_json,
    regular_file,
    relative_key,
    tool_call,
    walk_files,
)

PARSER_VERSION = "1"

AGENT = "gemini"
CHATS_DIR = "chats"
PROJECT_ROOT_FILE = ".project_root"
CREDENTIAL_FILES = frozenset(
    {"oauth_creds.json", "google_accounts.json", "mcp-oauth-tokens.json", "installation_id"}
)
"""Never opened, wherever they turn up."""
_ROLES = {"user": "user", "gemini": "assistant", "model": "assistant"}
_SYSTEM_TYPES = frozenset({"info", "warning", "error"})


def _str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _parts_text(content: Any) -> str:
    """Text of a Gemini ``PartListUnion``: a string, one part or a list; thoughts dropped."""

    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        content = [content]
    if not isinstance(content, list):
        return ""
    pieces: list[str] = []
    for part in content:
        if isinstance(part, str):
            pieces.append(part)
        elif isinstance(part, dict) and not part.get("thought"):
            text = part.get("text")
            if isinstance(text, str):
                pieces.append(text)
    return "\n".join(piece for piece in pieces if piece.strip())


def _project_root(project_dir: Path) -> str:
    raw = read_bytes(project_dir / PROJECT_ROOT_FILE, limit=64 * 1024)
    return raw.decode("utf-8", errors="replace").strip() if raw else ""


def _session_file(path: Path, chats: Path) -> bool:
    if path.name in CREDENTIAL_FILES:
        return False
    if path.parent == chats:
        return path.name.startswith("session-")
    # Sub-agent chats sit one level down, in a directory named after the parent.
    return path.parent.parent == chats and path.suffix == ".jsonl"


def _replay(path: Path) -> tuple[dict[str, Any], list[tuple[int, dict[str, Any]]]]:
    """Session metadata and the surviving ``(position, message)`` pairs, in order."""

    meta: dict[str, Any] = {}
    messages: dict[str, tuple[int, dict[str, Any]]] = {}

    def put(position: int, message: dict[str, Any]) -> None:
        key = message["id"]
        first = messages.get(key)
        messages[key] = (first[0] if first else position, message)

    def patch_one(change: dict[str, Any]) -> None:
        key = _str(change.get("id"))
        current = messages.get(key)
        if current is None:
            return
        message = dict(current[1])
        if change.get("content") is not None:
            message["content"] = change["content"]
        updates = change.get("toolCalls")
        if isinstance(updates, list) and isinstance(message.get("toolCalls"), list):
            calls = [
                dict(call) if isinstance(call, dict) else call for call in message["toolCalls"]
            ]
            for update in updates:
                if not isinstance(update, dict) or "result" not in update:
                    continue
                for call in calls:
                    if isinstance(call, dict) and call.get("id") == update.get("id"):
                        call["result"] = update["result"]
            message["toolCalls"] = calls
        messages[key] = (current[0], message)

    for position, record in iter_jsonl(path):
        rewind = record.get("$rewindTo")
        if isinstance(rewind, str):
            if rewind in messages:
                keys = list(messages)
                for key in keys[keys.index(rewind) :]:
                    del messages[key]
            else:
                messages.clear()
            continue
        change = record.get("$patch")
        if isinstance(change, dict):
            patch_one(change)
            for update in change.get("updates") or ():
                if isinstance(update, dict):
                    patch_one(update)
            for key in change.get("removeIds") or ():
                if isinstance(key, str):
                    messages.pop(key, None)
            order = change.get("orderIds")
            if isinstance(order, list):
                wanted = [key for key in order if isinstance(key, str) and key in messages]
                rest = [key for key in messages if key not in set(wanted)]
                messages = {key: messages[key] for key in rest + wanted}
            continue
        if isinstance(record.get("id"), str):
            put(position, record)
            continue
        update = record.get("$set")
        if isinstance(update, dict):
            if isinstance(update.get("messages"), list):
                messages.clear()
                for message in update["messages"]:
                    if isinstance(message, dict) and isinstance(message.get("id"), str):
                        put(position, message)
            meta.update({k: v for k, v in update.items() if k != "messages"})
            continue
        if isinstance(record.get("sessionId"), str):
            meta.update({k: v for k, v in record.items() if k != "messages"})
            for message in record.get("messages") or ():
                if isinstance(message, dict) and isinstance(message.get("id"), str):
                    put(position, message)
    # Several messages can share a position (a ``$set`` list): keep their order.
    ordered = sorted(enumerate(messages.values()), key=lambda item: (item[1][0], item[0]))
    return meta, [pair for _index, pair in ordered]


def _snapshot(path: Path) -> tuple[dict[str, Any], list[tuple[int, dict[str, Any]]]]:
    data = read_json(path)
    if not isinstance(data, dict):
        return {}, []
    meta = {k: v for k, v in data.items() if k != "messages"}
    raw = data.get("messages")
    items = raw if isinstance(raw, list) else []
    return meta, [(index, item) for index, item in enumerate(items) if isinstance(item, dict)]


def _result_text(call: dict[str, Any]) -> str:
    """Text of one tool call's result: the function response output, else its display."""

    pieces: list[str] = []
    result = call.get("result")
    for part in result if isinstance(result, list) else [result]:
        response = _dict(_dict(part).get("functionResponse")).get("response")
        if isinstance(response, dict):
            value = response.get("output", response.get("error", response.get("content")))
            text = value if isinstance(value, str) else content_text(value) if value else ""
        elif isinstance(response, str):
            text = response
        else:
            text = content_text(part) if isinstance(part, (str, dict)) else ""
        if text.strip():
            pieces.append(text.strip())
    if not pieces:
        display = call.get("resultDisplay")
        if isinstance(display, str) and display.strip():
            pieces.append(display.strip())
    return "\n".join(pieces)


def _message_records(
    message: dict[str, Any], position: int, *, session_id: str, cwd: str, project: str
) -> list[ConversationRecord]:
    kind = _str(message.get("type"))
    role = _ROLES.get(kind) or ("system" if kind in _SYSTEM_TYPES else "")
    if not role:
        return []
    content = message.get("content")
    text = _parts_text(content).strip()
    if not text and kind == "user":
        text = _parts_text(message.get("displayContent")).strip()
    calls: list[ToolCall] = []
    outputs: list[str] = []
    for call in message.get("toolCalls") or ():
        if not isinstance(call, dict):
            continue
        calls.append(tool_call(call.get("name"), call.get("args")))
        output = _result_text(call)
        if output:
            outputs.append(output[:TOOL_OUTPUT_CHARS])
    timestamp = iso_timestamp(message.get("timestamp"))
    model = _str(message.get("model"))
    records: list[ConversationRecord] = []
    if text or calls:
        records.append(
            ConversationRecord(
                agent=AGENT,
                session_id=session_id,
                offset=2 * position,
                turn=0,
                role=role,
                text=text,
                timestamp=timestamp,
                tool_calls=tuple(calls),
                cwd=cwd,
                project=project,
                model=model,
            )
        )
    if outputs:
        records.append(
            ConversationRecord(
                agent=AGENT,
                session_id=session_id,
                offset=2 * position + 1,
                turn=0,
                role="tool",
                text="\n".join(outputs),
                timestamp=timestamp,
                cwd=cwd,
                project=project,
            )
        )
    return records


class GeminiReader:
    """Reads Gemini CLI ``chats/`` session logs under ``~/.gemini/tmp``."""

    agent = AGENT
    parser_version = PARSER_VERSION
    append_only = False
    default_roots: tuple[str, ...] = (".gemini/tmp",)

    def sessions(self, root: Path) -> Iterator[SessionLocator]:
        if not root.is_dir() or root.is_symlink():
            return
        for project_dir in sorted(root.iterdir()):
            chats = project_dir / CHATS_DIR
            if project_dir.is_symlink() or not chats.is_dir() or chats.is_symlink():
                continue
            cwd = _project_root(project_dir)
            for path in walk_files(chats, (".jsonl", ".json")):
                if not _session_file(path, chats):
                    continue
                parent = path.parent.name if path.parent != chats else ""
                yield SessionLocator(
                    agent=self.agent,
                    path=path,
                    key=relative_key(root, path),
                    session_id="",
                    extra={"cwd": cwd, "project": project_dir.name, "parent_id": parent},
                )

    def read(self, locator: SessionLocator, *, after: int = -1) -> Iterator[ConversationRecord]:
        path = locator.path
        if path.name in CREDENTIAL_FILES or not regular_file(path):
            return
        try:
            meta, messages = _snapshot(path) if path.suffix == ".json" else _replay(path)
        except (OSError, ValueError, TypeError, KeyError, RecursionError):
            return
        session_id = locator.session_id or _str(meta.get("sessionId")).strip() or path.stem
        cwd = _str(locator.extra.get("cwd"))
        if not cwd:
            directories = meta.get("directories")
            if isinstance(directories, list) and directories and isinstance(directories[0], str):
                cwd = directories[0]
        project = _str(locator.extra.get("project"))
        turn = 0
        for position, message in messages:
            for record in _message_records(
                message, position, session_id=session_id, cwd=cwd, project=project
            ):
                record.turn = turn
                turn += 1
                if record.offset > after:
                    yield record


READER = GeminiReader()
