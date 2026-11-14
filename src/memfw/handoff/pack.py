"""Build a handoff pack from one stored session.

:func:`build_pack` is the Python entry point; ``memfw handoff`` (``cli.py``)
wraps it. The ``system`` part is derived by code only: source facts, the
goal (the caller's task text, else the first user message), files touched
(edit-tool arguments per agent, :mod:`.tools_map`, plus read-only git state
when the work tree is known), the latest turns and the lineage. The optional
model part (:mod:`.summarize`) is built only when a spec is given.

Every text is redacted again on the way out (a value that still looks like a
secret is withheld). The pack is cut to ``max_bytes`` of its rendered form,
dropping the oldest recent turns first, then files touched (and git state),
then the model part, then the goal's tail.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from memfw.conversations.backends import load_backend
from memfw.conversations.store import WITHHELD, safe_text

from . import gitinfo, summarize, tools_map
from .schema import (
    DEFAULT_BYTES,
    DEFAULT_RECENT,
    EXIT_NOT_INDEXED,
    EXIT_USAGE,
    GOAL_BYTES,
    MAX_BYTES,
    MAX_CALLS_PER_TURN,
    MAX_FILES,
    MAX_RECENT,
    MIN_BYTES,
    MODEL_KEYS,
    NOTICE,
    REASONS,
    SCHEMA,
    SYSTEM_LABEL,
    TURN_BYTES,
    validate_pack,
)
from .source import StoredSession, load_session

CALL_SUMMARY_CHARS = 200
TALK_ROLES = ("user", "assistant")


class HandoffError(Exception):
    """A pack cannot be built; ``code`` is the CLI exit code, ``reason`` a short id."""

    def __init__(self, code: int, reason: str | None = None, message: str = "") -> None:
        self.code = code
        self.reason = reason or REASONS.get(code, "error")
        super().__init__(message or self.reason)


def trim_bytes(text: str, limit: int) -> str:
    """``text`` cut to at most ``limit`` UTF-8 bytes (an ellipsis marks a cut)."""

    data = text.encode("utf-8")
    if len(data) <= limit:
        return text
    if limit <= 3:
        return ""
    return data[: limit - 3].decode("utf-8", errors="ignore") + "…"


def _safe(text: str, limit_bytes: int) -> tuple[str, bool]:
    cleaned, withheld = safe_text(text)
    return (WITHHELD if withheld else trim_bytes(cleaned, limit_bytes)), withheld


def _goal(session: StoredSession, task_text: str | None) -> tuple[str, str]:
    if task_text is not None and task_text.strip():
        text, _ = _safe(task_text.strip(), GOAL_BYTES)
        return text, "task_file"
    if session.first_user.strip():
        text, _ = _safe(session.first_user.strip(), GOAL_BYTES)
        return text, "first_user_message"
    return "", "none"


def _calls(calls: list[dict[str, str]]) -> list[dict[str, str]]:
    out = []
    for call in calls[:MAX_CALLS_PER_TURN]:
        name, name_withheld = safe_text(call.get("name", ""), 64)
        summary, summary_withheld = safe_text(call.get("summary", ""), CALL_SUMMARY_CHARS)
        if name_withheld:
            continue
        out.append({"name": name, "summary": "" if summary_withheld else summary})
    return out


def _recent(session: StoredSession, count: int) -> list[dict[str, Any]]:
    """The last ``count`` user/assistant turns; tool turns in that window keep only calls."""

    talk = [index for index, turn in enumerate(session.tail) if turn.role in TALK_ROLES]
    if not talk:
        return []
    start = talk[-count] if len(talk) >= count else talk[0]
    out: list[dict[str, Any]] = []
    for turn in session.tail[start:]:
        if turn.role in TALK_ROLES:
            text, withheld = _safe(turn.text, TURN_BYTES) if turn.text else ("", False)
            withheld = withheld or turn.withheld
            calls = [] if withheld else _calls(turn.tool_calls)
            text = WITHHELD if turn.withheld else text
        elif turn.tool_calls and not turn.withheld:
            text, withheld, calls = "", False, _calls(turn.tool_calls)
        else:
            continue  # tool output and other roles: no text in the pack
        out.append(
            {
                "offset": turn.offset,
                "role": turn.role,
                "ts": turn.ts,
                "text": text,
                "withheld": withheld,
                "tool_calls": calls,
            }
        )
    return out


def _source(session: StoredSession) -> dict[str, Any]:
    cwd, cwd_withheld = safe_text(session.cwd, 300)
    project, project_withheld = safe_text(session.project, 200)
    return {
        "agent": session.agent,
        "session_id": session.session_id,
        "origin": session.origin,
        "started_at": session.started_at,
        "updated_at": session.updated_at,
        "turns": session.turns,
        "withheld": session.withheld,
        "parser_version": session.parser_version,
        "cwd": "" if cwd_withheld else cwd,
        "project": "" if project_withheld else project,
    }


def check_max_bytes(value: int) -> int:
    if not MIN_BYTES <= int(value) <= MAX_BYTES:
        raise HandoffError(EXIT_USAGE, message=f"--max-bytes must be {MIN_BYTES}..{MAX_BYTES}")
    return int(value)


def build_pack(
    db: Path | str,
    agent: str,
    session_id: str,
    *,
    for_agent: str | None = None,
    max_bytes: int = DEFAULT_BYTES,
    recent: int = DEFAULT_RECENT,
    task_text: str | None = None,
    cwd: str | Path | None = None,
    git: bool = True,
    previous: list[dict[str, Any]] | None = None,
    summarize_spec: str | None = None,
    allow_cloud: bool = False,
    measure: Callable[[dict[str, Any]], int] | None = None,
    saved_as: str | None = None,
    loader: Callable[..., Any] | None = None,
    git_runner: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """The pack for ``agent``/``session_id``; :class:`HandoffError` when there is none.

    ``measure`` is the size of the pack as it will be printed (default: the
    JSON form); the pack is cut until ``measure(pack) <= max_bytes``.
    """

    cap = check_max_bytes(max_bytes)
    if not 1 <= int(recent) <= MAX_RECENT:
        raise HandoffError(EXIT_USAGE, message=f"--recent must be 1..{MAX_RECENT}")
    name = tools_map.canonical_agent(agent)
    session = load_session(db, name, session_id)
    if session is None:
        raise HandoffError(EXIT_NOT_INDEXED)
    goal, goal_source = _goal(session, task_text)
    work_tree = cwd if cwd is not None else (session.cwd or None)
    snapshot = None
    if git and work_tree:
        if git_runner is not None:
            snapshot = gitinfo.git_snapshot(work_tree, runner=git_runner)
        else:
            snapshot = gitinfo.git_snapshot(work_tree)
    recent_turns = _recent(session, int(recent))
    pack: dict[str, Any] = {
        "schema": SCHEMA,
        "notice": NOTICE,
        "for_agent": tools_map.canonical_agent(for_agent) if for_agent else None,
        "source": _source(session),
        "system": {
            "label": SYSTEM_LABEL,
            "goal": goal,
            "goal_source": goal_source,
            "files_touched": tools_map.files_from_calls(name, session.calls, limit=MAX_FILES),
            "git": snapshot,
            "recent_turns": recent_turns,
            "lineage": {
                # Store schema v1 records no parent session; kept for forward compatibility.
                "parent_session_id": None,
                "previous_handoffs": list(previous or []),
            },
        },
        "truncated": False,
        "truncated_parts": [],
        "max_bytes": cap,
        "saved_as": saved_as,
    }
    if summarize_spec is not None:
        talk = [
            {"role": turn["role"], "text": turn["text"]}
            for turn in recent_turns
            if turn["role"] in TALK_ROLES and turn["text"] and not turn["withheld"]
        ]
        pack["model_summarized"] = summarize.summarize(
            summarize_spec,
            goal=goal,
            turns=talk,
            allow_cloud=allow_cloud,
            loader=loader or load_backend,
        )
    fit(pack, cap, measure or json_size)
    validate_pack(pack)
    return pack


def fit(pack: dict[str, Any], cap: int, measure: Callable[[dict[str, Any]], int]) -> None:
    """Cut ``pack`` in place until ``measure(pack) <= cap``, recording what was cut."""

    system = pack["system"]
    parts: list[str] = pack["truncated_parts"]

    def over() -> bool:
        pack["truncated"] = True  # measured with the flag set, so clearing it never grows
        result = measure(pack) > cap
        pack["truncated"] = bool(parts)
        return result

    def mark(part: str) -> None:
        if part not in parts:
            parts.append(part)
        pack["truncated"] = True

    while over() and system["recent_turns"]:
        system["recent_turns"].pop(0)
        mark("recent_turns")
    while over() and system["files_touched"]:
        system["files_touched"].pop()
        mark("files_touched")
    if over() and system.get("git"):
        system["git"] = None
        mark("git")
    if over() and "model_summarized" in pack:
        del pack["model_summarized"]
        mark("model_summarized")
    while over() and system["goal"]:
        excess = measure(pack) - cap
        system["goal"] = trim_bytes(
            system["goal"], max(0, len(system["goal"].encode()) - excess - 8)
        )
        if len(system["goal"]) <= 1:
            system["goal"] = ""
        mark("goal")
    while over() and system["lineage"]["previous_handoffs"]:
        system["lineage"]["previous_handoffs"].pop(0)
        mark("lineage")


# -- rendering -------------------------------------------------------------------


def render_json(pack: dict[str, Any]) -> str:
    return json.dumps(pack, ensure_ascii=False, indent=2) + "\n"


def render_markdown(pack: dict[str, Any]) -> str:
    """The pack as Markdown to paste into another agent."""

    source = pack["source"]
    system = pack["system"]
    lines = [
        f"> {pack['notice']}",
        "",
        f"# Handoff: {source['agent']} session {source['session_id']}",
        "",
    ]
    if pack.get("for_agent"):
        lines += [f"For: {pack['for_agent']}", ""]
    lines += [
        system["label"],
        "",
        f"- origin: {source['origin'] or 'unknown'}; turns: {source['turns']}"
        f" (withheld {source['withheld']}); parser {source['parser_version'] or '?'}",
        f"- time: {source['started_at'] or '?'} .. {source['updated_at'] or '?'}",
    ]
    if source.get("cwd"):
        lines.append(f"- cwd: {source['cwd']}")
    goal_title = "Goal (task text)" if system["goal_source"] == "task_file" else "Goal"
    lines += ["", f"## {goal_title}", "", system["goal"] or "(no user message)", ""]
    if system["files_touched"]:
        lines += ["## Files touched", "", *(f"- {item}" for item in system["files_touched"]), ""]
    git = system.get("git")
    if git:
        lines += ["## Git (read-only)", "", "```", *git["status"], "", *git["diff_stat"], "```", ""]
    lineage = system["lineage"]
    if lineage["previous_handoffs"] or lineage["parent_session_id"]:
        lines += ["## Lineage", ""]
        if lineage["parent_session_id"]:
            lines.append(f"- parent session: {lineage['parent_session_id']}")
        for item in lineage["previous_handoffs"]:
            lines.append(
                f"- handoff #{item['n']} for {item['for_agent'] or 'any'} at {item['created_at']}"
            )
        lines.append("")
    model = pack.get("model_summarized")
    if model:
        lines += ["## Summary", "", model["label"], ""]
        if model["status"] == "ok":
            for key in MODEL_KEYS:
                if model.get(key):
                    lines += [
                        f"### {key.replace('_', ' ')}",
                        "",
                        *(f"- {i}" for i in model[key]),
                        "",
                    ]
        else:
            lines += [f"(dropped: {model.get('reason', 'invalid')})", ""]
    lines += ["## Recent turns", ""]
    for turn in system["recent_turns"]:
        lines.append(f"**{turn['role']}** @{turn['offset']} {turn['ts']}".rstrip())
        lines.append("")
        if turn["text"]:
            lines += [turn["text"], ""]
        for call in turn["tool_calls"]:
            lines.append(f"- tool `{call['name']}`: {call['summary']}".rstrip(": "))
        if turn["tool_calls"]:
            lines.append("")
    if pack.get("truncated"):
        lines.append(f"_(cut to {pack['max_bytes']} bytes: {', '.join(pack['truncated_parts'])})_")
    return "\n".join(lines).rstrip() + "\n"


def markdown_size(pack: dict[str, Any]) -> int:
    return len(render_markdown(pack).encode("utf-8"))


def json_size(pack: dict[str, Any]) -> int:
    return len(render_json(pack).encode("utf-8"))
