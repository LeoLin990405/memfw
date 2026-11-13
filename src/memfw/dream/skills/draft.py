"""Draft a SKILL.md for a verified procedure; optionally let a model word it.

The default draft is deterministic: the name comes from the request keywords
and each step from its signature. An operator may name a *drafter*
(``module:function``, typically a wrapper around a model) that rewords the
draft. Its output is checked like transcript text, verify-first: every step
it returns must cite at least one verified step id, uncited or unsafe steps
are dropped, and if too little survives the deterministic draft is kept.
"""

from __future__ import annotations

import importlib
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, cast

import yaml

from memfw.history import has_secret, redact

from .distill import Procedure
from .private import PrivateTerms

Drafter = Callable[[dict[str, Any]], dict[str, Any]]
"""``drafter(procedure) -> {"name", "description", "when_to_use", "steps"}``.

``procedure`` holds keywords, verified steps (``id``, ``signature``,
``sessions``) and counts; never transcript text. Each returned step is
``{"text": str, "evidence": [step ids]}``.
"""

NAME_CHARS = 64
DESCRIPTION_CHARS = 1024
TEXT_CHARS = 400
_SLUG = re.compile(r"[^a-z0-9]+")
_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_URL = re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://\S+")
_PATH = re.compile(r"(?:^|(?<=[\s=:'\"(`,]))(?:~|\.{1,2})?/[^\s'\"`;,)]*|[A-Za-z]:\\\S*")
_HOME = re.compile(r"(?i)\b(?:Users|home)[/\\]")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


class UnsafeText(ValueError):
    """Text that still carries a secret or a path after cleaning."""


def clean(text: str, limit: int = TEXT_CHARS) -> str:
    """Redacted, path-free, single-line text; raises :class:`UnsafeText`."""

    value = _CONTROL.sub(" ", str(text))
    value = redact(value)
    value = _URL.sub("<url>", value)
    value = _PATH.sub("<path>", value)
    value = " ".join(value.split())[:limit].strip()
    if has_secret(value) or _HOME.search(value) or "REDACTED" in value or "withheld" in value:
        raise UnsafeText("text still carries a secret or a home path")
    return value


def slug(words: list[str]) -> str:
    joined = "-".join(_SLUG.sub("-", word.lower()).strip("-") for word in words)
    value = re.sub(r"-{2,}", "-", joined).strip("-")[:NAME_CHARS].strip("-")
    return value


@dataclass
class Draft:
    name: str
    description: str
    when_to_use: str
    steps: list[dict[str, Any]]
    drafter: str = "deterministic"
    notes: list[str] = field(default_factory=list)


def _step_text(signature: str) -> str:
    kind, _, value = signature.partition(":")
    if kind == "cmd":
        return f"Run `{value}`"
    return f"Use the `{value}` tool"


def deterministic(procedure: Procedure) -> Draft:
    words = [word for word in procedure.keywords if _SLUG.sub("", word.lower())][:3]
    tool_words = [step.signature.partition(":")[2].split()[0] for step in procedure.steps]
    name = slug(words or tool_words[:2]) or "procedure"
    sessions = len(procedure.sessions)
    agents = ", ".join(procedure.agents) or "unknown"
    summary = "; ".join(_step_text(step.signature) for step in procedure.steps)
    topic = ", ".join(procedure.keywords) or "the same steps"
    return Draft(
        name=name,
        description=clean(
            f"Repeated procedure seen in {sessions} sessions (agents: {agents}): {summary}.",
            DESCRIPTION_CHARS,
        ),
        when_to_use=clean(f"When a task is about {topic}.", DESCRIPTION_CHARS),
        steps=[
            {"text": clean(_step_text(step.signature)), "evidence": [step.key]}
            for step in procedure.steps
        ],
    )


def _mentions(text: str, signature: str) -> bool:
    """Whether step text names a program, sub-command or tool of a verified signature."""

    names = set(signature.partition(":")[2].split()) - {"run", "exec", "x"}
    words = {word.lower() for word in re.findall(r"[A-Za-z0-9_.+-]+", text)}
    return bool(names & words)


def load_drafter(spec: str | None) -> Drafter | None:
    """An optional local drafter ``module:function``; off unless named."""

    if not spec:
        return None
    module_name, _, attribute = spec.partition(":")
    if not module_name or not attribute:
        raise ValueError("drafter must look like module:function")
    function = getattr(importlib.import_module(module_name), attribute, None)
    if function is None or not callable(function):
        raise ValueError("drafter is not callable")
    return cast(Drafter, function)


