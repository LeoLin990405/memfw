"""``proxy_segments``: the disposable full-text index over attachment proxies (MM2b).

A separate SQLite file **outside the vault**,
``~/.local/share/memfw/proxy_index/<vault key>.sqlite3``
(``memfw.core.local_data.projection_file``, directory 0700, file 0600), never
the memory index: memory search, the HTTP read-only service and every memory
export keep reading ``index.sqlite3`` alone, so they can never return proxy
text. Being outside the vault, it is not hashed by the transaction
fingerprint, not counted in the vault's size, not copied by vault snapshots
(``memfw eval context``), and it can be rebuilt from a read-only vault without
the vault's write lock. A ``proxy_index.sqlite3`` an earlier build left in the
vault root is never read, written or deleted (it can be removed by hand). The
table uses the memory index's tokenizer choice (FTS5 trigram when SQLite has
it, else jieba segments in unicode61) and the same query terms, including the
two-character ``instr`` fallback on a trigram index.

Each row is one segment of an **active** proxy (not superseded) of a
committed, readable, **active** attachment (not detached, missing or purged,
when the vault's log says so) whose retention has not ended, with the
segment's canonical locator, the proxy's kind and coverage, the tenant
(attachments belong to the vault's default tenant ``""``) and the effective
privacy level (the strictest of the proxy's and the attachment's). Search
filters by tenant and clearance in SQL; callers re-check every hit against the
proxy file and the attachment record before returning text.

The index records a fingerprint of the vault id and the attachment and proxy
events it was built from; :meth:`ProxyIndex.is_current` compares it with the
log, and :func:`ensure_proxy_index` rebuilds when they differ. The fingerprint
is taken *before* the vault is read, so a write that lands during a rebuild
leaves the new file stale and the next call rebuilds again; no lock is
needed. It is never built on a vault without proxies.

:func:`drop_proxy_index` deletes the file (``attach purge`` must call it, so
purged text leaves no copy), and :func:`proxy_index_residue` counts what it
still holds for one attachment (for the purge residue check).
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
from collections.abc import Callable, Iterable
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Final

from ..attachments.proxies import ProxyRecord, proxy_log
from ..attachments.records import AttachmentRecord
from ..core.errors import IntegrityError, MemfwError, NotFoundError
from ..core.local_data import ensure_private_dir, projection_file
from ..core.models import ATTACHMENT_EVENT_KINDS, Event, EventKind
from ..core.repository import Repository
from .fts_terms import (
    fts_query_from_terms,
    jieba_segmenter,
    query_terms,
    short_terms_clause,
    split_short_terms,
)

#: The ``memfw.core.local_data`` kind: ``~/.local/share/memfw/proxy_index/<key>.sqlite3``.
PROXY_INDEX_KIND: Final = "proxy_index"
#: Where MM2b's first draft put the index (the vault root). Never read or deleted.
LEGACY_PROXY_INDEX_NAME: Final = "proxy_index.sqlite3"
PROXY_INDEX_SCHEMA_VERSION: Final = 2
_PROXY_KINDS: Final = frozenset({EventKind.PROXY_ADD, EventKind.PROXY_SUPERSEDE})


def proxy_index_path(repository: Repository | Path, *, home: Path | None = None) -> Path:
    """The vault's proxy index: outside it, under the home directory."""

    root = repository if isinstance(repository, Path) else repository.root
    return projection_file(root, PROXY_INDEX_KIND, home=home)


def drop_proxy_index(repository: Repository | Path, *, home: Path | None = None) -> bool:
    """Delete the vault's proxy index (and SQLite side files); ``True`` when one existed.

    ``attach purge`` calls this so no index page keeps purged text; the next
    search rebuilds from the remaining proxies.
    """

    path = proxy_index_path(repository, home=home)
    existed = path.exists()
    for suffix in ("", "-wal", "-shm", "-journal"):
        Path(f"{path}{suffix}").unlink(missing_ok=True)
    return existed


