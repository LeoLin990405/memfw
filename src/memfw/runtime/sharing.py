"""The conversation share and the transcript history, as one caller sees them (A3b).

Moved from ``memfw.mcp.tools_conversations`` and ``tools_history``: opening
the share as the calling app, and the history index with the sources that
app may read (the share's visibility rules). Messages are unchanged.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from memfw.conversations import OPERATOR, SharePolicy, open_share
from memfw.core.repository import Repository
from memfw.history import HistoryLimits, TranscriptIndex, default_db, sources_from_config
from memfw.history.index import split_ref


def check_conversation_app(app: str) -> None:
    """Refuse a missing app name and the reserved operator name."""

    if not app:
        raise ValueError("conversation tools need the calling app: pass --app or MEMFW_APP")
    if app.strip().lower() == OPERATOR:
        raise ValueError(f"'{OPERATOR}' is reserved for the memfw CLI; name the app instead")


def history_allowed(app: str, policy: SharePolicy, sources: Sequence[Any]) -> frozenset[str] | None:
    """The source names ``app`` may read (``None``: all of them)."""

    caller = (app or "").strip().lower()
    if caller and caller in policy.optout:
        raise ValueError(f"app {caller} has opted out of conversation sharing")
    can_read = True if caller == OPERATOR else policy.rule(caller).can_read
    if can_read is False:
        raise ValueError(f"app {caller or '(unnamed)'} may not read shared conversations")
    hidden = set(policy.hidden())
    include = None if can_read is True else set(can_read)
    names = [str(source.name) for source in sources]
    allowed = frozenset(
        name
        for name in names
        if name.strip().lower() not in hidden
        and (include is None or name.strip().lower() in include)
    )
    return None if len(allowed) == len(names) else allowed


def history_scoped(
    repository: Repository, app: str, *, refresh_seconds: float
) -> tuple[TranscriptIndex, frozenset[str] | None]:
    """The transcript index from ``history_sources`` and the sources ``app`` may read.

    Refreshed incrementally, at most once per ``refresh_seconds``, never
    waiting on another refresh (the existing index is served instead).
    """

    config = repository.read_config()
    sources = sources_from_config(config)
    if not sources:
        raise ValueError("no history_sources configured in vault.yaml")
    allowed = history_allowed(app, SharePolicy.from_config(config), sources)
    index = TranscriptIndex(
        default_db(repository.root), sources, limits=HistoryLimits.from_config(config)
    )
    index.refresh(min_interval=refresh_seconds, blocking=False)
    return index, allowed


def ref_source(ref: str) -> str:
    """The source name of a history ``ref``."""

    return split_ref(ref)[0]


__all__ = [
    "check_conversation_app",
    "history_allowed",
    "history_scoped",
    "open_share",
    "ref_source",
]
