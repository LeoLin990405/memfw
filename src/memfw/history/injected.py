"""Harness-injected text in conversation turns: detect and strip it (no dependencies).

Many "user" turns in agent transcripts are not what a person asked for: the
harness prepends system reminders, echoes AGENTS.md / CLAUDE.md, reports
background tasks and embeds tool schemas. :func:`strip_structural` removes
these generic, publicly known markers and unwraps request wrappers
(``<user_query>`` ...) so the request inside is kept. It is shared by the
conversation readers' labels (:mod:`memfw.history.sources.labels`), the
summary worker's intent and the dream skills' boilerplate filter
(:mod:`memfw.dream.skills.boilerplate`, which adds the corpus-aware layer).
"""

from __future__ import annotations

import json
import re

_BLOCK_TAGS = (
    "system-reminder",
    "system_reminder",
    "environment_context",
    "user_instructions",
    "INSTRUCTIONS",
    "permissions instructions",
    "functions",
    "background-task-finished",
    "task-notification",
    "local-command-stdout",
    "local-command-stderr",
    "command-message",
    "turn_aborted",
    "persisted-output",
)
"""Harness blocks, matched case-sensitively in the exact spelling harnesses emit.

A person's own ``<instructions>`` or ``<tools>`` text is not one of them.
"""
_CLOSED_ONLY_TAGS = ("ide_opened_file", "ide_selection", "user-prompt-submit-hook")
"""Stripped only as a complete open...close block: an unclosed tag in prose stays."""
_UNWRAP_TAGS = (
    "user_query",
    "user_request",
    "goal_round",
    "command-args",
    "command-name",
)
"""Request wrappers: the tags go, the request inside stays. Generic words such
as ``<task>`` or ``<request>`` are left alone: a person may type them."""
_BLOCKS = [
    re.compile(
        rf"<{re.escape(tag)}(?:\s[^>]*)?>.*?(?:</{re.escape(tag)}\s*>|\Z)",
        re.DOTALL,
    )
    for tag in _BLOCK_TAGS
]
_BLOCKS += [
    re.compile(
        rf"<{re.escape(tag)}(?:\s[^>]*)?>.*?</{re.escape(tag)}\s*>",
        re.DOTALL,
    )
    for tag in _CLOSED_ONLY_TAGS
]
_UNWRAP = re.compile(
    r"</?(?:" + "|".join(re.escape(tag) for tag in _UNWRAP_TAGS) + r")(?:\s[^>]*)?>"
)
_INJECTED_PREFIX = re.compile(
    r"^\s*(?:"
    r"#\s*(?:AGENTS|CLAUDE)\.md\b"
    r"|Contents of \S*(?:CLAUDE|AGENTS)\.md"
    r"|\[SYSTEM NOTIFICATION"
    r"|The following is the Codex agent history"
    r"|Caveat: The messages below were generated"
    r")",
    re.IGNORECASE,
)
_INSTRUCTIONS_END = re.compile(r"</INSTRUCTIONS\s*>")
_INJECTED_EXACT = frozenset(
    {
        "tool loaded.",
        "[request interrupted by user]",
        "[request interrupted by user for tool use]",
    }
)
_SCHEMA_KEYS = frozenset({"parameters", "properties", "input_schema", "inputSchema", "$schema"})
_PARAGRAPH = re.compile(r"\n\s*\n")
_SENTENCE = re.compile(r"(?<=[.!?。！？])\s+(?=\S)")
_CONTENT_WORD = re.compile(r"[A-Za-z]{2,}|[一-鿿]")


def _is_schema(paragraph: str) -> bool:
    text = paragraph.strip()
    if '"type": "object"' in text and '"properties"' in text:
        return True
    if not text or text[0] not in "[{":
        return False
    try:
        value = json.loads(text)
    except ValueError:
        return False
    items = value if isinstance(value, list) else [value]
    for item in items:
        if isinstance(item, dict) and (
            _SCHEMA_KEYS & set(item) or _SCHEMA_KEYS & set(item.get("function") or {})
        ):
            return True
    return False


def _structural(text: str) -> tuple[str, int]:
    """Residual text and the number of injected pieces removed."""

    removed = 0
    value = str(text or "")
    stripped = value.strip()
    if stripped.lower() in _INJECTED_EXACT:
        return "", 1
    if _INJECTED_PREFIX.match(value):
        ends = list(_INSTRUCTIONS_END.finditer(value))
        end = ends[-1] if ends else None
        if end is None:
            return "", 1
        value = value[end.end() :]
        removed += 1
    for pattern in _BLOCKS:
        value, count = pattern.subn("\n\n", value)
        removed += count
    value = _UNWRAP.sub("\n", value)
    kept = []
    for paragraph in paragraphs(value):
        if _is_schema(paragraph) or paragraph.strip().lower() in _INJECTED_EXACT:
            removed += 1
            continue
        kept.append(paragraph)
    return "\n\n".join(kept), removed


def strip_structural(text: str) -> str:
    """``text`` without generic harness markers (static patterns only)."""

    return _structural(text)[0]


def paragraphs(text: str) -> list[str]:
    """Blank-line separated paragraphs, each with trailing space trimmed."""

    out = []
    for part in _PARAGRAPH.split(str(text or "")):
        lines = [line.rstrip() for line in part.strip("\n").splitlines()]
        joined = "\n".join(lines).strip()
        if joined:
            out.append(joined)
    return out


def has_content(text: str) -> bool:
    """At least two content words (or two CJK characters)."""

    return len(_CONTENT_WORD.findall(text or "")) >= 2
