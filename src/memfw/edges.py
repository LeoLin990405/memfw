"""Typed-edge projection (K3): the vault's relations as one rebuildable SQLite file.

``collect_edges`` reads vault fields and turns them into ``memfw.ports.Edge``
values, one relation of the K2 relation table (``ontology/relations.yaml``)
each; ``SqliteProjectionStore`` stores them behind the ``ProjectionStore``
port. The file (``edges.sqlite3`` next to ``index.sqlite3``, mode 0600) is
never the authority: it can be deleted and rebuilt, and building it only
reads the vault. Nothing on the search or context path reads it.

**What becomes an edge** (``field`` names where it was read):

* memory ``about`` / ``applies_to`` → concept. The value is a concept oid
  (followed through ``merged_into``), else a label or alias with one owner;
  any other value stays a concept node named by the value itself;
* memory ``superseded_by`` → memory; concept ``superseded_by`` /
  ``supersedes`` (stored as ``superseded_by`` read backwards) → memory or
  concept. ``supersedes`` is never stored: a K4 pattern walks
  ``superseded_by`` backwards for it;
* ``evidence`` → memory, concept, ``att:`` attachment, ``kh:`` note or
  external reference; an ``att:`` ref also gives ``observed_in`` and a
  ``kh:`` ref ``cites`` (memories only), as does a ``kh:`` ref in the body;
* ``causal_record.treatment`` / ``.outcome`` → concept (``treatment_of`` /
  ``outcome_of``), only when the text names a concept;
* ``experience.task`` → task, ``experience.evidence`` → memory (``experience_of``);
* ``related`` → memory or concept;
* an open question's ``open_question.conflicts`` and ``.resolved_by`` →
  memory (``conflicts``, ``resolved_by``);
* concept ``broader`` → concept (``narrower`` is ``broader`` walked
  backwards), and every other concept-file relation of the table → concept.
  A merged concept's relations count for the concept it was merged into.

A memory target must exist in the vault (a dangling id is dropped, except as
an external evidence string), self loops are dropped, the same
``(relation, source, target)`` read from two fields is one edge (the first
field in sort order), and a relation or node kind the table does not allow is
never built.

**Visibility.** Each edge carries both endpoints' tenant and privacy level
(``memfw.ports.projection_store``). A memory's level is the MM0 rule every
exit uses (``memfw.ports.privacy.effective_level``: the ``privacy`` field,
else the vault's ``legacy_privacy_default``, then ``privacy:*`` and
``sensitivity:*`` tags and a top-level ``sensitivity`` key; strictest wins).
A concept is shared vocabulary: it has no tenant, and a level only when its
file sets ``privacy``, privacy tags or ``sensitivity`` (then read the same
way, without a legacy default); a merged concept carries the strictest of
itself and its target. An edge between memories of two tenants is dropped
when the projection is built. The port's query filters every edge by the
caller's tenant and clearance; ``memfw.runtime.graph`` re-checks every
memory against the live vault as well, so a memory made private after the
last rebuild is hidden before the next one.

Rebuilds are deterministic: the same vault gives the same edges, version and
file bytes.
"""

from __future__ import annotations

import os
import re
import secrets
import sqlite3
from collections.abc import Collection, Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from .core.errors import MemfwError
from .core.models import MemoryDocument
from .core.repository import Repository
from .ontology.model import AliasIndex, Concept, _resolve, load_concepts, project_aliases
from .ontology.normalise import normalise
from .ontology.relations import RelationTable, builtin_relation_table
from .ontology.taxonomy import broader_of
from .ports.privacy import (
    SENSITIVITY_KEY,
    PrivacyLevel,
    effective_level,
    field_level,
    legacy_default_from_config,
    sensitivity_key_levels,
    strictest,
    tag_levels,
)
from .ports.projection_store import (
    Direction,
    Edge,
    EdgeScope,
    RebuildResult,
    check_query,
    checked_edges,
    edge_set_version,
    edge_visible,
)
from .ports.types import Availability

