"""C4: evaluate the session-start context pack on a read-only vault snapshot.

``memfw eval context`` runs this. It measures, per slot (``core``,
``handoff``, ``topics`` and ``current``) and for the pack as a whole.
``current`` (U6) is measured only when the snapshot's ``vault.yaml`` enables
it (``runtime.context.current``) or ``--current`` simulates that switch
(``settings.current_simulated``); otherwise it is one ``unavailable`` row:

**coverage**
    Of the memories the session's searches returned later, the share that
    was already in the opening pack. ``core`` (and any slot of memory
    entries) covers a memory by carrying it; ``topics`` covers the memories
    about a listed concept and ``handoff`` the memories whose evidence cites
    a listed session (``coverage_kind: pointer``: the pack pointed at them,
    it did not carry them).
**waste**
    Of the items in the pack, the share a session never used. "Used" is a
    C5 ``memory_used`` signal of that session when a usage store is given
    (``waste_basis: memory_used``); otherwise it is estimated as "returned by
    one of the session's searches" (``estimated_from_hits``).
**latency**
    P50 and P95 of the real slot providers (the ones ``memory_context``
    runs, from ``Runtime.context_slot_provider``), ``repeats`` builds each,
    cold: the runtime has no C3 context cache, so every ``core`` build reads
    the vault (``settings.latency: cold``; a served pack can be faster).
    A row whose P95 exceeds the slot's ``memory_context`` deadline
    (``CONTEXT_SLOT_TIMEOUTS``, reported as ``deadline_ms``) is flagged
    ``over_deadline: true``; the deadlines are not changed.
**token cost**
    The slot's ``used_tokens_est`` (the budget's own estimate).

The searches of a session come, in this order of preference, from the
snapshot's ``memory_access`` events of search operations (grouped by
``details.session_id`` when present, else by ``session_gap_minutes`` of
silence), from the usage store's ``memory_used`` signals (one session per
``session_ref``), or from a synthetic query set: ``sessions`` x ``queries``
queries drawn with ``seed`` from the visible memories and run through the
read-only FTS search. The report names the basis it used.

The pack is rebuilt for every slot cap and every resident-prefix cap of the
sweep. The resident prefix is simulated, not implemented: slots are taken in
order while their tokens fit under the cap; the first one that does not fit
and every one after it are "demoted" (they would be a catalogue line and a
``memory_search(mode=two_stage)`` hint, not resident content), and cost and
cover nothing in that row. Nothing here changes a default: the evaluation
only reports numbers for the defaults to be decided on.

**Read-only.** The snapshot is never opened in place. The evaluation
refuses a snapshot that would need ``memfw recover`` (a pending transaction
or event append), one that contains a symlink, and an output path inside it, copies it once into a private
temporary directory (without its search index and lock file) and runs every
provider and search there; the copy is deleted afterwards. The snapshot, the
conversation store and the usage store (with their ``-wal``, ``-shm`` and
``-journal`` sidecars) are fingerprinted (path, size, mtime) before and
after; a change fails the run (``SnapshotChanged``) and no report is
returned. The stores are never opened where they are: each is copied with
its present sidecars into the private directory (a WAL store's last writes
may be only in its ``-wal``) and the copy is opened read-only (``mode=ro``).

**Privacy.** The report holds counts, ratios, token estimates, timings,
slot names and cap values only: no memory body, title, query text, session
id, path or tenant name. The slots apply the same visibility as
``memory_context``: tenant, allowed sources, privacy clearance and the
``compat_fts``/``strict`` policy; ``handoff`` applies ``conversation_share``
for ``app`` and reads model summaries only from an explicit ``--state`` file
(copied and fingerprinted like the stores), never from the worker state
``vault.yaml`` names. Memories the caller may not see are never counted as hits
(only their number is reported, as ``hits_dropped``).

**Deterministic.** The same snapshot, stores, arguments and seed give the
same report, except the ``latency_ms`` and ``over_deadline`` fields
(``strip_timings`` removes them for comparison).
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import random
import re
import shutil
import tempfile
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from itertools import product
from pathlib import Path
from typing import Any

from memfw.context import (
    CURRENT_SLOT,
    HANDOFF_SLOT,
    SLOT_NAMES,
    TOPICS_SLOT,
    HandoffSlotProvider,
    Keep,
    SlotUnavailable,
)
from memfw.context.slots import degraded_reason
from memfw.core.models import Event, EventKind, MemoryDocument
from memfw.core.repository import Repository
from memfw.ports.context import ContextEntry, ContextItem, ContextSlot, SlotRequest

from .runtime import RECALL_POLICIES, Runtime, build_runtime
from .settings import resolve_settings

SCHEMA = "memfw.context_eval/1"
#: The slots evaluated by default, in pack order. ``current`` (U6) is
#: ``unavailable`` unless the snapshot's ``vault.yaml`` enables it
#: (``runtime.context.current``) or ``EvalOptions.current`` simulates it.
EVAL_SLOTS: tuple[str, ...] = (*SLOT_NAMES, CURRENT_SLOT)
#: Slots whose items point at memories (topics, sessions) instead of carrying them.
POINTER_SLOTS = frozenset({HANDOFF_SLOT, TOPICS_SLOT})
#: ``memory_context``'s default ``limit_tokens`` (unchanged by this evaluation).
DEFAULT_LIMIT_TOKENS = 2000
DEFAULT_SLOT_CAPS: tuple[int, ...] = (500, 1000, 2000, 4000)
DEFAULT_RESIDENT_CAPS: tuple[int, ...] = (1000, 2000, 4000, 6000)
DEFAULT_SEED = 17
DEFAULT_SESSIONS = 20
DEFAULT_QUERIES = 5
DEFAULT_REPEATS = 5
DEFAULT_SESSION_GAP_MINUTES = 30.0
#: Hits per synthetic query (the ``memory_search`` default page).
SYNTHETIC_LIMIT = 10
#: A synthetic query fetches this many pages, so hits the caller may not see
#: (dropped after the search) do not take the place of visible ones.
SYNTHETIC_OVERFETCH = 4
#: At most this many sweep rows (slot-cap combinations x resident caps).
MAX_SWEEP_ROWS = 5000
#: ``memory_access`` operations written by a search (not by a get or a curation step).
SEARCH_OPERATIONS = frozenset({"fts_retrieve", "hybrid_retrieve", "two_stage_retrieve", "retrieve"})
#: Snapshot files that mean "a write was interrupted": reading would need ``memfw recover``.
PENDING_MARKERS = (".pending.json", ".event-append.pending.json")
#: Not copied: the disposable search index (rebuilt in the copy when needed) and the lock.
_COPY_IGNORED = frozenset(
    {"index.sqlite3", "index.sqlite3-wal", "index.sqlite3-shm", "index.sqlite3-journal"}
    | {".memfw.lock"}
)
#: SQLite files beside a store: copied with it and fingerprinted (a WAL store's last
#: writes may live only in ``-wal``; opening the original would create ``-shm``).
STORE_SIDECARS = ("-wal", "-shm", "-journal")
_WORD = re.compile(r"\w{3,}", re.UNICODE)
_SETTINGS_ENV = ("MEMFW_APP", "MEMFW_TENANT", "MEMFW_ALLOWED_SOURCES", "MEMFW_MCP_CLEARANCE")


class SnapshotRefused(ValueError):
    """The snapshot (or an output path) is refused before anything is read."""


class SnapshotChanged(RuntimeError):
    """The snapshot or a store changed while the evaluation ran: no report."""


# --- the inputs ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EvalOptions:
    """What one evaluation run is asked for (``memfw eval context`` options)."""

    policy: str = "compat_fts"
    tenant: str | None = None
    allowed_sources: tuple[str, ...] | None = None
    clearance: str | None = None
    app: str | None = None
    slots: tuple[str, ...] = EVAL_SLOTS
    #: Per slot, the caps to sweep; a slot not named takes ``default_slot_caps``.
    slot_caps: Mapping[str, tuple[int, ...]] = field(default_factory=dict)
    default_slot_caps: tuple[int, ...] = DEFAULT_SLOT_CAPS
    resident_caps: tuple[int, ...] = DEFAULT_RESIDENT_CAPS
    seed: int = DEFAULT_SEED
    sessions: int = DEFAULT_SESSIONS
    queries: int = DEFAULT_QUERIES
    repeats: int = DEFAULT_REPEATS
    session_gap_minutes: float = DEFAULT_SESSION_GAP_MINUTES
    now: datetime | None = None
    #: Simulate ``runtime.context.current: true`` (``--current``); off, the vault decides.
    current: bool = False
    #: An explicit (real) environment for the settings fallback; ``None`` reads ``os.environ``.
    environ: Mapping[str, str] | None = None

    def caps_for(self, slot: str) -> tuple[int, ...]:
        return tuple(self.slot_caps.get(slot, self.default_slot_caps))

    def check(self) -> None:
        if self.policy not in RECALL_POLICIES:
            raise ValueError("unknown policy")
        if not self.slots or len(set(self.slots)) != len(self.slots):
            raise ValueError("slots must be a non-empty list without repeats")
        unknown = set(self.slot_caps) - set(self.slots)
        if unknown:
            raise ValueError("slot caps name a slot that is not evaluated")
        for caps in [self.default_slot_caps, self.resident_caps, *self.slot_caps.values()]:
            if not caps or any(type(cap) is not int or cap < 1 for cap in caps):
                raise ValueError("caps must be non-empty lists of positive integers")
        for name, value in (
            ("sessions", self.sessions),
            ("queries", self.queries),
            ("repeats", self.repeats),
        ):
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if not self.session_gap_minutes > 0:
            raise ValueError("session_gap_minutes must be positive")
        rows = len(self.resident_caps)
        for slot in self.slots:
            rows *= len(set(self.caps_for(slot)))
        if rows > MAX_SWEEP_ROWS:
            raise ValueError(f"the sweep has {rows} rows (at most {MAX_SWEEP_ROWS})")


# --- read-only snapshot handling -----------------------------------------------


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def check_snapshot(vault: Path, *, outputs: Sequence[Path] = ()) -> Path:
    """The snapshot's resolved root, or ``SnapshotRefused``.

    Refused: a path that is not a vault (no ``vault.yaml``), a snapshot with
    an interrupted write (reading it would need ``memfw recover``, which
    writes), a snapshot that contains a symlink (it could point outside the
    snapshot, at the live vault say), and any output path inside the snapshot.
    """

    root = Path(vault).expanduser()
    if not root.is_dir() or not (root / "vault.yaml").is_file():
        raise SnapshotRefused("not a vault snapshot (no vault.yaml)")
    resolved = root.resolve()
    for marker in PENDING_MARKERS:
        if (resolved / marker).exists():
            raise SnapshotRefused(
                "the snapshot has an interrupted write; reading it would need `memfw recover`"
                " (which writes): take a clean copy"
            )
    for directory, dirs, files in os.walk(resolved, followlinks=False):
        for name in [*dirs, *files]:
            if os.path.islink(os.path.join(directory, name)):
                raise SnapshotRefused("the snapshot contains a symlink; take a copy without links")
    for output in outputs:
        if _inside(Path(output).expanduser().resolve(), resolved):
            raise SnapshotRefused("an output path is inside the snapshot; write it elsewhere")
    return resolved


def fingerprint(paths: Iterable[Path | None]) -> str:
    """sha256 over every file's relative path, type, size and mtime under ``paths``.

    Symlinks are recorded, never followed. A missing path is recorded as
    missing, so one that appears or disappears changes the value too.
    """

    digest = hashlib.sha256()
    for top in paths:
        if top is None:
            continue
        top = Path(top)
        digest.update(b"\x00top\x00" + str(top).encode("utf-8", "surrogateescape"))
        if not os.path.lexists(top):
            digest.update(b"missing")
            continue
        entries: list[Path] = [top]
        if top.is_dir() and not top.is_symlink():
            for directory, dirs, files in os.walk(top, followlinks=False):
                dirs.sort()
                for name in sorted(dirs + files):
                    entries.append(Path(directory) / name)
        for entry in entries:
            info = entry.lstat()
            relative = str(entry.relative_to(top)) if entry != top else "."
            digest.update(
                f"{relative}\x00{info.st_mode}\x00{info.st_size}\x00{info.st_mtime_ns}\n".encode(
                    "utf-8", "surrogateescape"
                )
            )
    return digest.hexdigest()


def private_copy(snapshot: Path, workdir: Path) -> Path:
    """Copy ``snapshot`` to ``workdir/vault``, without index or lock.

    ``check_snapshot`` has refused a snapshot with a symlink, so the copy
    holds regular files only (``symlinks=False``); a link that appears since
    changes the snapshot's fingerprint, so the run fails with no report.
    """

    def ignore(_directory: str, names: list[str]) -> set[str]:
        return {name for name in names if name in _COPY_IGNORED}

    target = workdir / "vault"
    shutil.copytree(snapshot, target, symlinks=False, ignore=ignore)
    return target


def store_files(store: Path) -> list[Path]:
    """``store`` and its SQLite sidecar paths (present or not: both are fingerprinted)."""

    return [store, *(Path(f"{store}{suffix}") for suffix in STORE_SIDECARS)]


def private_store(store: Path, workdir: Path, name: str) -> Path:
    """Copy ``store`` and its present sidecars to ``workdir/stores/name``; the copy is opened.

    The original is never opened, so a WAL-mode store gets no ``-shm`` or
    ``-wal`` beside it, and the copy keeps the writes still in its ``-wal``.
    """

    target_dir = workdir / "stores"
    target_dir.mkdir(mode=0o700, exist_ok=True)
    target = target_dir / name
    for source, suffix in zip(store_files(store), ("", *STORE_SIDECARS), strict=True):
        if os.path.lexists(source):
            shutil.copyfile(source, f"{target}{suffix}")
    return target


# --- sessions --------------------------------------------------------------------


def access_sessions(
    events: Iterable[Event], visible: Callable[[str], bool], *, gap_minutes: float
) -> tuple[list[frozenset[str]], int]:
    """Sessions of search hits from ``memory_access`` events, and the hits dropped.

    Only search operations count. Events with a ``details.session_id`` are
    grouped by it; the others by time, a new session after ``gap_minutes``
    without a search. Hits the caller may not see now (deleted, other
    tenant, withheld by the clearance or the policy) are dropped and counted.
    """

    gap = timedelta(minutes=gap_minutes)
    keyed: dict[str, set[str]] = {}
    order: list[str] = []
    timed: list[tuple[datetime, str]] = []
    for event in events:
        if event.kind != EventKind.MEMORY_ACCESS:
            continue
        if event.details.get("operation") not in SEARCH_OPERATIONS:
            continue
        memory_id = str(event.details.get("memory_id") or event.memory_id)
        session = event.details.get("session_id")
        if isinstance(session, str) and session.strip():
            key = session.strip()
            if key not in keyed:
                keyed[key] = set()
                order.append(key)
            keyed[key].add(memory_id)
        else:
            timed.append((event.timestamp, memory_id))
    raw: list[set[str]] = [keyed[key] for key in order]
    timed.sort(key=lambda item: item[0])
    last: datetime | None = None
    for moment, memory_id in timed:
        if last is None or moment - last > gap:
            raw.append(set())
        raw[-1].add(memory_id)
        last = moment
    return _visible_sets(raw, visible)


def usage_sessions(
    rows: Iterable[Mapping[str, str]], visible: Callable[[str], bool]
) -> tuple[list[frozenset[str]], int]:
    """One session per ``session_ref`` of the usage rows (first signal order), visible ids only."""

    grouped: dict[str, set[str]] = {}
    for row in rows:
        grouped.setdefault(str(row["session_ref"]), set()).add(str(row["memory_id"]))
    return _visible_sets(list(grouped.values()), visible)


def _visible_sets(
    raw: Sequence[set[str]], visible: Callable[[str], bool]
) -> tuple[list[frozenset[str]], int]:
    sessions: list[frozenset[str]] = []
    dropped = 0
    for ids in raw:
        kept = frozenset(memory_id for memory_id in ids if visible(memory_id))
        dropped += len(ids) - len(kept)
        if kept:
            sessions.append(kept)
    return sessions, dropped


def synthetic_queries(
    documents: Sequence[MemoryDocument], *, seed: int, sessions: int, queries: int
) -> list[list[str]]:
    """``sessions`` lists of ``queries`` query strings drawn with ``seed``.

    A query is the first six words (three characters or more) of a visible
    memory's title and body. Documents are taken in id order, so the draw
    depends on the snapshot and the seed only. Query text never leaves this
    module: the report counts queries and hits.
    """

    pool = sorted(documents, key=lambda document: document.id)
    texts = [" ".join(_WORD.findall(f"{document.title} {document.body}")[:6]) for document in pool]
    texts = [text for text in texts if text]
    if not texts:
        return []
    rng = random.Random(seed)
    return [[rng.choice(texts) for _ in range(queries)] for _ in range(sessions)]


def synthetic_sessions(
    runtime: Runtime,
    documents: Sequence[MemoryDocument],
    visible: Callable[[str], bool],
    *,
    seed: int,
    sessions: int,
    queries: int,
) -> tuple[list[frozenset[str]], int]:
    """Sessions of hits of the synthetic queries through ``memory_search(mode=fts)``.

    Each query asks for ``SYNTHETIC_OVERFETCH`` pages and keeps, in rank
    order, the first ``SYNTHETIC_LIMIT`` hits the caller may see; the hidden
    ones ranked before them are dropped and counted, so they do not take the
    place of visible hits (a page of hidden hits would otherwise read as no
    hits). The runtime's vault is the private copy, with access recording
    off: the search writes nothing (its index is built in the copy).
    """

    out: list[frozenset[str]] = []
    dropped = 0
    for session in synthetic_queries(documents, seed=seed, sessions=sessions, queries=queries):
        hits: set[str] = set()
        hidden: set[str] = set()
        for query in session:
            payload = runtime.search(query, SYNTHETIC_LIMIT * SYNTHETIC_OVERFETCH, mode="fts")
            kept = 0
            for row in payload.get("results", []):
                memory_id = str(row["id"])
                if not visible(memory_id):
                    hidden.add(memory_id)
                    continue
                hits.add(memory_id)
                kept += 1
                if kept == SYNTHETIC_LIMIT:
                    break
        dropped += len(hidden)
        if hits:
            out.append(frozenset(hits))
    return out, dropped


# --- metrics (pure) --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SlotBuild:
    """One slot at one cap: what the pack would carry and what it cost.

    ``items`` holds, per carried entry, the memory ids it covers (its own id
    for a memory entry; the memories it points at for a topic or session
    item). ``direct`` says whether the entries are memories.
    """

    name: str
    cap: int
    available: bool
    degraded: str | None
    items: tuple[frozenset[str], ...]
    direct: bool
    tokens: int
    overflow: int
    latency_ms: tuple[float, ...]

    @property
    def covered(self) -> frozenset[str]:
        return frozenset().union(*self.items) if self.items else frozenset()

    @property
    def usable(self) -> bool:
        return self.available and self.degraded is None


def ratio(numerator: int, denominator: int) -> float | None:
    """``numerator / denominator`` rounded to 6 places, ``None`` when there is nothing to divide."""

    return round(numerator / denominator, 6) if denominator else None


def coverage_counts(
    hit_sessions: Sequence[frozenset[str]], covered: frozenset[str]
) -> tuple[int, int]:
    """``(covered hits, hits)`` summed over sessions (each session's hits counted once)."""

    return (
        sum(len(hits & covered) for hits in hit_sessions),
        sum(len(hits) for hits in hit_sessions),
    )


def waste_counts(
    used_sessions: Sequence[frozenset[str]], items: Sequence[frozenset[str]]
) -> tuple[int, int]:
    """``(unused item-sessions, item-sessions)``: an item is used when it covers a used id.

    An item that covers nothing (a topic with no visible memory, say) is
    never used.
    """

    unused = sum(1 for used in used_sessions for item in items if not item & used)
    return unused, len(used_sessions) * len(items)


def percentile(samples: Sequence[float], p: float) -> float | None:
    """Nearest-rank percentile (``p`` in 0..100) of ``samples``, ``None`` when empty."""

    if not samples:
        return None
    ordered = sorted(samples)
    rank = max(1, math.ceil(p / 100 * len(ordered)))
    return round(ordered[rank - 1], 3)


def resident_prefix(builds: Sequence[SlotBuild], cap: int) -> tuple[list[str], list[str]]:
    """``(resident, demoted)`` slot names: the longest prefix whose tokens fit under ``cap``.

    A slot that is unavailable or degraded carries nothing and costs nothing;
    it neither breaks nor joins the prefix.
    """

    resident: list[str] = []
    demoted: list[str] = []
    used = 0
    for build in builds:
        if not build.usable:
            continue
        if demoted or used + build.tokens > cap:
            demoted.append(build.name)
            continue
        resident.append(build.name)
        used += build.tokens
    return resident, demoted


# --- slot builds -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Pointers:
    """What a topic or session item points at: visible memory ids per concept and session."""

    by_concept: Mapping[str, frozenset[str]]
    by_session: Mapping[str, frozenset[str]]

    def covers(self, entry: ContextEntry | ContextItem) -> frozenset[str]:
        if isinstance(entry, ContextEntry):
            return frozenset({entry.id})
        if entry.kind == "topic":
            return self.by_concept.get(entry.id, frozenset())
        if entry.kind == "session":
            return self.by_session.get(entry.id, frozenset())
        return frozenset()


def pointers(root: Path, documents: Sequence[MemoryDocument]) -> Pointers:
    """Concept and session pointers of the visible memories ``documents``.

    A memory counts for each concept its ``about``/``applies_to`` names (a
    merged concept counts for the one it was merged into, as the topics slot
    counts it) and for each conversation session its ``evidence`` cites
    (``conv:APP:SESSION[@OFFSET]``), as ``conversation:<app>/<session>``.
    """

    from memfw.corroboration import parse_conv_ref

    from .context_sources import concept_catalogue

    try:
        canonical: Mapping[str, str] = concept_catalogue(root).canonical
    except Exception:  # noqa: BLE001 - an invalid concept file degrades topics, not the eval
        canonical = {}
    by_concept: dict[str, set[str]] = {}
    by_session: dict[str, set[str]] = {}
    for document in documents:
        if document.valid_to is not None:
            continue
        named = {*document.about, *document.applies_to}
        for oid in {canonical[item] for item in named if item in canonical}:
            by_concept.setdefault(oid, set()).add(document.id)
        for reference in document.evidence:
            parsed = parse_conv_ref(str(reference))
            if parsed is not None:
                key = f"conversation:{parsed[0]}/{parsed[1]}"
                by_session.setdefault(key, set()).add(document.id)
    return Pointers(
        by_concept={key: frozenset(value) for key, value in by_concept.items()},
        by_session={key: frozenset(value) for key, value in by_session.items()},
    )


def _provider(
    runtime: Runtime,
    name: str,
    repository: Repository,
    *,
    keep: Keep,
    app: str,
    policy: str,
    conversations: Path | None,
    state: Path | None = None,
) -> Any:
    """The real provider of slot ``name``; ``None`` when this build has no such slot.

    ``current`` is ``None`` (unavailable) unless the runtime enables it, as
    ``memory_context`` refuses it then. ``handoff`` reads the given conversation store (read-only) through the
    conversation share, never the store ``vault.yaml`` names: without
    ``conversations`` it is ``unavailable``. Model summaries come from
    ``state`` (a private copy of ``--state``) or, without it, from nowhere:
    the worker state ``vault.yaml`` names is never read.
    """

    if name == HANDOFF_SLOT:
        if conversations is None:

            def no_store(_limit: int) -> list[Any]:
                raise SlotUnavailable("no conversation store given")

            return HandoffSlotProvider(no_store)
        from memfw.conversations.adapter import SqliteStoreSource

        from .context_sources import share_feed

        return HandoffSlotProvider(
            share_feed(
                repository.root,
                app,
                source=SqliteStoreSource(conversations),
                state=state if state is not None else False,
            )
        )
    if name == CURRENT_SLOT and not runtime.context_current_enabled():
        return None
    try:
        return runtime.context_slot_provider(
            name, repository, keep=keep, app=app, policy_name=policy
        )
    except ValueError:
        return None


def build_slot(
    runtime: Runtime,
    name: str,
    cap: int,
    *,
    repository: Repository,
    keep: Keep,
    app: str,
    policy: str,
    now: datetime,
    conversations: Path | None,
    marks: Pointers,
    repeats: int,
    state: Path | None = None,
) -> SlotBuild:
    """Build slot ``name`` at ``cap`` ``repeats`` times with a fresh real provider; time each."""

    request = SlotRequest(limit_tokens=cap, policy=policy, now=now)
    direct = name not in POINTER_SLOTS
    timings: list[float] = []
    slot: ContextSlot | None = None
    reason: str | None = None
    for _ in range(repeats):
        provider = _provider(
            runtime,
            name,
            repository,
            keep=keep,
            app=app,
            policy=policy,
            conversations=conversations,
            state=state,
        )
        if provider is None:
            return SlotBuild(name, cap, False, None, (), direct, 0, 0, ())
        started = time.perf_counter()
        try:
            built = provider.build(request)
        except Exception as exc:  # noqa: BLE001 - a failing slot is reported, as memory_context does
            timings.append((time.perf_counter() - started) * 1000)
            reason = degraded_reason(exc)
            slot = None
            continue
        timings.append((time.perf_counter() - started) * 1000)
        if type(built) is not ContextSlot or built.name != name:
            reason, slot = "invalid", None
            continue
        reason, slot = built.degraded, built
    if slot is None or reason is not None:
        return SlotBuild(name, cap, True, reason or "error", (), direct, 0, 0, tuple(timings))
    items = tuple(marks.covers(entry) for entry in slot.entries)
    return SlotBuild(
        name,
        cap,
        True,
        None,
        items,
        direct,
        slot.used_tokens_est,
        slot.overflow,
        tuple(timings),
    )


# --- the run -----------------------------------------------------------------------


def _default_now(documents: Sequence[MemoryDocument], events: Sequence[Event]) -> datetime:
    """The snapshot's own latest instant (memory ``updated_at`` or event time)."""

    moments = [document.updated_at for document in documents]
    if events:
        moments.append(events[-1].timestamp)
    return max(moments) if moments else datetime(1970, 1, 1, tzinfo=UTC)


def _unavailable(build: SlotBuild) -> bool:
    """The slot does not exist here, or its source is missing (``degraded: unavailable``)."""

    return not build.available or build.degraded == "unavailable"


def _slot_row(
    build: SlotBuild,
    hits: Sequence[frozenset[str]],
    used: Sequence[frozenset[str]],
    deadline_s: float | None = None,
) -> dict[str, Any]:
    """One per-slot row. ``deadline_s`` is the slot's ``memory_context`` deadline."""

    covered, total = coverage_counts(hits, build.covered)
    unused, item_sessions = waste_counts(used, build.items)
    row: dict[str, Any] = {
        "slot": build.name,
        "cap": build.cap,
        "available": build.available,
        "degraded": build.degraded,
        "items": len(build.items),
        "tokens": build.tokens,
        "overflow": build.overflow,
        "coverage_kind": "direct" if build.direct else "pointer",
        "coverage": ratio(covered, total) if build.usable else None,
        "covered_hits": covered if build.usable else 0,
        "waste": ratio(unused, item_sessions) if build.usable else None,
        "unused_item_sessions": unused if build.usable else 0,
        "latency_ms": {
            "n": len(build.latency_ms),
            "p50": percentile(build.latency_ms, 50),
            "p95": percentile(build.latency_ms, 95),
        },
        "deadline_ms": round(deadline_s * 1000, 3) if deadline_s is not None else None,
    }
    # Flagged, never acted on: the deadlines (CONTEXT_SLOT_TIMEOUTS) are not changed.
    p95 = row["latency_ms"]["p95"]
    row["over_deadline"] = (
        None if p95 is None or row["deadline_ms"] is None else p95 > row["deadline_ms"]
    )
    return row


def _sweep_row(
    builds: Sequence[SlotBuild],
    resident_cap: int,
    hits: Sequence[frozenset[str]],
    used: Sequence[frozenset[str]],
) -> dict[str, Any]:
    resident, demoted = resident_prefix(builds, resident_cap)
    chosen = [build for build in builds if build.name in resident]
    covered = frozenset().union(*(build.covered for build in chosen)) if chosen else frozenset()
    direct = (
        frozenset().union(*(build.covered for build in chosen if build.direct))
        if chosen
        else frozenset()
    )
    items = [item for build in chosen for item in build.items]
    covered_hits, total = coverage_counts(hits, covered)
    direct_hits, _ = coverage_counts(hits, direct)
    unused, item_sessions = waste_counts(used, items)
    return {
        "slot_caps": {build.name: build.cap for build in builds},
        "resident_cap": resident_cap,
        "resident": resident,
        "demoted": demoted,
        "tokens": sum(build.tokens for build in chosen),
        "items": len(items),
        "coverage": ratio(covered_hits, total),
        "coverage_direct": ratio(direct_hits, total),
        "waste": ratio(unused, item_sessions),
    }


def evaluate_snapshot(
    vault: Path,
    *,
    options: EvalOptions | None = None,
    conversations: Path | None = None,
    usage: Path | None = None,
    outputs: Sequence[Path] = (),
    workdir: Path | None = None,
    state: Path | None = None,
) -> dict[str, Any]:
    """The C4 report (a JSON-ready mapping) for the read-only snapshot ``vault``.

    ``conversations`` is a conversation store file for ``handoff`` and
    ``usage`` a C5 usage projection file, ``state`` a conversation worker
    state file for ``handoff``'s model summaries (without it none are read),
    all optional and read-only.
    ``outputs`` are paths the caller will write the report to: one inside
    the snapshot is refused. ``workdir`` (default: a new private temporary
    directory, deleted afterwards) holds the copy. See the module docstring.
    """

    options = options or EvalOptions()
    options.check()
    snapshot = check_snapshot(vault, outputs=outputs)
    stores = [Path(path).expanduser().resolve() for path in (conversations, usage, state) if path]
    for store in stores:
        if not store.is_file():
            raise SnapshotRefused("a store path is not a file")
        if _inside(store, snapshot):
            raise SnapshotRefused("a store path is inside the snapshot; pass a copy outside it")
        for output in outputs:
            if Path(output).expanduser().resolve() == store:
                raise SnapshotRefused("an output path is a store path")
    watched = [snapshot, *(path for store in stores for path in store_files(store))]
    before = fingerprint(watched)
    own_workdir = workdir is None
    base = Path(tempfile.mkdtemp(prefix="memfw-c4-")) if workdir is None else Path(workdir)
    try:
        conversations_path = Path(conversations).expanduser().resolve() if conversations else None
        usage_path = Path(usage).expanduser().resolve() if usage else None
        report = _evaluate_copy(
            private_copy(snapshot, base),
            options,
            conversations=(
                private_store(conversations_path, base, "conversations.sqlite")
                if conversations_path
                else None
            ),
            usage=private_store(usage_path, base, "usage.sqlite3") if usage_path else None,
            state=(
                private_store(Path(state).expanduser().resolve(), base, "state.sqlite3")
                if state
                else None
            ),
        )
    finally:
        if own_workdir:
            shutil.rmtree(base, ignore_errors=True)
    if fingerprint(watched) != before:
        raise SnapshotChanged("the snapshot or a store changed during the evaluation; no report")
    return report


def _evaluate_copy(
    root: Path,
    options: EvalOptions,
    *,
    conversations: Path | None,
    usage: Path | None,
    state: Path | None = None,
) -> dict[str, Any]:
    environ = os.environ if options.environ is None else options.environ
    settings = resolve_settings(
        tenant=options.tenant,
        allowed_sources=options.allowed_sources,
        app=options.app,
        clearance=options.clearance,
        # Only the visibility variables: never an embedder (no network).
        environ={key: environ[key] for key in _SETTINGS_ENV if key in environ},
    )
    runtime = build_runtime(
        root,
        tenant=settings.tenant,
        allowed_sources=settings.allowed_sources,
        embedder=None,
        entity_enabled=False,
        # Cold timings: no C3 cache, so every timed core build reads the vault.
        context_cache=False,
        usage_signal=False,
        record_access=False,
        clearance=settings.clearance,
        # ``--current`` simulates the vault switch; otherwise ``vault.yaml`` decides.
        context_current=True if options.current else None,
    )
    repository = runtime.open_repository()
    documents = repository.list()
    events = repository.events()
    now = options.now or _default_now(documents, events)
    keep = runtime.context_keep(options.policy, now=now)
    visible_documents = [document for document in documents if keep(document)]
    visible_ids = {document.id for document in visible_documents}

    def visible(memory_id: str) -> bool:
        return memory_id in visible_ids

    access, access_dropped = access_sessions(
        events, visible, gap_minutes=options.session_gap_minutes
    )
    used_sessions: list[frozenset[str]] = []
    used_dropped = 0
    if usage is not None:
        from memfw.usage import SqliteUsageSink

        used_sessions, used_dropped = usage_sessions(SqliteUsageSink(usage).rows(), visible)
    # A basis with records counts even when the caller may see none of them
    # (no fallback to synthetic queries that would see other memories).
    if access or access_dropped:
        coverage_basis, hit_sessions, dropped = "memory_access", access, access_dropped
    elif used_sessions or used_dropped:
        coverage_basis, hit_sessions, dropped = "memory_used", used_sessions, used_dropped
    else:
        hit_sessions, dropped = synthetic_sessions(
            runtime,
            visible_documents,
            visible,
            seed=options.seed,
            sessions=options.sessions,
            queries=options.queries,
        )
        coverage_basis = "synthetic"
    if used_sessions or used_dropped:
        waste_basis, waste_sessions = "memory_used", used_sessions
    else:
        waste_basis, waste_sessions = "estimated_from_hits", hit_sessions

    marks = pointers(root, visible_documents)
    app = settings.app
    builds: dict[tuple[str, int], SlotBuild] = {}
    caps_of: dict[str, list[int]] = {}
    for name in options.slots:
        caps_of[name] = []
        for cap in sorted(set(options.caps_for(name))):
            build = build_slot(
                runtime,
                name,
                cap,
                repository=repository,
                keep=keep,
                app=app,
                policy=options.policy,
                now=now,
                conversations=conversations,
                marks=marks,
                repeats=options.repeats,
                state=state,
            )
            builds[(name, cap)] = build
            if not build.available:
                break  # this build has no such slot: one row, and no sweep column
            caps_of[name].append(cap)
        if all(_unavailable(builds[(name, cap)]) for cap in caps_of[name]):
            # Unavailable at every cap (``handoff`` without a store, say): its rows
            # stay, but a sweep column would only repeat every row once per cap.
            caps_of[name] = []

    deadlines = runtime.context_slot_timeouts
    slot_rows = [
        _slot_row(build, hit_sessions, waste_sessions, deadlines.get(build.name))
        for build in builds.values()
    ]
    sweep: list[dict[str, Any]] = []
    swept = [name for name in options.slots if caps_of[name]]
    for combination in product(*(caps_of[name] for name in swept)):
        chosen = [builds[(name, cap)] for name, cap in zip(swept, combination, strict=True)]
        for resident_cap in sorted(set(options.resident_caps)):
            sweep.append(_sweep_row(chosen, resident_cap, hit_sessions, waste_sessions))

    return {
        "schema": SCHEMA,
        "settings": {
            "policy": options.policy,
            "clearance": sorted(settings.clearance) if settings.clearance is not None else None,
            "tenant_bound": bool(settings.tenant),
            "allowed_sources": len(settings.allowed_sources),
            "app_bound": bool(app),
            "slots": list(options.slots),
            "current_enabled": runtime.context_current_enabled(),
            "current_simulated": options.current,
            "unavailable_slots": [name for name in options.slots if not caps_of[name]],
            "resident_caps": sorted(set(options.resident_caps)),
            "seed": options.seed,
            "synthetic_sessions": options.sessions,
            "synthetic_queries": options.queries,
            "repeats": options.repeats,
            "latency": "cold",
            "session_gap_minutes": options.session_gap_minutes,
            "now": now.astimezone(UTC).isoformat(),
            "conversation_store": conversations is not None,
            "usage_store": usage is not None,
            "state_store": state is not None,
        },
        "defaults": {
            "limit_tokens": DEFAULT_LIMIT_TOKENS,
            "resident_prefix": "not implemented (C4 decides it with this report)",
        },
        # Visible memories only: a count of all of them would reveal how many the
        # caller may not see (other tenants, withheld by the clearance).
        "snapshot": {"visible": len(visible_documents)},
        "basis": {"coverage": coverage_basis, "waste": waste_basis},
        "sessions": {
            "coverage": len(hit_sessions),
            "waste": len(waste_sessions),
            "hits": sum(len(hits) for hits in hit_sessions),
            "hits_dropped": dropped,
        },
        "slots": slot_rows,
        "sweep": sweep,
    }


def strip_timings(report: Mapping[str, Any]) -> dict[str, Any]:
    """``report`` without ``latency_ms`` and ``over_deadline`` (the timing-dependent fields)."""

    copied: dict[str, Any] = json.loads(json.dumps(report))
    for row in copied.get("slots", []):
        row.pop("latency_ms", None)
        row.pop("over_deadline", None)
    return copied


# --- markdown ----------------------------------------------------------------------


def _fmt(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.3f}"
    if isinstance(value, list):
        return ", ".join(str(item) for item in value) or "-"
    return str(value)


def render_markdown(report: Mapping[str, Any]) -> str:
    """A markdown report of ``report``: settings, per-slot table, sweep table."""

    settings = report["settings"]
    basis = report["basis"]
    sessions = report["sessions"]
    lines = [
        "# C4 context pack evaluation",
        "",
        f"- schema: `{report['schema']}`",
        f"- policy: `{settings['policy']}`; clearance: {_fmt(settings['clearance'])}",
        f"- visible memories: {report['snapshot']['visible']}",
        f"- coverage basis: `{basis['coverage']}` ({sessions['coverage']} sessions,"
        f" {sessions['hits']} hits, {sessions['hits_dropped']} not visible)",
        f"- waste basis: `{basis['waste']}` ({sessions['waste']} sessions)",
        f"- seed: {settings['seed']}; repeats: {settings['repeats']}; now: {settings['now']}",
        f"- default `limit_tokens`: {report['defaults']['limit_tokens']} (unchanged); resident"
        " prefix: simulated only",
        "",
        "## Per slot",
        "",
        "| slot | cap | items | tokens | overflow | coverage | kind | waste | p50 ms | p95 ms |"
        " state |",
        "|---|---:|---:|---:|---:|---:|---|---:|---:|---:|---|",
    ]
    for row in report["slots"]:
        state = "unavailable" if not row["available"] else (row["degraded"] or "ok")
        if row.get("over_deadline"):
            state += ", p95 over deadline"
        latency = row.get("latency_ms") or {}
        lines.append(
            f"| {row['slot']} | {row['cap']} | {row['items']} | {row['tokens']} |"
            f" {row['overflow']} | {_fmt(row['coverage'])} | {row['coverage_kind']} |"
            f" {_fmt(row['waste'])} | {_fmt(latency.get('p50'))} | {_fmt(latency.get('p95'))} |"
            f" {state} |"
        )
    lines += [
        "",
        "## Sweep (slot caps x resident-prefix cap)",
        "",
        "| slot caps | resident cap | resident | demoted | tokens | items | coverage |"
        " direct | waste |",
        "|---|---:|---|---|---:|---:|---:|---:|---:|",
    ]
    for row in report["sweep"]:
        caps = " ".join(f"{name}={cap}" for name, cap in row["slot_caps"].items())
        lines.append(
            f"| {caps} | {row['resident_cap']} | {_fmt(row['resident'])} |"
            f" {_fmt(row['demoted'])} | {row['tokens']} | {row['items']} |"
            f" {_fmt(row['coverage'])} | {_fmt(row['coverage_direct'])} | {_fmt(row['waste'])} |"
        )
    return "\n".join(lines) + "\n"


__all__ = [
    "CURRENT_SLOT",
    "DEFAULT_RESIDENT_CAPS",
    "DEFAULT_SLOT_CAPS",
    "EVAL_SLOTS",
    "SCHEMA",
    "EvalOptions",
    "Pointers",
    "SlotBuild",
    "SnapshotChanged",
    "SnapshotRefused",
    "access_sessions",
    "build_slot",
    "check_snapshot",
    "coverage_counts",
    "evaluate_snapshot",
    "fingerprint",
    "percentile",
    "pointers",
    "private_copy",
    "ratio",
    "render_markdown",
    "resident_prefix",
    "strip_timings",
    "synthetic_queries",
    "synthetic_sessions",
    "usage_sessions",
    "waste_counts",
]
