"""Corroboration projection: which apps and sessions independently mention a memory.

A rebuildable SQLite file derived from the vault (design U4). It is never the
authority for anything: it can be deleted and rebuilt, it never writes back
into the vault, and it never changes ``epistemic`` or ``status``. Nothing else
in memfw reads it yet (U5 decides how it is used, default off).

**What is counted.** A *mention* of memory ``M`` is one ``conv:APP:SESSION@OFFSET``
reference (the M2 conversation store's namespace) carried by one of:

* ``M``'s own ``evidence`` (``self``);
* a staged ``memory_claims`` candidate (``_candidates/dream/<id>/``, report
  kind ``conversation-memory-candidates``) whose claim, normalised (case,
  whitespace, trailing punctuation), equals ``M``'s match key: its summary,
  else its title, and only a key of at least 4 tokens and 20 characters that
  no other memory (any tenant) shares as title or summary. Each evidence ref
  is a transcript turn the worker verified. The conversation store has no
  tenant, so claims credit only memories in the default tenant ``""``. The
  worker stages each claim once per app (it dedupes on app + claim text), so
  later sessions of that app repeating the same claim add nothing. Dream review records no
  rejected state: a rejected or stale candidate keeps counting until it is
  removed from ``_candidates/``;
* an accepted experience point (``type: experience-point``, review verdict
  ``accepted``; ``failed`` is not support) in ``M``'s tenant whose
  ``related`` lists ``M``.

**Independence rules.** Each surviving mention becomes one (app, session)
pair; the same app in the same session counts once, however often it is
cited. A mention does not count when:

* its carrier is derived from ``M``: the carrier's evidence (an experience
  point's, or a claim candidate's) cites ``M``'s id (bare, ``memory:``/``mem:``
  prefixed) or a candidate names ``M`` in ``memory_ids``;
* it falls in a session where ``M`` was injected, at or after the injection:
  a ``memory_access`` event for ``M`` whose details carry ``session_id``
  (and, when present, ``source``/``agent_id`` as the app). memfw's own
  retrieval and ``memory_context`` do not record a session today, so this
  rule has no producer yet; an agent restating an injected memory in a new
  session without such an event is *not* detected (documented gap);
* its carrier has a generated provenance (``_GENERATED_PROVENANCES``), or it
  is a dream candidate from anything but the conversation worker (dream
  verify/curate output never counts);
* its app is not ``readable`` or has opted out under ``conversation_share``
  (such apps are never written to the file).

**Output.** Per memory: ``count``, ``apps``, ``sessions`` (``APP:SESSION``
ids), ``first_seen`` and ``last_seen`` — ids, names, counts and times only, no
text. A mention's time is its evidence timestamp, else its carrier's
``created_at`` (an upper bound: the turn existed before the carrier was
written).

**Reading.** :func:`corroboration_for` trims to what ``caller_app`` may read
under the current share policy: only apps the caller may read (plus its own)
appear in ``apps`` and in ``count``. Hidden apps are filtered again at read
time, so a policy tightened after a rebuild still holds.

Rebuilds are deterministic: the same vault gives the same bytes.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Protocol

import yaml

from .core.epistemic import _GENERATED_PROVENANCES, EXPERIENCE_POINT_TYPE
from .core.models import EventKind, MemoryDocument
from .core.repository import Repository

SCHEMA_VERSION: Final = 1
FILENAME: Final = "corroboration.sqlite3"
CLAIMS_REPORT_KIND: Final = "conversation-memory-candidates"
_DREAM_DIR: Final = Path("_candidates") / "dream"
_APP = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
_SESSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:+=-]{0,199}$")
_MEMORY_PREFIXES: Final = ("memory:", "mem:")
_TRAILING = " \t.,;:!?。！？；，"
#: A claim matches a memory only on a key at least this long (after normalising).
MIN_KEY_TOKENS: Final = 4
MIN_KEY_CHARS: Final = 20
#: The conversation store is tenant-less (one user): claims credit only this tenant.
CLAIMS_TENANT: Final = ""


class CorroborationError(ValueError):
    """The projection cannot be built or read (message is safe to show)."""


class ShareRules(Protocol):
    """The part of :class:`memfw.conversations.policy.SharePolicy` this reads.

    Injected by the caller (the CLI), so this projection does not import the
    conversations capability.
    """

    @property
    def enabled(self) -> bool: ...

    def readable(self, app: str) -> bool: ...

    def scope(self, caller: str) -> tuple[tuple[str, ...] | None, tuple[str, ...]]: ...


@dataclass(frozen=True)
class Corroboration:
    count: int
    apps: tuple[str, ...]
    sessions: tuple[str, ...]
    first_seen: str | None
    last_seen: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "count": self.count,
            "apps": list(self.apps),
            "sessions": list(self.sessions),
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
        }


EMPTY: Final = Corroboration(0, (), (), None, None)


@dataclass(frozen=True)
class _Mention:
    app: str
    session: str
    seen: str | None


def projection_path(root: Path | str) -> Path:
    """Where the projection lives: next to ``index.sqlite3`` at the vault root."""

    return Path(root) / FILENAME


# --- parsing -----------------------------------------------------------------


def parse_conv_ref(reference: str) -> tuple[str, str] | None:
    """``(app, session)`` of a ``conv:APP:SESSION[@OFFSET|#TURN]`` ref, else ``None``."""

    value = reference.strip()
    if not value.lower().startswith("conv:"):
        return None
    value = value[len("conv:") :]
    for mark in ("@", "#"):
        value = value.split(mark, 1)[0]
    app, sep, session = value.partition(":")
    app = app.strip().lower()
    session = session.strip().rstrip("/")
    if not sep or not _APP.fullmatch(app) or not _SESSION.fullmatch(session) or ".." in session:
        return None
    return app, session


def _instant(value: Any) -> str | None:
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, str) and value.strip():
        try:
            moment = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).isoformat(timespec="microseconds")


