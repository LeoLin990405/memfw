"""``proxy_segments``: the disposable full-text index over attachment proxies (MM2b).

A separate SQLite file, ``<vault>/proxy_index.sqlite3``, never the memory
index: memory search, the HTTP read-only service and every memory export keep
reading ``index.sqlite3`` alone, so they can never return proxy text. The
table uses the memory index's tokenizer choice (FTS5 trigram when SQLite has
it, else jieba segments in unicode61) and the same query terms, including the
two-character ``instr`` fallback on a trigram index.

Each row is one segment of an **active** proxy (not superseded) of a
committed, readable attachment whose retention has not ended, with the
segment's canonical locator, the proxy's kind and coverage, the tenant
(attachments belong to the vault's default tenant ``""``) and the effective
privacy level (the strictest of the proxy's and the attachment's). Search
filters by tenant and clearance in SQL; callers re-check every hit against the
proxy file and the attachment record before returning text.

The index records a fingerprint of the attachment and proxy events it was
built from; :meth:`ProxyIndex.is_current` compares it with the log, and
:func:`ensure_proxy_index` rebuilds when they differ. It is never built on a
vault without proxies.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections.abc import Callable, Iterable
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Final

from ..attachments.proxies import ProxyRecord, proxy_log
from ..attachments.records import AttachmentRecord
from ..core.errors import IntegrityError, NotFoundError
from ..core.models import ATTACHMENT_EVENT_KINDS, Event, EventKind
from ..core.repository import Repository
from .fts_terms import (
    fts_query_from_terms,
    jieba_segmenter,
    query_terms,
    short_terms_clause,
    split_short_terms,
)

PROXY_INDEX_NAME: Final = "proxy_index.sqlite3"
PROXY_INDEX_SCHEMA_VERSION: Final = 1
_PROXY_KINDS: Final = frozenset({EventKind.PROXY_ADD, EventKind.PROXY_SUPERSEDE})


def proxy_index_path(repository: Repository) -> Path:
    return repository.root / PROXY_INDEX_NAME


def proxy_fingerprint(events: Iterable[Event]) -> str:
    """Hash of every attachment and proxy event (kind, id, hashes, supersession)."""

    digest = hashlib.sha256()
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
    for _proxy_id, record, _by in repository.proxy_records():
        if not isinstance(record, ProxyRecord):
            continue
        owner = record.attachment_id
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

    def is_current(self, events: Iterable[Event], *, today: date) -> bool:
        stored = self.fingerprint()
        return stored is not None and stored == _built_for(proxy_fingerprint(events), today)

    def rebuild(self, repository: Repository, *, today: date) -> int:
        """Write a complete replacement under the vault lock; returns the segment count."""

        temporary = self.path.with_name(self.path.name + ".tmp")
        with repository._locked():
            temporary.unlink(missing_ok=True)
            fingerprint = _built_for(proxy_fingerprint(repository.events()), today)
            rows = active_proxies(repository, today=today)
            tokenizer = "trigram" if _supports_trigram() else "unicode61"
            segment: Callable[[str], list[str]] | None = (
                None if tokenizer == "trigram" else jieba_segmenter()
            )
            connection: sqlite3.Connection | None = None
            try:
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


def ensure_proxy_index(repository: Repository, *, now: datetime) -> ProxyIndex | None:
    """The vault's proxy index, rebuilt when stale; ``None`` when the vault has no proxies.

    A vault without a ``proxy_add`` event never gets a ``proxy_index.sqlite3``.
    """

    events = repository.events()
    if not proxy_log(events).committed:
        return None
    index = ProxyIndex(proxy_index_path(repository))
    today = now.date()
    if not index.is_current(events, today=today):
        index.rebuild(repository, today=today)
    return index


__all__ = [
    "PROXY_INDEX_NAME",
    "PROXY_INDEX_SCHEMA_VERSION",
    "ProxyIndex",
    "SegmentHit",
    "active_proxies",
    "ensure_proxy_index",
    "proxy_fingerprint",
    "proxy_index_path",
    "retention_ended",
]
