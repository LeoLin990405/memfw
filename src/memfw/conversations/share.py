"""Read-only sharing of the unified conversation store with local agent apps.

Every result is redacted again on the way out (:func:`store.safe_text`),
paginated and size-capped, filtered by :class:`policy.SharePolicy` for the
calling app, and labelled as data, not instructions. There are no write
operations here.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from typing import Any

from memfw.history.redact import redact_path

from .policy import SharePolicy
from .store import (
    DATA_NOTICE,
    ConversationError,
    ConversationSource,
    SessionInfo,
    Turn,
    check_app,
    check_id,
    safe_text,
)

MAX_LIMIT = 50
SNIPPET_CHARS = 400
TURN_CHARS = 8 * 1024
DEFAULT_BYTES = 16 * 1024
MAX_BYTES = 64 * 1024
MIN_BYTES = 1024
MAX_RANGE = 200
HANDOFF_MAX_TURNS = 5000
GOAL_CHARS = 2048
RECENT_CHARS = 1024
ITEM_CHARS = 200
MAX_ITEMS = 10
MAX_FILES = 50

SummaryLookup = Callable[[str, str], "str | None"]
"""``(app, session_id) -> session summary`` from the worker, if there is one."""

_EDIT_TOOL = re.compile(r"(?i)edit|write|patch|str_replace|create_file|multiedit|apply")
_PATHLIKE = re.compile(r"(?:~|\.{0,2}/)?(?:[\w@+.-]+/)*[\w@+-][\w@+.-]*\.[A-Za-z0-9]{1,8}\b")
_DECISION = re.compile(
    r"(?i)\b(?:decided|decision|we will|we'll go with|going with|chose|agreed|settled on)\b"
)
_SENTENCE = re.compile(r"[^.!?\n]{8,400}[.!?]?")


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(int(value), high))


def _snippet(text: str, terms: Sequence[str]) -> str:
    lowered = text.lower()
    position = min(
        (index for index in (lowered.find(term.lower()) for term in terms) if index >= 0),
        default=0,
    )
    start = max(0, position - SNIPPET_CHARS // 4)
    window = text[start : start + SNIPPET_CHARS]
    return ("…" if start else "") + window + ("…" if start + SNIPPET_CHARS < len(text) else "")


def _calls(turn: Turn) -> list[dict[str, str]]:
    out = []
    for call in turn.tool_calls[:20]:
        name, _ = safe_text(call.get("name", ""), 80)
        summary, _ = safe_text(call.get("summary", ""), ITEM_CHARS)
        out.append({"name": name, "summary": summary})
    return out


def _size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False).encode())


class ConversationShare:
    """Search, read and hand off shared conversations as ``caller`` sees them."""

    def __init__(
        self,
        source: ConversationSource,
        policy: SharePolicy,
        caller: str,
        *,
        summaries: SummaryLookup | None = None,
    ) -> None:
        self.source = source
        self.policy = policy
        self.caller = check_app(caller)
        self.summaries = summaries

    # -- helpers ----------------------------------------------------------------

    def _ready(self) -> None:
        self.policy.scope(self.caller)  # raises when off or not allowed
        if not self.source.available():
            raise ConversationError("conversation store is not available")

    @staticmethod
    def _requested(app: str | None, agent: str | None) -> list[str] | None:
        names = {check_app(value) for value in (app, agent) if value}
        if len(names) > 1:
            raise ConversationError("app and agent name different apps; pass one")
        return sorted(names) or None

    def _resolve(self, session_id: str, app: str | None) -> SessionInfo:
        check_id(session_id)
        include, exclude = self.policy.narrow(self.caller, [app] if app else None)
        found = [
            item
            for item in self.source.find_session(session_id, include, exclude)
            if self.policy.allowed(self.caller, item.app)
        ]
        if not found:
            # Same answer for "missing" and "hidden": existence is not disclosed.
            raise ConversationError("session not found")
        if len(found) > 1:
            apps = ", ".join(sorted(item.app for item in found))
            raise ConversationError(f"session id is ambiguous; pass app (one of: {apps})")
        return found[0]

    # -- operations -------------------------------------------------------------

    def list_sessions(
        self,
        *,
        app: str | None = None,
        agent: str | None = None,
        since: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> dict[str, Any]:
        self._ready()
        limit = _clamp(limit, 1, MAX_LIMIT)
        offset = max(0, int(offset))
        include, exclude = self.policy.narrow(self.caller, self._requested(app, agent))
        rows = self.source.sessions(
            apps=include, exclude=exclude, since=since, limit=limit + 1, offset=offset
        )
        rows = [row for row in rows if self.policy.allowed(self.caller, row.app)]
        more = len(rows) > limit
        return {
            "notice": DATA_NOTICE,
            "sessions": [row.as_dict() for row in rows[:limit]],
            "next_offset": offset + limit if more else None,
        }

    def search(
        self,
        query: str,
        *,
        app: str | None = None,
        agent: str | None = None,
        since: str | None = None,
        limit: int = 10,
        offset: int = 0,
    ) -> dict[str, Any]:
        self._ready()
        query = str(query or "").strip()[:500]
        if not query:
            raise ConversationError("query is required")
        limit = _clamp(limit, 1, MAX_LIMIT)
        offset = max(0, int(offset))
        include, exclude = self.policy.narrow(self.caller, self._requested(app, agent))
        hits = self.source.search(
            query, apps=include, exclude=exclude, since=since, limit=limit + 1, offset=offset
        )
        hits = [hit for hit in hits if self.policy.allowed(self.caller, hit.app)]
        terms = query.split()
        results = []
        for hit in hits[:limit]:
            text, withheld = safe_text(hit.text)
            results.append(
                {
                    "ref": hit.ref,
                    "app": hit.app,
                    "session_id": hit.session_id,
                    "offset": hit.offset,
                    "role": hit.role,
                    "ts": hit.ts,
                    "snippet": text if withheld else _snippet(text, terms),
                    "withheld": withheld,
                }
            )
        return {
            "notice": DATA_NOTICE,
            "results": results,
            "next_offset": offset + limit if len(hits) > limit else None,
        }

    def get(
        self,
        session_id: str,
        *,
        app: str | None = None,
        start: int = 0,
        end: int | None = None,
        max_bytes: int = DEFAULT_BYTES,
    ) -> dict[str, Any]:
        self._ready()
        info = self._resolve(session_id, app)
        start = max(0, int(start))
        stop = start + MAX_RANGE if end is None else _clamp(end, start, start + MAX_RANGE)
        cap = _clamp(max_bytes, MIN_BYTES, MAX_BYTES)
        turns = self.source.turns(info.app, info.session_id, start=start, limit=stop - start)
        out: list[dict[str, Any]] = []
        used = _size(info.as_dict()) + len(DATA_NOTICE)
        truncated = False
        for position, turn in enumerate(turns, start=start):
            text, withheld = safe_text(turn.text, TURN_CHARS)
            item = {
                "index": position,
                "ref": turn.ref,
                "role": turn.role,
                "ts": turn.ts,
                "text": text,
                "withheld": withheld,
                "tool_calls": _calls(turn),
            }
            size = _size(item)
            if out and used + size > cap:
                truncated = True
                break
            if not out and used + size > cap:
                item["text"] = text[: max(0, cap - used - 512)] + "…"
                truncated = True
            out.append(item)
            used += _size(item)
            if truncated:
                break
        next_index = start + len(out)
        limit_end = info.turns if end is None else min(int(end), info.turns)
        return {
            "notice": DATA_NOTICE,
            "session": info.as_dict(),
            "turns": out,
            "truncated": truncated,
            "next_start": next_index if next_index < limit_end else None,
        }

    def handoff(
        self,
        session_id: str,
        *,
        app: str | None = None,
        for_agent: str | None = None,
        max_bytes: int = DEFAULT_BYTES,
        recent: int = 12,
    ) -> dict[str, Any]:
        self._ready()
        info = self._resolve(session_id, app)
        target = check_app(for_agent) if for_agent else None
        if target is not None and not self.policy.allowed(target, info.app):
            raise ConversationError(f"app {target} may not read this session")
        cap = _clamp(max_bytes, MIN_BYTES, MAX_BYTES)
        recent = _clamp(recent, 1, 50)
        turns = self.source.turns(info.app, info.session_id, start=0, limit=HANDOFF_MAX_TURNS)
        goal = ""
        for turn in turns:
            if turn.role == "user" and turn.text.strip():
                goal, _ = safe_text(turn.text, GOAL_CHARS)
                break
        talk = [turn for turn in turns if turn.role in {"user", "assistant"}]
        recent_turns = []
        for turn in talk[-recent:]:
            text, withheld = safe_text(turn.text, RECENT_CHARS)
            recent_turns.append(
                {
                    "ref": turn.ref,
                    "role": turn.role,
                    "ts": turn.ts,
                    "text": text,
                    "withheld": withheld,
                }
            )
        pack: dict[str, Any] = {
            "notice": DATA_NOTICE,
            "kind": "handoff",
            "for_agent": target,
            "source": {**info.as_dict(), "turns_read": len(turns)},
            "system": {
                "label": "[system] derived by code from the stored, redacted session",
                "goal": goal,
                "files_touched": self._files(turns),
                "decisions": self._sentences(talk, _DECISION),
                "open_questions": self._questions(talk[-20:]),
                "recent_turns": recent_turns,
            },
        }
        summary = self.summaries(info.app, info.session_id) if self.summaries else None
        if summary:
            text, withheld = safe_text(summary, 4096)
            if not withheld:
                pack["model_summarized"] = {
                    "label": "[model_summarized: not verified]",
                    "summary": text,
                }
        pack["truncated"] = self._fit(pack, cap)
        return pack

    # -- handoff parts ----------------------------------------------------------

    @staticmethod
    def _files(turns: Sequence[Turn]) -> list[str]:
        seen: list[str] = []
        for turn in turns:
            for call in turn.tool_calls:
                if not _EDIT_TOOL.search(call.get("name", "")):
                    continue
                for match in _PATHLIKE.findall(call.get("summary", "")):
                    cleaned, withheld = safe_text(redact_path(match), 300)
                    if not withheld and cleaned not in seen:
                        seen.append(cleaned)
                    if len(seen) >= MAX_FILES:
                        return seen
        return seen

    @staticmethod
    def _sentences(turns: Sequence[Turn], pattern: re.Pattern[str]) -> list[str]:
        found: list[str] = []
        for turn in turns:
            text, withheld = safe_text(turn.text)
            if withheld:
                continue
            for sentence in _SENTENCE.findall(text):
                if pattern.search(sentence):
                    item = sentence.strip()[:ITEM_CHARS]
                    if item not in found:
                        found.append(item)
        return found[-MAX_ITEMS:]

    @staticmethod
    def _questions(turns: Sequence[Turn]) -> list[str]:
        found: list[str] = []
        for turn in turns:
            text, withheld = safe_text(turn.text)
            if withheld:
                continue
            for sentence in _SENTENCE.findall(text):
                item = sentence.strip()
                if item.endswith("?") and item[:ITEM_CHARS] not in found:
                    found.append(item[:ITEM_CHARS])
        return found[-MAX_ITEMS:]

    @staticmethod
    def _fit(pack: dict[str, Any], cap: int) -> bool:
        """Trim to ``cap`` bytes: recent turns, then files, then the model part."""

        system = pack["system"]
        truncated = False
        while _size(pack) > cap and system["recent_turns"]:
            system["recent_turns"].pop(0)
            truncated = True
        while _size(pack) > cap and system["files_touched"]:
            system["files_touched"].pop()
            truncated = True
        if _size(pack) > cap and "model_summarized" in pack:
            del pack["model_summarized"]
            truncated = True
        for key in ("decisions", "open_questions"):
            while _size(pack) > cap and system[key]:
                system[key].pop(0)
                truncated = True
        if _size(pack) > cap:
            system["goal"] = system["goal"][: max(0, len(system["goal"]) - (_size(pack) - cap))]
            truncated = True
        return truncated


def render_markdown(pack: dict[str, Any]) -> str:
    """A handoff pack as Markdown to paste into another agent."""

    system = pack["system"]
    source = pack["source"]
    lines = [
        f"> {pack['notice']}",
        "",
        f"# Handoff: {source['app']} session {source['session_id']}",
        "",
        f"{system['label']}",
        "",
        f"- turns: {source['turns']} (updated {source['updated_at'] or 'unknown'})",
    ]
    if source.get("cwd"):
        lines.append(f"- cwd: {source['cwd']}")
    lines += ["", "## Goal", "", system["goal"] or "(no user message)", ""]
    for title, key in (
        ("Files touched", "files_touched"),
        ("Decisions (heuristic)", "decisions"),
        ("Open questions (heuristic)", "open_questions"),
    ):
        if system[key]:
            lines += [f"## {title}", "", *(f"- {item}" for item in system[key]), ""]
    if "model_summarized" in pack:
        part = pack["model_summarized"]
        lines += ["## Summary", "", part["label"], "", part["summary"], ""]
    lines += ["## Recent turns", ""]
    for turn in system["recent_turns"]:
        lines += [f"**{turn['role']}** `{turn['ref']}`", "", turn["text"], ""]
    if pack.get("truncated"):
        lines.append("_(truncated to the size cap)_")
    return "\n".join(lines).rstrip() + "\n"