def _normal(text: str) -> str:
    return " ".join(text.casefold().split()).rstrip(_TRAILING)


def _match_key(text: str) -> str | None:
    """A claim-matching key, or ``None`` when ``text`` is too short or generic."""

    key = _normal(text)
    if len(key) < MIN_KEY_CHARS or len(key.split()) < MIN_KEY_TOKENS:
        return None
    return key


def _cites(references: Iterable[str], memory_id: str) -> bool:
    for reference in references:
        value = str(reference).strip()
        for prefix in _MEMORY_PREFIXES:
            if value.lower().startswith(prefix):
                value = value[len(prefix) :]
                break
        if value == memory_id:
            return True
    return False


def _generated(document: MemoryDocument) -> bool:
    return document.provenance.strip().lower() in _GENERATED_PROVENANCES


def _evidence_of(document: MemoryDocument) -> list[str]:
    refs = list(document.evidence)
    if document.experience is not None:
        refs.extend(document.experience.evidence)
    return refs


def _from_refs(references: Iterable[str], seen: str | None) -> Iterator[_Mention]:
    for reference in references:
        parsed = parse_conv_ref(str(reference))
        if parsed is not None:
            yield _Mention(parsed[0], parsed[1], seen)


# --- staged dream candidates --------------------------------------------------


@dataclass(frozen=True)
class _StagedCandidate:
    claims: bool
    memory_ids: tuple[str, ...]
    reason: str
    mentions: tuple[_Mention, ...]
    refs: tuple[str, ...]


def _plain_file(path: Path) -> bool:
    return path.is_file() and not path.is_symlink()


def _frontmatter(text: str) -> Mapping[str, Any] | None:
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---\n", 4)
    if end < 0:
        return None
    try:
        front = yaml.safe_load(text[4:end])
    except yaml.YAMLError:
        return None
    return front if isinstance(front, Mapping) else None