SCHEMA_VERSION: Final = 1
FILENAME: Final = "edges.sqlite3"
#: Stored relation → the inverse name a pattern may use for it (walked backwards).
INVERSE_ALIASES: Final[dict[str, str]] = {"supersedes": "superseded_by", "narrower": "broader"}
_MEMORY_PREFIXES: Final = ("memory:", "mem:")
_BODY_NOTE = re.compile(r"(?<![A-Za-z0-9_])kh:[^\s)\]>\"'`]+")
_TRAILING = ".,;:!?"


class EdgeError(MemfwError):
    """The edge projection cannot be built or read (message is safe to show)."""


def projection_path(root: Path | str) -> Path:
    """Where the projection lives: next to ``index.sqlite3`` at the vault root."""

    return Path(root) / FILENAME


# --- building -----------------------------------------------------------------------


def _instant(value: datetime | None) -> str | None:
    if value is None:
        return None
    moment = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return moment.astimezone(UTC).isoformat()


def concept_level(concept: Concept) -> PrivacyLevel | None:
    """A concept file's own level, ``None`` when it states none (see the module docstring)."""

    extras = concept.extras
    levels: list[str] = []
    if "privacy" in extras:
        levels.append(field_level(extras["privacy"]))
    tags = extras.get("tags")
    if tags is not None:
        levels.extend(tag_levels(tags) if isinstance(tags, list) else ["unknown"])
    if SENSITIVITY_KEY in extras:
        levels.extend(sensitivity_key_levels(extras[SENSITIVITY_KEY]))
    return strictest(levels) if levels else None


def _join(levels: Iterable[str | None]) -> str | None:
    present = [level for level in levels if level is not None]
    return strictest(present) if present else None


def concept_ref(
    concepts: dict[str, Concept], aliases: AliasIndex, value: str
) -> tuple[str, str | None] | None:
    """``(oid, level)`` of the concept ``value`` names (through merges), else ``None``.

    ``value`` is an oid, or a label or alias with exactly one owner.
    """

    if value in concepts:
        target = _resolve(value, concepts)
        final = concepts.get(target)
        levels = [concept_level(concepts[value]), concept_level(final) if final else None]
        return target, _join(levels)
    oid, _ambiguous = aliases.lookup(normalise(value))
    if oid is not None and oid in concepts:
        return oid, concept_level(concepts[oid])
    return None


