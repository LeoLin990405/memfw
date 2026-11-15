"""Session labels shared by the conversation readers: origin and injected text.

``origin`` says who drove a session:

* ``interactive``: a person, in the interactive home (the default);
* ``worker``: any session found under a Conductor Worker home (set from the
  :class:`~memfw.history.sources.base.StoreRoot`, not by a reader);
* ``automated``: started by a program, not a person: a CLI run in exec /
  headless / print mode, or a session whose first prompt is a Conductor
  dispatch or Ticket prompt (:func:`prompt_origin`);
* ``subagent``: a session another agent session spawned (Claude sidechains,
  Codex sub-agent rollouts, Cursor ``task_v2`` agents ...).

A reader sets :attr:`ConversationRecord.origin` when its store records how a
session was started; the ingest step (:mod:`memfw.history.conversations`)
also checks the first prompt with :func:`prompt_origin` and combines both
with the root's origin through :func:`merge_origin`, once per session.

:func:`strip_injected` removes harness wrappers (``<system-reminder>``,
instruction-file echoes, ``<user_query>`` ...) so labels and intents are
derived from what was asked. It reuses :mod:`memfw.history.injected`,
the structural filter the dream skills use too.
"""

from __future__ import annotations

import re

from ..injected import has_content, strip_structural

INTERACTIVE = "interactive"
WORKER = "worker"
AUTOMATED = "automated"
SUBAGENT = "subagent"
SESSION_ORIGINS = (INTERACTIVE, WORKER, AUTOMATED, SUBAGENT)

PROMPT_SCAN_CHARS = 4000
"""Only the start of a first prompt is checked for a dispatch marker."""

_DISPATCH_MARKERS = re.compile(
    r"Ticket delivery contract"
    r"|You review one work line of a parallel dispatch"
    r"|You are in a git worktree of the Conductor repo \(branch already checked out"
    r"|<goal_round\b",
)
"""Conductor dispatch prompts: the Ticket delivery contract a Ticket worker
gets, the dispatch line reviewer prompt, the dispatch line worker prompt, and
a goal-round continuation wrapper. Each is generated, never typed."""


def strip_injected(text: str) -> str:
    """``text`` without harness-injected blocks and wrappers (see module doc)."""

    return strip_structural(text)


def first_request(texts: list[str] | tuple[str, ...]) -> str:
    """The first of ``texts`` that still has content once injected text is stripped."""

    for text in texts:
        residual = strip_injected(text)
        if has_content(residual):
            return residual
    return ""


def prompt_origin(text: str) -> str:
    """``automated`` when ``text`` (a session's first prompt) is a Conductor dispatch prompt.

    The raw text is checked as well as the stripped one: a goal-round wrapper
    is itself a marker and stripping unwraps it.
    """

    head = str(text or "")[:PROMPT_SCAN_CHARS]
    if _DISPATCH_MARKERS.search(head):
        return AUTOMATED
    if _DISPATCH_MARKERS.search(strip_injected(head)):
        return AUTOMATED
    return ""


_RANK = {"": 0, INTERACTIVE: 0, AUTOMATED: 1, WORKER: 2, SUBAGENT: 3}


def merge_origin(root_origin: str, *detected: str) -> str:
    """The session's origin: the most specific of the root's and the detected ones.

    ``subagent`` beats everything; a Worker home's ``worker`` (already an
    automated run, and more specific) beats ``automated``; ``automated``
    beats ``interactive``. Unknown values are ignored.
    """

    best = root_origin if root_origin in _RANK else INTERACTIVE
    for value in detected:
        if value in _RANK and _RANK[value] > _RANK.get(best, 0):
            best = value
    return best or INTERACTIVE


__all__ = [
    "AUTOMATED",
    "INTERACTIVE",
    "SESSION_ORIGINS",
    "SUBAGENT",
    "WORKER",
    "first_request",
    "merge_origin",
    "prompt_origin",
    "strip_injected",
]