def _staged(root: Path) -> list[_StagedCandidate]:
    base = root / _DREAM_DIR
    if not base.is_dir() or base.is_symlink():
        return []
    found: list[_StagedCandidate] = []
    for directory in sorted(base.iterdir()):
        if directory.name.startswith(".") or directory.is_symlink() or not directory.is_dir():
            continue
        report_path = directory / "report.json"
        kind = ""
        if _plain_file(report_path):
            try:
                report = json.loads(report_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                report = {}
            if isinstance(report, Mapping):
                kind = str(report.get("kind") or "")
        for path in sorted(directory.glob("*.md")):
            if not _plain_file(path):
                continue
            try:
                front = _frontmatter(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError):
                continue
            if front is None:
                continue
            created = _instant(front.get("created_at"))
            for raw in front.get("candidates") or []:
                if not isinstance(raw, Mapping):
                    continue
                mentions: list[_Mention] = []
                refs: list[str] = []
                for item in raw.get("evidence") or []:
                    if not isinstance(item, Mapping):
                        continue
                    refs.append(str(item.get("ref") or ""))
                    if item.get("polarity") != "support":
                        continue
                    seen = _instant(item.get("timestamp")) or created
                    mentions.extend(_from_refs([str(item.get("ref") or "")], seen))
                found.append(
                    _StagedCandidate(
                        claims=kind == CLAIMS_REPORT_KIND,
                        memory_ids=tuple(str(item) for item in raw.get("memory_ids") or []),
                        reason=str(raw.get("reason") or ""),
                        mentions=tuple(mentions),
                        refs=tuple(refs),
                    )
                )
    return found


# --- injection events -----------------------------------------------------------


def _injections(repository: Repository) -> dict[str, list[tuple[str | None, str, str]]]:
    """``memory_id -> [(app or None, session, at)]`` from session-scoped access events."""

    found: dict[str, list[tuple[str | None, str, str]]] = {}
    for event in repository.events():
        if event.kind != EventKind.MEMORY_ACCESS:
            continue
        session = str(event.details.get("session_id") or "").strip()
        memory_id = str(event.details.get("memory_id") or event.memory_id or "")
        if not session or not memory_id:
            continue
        app_raw = str(event.details.get("source") or event.details.get("agent_id") or "")
        app = app_raw.strip().lower() if _APP.fullmatch(app_raw.strip().lower()) else None
        at = _instant(event.timestamp)
        if at is not None:
            found.setdefault(memory_id, []).append((app, session, at))
    return found


def _after_injection(mention: _Mention, injections: Sequence[tuple[str | None, str, str]]) -> bool:
    for app, session, at in injections:
        if session != mention.session or (app is not None and app != mention.app):
            continue
        # An unknown time cannot be shown to precede the injection.
        if mention.seen is None or mention.seen >= at:
            return True
    return False


# --- rebuild ----------------------------------------------------------------------


_Seen = tuple[str | None, str | None]


def _claim_key(memory: MemoryDocument, owners: Mapping[str, set[str]]) -> str | None:
    """The memory's summary key if usable and unique, else its title key."""

    for text in (memory.summary, memory.title):
        key = _match_key(text)
        if key is not None and owners.get(key) == {memory.id}:
            return key
    return None


def collect(repository: Repository, rules: ShareRules) -> dict[str, dict[tuple[str, str], _Seen]]:
    """``memory_id -> {(app, session): (first_seen, last_seen)}`` after the rules."""

    documents = sorted(repository.list(), key=lambda item: item.id)
    staged = _staged(repository.root)
    injections = _injections(repository)
    experience = [
        item
        for item in documents
        if item.type == EXPERIENCE_POINT_TYPE
        and item.experience is not None
        and item.experience.review.verdict == "accepted"
    ]
    claims_by_text: dict[str, list[_StagedCandidate]] = {}
    for candidate in staged:
        if candidate.claims and candidate.reason:
            claims_by_text.setdefault(_normal(candidate.reason), []).append(candidate)
    # A key two memories share (title or summary, any tenant) names neither.
    key_owners: dict[str, set[str]] = {}
    for document in documents:
        for text in (document.title, document.summary):
            if (key := _match_key(text)) is not None:
                key_owners.setdefault(key, set()).add(document.id)

    result: dict[str, dict[tuple[str, str], _Seen]] = {}
    for memory in documents:
        mentions: list[_Mention] = []
        if not _generated(memory):
            mentions.extend(_from_refs(_evidence_of(memory), _instant(memory.created_at)))
        if memory.tenant == CLAIMS_TENANT:
            key = _claim_key(memory, key_owners)
            for candidate in claims_by_text.get(key, ()) if key else ():
                if memory.id in candidate.memory_ids or _cites(candidate.refs, memory.id):
                    continue  # derived from this memory
                mentions.extend(candidate.mentions)
        for point in experience:
            if point.id == memory.id or point.tenant != memory.tenant:
                continue
            if memory.id not in point.related or _generated(point):
                continue
            if _cites(_evidence_of(point), memory.id):
                continue  # derived from this memory
            mentions.extend(_from_refs(_evidence_of(point), _instant(point.created_at)))
        pairs: dict[tuple[str, str], _Seen] = {}
        for mention in mentions:
            if not rules.readable(mention.app):
                continue
            if _after_injection(mention, injections.get(memory.id, ())):
                continue
            pair = (mention.app, mention.session)
            first, last = pairs.get(pair, (None, None))
            if mention.seen is not None:
                first = mention.seen if first is None else min(first, mention.seen)
                last = mention.seen if last is None else max(last, mention.seen)
            pairs[pair] = (first, last)
        if pairs:
            result[memory.id] = pairs
    return result


_SCHEMA: Final = (
    "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID",
    "CREATE TABLE mentions (memory_id TEXT NOT NULL, app TEXT NOT NULL,"
    " session_id TEXT NOT NULL, first_seen TEXT, last_seen TEXT,"
    " PRIMARY KEY (memory_id, app, session_id)) WITHOUT ROWID",
)


def rebuild(repository: Repository, rules: ShareRules, path: Path | None = None) -> dict[str, int]:
    """Rebuild the projection from the vault; return ``{memories, mentions}``.

    Read-only on the vault. Writes a fresh file and renames it into place, so a
    reader never sees a half-built projection.
    """

    target = Path(path) if path is not None else projection_path(repository.root)
    memories_dir = repository.memories_dir.resolve()
    resolved = target.resolve()
    if resolved == memories_dir or memories_dir in resolved.parents:
        raise CorroborationError("refusing to write the projection inside memories/")
    data = collect(repository, rules)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{secrets.token_hex(8)}.tmp")
    # Private from the first byte; O_EXCL refuses a planted file or symlink.
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    os.close(os.open(temporary, flags, 0o600))
    rows = 0
    try:
        connection = sqlite3.connect(temporary)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    try:
        connection.execute("PRAGMA journal_mode = DELETE")
        connection.execute("PRAGMA page_size = 4096")
        with connection:
            for statement in _SCHEMA:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO meta (key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),)
            )
            for memory_id in sorted(data):
                for (app, session), (first, last) in sorted(data[memory_id].items()):
                    connection.execute(
                        "INSERT INTO mentions VALUES (?, ?, ?, ?, ?)",
                        (memory_id, app, session, first, last),
                    )
                    rows += 1
    except BaseException:
        connection.close()
        temporary.unlink(missing_ok=True)
        raise
    connection.close()
    os.chmod(temporary, 0o600)
    os.replace(temporary, target)
    return {"memories": len(data), "mentions": rows}


