"""Tag filters and the two-stage conversation search (M3c).

Stage 1 gathers candidate sessions from three lists:

* ``fts``: sessions with turns matching every query term (the store's search);
* ``semantic``: sessions whose "summary + tags" text is close to the query,
  through the embedder the runtime already has (``MEMFW_EMBEDDING_*``, the one
  ``memory_search`` hybrid uses). Without one this list is empty and the
  response says ``degraded: ["no_embedder"]``;
* ``tags``: sessions whose ``conv-v2`` tags match the tag filters.

Every list is cut to the caller's visible apps (share policy, the requested
app) before anything is ranked or counted, and the filters (tags, ``since``,
``archived_only``) apply to every candidate: a session without tags never
matches a tag filter. The lists are fused by reciprocal rank.

Stage 2 returns, per session: its info, archive state, the newest worker
summary and tags (model output, labelled ``untrusted_model_output``) and up to
``windows`` redacted turn windows around hits, all within ``max_bytes``.
Nothing here writes anywhere.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import threading
import time
from array import array
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Any, Protocol

from memfw.notes.http import is_loopback_url
from memfw.ports.models import (
    TAG_INTENT_CHARS,
    TAG_ITEM_CHARS,
    TAG_OUTCOMES,
    TASK_TYPES,
    normalize_tags,
)

from .store import DATA_NOTICE, ConversationError, SessionInfo, Turn, safe_text, ts_key

if TYPE_CHECKING:
    from .share import ConversationShare

MODES = ("default", "two_stage")
TAG_FILTERS = ("task_type", "outcome", "project", "component", "intent_like")

TWO_STAGE_NOTICE = (
    DATA_NOTICE + " Summaries and tags are unverified model output (untrusted_model_output)."
)
MODEL_LABEL = "[model_summarized: not verified]"

FTS_POOL = 200
"""Most visible turn hits stage 1 reads from the store's search."""
LABEL_POOL = 500
"""Most tagged sessions the tag list takes (newest summaries first)."""
SEMANTIC_POOL = 200
"""Most session summaries embedded per call (newest first, filters applied)."""
SEMANTIC_KEEP = 50
"""Most sessions the semantic list contributes."""
RRF_K = 60
HITS_PER_SESSION = 20
SCAN_TURNS = 1000
"""Turns read from a session without a store hit to find query terms for windows."""
SCAN_BUDGET = 5000
"""Turns read for that, across all sessions of one call."""
DEFAULT_WINDOWS = 2
MAX_WINDOWS = 5
DEFAULT_WINDOW_TURNS = 1
MAX_WINDOW_TURNS = 3
WINDOW_TURN_CHARS = 1200
SUMMARY_CHARS = 1024
_SINCE_CHARS = "0123456789-:TZ.+ "


class LabelLookup(Protocol):
    """Session summaries and tags from the worker (``WorkState.session_labels``)."""

    def session_labels(
        self,
        *,
        keys: Sequence[tuple[str, str]] | None = None,
        apps: Sequence[str] | None = None,
        exclude: Sequence[str] = (),
        filters: Mapping[str, str] | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]] | None: ...


def _clean(value: Any, limit: int) -> str:
    return " ".join(str(value).split()).lower()[:limit].strip()


def tag_filters(
    *,
    task_type: str | None = None,
    outcome: str | None = None,
    project: str | None = None,
    component: str | None = None,
    intent_like: str | None = None,
) -> dict[str, str]:
    """The given tag filters, normalised like the tags themselves (lower case)."""

    out: dict[str, str] = {}
    if task_type:
        value = _clean(task_type, 16)
        if value not in TASK_TYPES:
            raise ConversationError(f"task_type must be one of: {', '.join(TASK_TYPES)}")
        out["task_type"] = value
    if outcome:
        value = _clean(outcome, 16)
        if value not in TAG_OUTCOMES:
            raise ConversationError(f"outcome must be one of: {', '.join(TAG_OUTCOMES)}")
        out["outcome"] = value
    for name, raw in (("project", project), ("component", component)):
        if raw:
            value = _clean(raw, TAG_ITEM_CHARS)
            if value:
                out[name] = value
    if intent_like:
        value = _clean(intent_like, TAG_INTENT_CHARS)
        if value:
            out["intent_like"] = value
    return out