def proxy_index_residue(
    repository: Repository | Path, attachment_id: str, *, home: Path | None = None
) -> int:
    """Rows the proxy index holds for ``attachment_id`` (0 without an index).

    Counts both tables, so a residue check after a purge sees a stale row in
    either. An unreadable index counts as one row (it must be dropped).
    """

    path = proxy_index_path(repository, home=home)
    if not path.is_file():
        return 0
    try:
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as connection:
            rows = connection.execute(
                "SELECT COUNT(*) FROM segment_rows WHERE attachment_id = ?", (attachment_id,)
            ).fetchone()[0]
            prefixes = [
                str(row[0])
                for row in connection.execute(
                    "SELECT DISTINCT proxy_id FROM segment_rows WHERE attachment_id = ?",
                    (attachment_id,),
                )
            ]
            fts = 0
            for proxy_id in prefixes:
                prefix = f"{proxy_id}:"
                fts += connection.execute(
                    "SELECT COUNT(*) FROM proxy_segments WHERE substr(ref, 1, ?) = ?",
                    (len(prefix), prefix),
                ).fetchone()[0]
    except sqlite3.DatabaseError:
        return 1
    return int(rows) + int(fts)


def _active_attachments(repository: Repository) -> Callable[[str], bool]:
    """Which attachments are active (not detached, missing or purged).

    Uses the vault's attachment state map when this memfw has one (MM1b);
    without it every committed attachment is active.
    """

    state_map = getattr(repository, "attachment_state_map", None)
    if state_map is None:
        return lambda _attachment_id: True
    states = state_map()

    def active(attachment_id: str) -> bool:
        state = states.get(attachment_id)
        return state is not None and getattr(state, "label", "active") == "active"

    return active


def attachment_active(repository: Repository, attachment_id: str) -> bool:
    """One attachment's state (see :func:`_active_attachments`)."""

    return _active_attachments(repository)(attachment_id)


def _vault_id(repository: Repository) -> str:
    try:
        value = repository.read_config().get("vault_id")
    except (MemfwError, OSError, ValueError):
        return ""
    return value if isinstance(value, str) else ""


def proxy_fingerprint(events: Iterable[Event], *, vault_id: str = "") -> str:
    """Hash of the vault id and every attachment and proxy event (kind, id, hashes, by)."""

    digest = hashlib.sha256()
    digest.update(vault_id.encode() + b"\n")
    for event in events:
        if event.kind in ATTACHMENT_EVENT_KINDS or event.kind in _PROXY_KINDS:
            item = [
                event.kind.value,
                event.memory_id,
                event.before_hash,
                event.after_hash,
                event.details.get("by"),
            ]
            digest.update(json.dumps(item).encode())
            digest.update(b"\n")
    return digest.hexdigest()


def retention_ended(record: AttachmentRecord, today: date) -> bool:
    """``until:<date>`` retention ends after that day (design 2: no longer searchable)."""

    if not record.retention.startswith("until:"):
        return False
    return date.fromisoformat(record.retention.split(":", 1)[1]) < today


def _supports_trigram() -> bool:
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("CREATE VIRTUAL TABLE probe USING fts5(value, tokenize='trigram')")
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        connection.close()


@dataclass(frozen=True)
class SegmentHit:
    """One indexed segment that matched (no text: callers read it from the proxy file)."""

    ref: str
    proxy_id: str
    n: int
    attachment_id: str
    kind: str
    locator: str
    coverage: str
    privacy: str
    score: float


def active_proxies(
    repository: Repository, *, today: date
) -> list[tuple[ProxyRecord, AttachmentRecord]]:
    """Active, readable proxies of committed, readable, retained attachments.

    A proxy or attachment that cannot be read (missing, modified, invalid) or
    whose sha256 no longer matches is left out: fail closed.
    """

    attachments: dict[str, AttachmentRecord | None] = {}
    rows: list[tuple[ProxyRecord, AttachmentRecord]] = []
    active = _active_attachments(repository)
    for _proxy_id, record, _by in repository.proxy_records():
        if not isinstance(record, ProxyRecord):
            continue
        owner = record.attachment_id
        if not active(owner):
            continue
        if owner not in attachments:
            try:
                attachments[owner] = repository.get_attachment(owner)
            except (IntegrityError, NotFoundError):
                attachments[owner] = None
        attachment = attachments[owner]
        if attachment is None or attachment.sha256 != record.source_sha256:
            continue
        if retention_ended(attachment, today):
            continue
        rows.append((record, attachment))
    return rows


