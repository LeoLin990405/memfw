"""``history_list``, ``history_search`` and ``history_read`` (read-only transcripts)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from memfw.history import HistoryLimits, TranscriptIndex, default_db, sources_from_config
from memfw.history.index import split_ref
from memfw.mcp.base import GatewayBase
from memfw.mcp.tools_conversations import OPERATOR, SharePolicy


class HistoryTools(GatewayBase):
    """Transcript history tools, scoped by the conversation share rules."""

    def _history(self) -> TranscriptIndex:
        """Transcript index from ``history_sources`` in vault.yaml.

        Refreshed incrementally, at most once per ``MEMFW_HISTORY_REFRESH_SECONDS``
        (default 30); concurrent refreshes are serialised by the index's lock.
        """
        return self._history_scoped()[0]

    def _history_scoped(self) -> tuple[TranscriptIndex, frozenset[str] | None]:
        """The index and the source names this caller may read (``None``: all).

        The same visibility as the conversation tools: a source named after an app
        in ``conversation_optout.agents`` or with ``readable: false`` is never
        returned, and the caller's own rule applies (opted out or ``can_read:
        false`` is refused; a ``can_read`` list narrows the sources). The index
        keeps every configured source, so hidden ones are filtered per query.
        """
        repository = self._repo()
        config = repository.read_config()
        sources = sources_from_config(config)
        if not sources:
            raise ValueError("no history_sources configured in vault.yaml")
        allowed = self._history_allowed(SharePolicy.from_config(config), sources)
        index = TranscriptIndex(
            default_db(repository.root), sources, limits=HistoryLimits.from_config(config)
        )
        # Never wait on another refresh: serve the existing index instead.
        index.refresh(min_interval=self.history_refresh_seconds, blocking=False)
        return index, allowed

    def _history_allowed(
        self, policy: SharePolicy, sources: Sequence[Any]
    ) -> frozenset[str] | None:
        caller = (self.app or "").strip().lower()
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

    @staticmethod
    def _history_source(source: str, allowed: frozenset[str] | None) -> None:
        if source and allowed is not None and source not in allowed:
            # Same answer as a source that does not exist: nothing leaks.
            raise ValueError(f"unknown history source: {source}")

    def history_list(
        self,
        *,
        source: str = "",
        session: str = "",
        since: str = "",
        until: str = "",
        limit: int = 50,
    ) -> dict[str, Any]:
        index, allowed = self._history_scoped()
        self._history_source(source, allowed)
        if source or allowed is None:
            listed = index.list(
                source=source or None,
                session=session or None,
                since=since or None,
                until=until or None,
                limit=limit,
            )
            if allowed is not None and "stats" in listed:
                # Restricted caller: count only what it may see.
                listed["stats"] = index.stats([source] if source else allowed)
            return listed
        parts = [
            index.list(
                source=name,
                session=session or None,
                since=since or None,
                until=until or None,
                limit=limit,
            )
            for name in sorted(allowed)
        ]
        bounded = max(1, min(int(limit), 500))
        if session:
            entries = [entry for part in parts for entry in part["entries"]][:bounded]
            windows: dict[str, dict[str, Any]] = {}
            for entry in entries:
                window = windows.setdefault(
                    entry["window_id"],
                    {
                        "window_id": entry["window_id"],
                        "parent_window_id": entry["parent_window_id"],
                        "entries": 0,
                        "first_ref": entry["ref"],
                    },
                )
                window["entries"] += 1
            return {"session_id": session, "windows": list(windows.values()), "entries": entries}
        sessions = [item for part in parts for item in part["sessions"]]
        sessions.sort(key=lambda item: (item["source"], item["session_id"]))
        sessions.sort(key=lambda item: item["started_at"] or "", reverse=True)
        return {"sessions": sessions[:bounded], "stats": index.stats(allowed)}

    def history_search(
        self,
        query: str,
        *,
        source: str = "",
        session: str = "",
        since: str = "",
        until: str = "",
        limit: int = 10,
    ) -> dict[str, Any]:
        index, allowed = self._history_scoped()
        self._history_source(source, allowed)
        if source or allowed is None:
            return index.search(
                query,
                source=source or None,
                session=session or None,
                since=since or None,
                until=until or None,
                limit=limit,
            )
        parts = [
            index.search(
                query,
                source=name,
                session=session or None,
                since=since or None,
                until=until or None,
                limit=limit,
            )
            for name in sorted(allowed)
        ]
        results = [hit for part in parts for hit in part["results"]]
        results.sort(key=lambda hit: hit.get("timestamp") or "", reverse=True)
        results.sort(key=lambda hit: float(hit.get("score") or 0.0), reverse=True)
        return {
            "query": query,
            "fallback": any(part["fallback"] for part in parts),
            "results": results[: max(1, min(int(limit), 100))],
        }

    def history_read(
        self, ref: str, *, before: int = 2, after: int = 2, max_bytes: int = 8192
    ) -> dict[str, Any]:
        index, allowed = self._history_scoped()
        if allowed is not None:
            self._history_source(split_ref(ref)[0], allowed)
        return index.read(ref, before=before, after=after, max_bytes=max_bytes)
