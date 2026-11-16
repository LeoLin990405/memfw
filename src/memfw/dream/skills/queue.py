"""Episodes for ``memfw dream skills --from-queue``: the conversation worker's queue.

The asynchronous conversation worker (:mod:`memfw.conversations.worker`)
queues one ``episode`` per processed session in its local state. Episodes
carry ids and offsets only; the turns are read from the unified conversation
store through the same read-only source the share tools use, so the worker's
per-app ``process`` policy has already decided which sessions are offered.

Each stored turn becomes one entry in the shape :func:`.episodes.segment`
reads (``snippet``, ``role``, ``ref``, ``agent``, ``timestamp``): its redacted
text, then one ``[tool NAME] SUMMARY`` line per tool call. Withheld turns
contribute nothing. Refs are ``conv:APP:SESSION@OFFSET`` (read with
``memfw conversations get``).

A procedure must repeat across ``min_sessions`` sessions, which one run of
the queue rarely holds, so each run reads the newly pending episodes plus the
newest already consumed ones (up to ``limit`` sessions in all). Staging is
idempotent (same evidence, same candidate id; a known procedure only gets
later evidence appended), so re-reading consumed episodes stages nothing twice.

A dry run only peeks; a real run claims the pending episodes and acks them
once staging finished without an error, or releases them back to pending, so
a failed run leaves them for the next one.

This module does not import :mod:`memfw.conversations` (architecture A2a): the
worker's queue, its share policy and the store reader come from an injected
:class:`EpisodeFeed` (``memfw dream skills`` passes
:class:`memfw.conversations.feed.WorkerEpisodeFeed`). Turn text is redacted
with :func:`memfw.history.transcripts.safe_text`.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from memfw.history.transcripts import ConversationSource, safe_text

from .boilerplate import BoilerplateFilter
from .episodes import Episode, segment

CONSUMER = "dream-skills"
MAX_EPISODES = 200
STALE_CLAIM = timedelta(hours=6)
"""A claim older than this belongs to a consumer that died; it is taken back."""


class EpisodeQueuePort(Protocol):
    """The conversation worker's episode queue (ids, offsets and states only)."""

    def pending(self, limit: int = 50) -> list[dict[str, Any]]: ...

    def claim(self, consumer: str, limit: int = 10, *, now: str | None = None) -> list[str]: ...

    def ack(self, episode_id: str) -> bool: ...

    def release(self, episode_id: str) -> bool: ...

    def rows(self, episode_ids: Sequence[str]) -> list[dict[str, Any]]: ...

    def reclaim(self, before: str) -> int: ...

    def consumed(self, limit: int = 50) -> list[dict[str, Any]]: ...


class EpisodeFeed(Protocol):
    """Opens the worker's queue, its share policy and the store for one vault.

    ``state`` and ``source`` override the worker state and the store reader
    (tests). ``open`` returns ``(processable, queue, store)``: whether the
    share policy still lets the worker process an app, the queue, and the
    read-only transcript reader.
    """

    def open(
        self, config: Mapping[str, Any], vault: Path, *, state: Any = None, source: Any = None
    ) -> tuple[Callable[[str], bool], EpisodeQueuePort, ConversationSource]: ...

    def queue(
        self, config: Mapping[str, Any], vault: Path, *, state: Any = None
    ) -> EpisodeQueuePort: ...


@dataclass
class QueueBatch:
    episodes: list[Episode] = field(default_factory=list)
    claimed: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    info: dict[str, Any] = field(default_factory=dict)


def _entries(turns: list[Any]) -> list[dict[str, Any]]:
    entries = []
    for turn in turns:
        if getattr(turn, "withheld", False):
            continue
        text, withheld = safe_text(turn.text)
        if withheld:
            continue
        lines = [text] if text else []
        for call in turn.tool_calls:
            name, name_withheld = safe_text(str(call.get("name") or ""), 64)
            summary, summary_withheld = safe_text(str(call.get("summary") or ""), 400)
            if name and not name_withheld and not summary_withheld:
                lines.append(f"[tool {name}] {summary}".rstrip())
        entries.append(
            {
                "snippet": "\n".join(lines),
                "role": turn.role,
                "ref": f"conv:{turn.ref}",
                "agent": turn.app,
                "timestamp": turn.ts,
            }
        )
    return entries