class ProxyIndex:
    """The ``proxy_segments`` SQLite file of one vault."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def fingerprint(self) -> str | None:
        if not self.path.is_file():
            return None
        try:
            with closing(sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)) as connection:
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                if version != PROXY_INDEX_SCHEMA_VERSION:
                    return None
                row = connection.execute(
                    "SELECT value FROM proxy_meta WHERE key='fingerprint'"
                ).fetchone()
        except sqlite3.DatabaseError:
            return None
        return None if row is None else str(row[0])

    def is_current(self, events: Iterable[Event], *, today: date, vault_id: str = "") -> bool:
        stored = self.fingerprint()
        expected = _built_for(proxy_fingerprint(events, vault_id=vault_id), today)
        return stored is not None and stored == expected

    def rebuild(self, repository: Repository, *, today: date) -> int:
        """Write a complete replacement (no vault lock); returns the segment count.

        The fingerprint is read before the proxies: a write that lands while
        this runs makes the stored fingerprint stale, so the next call
        rebuilds again. The file is created 0600 in a 0700 directory and
        replaces the old one atomically; the vault is only read.
        """

        ensure_private_dir(self.path.parent)
        temporary = self.path.with_name(f".{self.path.name}.{secrets.token_hex(8)}.tmp")
        fingerprint = _built_for(
            proxy_fingerprint(repository.events(), vault_id=_vault_id(repository)), today
        )
        rows = active_proxies(repository, today=today)
        tokenizer = "trigram" if _supports_trigram() else "unicode61"
        segment: Callable[[str], list[str]] | None = (
            None if tokenizer == "trigram" else jieba_segmenter()
        )
        connection: sqlite3.Connection | None = None
        try:
            os.close(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
            connection = sqlite3.connect(temporary)
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute(
                "CREATE TABLE proxy_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE segment_rows (ref TEXT PRIMARY KEY, proxy_id TEXT NOT NULL, "
                "n INTEGER NOT NULL, attachment_id TEXT NOT NULL, kind TEXT NOT NULL, "
                "locator TEXT NOT NULL, coverage TEXT NOT NULL, tenant TEXT NOT NULL, "
                "privacy TEXT NOT NULL, text TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE VIRTUAL TABLE proxy_segments USING "
                f"fts5(ref UNINDEXED, text, tokenize='{tokenizer}')"
            )
            count = 0
            for record, attachment in rows:
                level = record.effective_level(attachment)
                for index, item in enumerate(record.segments, start=1):
                    ref = f"{record.id}:{index}"
                    connection.execute(
                        "INSERT INTO segment_rows VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (
                            ref,
                            record.id,
                            index,
                            record.attachment_id,
                            record.kind,
                            item.locator.fragment(),
                            record.coverage.status,
                            "",
                            level,
                            item.text,
                        ),
                    )
                    searchable = item.text if segment is None else " ".join(segment(item.text))
                    connection.execute(
                        "INSERT INTO proxy_segments (ref, text) VALUES (?,?)", (ref, searchable)
                    )
                    count += 1
            connection.executemany(
                "INSERT INTO proxy_meta VALUES (?,?)",
                [("fingerprint", fingerprint), ("tokenizer", tokenizer)],
            )
            connection.execute(f"PRAGMA user_version = {PROXY_INDEX_SCHEMA_VERSION}")
            connection.commit()
            connection.close()
            connection = None
            os.chmod(temporary, 0o600)
            with temporary.open("rb") as stream:
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            return count
        except BaseException:
            if connection is not None:
                connection.close()
            temporary.unlink(missing_ok=True)
            raise

    def search(
        self,
        query: str,
        *,
        tenant: str,
        clearance: frozenset[str] | None,
        limit: int,
    ) -> list[SegmentHit]:
        """Matching segments in this tenant whose level the clearance admits, best first."""

        needle = query.strip()
        if not needle or limit < 1 or not self.path.is_file():
            return []
        if clearance is not None:
            levels = sorted(level for level in clearance if level != "unknown")
            if not levels:
                return []
            where = "r.tenant = ? AND r.privacy IN (" + ",".join("?" * len(levels)) + ")"
            params: list[Any] = [tenant, *levels]
        else:
            where, params = "r.tenant = ?", [tenant]
        columns = (
            "r.ref, r.proxy_id, r.n, r.attachment_id, r.kind, r.locator, r.coverage, r.privacy"
        )
        with closing(sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)) as connection:
            tokenizer = str(
                connection.execute("SELECT value FROM proxy_meta WHERE key='tokenizer'").fetchone()[
                    0
                ]
            )
            found: list[SegmentHit] = []
            seen: set[str] = set()

            def add(rows: Iterable[tuple[Any, ...]], score: Callable[[Any], float]) -> None:
                for row in rows:
                    if row[0] in seen or len(found) >= limit:
                        continue
                    seen.add(str(row[0]))
                    found.append(
                        SegmentHit(
                            ref=str(row[0]),
                            proxy_id=str(row[1]),
                            n=int(row[2]),
                            attachment_id=str(row[3]),
                            kind=str(row[4]),
                            locator=str(row[5]),
                            coverage=str(row[6]),
                            privacy=str(row[7]),
                            score=score(row),
                        )
                    )

            if tokenizer == "trigram" and len(needle) == 2:
                rows = connection.execute(
                    f"SELECT {columns} FROM segment_rows AS r WHERE {where} "
                    "AND instr(lower(r.text), lower(?)) > 0 ORDER BY r.ref LIMIT ?",
                    [*params, needle, limit],
                )
                add(rows, lambda _row: 1.0)
                return found
            long_terms, short_terms = split_short_terms(query_terms(needle, tokenizer), tokenizer)
            match = fts_query_from_terms(long_terms)
            if match is not None:
                rows = connection.execute(
                    f"SELECT {columns}, bm25(proxy_segments) FROM proxy_segments AS f "
                    f"JOIN segment_rows AS r ON r.ref = f.ref WHERE proxy_segments MATCH ? "
                    f"AND {where} ORDER BY bm25(proxy_segments), r.ref LIMIT ?",
                    [match, *params, limit],
                )
                add(rows, lambda row: -float(row[8]))
            if short_terms and len(found) < limit:
                clause, short_params = short_terms_clause(["r.text"], short_terms)
                rows = connection.execute(
                    f"SELECT {columns} FROM segment_rows AS r WHERE {where} AND {clause} "
                    "ORDER BY r.ref",
                    [*params, *short_params],
                )
                add(rows, lambda _row: 0.0)
            return found


def _built_for(fingerprint: str, today: date) -> str:
    # Retention ends by date, so the same log is a new index on a new day.
    return f"{fingerprint}:{today.isoformat()}"


def ensure_proxy_index(
    repository: Repository, *, now: datetime, home: Path | None = None
) -> ProxyIndex | None:
    """The vault's proxy index, rebuilt when stale; ``None`` when the vault has no proxies.

    A vault without a ``proxy_add`` event never gets an index file. Works on a
    read-only vault (the index lives under the home directory).
    """

    events = repository.events()
    if not proxy_log(events).committed:
        return None
    index = ProxyIndex(proxy_index_path(repository, home=home))
    today = now.date()
    if not index.is_current(events, today=today, vault_id=_vault_id(repository)):
        index.rebuild(repository, today=today)
    return index


__all__ = [
    "LEGACY_PROXY_INDEX_NAME",
    "PROXY_INDEX_KIND",
    "PROXY_INDEX_SCHEMA_VERSION",
    "ProxyIndex",
    "SegmentHit",
    "active_proxies",
    "attachment_active",
    "drop_proxy_index",
    "ensure_proxy_index",
    "proxy_fingerprint",
    "proxy_index_path",
    "proxy_index_residue",
    "retention_ended",
]