class _Builder:
    def __init__(self, repository: Repository, table: RelationTable) -> None:
        self.table = table
        config = repository.read_config() if repository.config_path.exists() else {}
        self.legacy = legacy_default_from_config(config)
        self.concepts = load_concepts(repository.root)
        self.aliases = project_aliases(self.concepts)
        self.memories: dict[str, MemoryDocument] = {
            document.id: document for document in repository.list()
        }
        self.edges: dict[tuple[str, str, str], Edge] = {}

    # nodes

    def memory_level(self, document: MemoryDocument) -> str:
        return effective_level(
            document.privacy,
            document.tags,
            legacy=self.legacy,
            sensitivity=document.top_level_sensitivity,
        )

    def concept_oid(self, value: str) -> tuple[str, str | None] | None:
        return concept_ref(self.concepts, self.aliases, value)

    def node(self, value: str, kinds: Collection[str]) -> tuple[str, str, str | None, str | None]:
        """``(id, kind, tenant, level)`` for a reference, or ``("", ...)`` when it names none."""

        text = value.strip()
        bare = text
        for prefix in _MEMORY_PREFIXES:
            if bare.lower().startswith(prefix):
                bare = bare[len(prefix) :]
                break
        if "memory" in kinds and bare in self.memories:
            document = self.memories[bare]
            return bare, "memory", document.tenant, self.memory_level(document)
        if "concept" in kinds:
            concept = self.concept_oid(text)
            if concept is not None:
                return concept[0], "concept", None, concept[1]
        lowered = text.lower()
        if "attachment" in kinds and lowered.startswith("att:"):
            return text, "attachment", None, None
        if "note" in kinds and lowered.startswith("kh:"):
            return text, "note", None, None
        if "external" in kinds and text:
            return text, "external", None, None
        return "", "", None, None

    # edges

    def add(
        self,
        relation: str,
        source: tuple[str, str, str | None, str | None],
        target: tuple[str, str, str | None, str | None],
        field: str,
        valid: tuple[str | None, str | None] = (None, None),
    ) -> None:
        row = self.table.get(relation)
        if row is None or not source[0] or not target[0]:
            return
        if source[1] not in row.sources or target[1] not in row.targets:
            return
        if source[0] == target[0]:
            return
        if source[2] is not None and target[2] is not None and source[2] != target[2]:
            return  # cross-tenant: never built
        try:
            edge = Edge(
                relation,
                source[0],
                target[0],
                source[1],
                target[1],
                field,
                source_tenant=source[2],
                target_tenant=target[2],
                source_privacy=source[3],
                target_privacy=target[3],
                valid_from=valid[0],
                valid_to=valid[1],
                epistemic_max=row.epistemic_max,
            )
        except (TypeError, ValueError):
            return  # an id memfw cannot store (too long, multi-line)
        prior = self.edges.get(edge.key)
        if prior is None or edge.field < prior.field:
            self.edges[edge.key] = edge

    def window(self, *documents: MemoryDocument | None) -> tuple[str | None, str | None]:
        starts = [d.valid_from for d in documents if d is not None and d.valid_from is not None]
        ends = [d.valid_to for d in documents if d is not None and d.valid_to is not None]
        return _instant(max(starts) if starts else None), _instant(min(ends) if ends else None)

    def memory_edges(self, document: MemoryDocument) -> None:
        me = (document.id, "memory", document.tenant, self.memory_level(document))

        def link(relation: str, value: object, field: str, kinds: Collection[str]) -> None:
            if not isinstance(value, str) or not value.strip():
                return
            target = self.node(value, kinds)
            other = self.memories.get(target[0]) if target[1] == "memory" else None
            self.add(relation, me, target, field, self.window(document, other))

        concept_only = ("concept",)
        for relation in ("about", "applies_to"):
            for value in getattr(document, relation):
                if not isinstance(value, str) or not value.strip():
                    continue
                target = self.node(value, concept_only)
                if not target[0]:
                    target = (value.strip(), "concept", None, None)
                self.add(relation, me, target, relation, self.window(document))
        link("superseded_by", document.superseded_by, "superseded_by", ("memory",))
        evidence_kinds = ("memory", "concept", "attachment", "note", "external")
        for value in document.evidence:
            link("evidence", value, "evidence", evidence_kinds)
            if isinstance(value, str):
                lowered = value.strip().lower()
                if lowered.startswith("att:"):
                    link("observed_in", value, "evidence", ("attachment",))
                elif lowered.startswith("kh:"):
                    link("cites", value, "evidence", ("note",))
        for found in _BODY_NOTE.finditer(document.body or ""):
            link("cites", found.group(0).rstrip(_TRAILING), "body", ("note",))
        record = document.causal_record
        if record is not None:
            for relation, value in (
                ("treatment_of", record.treatment),
                ("outcome_of", record.outcome),
            ):
                link(relation, value, f"causal_record.{relation.split('_')[0]}", concept_only)
        experience = document.experience
        if experience is not None:
            task = experience.task.strip()
            if task:
                self.add("experience_of", me, (task, "task", None, None), "experience.task")
            for value in experience.evidence:
                link("experience_of", value, "experience.evidence", ("memory",))
        for value in document.related:
            link("related", value, "related", ("memory", "concept"))
        question = document.open_question
        if question is not None:
            for value in question.conflicts:
                link("conflicts", value, "open_question.conflicts", ("memory",))
            link("resolved_by", question.resolved_by, "open_question.resolved_by", ("memory",))

    def concept_edges(self, oid: str, concept: Concept) -> None:
        resolved = self.concept_oid(oid)
        if resolved is None:
            return
        me = (resolved[0], "concept", None, resolved[1])
        for parent in broader_of(concept) or ():
            if parent in self.concepts:
                self.add("broader", me, self.node(parent, ("concept",)), "broader")
        written = self.table.written_on("concept") - {"broader"}
        for key in sorted(written & set(concept.extras)):
            value = concept.extras[key]
            values = [value] if isinstance(value, str) else value if isinstance(value, list) else []
            for item in values:
                if not isinstance(item, str) or not item.strip():
                    continue
                if key == "evidence":
                    kinds: tuple[str, ...] = ("memory", "concept", "attachment", "note", "external")
                elif key in ("supersedes", "superseded_by"):
                    kinds = ("memory", "concept")
                else:
                    kinds = ("concept",)
                other = self.node(item, kinds)
                if key == "supersedes":
                    # Stored one way only: the older record is superseded_by this one.
                    self.add("superseded_by", other, me, "supersedes")
                else:
                    self.add(key, me, other, key)

    def build(self) -> list[Edge]:
        for memory_id in sorted(self.memories):
            self.memory_edges(self.memories[memory_id])
        for oid in sorted(self.concepts):
            self.concept_edges(oid, self.concepts[oid])
        return [self.edges[key] for key in sorted(self.edges)]


