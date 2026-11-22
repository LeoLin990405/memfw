"""Read-only sharing of the unified conversation store with local agent apps.

Every result is redacted again on the way out (:func:`store.safe_text`),
paginated and size-capped, filtered by :class:`policy.SharePolicy` for the
calling app, and labelled as data, not instructions. There are no write
operations here.

Archive state (settled ingest) is opt-in: ``with_archive_state`` adds
``archived: bool`` to each session / result, ``archived_only`` keeps archived
sessions only. Both default off, and the output is then unchanged.

Tag filters (``task_type``, ``outcome``, ``project``, ``component``,
``intent_like``) keep sessions whose worker tags (``conv-v2``) match; a session
without tags never matches one. ``mode="two_stage"`` is the two-stage search of
:mod:`.two_stage`. With none of these the output is unchanged.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from typing import Any, TypeVar

from memfw.handoff.schema import (
    CONVERSATIONS_HANDOFF_SCHEMA,
    OMITTED_FIELD_NAMES,
    REDACTION_POLICY,
    listed_omissions,
)
from memfw.retrieval.health import HEALTH_KEY, from_conversation_search

from .policy import SharePolicy
from .store import (
    DATA_NOTICE,
    GOAL_SCAN_TURNS,
    ConversationError,
    ConversationSource,
    SessionInfo,
    Turn,
    check_app,
    check_id,
    goal_text,
    safe_text,
)
from .two_stage import (
    MODES,
    SUMMARY_CHARS,
    LabelLookup,
    parse_tags,
    session_card,
    tag_filters,
    tags_match,
    two_stage_search,
)

MAX_LIMIT = 50
SNIPPET_CHARS = 400
TURN_CHARS = 8 * 1024
DEFAULT_BYTES = 16 * 1024
MAX_BYTES = 64 * 1024
MIN_BYTES = 4096
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


def _calls(turn: Turn, omitted: set[str]) -> list[dict[str, str]]:
    """Tool calls with secrets removed. A secret name is dropped; a secret summary is emptied."""

    out = []
    for call in turn.tool_calls[:20]:
        name, name_withheld = safe_text(call.get("name", ""), 80)
        summary, summary_withheld = safe_text(call.get("summary", ""), ITEM_CHARS)
        if name_withheld or summary_withheld:
            omitted.add("tool_calls")
            if name_withheld:
                continue
            summary = ""
        out.append({"name": name, "summary": summary})
    return out


def _label_summary_tags(
    row: dict[str, Any] | None, omitted: set[str]
) -> tuple[str | None, dict[str, Any] | None]:
    """Worker summary and tags for a session card. A withheld summary is null, not invented."""

    if not row:
        return None, None
    raw = str(row.get("summary") or "")
    summary = None
    if raw:
        full, withheld = safe_text(raw)
        if withheld:
            omitted.add("summary")
        else:
            summary = full
            if len(full) > SUMMARY_CHARS:
                summary = full[: SUMMARY_CHARS - 1] + "…"
                omitted.add("summary")
            if not summary:
                summary = None
    return summary, parse_tags(row.get("tags"))


def _size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False).encode())


def _trim_bytes(text: str, limit: int) -> str:
    """``text`` cut to at most ``limit`` UTF-8 bytes (on a character boundary)."""

    data = text.encode()
    if len(data) <= limit:
        return text
    return data[: max(0, limit - 3)].decode(errors="ignore") + "…"


T = TypeVar("T")
SCAN_FACTOR = 10


def _page(
    fetch: Callable[[int, int], list[T]], keep: Callable[[T], bool], limit: int, offset: int
) -> tuple[list[T], int | None]:
    """``limit`` kept items from raw position ``offset`` and the raw offset of the next page.

    Filtering happens before paging, so a dropped row never ends a page early.
    The scan is bounded; when the bound is hit the next page starts there.
    """

    kept: list[T] = []
    raw = offset
    budget = limit * SCAN_FACTOR + 100
    batch_size = limit + 1
    while budget > 0:
        batch = fetch(min(batch_size, budget), raw)
        if not batch:
            return kept, None
        for item in batch:
            raw += 1
            budget -= 1
            if not keep(item):
                continue
            if len(kept) == limit:
                return kept, raw - 1
            kept.append(item)
        batch_size = max(batch_size, 2 * limit)
    return kept, raw


class ConversationShare:
    """Search, read and hand off shared conversations as ``caller`` sees them."""

    def __init__(
        self,
        source: ConversationSource,
        policy: SharePolicy,
        caller: str,
        *,
        summaries: SummaryLookup | None = None,
        labels: LabelLookup | None = None,
        embedder: Any = None,
        embed_cloud: bool = False,
    ) -> None:
        self.source = source
        self.policy = policy
        self.caller = check_app(caller)
        self.summaries = summaries
        #: Worker summaries and tags (tag filters, two_stage); ``None``: none.
        self.labels = labels
        #: The runtime's embedding provider (two_stage semantic list); ``None``: none.
        self.embedder = embedder
        #: ``conversations.cloud``: a non-loopback embedder may receive summaries.
        self.embed_cloud = embed_cloud

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

    def _archive_states(self, infos: Sequence[SessionInfo]) -> dict[tuple[str, str], bool]:
        lookup = getattr(self.source, "archive_state", None)
        if not callable(lookup):
            raise ConversationError("this conversation store does not record archive state")
        return dict(lookup(list(infos)))

    def _hit_states(self, hits: Sequence[Turn], states: dict[tuple[str, str], bool]) -> None:
        """Add the archive state of every session in ``hits`` not yet in ``states``."""

        infos = []
        for key in dict.fromkeys((hit.app, hit.session_id) for hit in hits):
            if key in states:
                continue
            info = self.source.session(*key)
            if info is None:
                states[key] = False
            else:
                infos.append(info)
        states.update(self._archive_states(infos))

    # -- operations -------------------------------------------------------------

    def list_sessions(
        self,
        *,
        app: str | None = None,
        agent: str | None = None,
        since: str | None = None,
        limit: int = 20,
        offset: int = 0,
        with_archive_state: bool = False,
        archived_only: bool = False,
    ) -> dict[str, Any]:
        self._ready()
        limit = _clamp(limit, 1, MAX_LIMIT)
        offset = max(0, int(offset))
        include, exclude = self.policy.narrow(self.caller, self._requested(app, agent))
        states: dict[tuple[str, str], bool] = {}
        tracked = with_archive_state or archived_only

        def fetch(count: int, position: int) -> list[SessionInfo]:
            rows = self.source.sessions(
                apps=include, exclude=exclude, since=since, limit=count, offset=position
            )
            if tracked:
                states.update(self._archive_states(rows))
            return rows

        def keep(row: SessionInfo) -> bool:
            if not self.policy.allowed(self.caller, row.app):
                return False
            return not archived_only or states.get((row.app, row.session_id), False)

        rows, next_offset = _page(fetch, keep, limit, offset)
        sessions = []
        for row in rows:
            item = row.as_dict()
            if with_archive_state:
                item["archived"] = states.get((row.app, row.session_id), False)
            sessions.append(item)
        return {"notice": DATA_NOTICE, "sessions": sessions, "next_offset": next_offset}

    def search(
        self,
        query: str,
        *,
        app: str | None = None,
        agent: str | None = None,
        since: str | None = None,
        limit: int = 10,
        offset: int = 0,
        with_archive_state: bool = False,
        archived_only: bool = False,
        mode: str | None = None,
        task_type: str | None = None,
        outcome: str | None = None,
        project: str | None = None,
        component: str | None = None,
        intent_like: str | None = None,
        windows: int | None = None,
        window_turns: int | None = None,
        max_bytes: int | None = None,
    ) -> dict[str, Any]:
        self._ready()
        query = str(query or "").strip()[:500]
        mode = mode or "default"
        if mode not in MODES:
            raise ConversationError(f"mode must be one of: {', '.join(MODES)}")
        filters = tag_filters(
            task_type=task_type,
            outcome=outcome,
            project=project,
            component=component,
            intent_like=intent_like,
        )
        if mode == "two_stage":
            return two_stage_search(
                self,
                query,
                app=app,
                agent=agent,
                since=since,
                filters=filters,
                archived_only=archived_only,
                limit=limit,
                offset=offset,
                windows=2 if windows is None else windows,
                window_turns=1 if window_turns is None else window_turns,
                max_bytes=DEFAULT_BYTES if max_bytes is None else max_bytes,
                min_bytes=MIN_BYTES,
                max_cap=MAX_BYTES,
                max_limit=MAX_LIMIT,
            )
        if windows is not None or window_turns is not None or max_bytes is not None:
            raise ConversationError("windows, window_turns and max_bytes need mode=two_stage")
        if not query:
            raise ConversationError("query is required")
        limit = _clamp(limit, 1, MAX_LIMIT)
        offset = max(0, int(offset))
        include, exclude = self.policy.narrow(self.caller, self._requested(app, agent))
        terms = query.split()
        states: dict[tuple[str, str], bool] = {}
        tracked = with_archive_state or archived_only

        def fetch(count: int, position: int) -> list[Turn]:
            hits = self.source.search(
                query, apps=include, exclude=exclude, since=since, limit=count, offset=position
            )
            if tracked:
                self._hit_states(
                    [hit for hit in hits if self.policy.allowed(self.caller, hit.app)], states
                )
            return hits

        tagged: dict[tuple[str, str], bool] = {}

        def session_matches(key: tuple[str, str]) -> bool:
            if key not in tagged:
                rows = (
                    self.labels.session_labels(
                        keys=[key], apps=include, exclude=exclude, filters=filters, limit=1
                    )
                    if self.labels is not None
                    else None
                )
                # Re-checked on the parsed, canonical tags (as two_stage does).
                tagged[key] = bool(rows) and tags_match(
                    parse_tags((rows or [{}])[0].get("tags")), filters
                )
            return tagged[key]

        def keep(hit: Turn) -> bool:
            # A turn that would be withheld is not a hit: no probing for its content.
            if not self.policy.allowed(self.caller, hit.app) or safe_text(hit.text)[1]:
                return False
            if filters and not session_matches((hit.app, hit.session_id)):
                return False
            return not archived_only or states.get((hit.app, hit.session_id), False)

        hits, next_offset = _page(fetch, keep, limit, offset)
        if not hits:
            next_offset = None  # an empty page never hints at filtered matches
        omitted: set[str] = set()
        keys = list(dict.fromkeys((hit.app, hit.session_id) for hit in hits))
        label_rows: dict[tuple[str, str], dict[str, Any]] = {}
        if keys and self.labels is not None:
            rows = self.labels.session_labels(keys=keys, limit=len(keys))
            if rows:
                for row in rows:
                    label_rows[(str(row["app"]), str(row["session_id"]))] = row
        infos: dict[tuple[str, str], SessionInfo | None] = {}
        for key in keys:
            if key not in infos:
                infos[key] = self.source.session(*key)
        results = []
        for hit in hits:
            text, _ = safe_text(hit.text)
            key = (hit.app, hit.session_id)
            info = infos.get(key)
            summary, tags = _label_summary_tags(label_rows.get(key), omitted)
            if summary is None and key not in label_rows and self.summaries is not None:
                raw = self.summaries(*key)
                if raw:
                    summary, tags = _label_summary_tags({"summary": raw, "tags": None}, omitted)
            results.append(
                {
                    "ref": hit.ref,
                    "app": hit.app,
                    "session_id": hit.session_id,
                    "offset": hit.offset,
                    "role": hit.role,
                    "ts": hit.ts,
                    "snippet": _snippet(text, terms),
                    "session_card": session_card(
                        app=hit.app,
                        session_id=hit.session_id,
                        started_at=None if info is None else info.started_at,
                        updated_at=None if info is None else info.updated_at,
                        turns=None if info is None else info.turns,
                        summary=summary,
                        tags=tags,
                        suggested_start=hit.offset,
                    ),
                }
            )
            if with_archive_state:
                results[-1]["archived"] = states.get((hit.app, hit.session_id), False)
        payload: dict[str, Any] = {
            "notice": DATA_NOTICE,
            "results": results,
            "next_offset": next_offset,
            "redaction_policy": REDACTION_POLICY,
            "omitted_fields": listed_omissions(omitted),
        }
        # Newest-first keyword turns: no score; only an empty page changes it.
        payload[HEALTH_KEY] = from_conversation_search(payload)
        return payload

    def get(
        self,
        session_id: str,
        *,
        app: str | None = None,
        start: int = 0,
        end: int | None = None,
        max_bytes: int = DEFAULT_BYTES,
        with_archive_state: bool = False,
        archived_only: bool = False,
        around: int | None = None,
        radius: int | None = None,
    ) -> dict[str, Any]:
        self._ready()
        info = self._resolve(session_id, app)
        session = info.as_dict()
        if with_archive_state or archived_only:
            archived = self._archive_states([info]).get((info.app, info.session_id), False)
            if archived_only and not archived:
                raise ConversationError("session is not archived")
            if with_archive_state:
                session["archived"] = archived
        center_offset: int | None = None
        center_index: int | None = None
        if around is not None:
            # ``--around`` is a store offset (ref after ``@``, session_card.suggested_start),
            # not a turn index. start/end are ignored. A missing offset is an error, not [].
            if isinstance(around, bool) or not isinstance(around, int) or around < 0:
                raise ConversationError("around must be a store offset (integer >= 0)")
            if radius is None:
                radius = 2
            elif isinstance(radius, bool) or not isinstance(radius, int) or radius < 0:
                raise ConversationError("radius must be an integer >= 0")
            found = self._turn_index_for_offset(info, around)
            if found is None:
                raise ConversationError(f"around offset {around} is not a turn in this session")
            center_offset = around
            center_index = found
            start = max(0, found - radius)
            end = found + radius + 1  # inclusive window, radius turns each side
        elif radius is not None:
            raise ConversationError("radius requires around")
        else:
            start = max(0, int(start))
        stop = start + MAX_RANGE if end is None else _clamp(int(end), start, start + MAX_RANGE)
        cap = _clamp(max_bytes, MIN_BYTES, MAX_BYTES)
        turns = self.source.turns(info.app, info.session_id, start=start, limit=stop - start)
        out: list[dict[str, Any]] = []
        omitted: set[str] = set()
        frame = {
            "notice": DATA_NOTICE,
            "session": session,
            "turns": [],
            "truncated": True,
            "next_start": 10**9,
            "redaction_policy": REDACTION_POLICY,
            "omitted_fields": list(OMITTED_FIELD_NAMES),
        }
        if center_offset is not None:
            # Wider than any real offset/index, so writing the real pair cannot overflow.
            frame["center_offset"] = 10**18
            frame["center_index"] = 10**9
        used = _size(frame)
        truncated = False
        for position, turn in enumerate(turns, start=start):
            full, withheld = safe_text(turn.text)
            withheld = withheld or turn.withheld
            if withheld:
                text = ""
                omitted.add("text")
                calls: list[dict[str, str]] = []
                if turn.tool_calls:
                    omitted.add("tool_calls")
            else:
                text = full if len(full) <= TURN_CHARS else full[: TURN_CHARS - 1] + "…"
                if text != full:
                    omitted.add("text")
                calls = _calls(turn, omitted)
            item: dict[str, Any] = {
                "index": position,
                "ref": turn.ref,
                "role": turn.role,
                "ts": turn.ts,
                "text": text,
                "withheld": withheld,
                "tool_calls": calls,
            }
            size = _size(item) + 2
            if used + size > cap:
                truncated = True
                if out:
                    break
                # The first turn alone is too big: drop its tool calls, then cut its text.
                if item["tool_calls"]:
                    omitted.add("tool_calls")
                item["tool_calls"] = []
                item["text"] = ""
                omitted.add("text")
                room = cap - used - _size(item) - 8
                source_text = "" if withheld else full
                while room > 0:
                    item["text"] = _trim_bytes(source_text, room)
                    size = _size(item) + 2
                    if used + size <= cap:
                        break
                    room -= max(16, used + size - cap)
                if room <= 0:
                    break
            out.append(item)
            used += size
            if truncated:
                break
        next_index = start + len(out)
        limit_end = info.turns if end is None else min(int(end), info.turns)
        payload = {
            "notice": DATA_NOTICE,
            "session": session,
            "turns": out,
            "truncated": truncated,
            # Nothing fitted: no cursor, so a paging client cannot loop on the same start.
            "next_start": next_index if out and next_index < limit_end else None,
            "redaction_policy": REDACTION_POLICY,
            "omitted_fields": listed_omissions(omitted),
        }
        if center_offset is not None:
            # Both coordinates of the window center: store offset, and which turn it is.
            payload["center_offset"] = center_offset
            payload["center_index"] = center_index
        return payload

    def _turn_index_for_offset(self, info: SessionInfo, offset: int) -> int | None:
        """0-based index of the turn stored at ``offset``, or None.

        Offsets increase with the index but are not contiguous (a JSONL byte
        offset, or a gapped ordinal). ``start``/``end`` stay indexes.
        """

        lo = 0
        hi = info.turns
        while lo < hi:
            mid = (lo + hi) // 2
            page = self.source.turns(info.app, info.session_id, start=mid, limit=1)
            if not page:
                return None
            found = page[0].offset
            if found == offset:
                return mid
            if found < offset:
                lo = mid + 1
            else:
                hi = mid
        return None

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
        if target is not None:
            try:
                target_allowed = self.policy.allowed(target, info.app)
            except ConversationError:
                target_allowed = False
            if not target_allowed:
                # Generic on purpose: another app's opt-out status is not disclosed.
                raise ConversationError(f"app {target} may not read this session")
        cap = _clamp(max_bytes, MIN_BYTES, MAX_BYTES)
        recent = _clamp(recent, 1, 50)
        # The latest turns (a long session's head is not what "recent" means).
        start = max(0, int(info.turns) - HANDOFF_MAX_TURNS)
        turns = self.source.turns(info.app, info.session_id, start=start, limit=HANDOFF_MAX_TURNS)
        # The goal comes from the session's head: a small bounded read when the
        # tail above does not start at the first turn.
        head = (
            turns[:GOAL_SCAN_TURNS]
            if start == 0
            else self.source.turns(info.app, info.session_id, start=0, limit=GOAL_SCAN_TURNS)
        )
        goal = ""
        found = goal_text([turn.text for turn in head if turn.role == "user" and not turn.withheld])
        if found:
            goal, _ = safe_text(found, GOAL_CHARS)
        talk = [turn for turn in turns if turn.role in {"user", "assistant"}]
        omitted: set[str] = set()
        recent_turns = []
        for turn in talk[-recent:]:
            full, withheld = safe_text(turn.text)
            withheld = withheld or turn.withheld
            if withheld:
                text = ""
                omitted.add("text")
            else:
                text = full if len(full) <= RECENT_CHARS else full[: RECENT_CHARS - 1] + "…"
                if text != full:
                    omitted.add("text")
            recent_turns.append(
                {
                    "ref": turn.ref,
                    "role": turn.role,
                    "ts": turn.ts,
                    "text": text,
                    "withheld": withheld,
                }
            )
        files_touched = self._files(turns)
        pack: dict[str, Any] = {
            "schema": CONVERSATIONS_HANDOFF_SCHEMA,
            "notice": DATA_NOTICE,
            "kind": "handoff",
            "for_agent": target,
            "source": {**info.as_dict(), "turns_read": len(turns), "turns_start": start},
            "system": {
                "label": "[system] derived by code from the stored, redacted session",
                "goal": goal,
                "files_touched": files_touched,
                "files": list(files_touched),
                "decisions": self._sentences(talk, _DECISION),
                "open_questions": self._questions(talk[-20:]),
                "constraints": [],
                "next_step": "",
                "evidence_refs": [turn["ref"] for turn in recent_turns],
                "recent_turns": recent_turns,
            },
            "redaction_policy": REDACTION_POLICY,
            "omitted_fields": list(OMITTED_FIELD_NAMES),
        }
        summary = self.summaries(info.app, info.session_id) if self.summaries else None
        if summary:
            text, withheld = safe_text(summary, 4096)
            if withheld:
                omitted.add("summary")
            elif text:
                pack["model_summarized"] = {
                    "label": "[model_summarized: not verified]",
                    "summary": text,
                }
        pack["truncated"] = True  # sized with the flag present
        pack["truncated"] = self._fit(pack, cap, omitted)
        pack["omitted_fields"] = listed_omissions(omitted)
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
                    # safe_text applies the full redact() (home-directory names included).
                    cleaned, withheld = safe_text(match, 300)
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
            if withheld or turn.withheld:
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
            if withheld or turn.withheld:
                continue
            for sentence in _SENTENCE.findall(text):
                item = sentence.strip()
                if item.endswith("?") and item[:ITEM_CHARS] not in found:
                    found.append(item[:ITEM_CHARS])
        return found[-MAX_ITEMS:]

    @staticmethod
    def _fit(pack: dict[str, Any], cap: int, omitted: set[str]) -> bool:
        """Trim to ``cap`` bytes: recent turns, then files, then the model part.

        ``files`` and ``evidence_refs`` stay aligned with what remains. Refs are the
        turn refs still in ``recent_turns`` (``app:session@offset``), never new text.
        """

        system = pack["system"]
        truncated = False

        def sync() -> None:
            system["files"] = list(system["files_touched"])
            system["evidence_refs"] = [turn["ref"] for turn in system["recent_turns"]]

        while _size(pack) > cap and system["recent_turns"]:
            system["recent_turns"].pop(0)
            sync()
            omitted.add("recent_turns")
            truncated = True
        while _size(pack) > cap and system["files_touched"]:
            system["files_touched"].pop()
            sync()
            omitted.add("files_touched")
            omitted.add("files")
            truncated = True
        if _size(pack) > cap and "model_summarized" in pack:
            del pack["model_summarized"]
            omitted.add("model_summarized")
            truncated = True
        for key in ("decisions", "open_questions"):
            while _size(pack) > cap and system[key]:
                system[key].pop(0)
                omitted.add(key)
                truncated = True
        if _size(pack) > cap:
            shortened = system["goal"][: max(0, len(system["goal"]) - (_size(pack) - cap))]
            if shortened != system["goal"]:
                system["goal"] = shortened
                omitted.add("goal")
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
