"""The relation-type table (K2): which typed edges exist and their constraints.

The table ships as ``relations.yaml`` next to this module. Version 1 lists only
relations that already live in vault fields (plus the K1 ``broader`` /
``narrower`` pair). Nothing on the search or context path reads it; it backs
lint now and the edge projection later. Loading is strict: an unknown key,
node kind, epistemic type or relation name, or a relation that is not
filtered by both privacy and tenant, is an error (fail closed).
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from memfw.core.epistemic import EPISTEMIC_VALUES

from .model import OntologyError

#: The only table version this memfw reads.
TABLE_VERSION = 1

_TOP_KEYS = frozenset({"version", "node_kinds", "relations"})
_REQUIRED = (
    "name",
    "from",
    "to",
    "fields",
    "derived",
    "typed",
    "cycles",
    "epistemic_max",
    "filter",
)
_OPTIONAL = frozenset({"inverse", "description"})
_FILE_KINDS = frozenset({"memory", "concept"})


class FieldMap(Mapping[str, tuple[str, ...]]):
    """``fields`` of a relation: file kind → field paths. Immutable and hashable.

    Compares equal to a plain ``dict`` with the same items, so
    ``relation.fields == {}`` still reads naturally; it cannot be changed in
    place, so the cached built-in table is safe to share.
    """

    __slots__ = ("_items",)

    def __init__(self, items: Mapping[str, Any] | None = None) -> None:
        pairs = {str(kind): tuple(paths) for kind, paths in (items or {}).items()}
        self._items: tuple[tuple[str, tuple[str, ...]], ...] = tuple(sorted(pairs.items()))

    def __getitem__(self, key: str) -> tuple[str, ...]:
        for kind, paths in self._items:
            if kind == key:
                return paths
        raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        return iter(kind for kind, _paths in self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __hash__(self) -> int:
        return hash(self._items)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, FieldMap):
            return self._items == other._items
        if isinstance(other, Mapping):
            return dict(self._items) == dict(other.items())
        return NotImplemented

    def __repr__(self) -> str:
        return f"FieldMap({dict(self._items)!r})"


@dataclass(frozen=True)
class RelationType:
    """One row of the table. Immutable and hashable (every field is)."""

    name: str
    sources: tuple[str, ...]
    targets: tuple[str, ...]
    fields: FieldMap
    derived: bool
    typed: bool
    cycles: bool
    epistemic_max: str
    filter_privacy: bool
    filter_tenant: bool
    inverse: str | None = None
    description: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "from": list(self.sources),
            "to": list(self.targets),
            "fields": {kind: list(paths) for kind, paths in sorted(self.fields.items())},
            "inverse": self.inverse,
            "derived": self.derived,
            "typed": self.typed,
            "cycles": self.cycles,
            "epistemic_max": self.epistemic_max,
            "filter": {"privacy": self.filter_privacy, "tenant": self.filter_tenant},
            "description": self.description,
        }


@dataclass(frozen=True)
class RelationTable:
    version: int
    node_kinds: tuple[str, ...]
    relations: tuple[RelationType, ...]

    def get(self, name: str) -> RelationType | None:
        for relation in self.relations:
            if relation.name == name:
                return relation
        return None

    def names(self) -> frozenset[str]:
        return frozenset(relation.name for relation in self.relations)

    def written_on(self, kind: str) -> frozenset[str]:
        """Relation names a file of ``kind`` (``memory`` or ``concept``) may carry."""
        return frozenset(relation.name for relation in self.relations if kind in relation.fields)

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "node_kinds": list(self.node_kinds),
            "relations": [relation.as_dict() for relation in self.relations],
        }


def check_relation_table(data: Any) -> list[dict[str, str]]:
    """Problems with a parsed table. An empty list means it loads."""
    issues: list[dict[str, str]] = []

    def issue(kind: str, name: str, detail: str) -> None:
        issues.append({"kind": kind, "relation": name, "detail": detail})

    if not isinstance(data, dict):
        issue("relation_table_invalid", "", "table is not a mapping")
        return issues
    for key in sorted(set(data) - _TOP_KEYS):
        issue("relation_table_invalid", "", f"unknown key {key}")
    version = data.get("version")
    # ``True == 1`` in Python: a boolean is not a version.
    if isinstance(version, bool) or not isinstance(version, int) or version != TABLE_VERSION:
        issue("relation_table_version", "", f"version {data.get('version')!r} is not supported")
    kinds = data.get("node_kinds")
    if not isinstance(kinds, list) or not kinds or any(not isinstance(k, str) for k in kinds):
        issue("relation_table_invalid", "", "node_kinds must be a list of strings")
        kinds = []
    known_kinds = set(kinds)
    rows = data.get("relations")
    if not isinstance(rows, list) or not rows:
        issue("relation_table_invalid", "", "relations must be a non-empty list")
        return issues
    names: list[str] = []
    for row in rows:
        if isinstance(row, dict) and isinstance(row.get("name"), str):
            names.append(row["name"])
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            issue("relation_table_invalid", "", "relation is not a mapping")
            continue
        name = row.get("name")
        if not isinstance(name, str) or not name:
            issue("relation_table_invalid", "", "relation has no name")
            continue
        if name in seen:
            issue("relation_table_duplicate", name, "relation is listed twice")
        seen.add(name)
        for key in _REQUIRED:
            if key not in row:
                issue("relation_table_invalid", name, f"missing {key}")
        for key in sorted(set(row) - set(_REQUIRED) - _OPTIONAL):
            issue("relation_table_invalid", name, f"unknown key {key}")
        for end in ("from", "to"):
            value = row.get(end)
            if not isinstance(value, list) or not value:
                issue("relation_table_invalid", name, f"{end} must be a non-empty list")
                continue
            for kind in value:
                if not isinstance(kind, str):
                    issue("relation_table_invalid", name, f"{end} entries must be node kinds")
                elif kind not in known_kinds:
                    issue("relation_unknown_node_kind", name, f"{end}: {kind!r}")
        fields = row.get("fields")
        if not isinstance(fields, dict):
            issue("relation_table_invalid", name, "fields must be a mapping")
        else:
            for kind, paths in fields.items():
                if not isinstance(kind, str) or kind not in _FILE_KINDS:
                    issue("relation_table_invalid", name, f"fields: unknown file kind {kind!r}")
                if (
                    not isinstance(paths, list)
                    or not paths
                    or any(not isinstance(p, str) or not p for p in paths)
                ):
                    issue("relation_table_invalid", name, f"fields.{kind} must list field paths")
            if row.get("derived") is False and not fields:
                issue("relation_table_invalid", name, "a written relation needs fields")
        for flag in ("derived", "typed", "cycles"):
            if flag in row and not isinstance(row[flag], bool):
                issue("relation_table_invalid", name, f"{flag} must be true or false")
        if row.get("epistemic_max") not in EPISTEMIC_VALUES:
            issue("relation_table_invalid", name, f"epistemic_max {row.get('epistemic_max')!r}")
        flt = row.get("filter")
        if not isinstance(flt, dict) or set(flt) != {"privacy", "tenant"}:
            issue("relation_table_invalid", name, "filter must name privacy and tenant")
        elif flt.get("privacy") is not True or flt.get("tenant") is not True:
            issue("relation_unfiltered", name, "every relation is filtered by privacy and tenant")
        inverse = row.get("inverse")
        if inverse is not None:
            if not isinstance(inverse, str) or inverse not in names:
                issue("relation_unknown_type", name, f"inverse {inverse!r}")
            else:
                other = next(r for r in rows if isinstance(r, dict) and r.get("name") == inverse)
                if other.get("inverse") != name:
                    issue("relation_table_invalid", name, f"inverse {inverse} does not point back")
        description = row.get("description")
        if description is not None and not isinstance(description, str):
            issue("relation_table_invalid", name, "description must be text")
    return issues


def parse_relation_table(data: Any) -> RelationTable:
    """Build a table from parsed YAML. Raises ``OntologyError`` on any problem."""
    issues = check_relation_table(data)
    if issues:
        first = issues[0]
        where = f" ({first['relation']})" if first["relation"] else ""
        raise OntologyError(f"relation table{where}: {first['kind']}: {first['detail']}")
    relations = tuple(
        RelationType(
            name=row["name"],
            sources=tuple(row["from"]),
            targets=tuple(row["to"]),
            fields=FieldMap(row["fields"]),
            derived=row["derived"],
            typed=row["typed"],
            cycles=row["cycles"],
            epistemic_max=row["epistemic_max"],
            filter_privacy=True,
            filter_tenant=True,
            inverse=row.get("inverse"),
            description=row.get("description") or "",
        )
        for row in data["relations"]
    )
    return RelationTable(
        version=data["version"], node_kinds=tuple(data["node_kinds"]), relations=relations
    )


def load_relation_table(path: Path | None = None) -> RelationTable:
    """Read a table file; without ``path``, the built-in table."""
    if path is None:
        return builtin_relation_table()
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise OntologyError(f"relation table {Path(path).name} cannot be read") from exc
    return parse_relation_table(data)


@lru_cache(maxsize=1)
def builtin_relation_table() -> RelationTable:
    text = resources.files("memfw.ontology").joinpath("relations.yaml").read_text(encoding="utf-8")
    return parse_relation_table(yaml.safe_load(text))


__all__ = [
    "TABLE_VERSION",
    "FieldMap",
    "RelationTable",
    "RelationType",
    "builtin_relation_table",
    "check_relation_table",
    "load_relation_table",
    "parse_relation_table",
]