def since_key(since: str | None) -> str | None:
    """``since`` as a :func:`ts_key` (the store's own rule for the value)."""

    if not since:
        return None
    text = str(since).strip()
    if len(text) > 40 or any(char not in _SINCE_CHARS for char in text):
        raise ConversationError("since must be an ISO-8601 timestamp or date")
    return ts_key(text)


def parse_tags(raw: Any) -> dict[str, Any] | None:
    """Stored tags in canonical form, redacted again; ``None`` when absent or invalid."""

    if not raw:
        return None
    try:
        tags = normalize_tags(json.loads(str(raw)))
    except ValueError:
        return None
    if tags is None:
        return None
    out: dict[str, Any] = {}
    for key, value in tags.items():
        if isinstance(value, list):
            items = [safe_text(item, TAG_ITEM_CHARS) for item in value]
            out[key] = [text for text, withheld in items if not withheld and text]
        else:
            text, withheld = safe_text(value, TAG_INTENT_CHARS)
            out[key] = "" if withheld else text
    return out


def label_text(summary: str, tags: Mapping[str, Any] | None) -> str:
    """The text the semantic list embeds for one session: summary plus tags."""

    parts = [summary]
    if tags:
        parts.append(
            "; ".join(
                f"{key}: {', '.join(value) if isinstance(value, list) else value}"
                for key, value in tags.items()
                if value
            )
        )
    return "\n".join(part for part in parts if part)


def tags_match(tags: Mapping[str, Any] | None, filters: Mapping[str, str]) -> bool:
    """Whether parsed ``tags`` (:func:`parse_tags`) pass every filter (none: True)."""

    if not filters:
        return True
    if not tags:
        return False
    for name, value in filters.items():
        if name in {"task_type", "outcome"} and tags.get(name) != value:
            return False
        if name == "project" and value not in tags.get("project", []):
            return False
        if name == "component" and value not in tags.get("components", []):
            return False
        if name == "intent_like" and value not in str(tags.get("intent", "")):
            return False
    return True


# -- embeddings ----------------------------------------------------------------------

CACHE_BYTES = 32 * 1024 * 1024
"""Bound on the per-process document vector cache (packed float32)."""
EMBED_BUDGET_SECONDS = 8.0
"""Total time one call may spend in the embedder (query plus documents), no retries."""
EMBED_BATCH = 64
"""Most uncached summaries embedded in one call; the rest wait for a later call."""

_cache: OrderedDict[tuple[str, str], bytes] = OrderedDict()
_cache_bytes = 0
_cache_lock = threading.Lock()


class EmbedderTimeout(RuntimeError):
    """The embedder did not answer within ``EMBED_BUDGET_SECONDS``."""


def embedder_is_local(embedder: Any) -> bool:
    """Whether ``embedder`` keeps text on this machine.

    ``local is True`` on the provider, or an ``endpoint`` URL whose host is a
    canonical loopback literal (:func:`memfw.notes.http.is_loopback_url`, the
    strict privacy rule; ``open_http`` goes direct for these and a few more
    forms of this host). Anything else (no
    endpoint attribute, another host, an unparseable URL) is not local: the semantic list then needs
    ``conversations.cloud: true``, the switch every non-local backend needs.
    """

    if getattr(embedder, "local", None) is True:
        return True
    endpoint = getattr(embedder, "endpoint", None)
    if not isinstance(endpoint, str) or not endpoint:
        return False
    return is_loopback_url(endpoint)


def _bounded(embedder: Any) -> Any:
    """``embedder`` without retries and with its own timeout cut to the budget."""

    if not hasattr(embedder, "retries") and not hasattr(embedder, "timeout"):
        return embedder
    bounded = copy.copy(embedder)
    if hasattr(bounded, "retries"):
        bounded.retries = 0
    timeout = getattr(bounded, "timeout", None)
    if isinstance(timeout, (int, float)):
        bounded.timeout = min(float(timeout), EMBED_BUDGET_SECONDS)
    return bounded