def collect_edges(repository: Repository, table: RelationTable | None = None) -> list[Edge]:
    """Every edge the vault's fields give (see the module docstring), sorted by key."""

    return _Builder(repository, table or builtin_relation_table()).build()


# --- the SQLite store ----------------------------------------------------------------

_COLUMNS: Final = (
    "relation",
    "source",
    "target",
    "source_kind",
    "target_kind",
    "field",
    "source_tenant",
    "target_tenant",
    "source_privacy",
    "target_privacy",
    "valid_from",
    "valid_to",
    "epistemic_max",
)
_SCHEMA: Final = (
    "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID",
    "CREATE TABLE edges (relation TEXT NOT NULL, source TEXT NOT NULL, target TEXT NOT NULL,"
    " source_kind TEXT NOT NULL, target_kind TEXT NOT NULL, field TEXT NOT NULL,"
    " source_tenant TEXT, target_tenant TEXT, source_privacy TEXT, target_privacy TEXT,"
    " valid_from TEXT, valid_to TEXT, epistemic_max TEXT NOT NULL,"
    " PRIMARY KEY (relation, source, target)) WITHOUT ROWID",
    "CREATE INDEX edges_by_source ON edges (source, relation, target)",
    "CREATE INDEX edges_by_target ON edges (target, relation, source)",
)