def drafter_input(procedure: Procedure, min_sessions: int) -> dict[str, Any]:
    return {
        "keywords": list(procedure.keywords),
        "steps": [
            {"id": step.key, "signature": step.signature, "sessions": step.sessions}
            for step in procedure.steps
        ],
        "sessions": len(procedure.sessions),
        "agents": dict(procedure.agents),
        "min_sessions": min_sessions,
    }


def apply_drafter(
    procedure: Procedure,
    base: Draft,
    drafter: Drafter,
    *,
    min_steps: int,
    min_sessions: int,
    private: PrivateTerms | None = None,
) -> Draft:
    """The drafter's wording where it is safe and cited; the base draft otherwise.

    The drafter never sees a private term: its input is masked first.
    """

    notes: list[str] = []
    payload = drafter_input(procedure, min_sessions)
    if private:
        payload = private.mask_all(payload)
    try:
        proposed = drafter(payload)
    except Exception as exc:  # noqa: BLE001 - a local hook must not break the run
        base.notes.append(f"drafter failed: {type(exc).__name__}")
        return base
    if not isinstance(proposed, dict):
        base.notes.append("drafter returned no mapping")
        return base
    known = {step.key: step for step in procedure.steps}
    used: set[str] = set()
    steps: list[dict[str, Any]] = []
    raw_steps = proposed.get("steps")
    for item in raw_steps if isinstance(raw_steps, list) else []:
        if not isinstance(item, dict):
            notes.append("dropped a malformed step")
            continue
        cited = item.get("evidence")
        cited_ids = sorted(
            {str(value) for value in cited if str(value) in known}
            if isinstance(cited, list)
            else set()
        )
        if not cited_ids:
            notes.append("dropped a step without verified evidence")
            continue
        try:
            text = clean(str(item.get("text") or ""))
        except UnsafeText:
            notes.append("dropped a step with unsafe text")
            continue
        if not text:
            notes.append("dropped an empty step")
            continue
        if not any(_mentions(text, known[key].signature) for key in cited_ids):
            notes.append("dropped a step that does not name its cited command or tool")
            continue
        if set(cited_ids) <= used:
            notes.append("dropped a step that repeats cited evidence")
            continue
        used.update(cited_ids)
        steps.append({"text": text, "evidence": cited_ids})
    if len(steps) < min_steps:
        base.notes.extend([*notes, "drafter steps rejected: too few verified steps"])
        return base
    draft = Draft(
        name=base.name,
        description=base.description,
        when_to_use=base.when_to_use,
        steps=steps,
        drafter="hook",
        notes=notes,
    )
    name = slug([str(proposed.get("name") or "")])
    if _NAME.match(name):
        draft.name = name
    else:
        notes.append("kept the deterministic name")
    for key, limit in (("description", DESCRIPTION_CHARS), ("when_to_use", DESCRIPTION_CHARS)):
        try:
            value = clean(str(proposed.get(key) or ""), limit)
        except UnsafeText:
            notes.append(f"kept the deterministic {key}: unsafe text")
            continue
        if value:
            setattr(draft, key, value)
    return draft


def render_skill(candidate_id: str, draft: Draft, procedure: Procedure, confidence: float) -> str:
    """SKILL.md: ``name`` / ``description`` frontmatter, then when-to-use and steps."""

    front = {"name": draft.name, "description": draft.description}
    dumped = yaml.safe_dump(front, allow_unicode=True, sort_keys=False, width=1000).rstrip()
    steps = procedure.steps
    by_id = {step.key: step for step in steps}
    lines = [
        f"# {draft.name}",
        "",
        "## When to use",
        "",
        draft.when_to_use,
        "",
        "## Steps",
        "",
    ]
    for number, step in enumerate(draft.steps, start=1):
        support = max(by_id[key].sessions for key in step["evidence"])
        lines.append(
            f"{number}. {step['text']} (evidence: {', '.join(step['evidence'])}; "
            f"{support} sessions)"
        )
    lines += [
        "",
        "## Evidence",
        "",
        f"- Candidate `{candidate_id}`, confidence {confidence:.2f}: proposal only, not "
        "installed or approved.",
        f"- {len(procedure.sessions)} sessions; agents: "
        + ", ".join(f"{agent} ({count})" for agent, count in procedure.agents.items())
        + ".",
        "- Per-step refs are in `evidence.json` (`memfw history read REF`).",
    ]
    return f"---\n{dumped}\n---\n\n" + "\n".join(lines) + "\n"