def _within(deadline: float, work: Callable[[], Any]) -> Any:
    """``work()`` on a daemon thread; :class:`EmbedderTimeout` past ``deadline``."""

    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise EmbedderTimeout("embedder budget spent")
    box: dict[str, Any] = {}

    def run() -> None:
        try:
            box["value"] = work()
        except BaseException as exc:  # noqa: BLE001 - re-raised in the caller
            box["error"] = exc

    thread = threading.Thread(target=run, name="memfw-conv-embed", daemon=True)
    thread.start()
    thread.join(remaining)
    if thread.is_alive():
        raise EmbedderTimeout("embedder did not answer in time")
    if "error" in box:
        raise box["error"]
    return box["value"]


def _pack(vector: Sequence[float]) -> bytes:
    return array("f", [float(value) for value in vector]).tobytes()


def _unpack(data: bytes) -> list[float]:
    values = array("f")
    values.frombytes(data)
    return values.tolist()


def _cache_put(key: tuple[str, str], data: bytes) -> None:
    global _cache_bytes
    old = _cache.pop(key, None)
    if old is not None:
        _cache_bytes -= len(old)
    _cache[key] = data
    _cache_bytes += len(data)
    while _cache_bytes > CACHE_BYTES and _cache:
        _, dropped = _cache.popitem(last=False)
        _cache_bytes -= len(dropped)


def _embedder_key(embedder: Any) -> str:
    marker = getattr(embedder, "fingerprint", "") or getattr(embedder, "model", "")
    return f"{type(embedder).__name__}:{marker or id(embedder)}"


def _embed_documents(
    embedder: Any, texts: Sequence[str], *, deadline: float, owner: str | None = None
) -> list[list[float] | None]:
    """Document vectors, memoised per process (an MCP server answers many calls).

    Only uncached texts are sent, at most ``EMBED_BATCH`` per call; the rest
    stay ``None`` this call (not scored) and are embedded by a later one.
    """

    owner = owner or _embedder_key(embedder)
    keys = [(owner, hashlib.sha256(text.encode()).hexdigest()) for text in texts]
    out: list[list[float] | None] = [None] * len(texts)
    missing: list[int] = []
    with _cache_lock:
        for position, key in enumerate(keys):
            data = _cache.get(key)
            if data is None:
                missing.append(position)
            else:
                _cache.move_to_end(key)
                out[position] = _unpack(data)
    missing = missing[:EMBED_BATCH]
    if missing:
        batch = [texts[position] for position in missing]
        vectors = _within(deadline, lambda: embedder.embed(batch, role="document"))
        if len(vectors) != len(missing):
            raise ValueError("embedder returned the wrong number of vectors")
        with _cache_lock:
            for position, vector in zip(missing, vectors, strict=True):
                data = _pack(vector)
                out[position] = _unpack(data)
                _cache_put(keys[position], data)
    return out


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right) or not left:
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    norm = math.sqrt(sum(a * a for a in left)) * math.sqrt(sum(b * b for b in right))
    return dot / norm if norm else 0.0


# -- the search ----------------------------------------------------------------------


def _clamp(value: Any, low: int, high: int) -> int:
    return max(low, min(int(value), high))


def _size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False).encode())