# --- read --------------------------------------------------------------------------


def visible_apps(rules: ShareRules, caller_app: str) -> frozenset[str] | None:
    """Apps ``caller_app`` may see corroboration from; ``None`` means every readable app.

    The caller always sees its own app. Sharing off, a caller that opted out
    or one with ``can_read: false`` sees only its own.
    """

    caller = caller_app.strip().lower()
    own = frozenset({caller})
    if not rules.enabled:
        return own
    try:
        include, exclude = rules.scope(caller)
    except ValueError:
        return own
    if include is None:
        return None
    return own | (frozenset(include) - frozenset(exclude))


def corroboration_for(
    memory_ids: Iterable[str],
    *,
    caller_app: str,
    rules: ShareRules,
    path: Path,
) -> dict[str, Corroboration]:
    """Corroboration per id, trimmed to what ``caller_app`` may read.

    An id with nothing visible (or a missing projection file) gets
    :data:`EMPTY`; the answer never shows that a hidden app exists.
    """

    wanted = sorted({str(item) for item in memory_ids})
    answer = dict.fromkeys(wanted, EMPTY)
    if not wanted or not Path(path).is_file():
        return answer
    allowed = visible_apps(rules, caller_app)
    uri = f"{Path(path).resolve().as_uri()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        placeholders = ",".join("?" for _ in wanted)
        rows = connection.execute(
            "SELECT memory_id, app, session_id, first_seen, last_seen FROM mentions"
            f" WHERE memory_id IN ({placeholders}) ORDER BY memory_id, app, session_id",
            wanted,
        ).fetchall()
    except sqlite3.DatabaseError as exc:
        raise CorroborationError(f"unreadable corroboration projection: {exc}") from exc
    finally:
        connection.close()
    grouped: dict[str, list[tuple[str, str, str | None, str | None]]] = {}
    for memory_id, app, session, first, last in rows:
        if not rules.readable(app):
            continue
        if allowed is not None and app not in allowed:
            continue
        grouped.setdefault(memory_id, []).append((app, session, first, last))
    for memory_id, items in grouped.items():
        firsts = [first for _app, _session, first, _last in items if first]
        lasts = [last for _app, _session, _first, last in items if last]
        answer[memory_id] = Corroboration(
            count=len(items),
            apps=tuple(sorted({app for app, _session, _first, _last in items})),
            sessions=tuple(f"{app}:{session}" for app, session, _first, _last in items),
            first_seen=min(firsts) if firsts else None,
            last_seen=max(lasts) if lasts else None,
        )
    return answer


__all__ = [
    "CLAIMS_REPORT_KIND",
    "EMPTY",
    "FILENAME",
    "SCHEMA_VERSION",
    "Corroboration",
    "CorroborationError",
    "ShareRules",
    "collect",
    "corroboration_for",
    "parse_conv_ref",
    "projection_path",
    "rebuild",
    "visible_apps",
]