class SqliteProjectionStore:
    """The ``ProjectionStore`` the CLI uses: one SQLite file, replaced whole on rebuild.

    ``forbid`` names a directory the file may never be written into (the
    vault's ``memories/``). The file is created 0600 with ``O_EXCL`` and
    renamed into place, so a reader never sees a half-built projection.
    """

    name = "sqlite"

    def __init__(self, path: Path | str, *, forbid: Path | None = None) -> None:
        self.path = Path(path)
        self.forbid = forbid

    def available(self) -> Availability:
        return Availability.ok()

    def _connect(self) -> sqlite3.Connection:
        if self.path.is_symlink():
            raise EdgeError("refusing a symlinked edge projection")
        uri = f"{self.path.resolve().as_uri()}?mode=ro"
        return sqlite3.connect(uri, uri=True)

    def rebuild(self, edges: Iterable[Edge]) -> RebuildResult:
        rows = checked_edges(edges)
        version = edge_set_version(rows)
        target = self.path
        if self.forbid is not None:
            forbidden = self.forbid.resolve()
            resolved = target.resolve()
            if resolved == forbidden or forbidden in resolved.parents:
                raise EdgeError("refusing to write the edge projection inside memories/")
        if target.is_symlink():
            raise EdgeError("refusing a symlinked edge projection")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{secrets.token_hex(8)}.tmp")
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
        os.close(os.open(temporary, flags, 0o600))
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
                connection.executemany(
                    "INSERT INTO meta (key, value) VALUES (?, ?)",
                    [("schema_version", str(SCHEMA_VERSION)), ("version", version)],
                )
                placeholders = ", ".join("?" for _ in _COLUMNS)
                connection.executemany(
                    f"INSERT INTO edges ({', '.join(_COLUMNS)}) VALUES ({placeholders})",
                    [tuple(getattr(edge, column) for column in _COLUMNS) for edge in rows],
                )
        except BaseException:
            connection.close()
            temporary.unlink(missing_ok=True)
            raise
        connection.close()
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
        return RebuildResult(edges=len(rows), version=version)

    def version(self) -> str | None:
        if not self.path.is_file():
            return None
        connection = self._connect()
        try:
            schema = connection.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()
            if schema is None or schema[0] != str(SCHEMA_VERSION):
                raise EdgeError("edge projection has another schema version: rebuild it")
            row = connection.execute("SELECT value FROM meta WHERE key = 'version'").fetchone()
        except sqlite3.DatabaseError as exc:
            raise EdgeError(f"unreadable edge projection: {exc}") from exc
        finally:
            connection.close()
        return str(row[0]) if row else None

    def _rows(
        self, node: str, direction: Direction, relations: frozenset[str] | None
    ) -> Iterator[Edge]:
        if not self.path.is_file():
            return
        end = "source" if direction == "out" else "target"
        sql = f"SELECT {', '.join(_COLUMNS)} FROM edges WHERE {end} = ?"
        args: list[Any] = [node]
        if relations is not None:
            if not relations:
                return
            names = sorted(relations)
            sql += f" AND relation IN ({', '.join('?' for _ in names)})"
            args.extend(names)
        sql += " ORDER BY relation, source, target"
        connection = self._connect()
        try:
            found = connection.execute(sql, args).fetchall()
        except sqlite3.DatabaseError as exc:
            raise EdgeError(f"unreadable edge projection: {exc}") from exc
        finally:
            connection.close()
        for values in found:
            try:
                yield Edge(**dict(zip(_COLUMNS, values, strict=True)))
            except (TypeError, ValueError):
                continue  # a row memfw would never write: fail closed

    def query(
        self,
        scope: EdgeScope,
        *,
        node: str,
        direction: Direction = "out",
        relations: Collection[str] | None = None,
        limit: int = 100,
    ) -> tuple[Edge, ...]:
        names = check_query(scope, node, direction, relations, limit)
        out: list[Edge] = []
        for edge in self._rows(node, direction, names):
            if edge_visible(edge, scope):
                out.append(edge)
                if len(out) >= limit:
                    break
        return tuple(out)


def vault_store(repository: Repository) -> SqliteProjectionStore:
    """The store at the vault's default path, refusing ``memories/``."""

    return SqliteProjectionStore(projection_path(repository.root), forbid=repository.memories_dir)


def rebuild(repository: Repository, store: SqliteProjectionStore | None = None) -> RebuildResult:
    """Rebuild the projection from the vault. Read-only on the vault."""

    return (store or vault_store(repository)).rebuild(collect_edges(repository))


__all__ = [
    "FILENAME",
    "INVERSE_ALIASES",
    "SCHEMA_VERSION",
    "EdgeError",
    "SqliteProjectionStore",
    "collect_edges",
    "concept_level",
    "concept_ref",
    "projection_path",
    "rebuild",
    "vault_store",
]