class _Search:
    """One two-stage call as ``share.caller`` sees the store."""

    def __init__(
        self,
        share: ConversationShare,
        query: str,
        *,
        app: str | None,
        agent: str | None,
        since: str | None,
        filters: dict[str, str],
        archived_only: bool,
    ) -> None:
        self.share = share
        self.query = query
        self.terms = [term.lower() for term in query.split() if term][:8]
        self.filters = filters
        self.archived_only = archived_only
        self.since = since
        self.since_key = since_key(since)
        self.include, self.exclude = share.policy.narrow(share.caller, share._requested(app, agent))
        self.degraded: list[str] = []
        self.infos: dict[tuple[str, str], SessionInfo | None] = {}
        self.labels: dict[tuple[str, str], dict[str, Any] | None] = {}
        self.archived: dict[tuple[str, str], bool | None] = {}
        #: Turns left for scanning sessions without a store hit (all sessions together).
        self.scan_budget = SCAN_BUDGET
        self.hits: dict[tuple[str, str], list[int]] = {}

    # -- visibility and filters, applied to every candidate before ranking ----------

    def _visible(self, app: str) -> bool:
        if app in self.exclude or (self.include is not None and app not in self.include):
            return False
        return self.share.policy.allowed(self.share.caller, app)

    def _info(self, key: tuple[str, str]) -> SessionInfo | None:
        if key not in self.infos:
            self.infos[key] = self.share.source.session(*key)
        return self.infos[key]

    def _label_rows(self, **kwargs: Any) -> list[dict[str, Any]]:
        lookup = self.share.labels
        rows = None if lookup is None else lookup.session_labels(**kwargs)
        if rows is None:
            if "no_summaries" not in self.degraded:
                self.degraded.append("no_summaries")
            return []
        return [row for row in rows if self._visible(str(row["app"]))]

    def _remember(self, row: Mapping[str, Any]) -> None:
        key = (str(row["app"]), str(row["session_id"]))
        summary, withheld = safe_text(str(row.get("summary") or ""), SUMMARY_CHARS)
        self.labels[key] = {
            "summary": "" if withheld else summary,
            "tags": parse_tags(row.get("tags")),
            "prompt_version": str(row.get("prompt_version") or ""),
        }

    def _label(self, key: tuple[str, str]) -> dict[str, Any] | None:
        if key not in self.labels:
            rows = self._label_rows(keys=[key], apps=self.include, exclude=self.exclude)
            if rows:
                self._remember(rows[0])
            else:
                self.labels[key] = None
        return self.labels[key]

    def _tags_match(self, tags: Mapping[str, Any] | None) -> bool:
        return tags_match(tags, self.filters)

    def _admit(self, key: tuple[str, str]) -> bool:
        """Visible, still in the store, and passing every filter."""

        if not self._visible(key[0]):
            return False
        info = self._info(key)
        if info is None:
            return False
        if self.since_key and (ts_key(info.updated_at) or "") < self.since_key:
            return False
        if self.filters:
            label = self._label(key)
            if label is None or not self._tags_match(label["tags"]):
                return False
        if self.archived_only and not self._archived(key):
            return False
        return True

    def _archived(self, key: tuple[str, str]) -> bool | None:
        """The session's archive state; ``None`` when the store cannot tell."""

        if key not in self.archived:
            info = self._info(key)
            lookup = getattr(self.share.source, "archive_state", None)
            if info is None or not callable(lookup):
                if self.archived_only:
                    raise ConversationError("this conversation store does not record archive state")
                self.archived[key] = None
            else:
                self.archived.update(lookup([info]))
        return self.archived.get(key)

    # -- the three lists ------------------------------------------------------------

    def fts(self) -> list[tuple[str, str]]:
        if not self.terms:
            return []
        rows = self.share.source.search(
            self.query,
            apps=self.include,
            exclude=self.exclude,
            since=self.since,
            limit=FTS_POOL,
            offset=0,
        )
        order: list[tuple[str, str]] = []
        for hit in rows:
            key = (hit.app, hit.session_id)
            # A turn that would be withheld is not a hit: no probing for its content.
            if not self._visible(hit.app) or safe_text(hit.text)[1]:
                continue
            if key not in self.hits:
                if not self._admit(key):
                    continue
                self.hits[key] = []
                order.append(key)
            if len(self.hits[key]) < HITS_PER_SESSION:
                self.hits[key].append(hit.offset)
        return order

    def tagged(self) -> list[tuple[str, str]]:
        if not self.filters:
            return []
        out = []
        for row in self._label_rows(
            apps=self.include, exclude=self.exclude, filters=self.filters, limit=LABEL_POOL
        ):
            key = (str(row["app"]), str(row["session_id"]))
            self._remember(row)
            if self._admit(key):
                out.append(key)
        return out

    def semantic(self, embedder: Any, *, cloud: bool) -> list[tuple[str, str]]:
        if embedder is None:
            self.degraded.append("no_embedder")
            return []
        if not cloud and not embedder_is_local(embedder):
            # Summaries would leave the machine: same switch as a non-local backend.
            self.degraded.append("semantic_off")
            return []
        if not self.terms:
            return []
        rows = self._label_rows(
            apps=self.include, exclude=self.exclude, filters=self.filters, limit=SEMANTIC_POOL
        )
        keys: list[tuple[str, str]] = []
        texts: list[str] = []
        for row in rows:
            key = (str(row["app"]), str(row["session_id"]))
            self._remember(row)
            label = self.labels[key] or {}
            text = label_text(label.get("summary", ""), label.get("tags"))
            if text and self._admit(key):
                keys.append(key)
                texts.append(text)
        if not keys:
            return []
        bounded = _bounded(embedder)
        deadline = time.monotonic() + EMBED_BUDGET_SECONDS
        try:
            (query_vector,) = _within(deadline, lambda: bounded.embed([self.query], role="query"))
            vectors = _embed_documents(
                bounded, texts, deadline=deadline, owner=_embedder_key(embedder)
            )
        except Exception:  # noqa: BLE001 - an embedder failure degrades, never fails
            self.degraded.append("embedder_error")
            return []
        scored = [
            (_cosine(query_vector, vector), key)
            for key, vector in zip(keys, vectors, strict=True)
            if vector is not None
        ]
        scored = [item for item in scored if item[0] > 0]
        scored.sort(key=lambda item: -item[0])
        return [key for _, key in scored[:SEMANTIC_KEEP]]

    # -- stage 2 --------------------------------------------------------------------

    def _hit_offsets(self, key: tuple[str, str], info: SessionInfo) -> list[int]:
        if key in self.hits:
            return self.hits[key]
        if not self.terms:
            return []
        found: list[int] = []
        allowance = min(SCAN_TURNS, self.scan_budget)
        if allowance <= 0:
            return found
        turns = self.share.source.turns(*key, start=0, limit=allowance)
        self.scan_budget -= len(turns)
        for turn in turns:
            if turn.withheld or not turn.text:
                continue
            text, withheld = safe_text(turn.text)
            if withheld:
                continue
            lowered = text.lower()
            if any(term in lowered for term in self.terms):
                found.append(turn.offset)
                if len(found) >= HITS_PER_SESSION:
                    break
        return found

    def windows(
        self, key: tuple[str, str], info: SessionInfo, count: int, around: int
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        covered: set[int] = set()
        for offset in self._hit_offsets(key, info):
            if len(out) >= count:
                break
            if offset in covered:
                continue
            turns = self._around(key, info, offset, around)
            if not turns:
                continue
            covered.update(turn.offset for turn in turns)
            out.append(
                {
                    "hit_offset": offset,
                    "turns": [self._window_turn(turn) for turn in turns],
                }
            )
        return out

    def _around(
        self, key: tuple[str, str], info: SessionInfo, offset: int, around: int
    ) -> list[Turn]:
        # Offsets are usually contiguous: guess the position, then check.
        guess = max(0, min(offset - info.first_offset, max(0, info.turns - 1)))
        slack = 8
        start = max(0, guess - around - slack)
        turns = self.share.source.turns(*key, start=start, limit=2 * (around + slack) + 1)
        positions = [index for index, turn in enumerate(turns) if turn.offset == offset]
        if not positions:
            return []
        at = positions[0]
        return turns[max(0, at - around) : at + around + 1]

    @staticmethod
    def _window_turn(turn: Turn) -> dict[str, Any]:
        text, withheld = safe_text(turn.text, WINDOW_TURN_CHARS)
        withheld = withheld or turn.withheld
        return {
            "offset": turn.offset,
            "ref": turn.ref,
            "role": turn.role,
            "ts": turn.ts,
            "text": "" if withheld else text,
            "withheld": withheld,
        }

    def model_output(self, key: tuple[str, str]) -> dict[str, Any] | None:
        label = self._label(key)
        if label is None or (not label["summary"] and not label["tags"]):
            return None
        return {
            "untrusted_model_output": True,
            "label": MODEL_LABEL,
            "prompt_version": label["prompt_version"],
            "summary": label["summary"],
            "tags": label["tags"],
        }


def _shrink(item: dict[str, Any], room: int) -> int:
    """Cut ``item`` towards ``room`` bytes; returns its size afterwards.

    In order: the model summary (cut), its tags (dropped), the model output
    (dropped), then ``cwd`` and ``project`` (emptied). Ids, times and ranks stay.
    """

    model = item.get("model_output")
    if isinstance(model, dict) and _size(item) > room:
        summary = str(model.get("summary") or "")
        over = _size(item) - room
        if summary and over < len(summary.encode()):
            data = summary.encode()[: max(0, len(summary.encode()) - over - 3)]
            model["summary"] = data.decode(errors="ignore") + "…"
        else:
            model["summary"] = ""
    if isinstance(model, dict) and _size(item) > room:
        model["tags"] = None
    if _size(item) > room:
        item["model_output"] = None
    for name in ("cwd", "project"):
        if _size(item) > room and item.get(name):
            item[name] = ""
    return _size(item)


def _fuse(lists: Mapping[str, Sequence[tuple[str, str]]]) -> list[tuple[tuple[str, str], float]]:
    scores: dict[tuple[str, str], float] = {}
    for items in lists.values():
        for rank, key in enumerate(items, start=1):
            scores[key] = scores.get(key, 0.0) + 1.0 / (RRF_K + rank)
    return sorted(scores.items(), key=lambda item: (-item[1], item[0]))


def two_stage_search(
    share: ConversationShare,
    query: str,
    *,
    app: str | None = None,
    agent: str | None = None,
    since: str | None = None,
    filters: dict[str, str] | None = None,
    archived_only: bool = False,
    limit: int = 10,
    offset: int = 0,
    windows: int = DEFAULT_WINDOWS,
    window_turns: int = DEFAULT_WINDOW_TURNS,
    max_bytes: int = 16 * 1024,
    min_bytes: int = 4096,
    max_cap: int = 64 * 1024,
    max_limit: int = 50,
) -> dict[str, Any]:
    """See the module docstring. ``share`` has already checked the caller may read."""

    filters = dict(filters or {})
    if not query and not filters:
        raise ConversationError("two_stage needs a query or a tag filter")
    limit = _clamp(limit, 1, max_limit)
    offset = max(0, int(offset))
    count = _clamp(windows, 0, MAX_WINDOWS)
    around = _clamp(window_turns, 0, MAX_WINDOW_TURNS)
    cap = _clamp(max_bytes, min_bytes, max_cap)
    search = _Search(
        share,
        query,
        app=app,
        agent=agent,
        since=since,
        filters=filters,
        archived_only=archived_only,
    )
    lists = {
        "fts": search.fts(),
        "semantic": search.semantic(share.embedder, cloud=share.embed_cloud),
        "tags": search.tagged(),
    }
    fused = _fuse(lists)
    members = {name: set(items) for name, items in lists.items()}
    page = fused[offset : offset + limit]
    response: dict[str, Any] = {
        "notice": TWO_STAGE_NOTICE,
        "mode": "two_stage",
        "untrusted_model_output": True,
        "degraded": search.degraded,
        "filters": filters,
        "candidates": {
            **{name: len(items) for name, items in lists.items()},
            "total": len(fused),
        },
        "sessions": [],
        "truncated": False,
        "next_offset": offset + limit if offset + limit < len(fused) else None,
    }
    # Sized with the widest cursor and flag, so setting them later never overflows.
    used = _size({**response, "truncated": True, "next_offset": 10**9})
    for position, (key, score) in enumerate(page):
        info = search._info(key)
        if info is None:  # admitted above, so only a store change in between
            continue
        item = {
            **info.as_dict(),
            "archived": search._archived(key),
            "rank": offset + position + 1,
            "score": round(score, 6),
            "matched_by": [name for name in lists if key in members[name]],
            "model_output": search.model_output(key),
            "windows": [],
        }
        size = _size(item) + 2
        if used + size > cap and not response["sessions"]:
            # The first session alone is too big: shrink it rather than stall the cursor.
            size = _shrink(item, cap - used - 2) + 2
            response["truncated"] = True
            if used + size > cap:
                # Nothing fits even shrunk: skip it so a paging client moves on.
                response["next_offset"] = offset + position + 1
                break
        elif used + size > cap:
            response["truncated"] = True
            response["next_offset"] = offset + position
            break
        response["sessions"].append(item)
        used += size
    for item in response["sessions"]:
        if not count:
            break
        key = (item["app"], item["session_id"])
        info = search._info(key)
        if info is None:
            continue
        for window in search.windows(key, info, count, around):
            size = _size(window) + 2
            if used + size > cap:
                response["truncated"] = True
                continue
            item["windows"].append(window)
            used += size
    return response


__all__ = [
    "MODES",
    "TAG_FILTERS",
    "TWO_STAGE_NOTICE",
    "EmbedderTimeout",
    "LabelLookup",
    "embedder_is_local",
    "label_text",
    "parse_tags",
    "since_key",
    "tag_filters",
    "tags_match",
    "two_stage_search",
]
