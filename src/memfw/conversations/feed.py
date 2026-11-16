"""The worker's episode queue as an episode feed for ``memfw dream skills --from-queue``.

:mod:`memfw.dream.skills.queue` reads the queue through its ``EpisodeFeed``
protocol and does not import this package (architecture A2a); the
composition (``memfw dream skills``) injects :class:`WorkerEpisodeFeed`.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from .adapter import SqliteStoreSource, store_path
from .policy import SharePolicy
from .store import ConversationSource
from .worker import EpisodeQueue, WorkState, state_path


class WorkerEpisodeFeed:
    """The conversation worker's local state, share policy and store for one vault."""

    def open(
        self, config: Mapping[str, Any], vault: Path, *, state: Any = None, source: Any = None
    ) -> tuple[Callable[[str], bool], EpisodeQueue, ConversationSource]:
        policy = SharePolicy.from_config(config)
        work = state if state is not None else WorkState(state_path(config, vault=vault))
        store = source if source is not None else SqliteStoreSource(store_path(config, vault=vault))
        return policy.processable, EpisodeQueue(work), store

    def queue(self, config: Mapping[str, Any], vault: Path, *, state: Any = None) -> EpisodeQueue:
        work = state if state is not None else WorkState(state_path(config, vault=vault))
        return EpisodeQueue(work)


__all__ = ["WorkerEpisodeFeed"]