def collect_queue(
    config: Mapping[str, Any],
    vault: Path,
    *,
    feed: EpisodeFeed,
    dry_run: bool,
    limit: int = MAX_EPISODES,
    state: Any = None,
    source: Any = None,
    now: datetime | None = None,
    boilerplate: BoilerplateFilter | None = None,
) -> QueueBatch:
    """Episodes of the pending (or, unless ``dry_run``, newly claimed) queue entries.

    Sessions of apps the share policy no longer lets the worker process
    (opted out, hidden, ``process: false``) are not read; their claimed
    episodes are still settled. If reading fails after the claim, the claims
    are released before the error propagates. Claims older than
    ``STALE_CLAIM`` (a crashed consumer's) are returned to pending first.

    With ``boilerplate``, the sessions are read twice: pass 1 counts the user
    and system texts repeated across sessions, pass 2 segments with them
    stripped, so the whole corpus is never held in memory.
    """

    processable, queue, store = feed.open(config, vault, state=state, source=source)
    batch = QueueBatch(info={"source": "episode_queue", "available": True, "dry_run": dry_run})
    if not store.available():
        batch.info.update(available=False, reason="conversation store not found")
        return batch
    current = now or datetime.now(UTC)
    if dry_run:
        rows = queue.pending(limit)
    else:
        queue.reclaim((current - STALE_CLAIM).isoformat())
        batch.claimed = queue.claim(CONSUMER, limit, now=current.isoformat())
        rows = queue.rows(batch.claimed)
    try:
        counts = {
            "episodes_pending": len(rows),
            "episodes_claimed": len(batch.claimed),
            "sessions_read": 0,
            "entries_read": 0,
            "sessions_not_processable": 0,
        }
        sessions = {(str(row["app"]), str(row["session_id"])) for row in rows}
        for row in queue.consumed(limit):
            key = (str(row["app"]), str(row["session_id"]))
            if len(sessions) >= limit:
                break
            if key not in sessions:
                sessions.add(key)
                rows.append(row)
        readable: list[tuple[str, str, int]] = []
        for row in rows:
            app, session_id = str(row["app"]), str(row["session_id"])
            if not processable(app):
                counts["sessions_not_processable"] += 1
                continue
            info = store.session(app, session_id)
            if info is None:
                continue
            readable.append((app, session_id, max(1, info.turns)))
        if boilerplate is not None:
            for app, session_id, total in readable:
                turns = store.turns(app, session_id, start=0, limit=total)
                # Only user and system text is counted: skip redacting the rest twice.
                asked = [turn for turn in turns if turn.role in {"user", "system"}]
                for entry in _entries(asked):
                    boilerplate.observe((app, session_id), app, entry["snippet"])
        for app, session_id, total in readable:
            entries = _entries(store.turns(app, session_id, start=0, limit=total))
            counts["sessions_read"] += 1
            counts["entries_read"] += len(entries)
            batch.episodes.extend(segment(app, session_id, entries, boilerplate))
    except BaseException:
        _settle(batch, ok=False, queue=queue)
        raise
    batch.counts = counts
    return batch


def settle(
    batch: QueueBatch,
    *,
    ok: bool,
    config: Mapping[str, Any],
    vault: Path,
    feed: EpisodeFeed,
    state: Any = None,
) -> int:
    """Ack the claimed episodes (``ok``) or release them to pending. Returns the count."""

    if not batch.claimed:
        return 0
    return _settle(batch, ok=ok, queue=feed.queue(config, vault, state=state))


def _settle(batch: QueueBatch, *, ok: bool, queue: EpisodeQueuePort) -> int:
    if not batch.claimed:
        return 0
    settle_one = queue.ack if ok else queue.release
    return sum(1 for episode_id in batch.claimed if settle_one(episode_id))
