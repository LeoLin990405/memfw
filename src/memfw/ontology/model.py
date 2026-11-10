"""Concept files under ``concepts/<class>/<slug>.md``.

Lint reports unknown classes, duplicate aliases, and dangling oids. It never
rewrites a file to hide one of those. Alias ownership is a projection of the
files and can be rebuilt.
"""

from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .normalise import normalise

CLASSES = frozenset(
    {
        "Person",
        "Agent",
        "Host",
        "Project",
        "Repo",
        "Workspace",
        "Ticket",
        "Artifact",
        "Service",
        "Concept",
        "Fact",
        "Decision",
        "Preference",
        "Constraint",
        "Lesson",
        "Procedure",
        "Event",
        "Incident",
        "Reference",
        "Observation",
    }
)
_STATUSES = frozenset({"candidate", "active", "retired"})
_STATUS_RANK = {"active": 2, "retired": 1, "candidate": 0}
_CORE_KEYS = frozenset(
    {"oid", "class", "pref_label", "alt_labels", "same_as", "status", "merged_into"}
)
RELATIONS = frozenset(
    {
        "about",
        "applies_to",
        "decided_by",
        "authored_by",
        "supersedes",
        "superseded_by",
        "contradicts",
        "derived_from",
        "evidence",
        "part_of",
        "runs_on",
    }
)
_MAX_CHAIN = 16
_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_CLASS_DIR = re.compile(r"^[A-Za-z][A-Za-z0-9]*$")


class OntologyError(ValueError):
    """A concept command cannot do what it was asked."""


@dataclass
class Label:
    text: str
    scope: str = ""


@dataclass
class Concept:
    oid: str
    class_name: str
    pref_label: str
    alt_labels: tuple[Label, ...] = ()
    same_as: tuple[str, ...] = ()
    status: str = "candidate"
    merged_into: str | None = None
    definition: str = ""
    path: Path | None = None
    extras: dict[str, Any] = field(default_factory=dict)
    collisions: tuple[str, ...] = ()

    def names(self) -> tuple[Label, ...]:
        labels = [Label(self.pref_label)]
        labels.extend(self.alt_labels)
        labels.extend(Label(item) for item in self.same_as)
        return tuple(labels)


@dataclass
class AliasIndex:
    """Rebuildable alias → oid projection. Ambiguous keys are not winners.

    ``by_alias`` and ``ambiguous`` hold unscoped labels only. ``scoped`` and
    ``scoped_ambiguous`` are keyed by scope first; each entry already folds in
    the unscoped owner of the same key, so a scoped label that names a
    different oid than an unscoped one is ambiguous inside that scope.
    """

    by_alias: dict[str, str] = field(default_factory=dict)
    ambiguous: dict[str, tuple[str, ...]] = field(default_factory=dict)
    scoped: dict[str, dict[str, str]] = field(default_factory=dict)
    scoped_ambiguous: dict[str, dict[str, tuple[str, ...]]] = field(default_factory=dict)

    def lookup(self, key: str, scope: str = "") -> tuple[str | None, tuple[str, ...]]:
        """Return ``(oid, ())`` for a winner, ``(None, oids)`` when ambiguous.

        A scoped label resolves only when ``scope`` equals its scope.
        """
        scope = scope.strip()
        if scope:
            if key in self.scoped_ambiguous.get(scope, {}):
                return None, self.scoped_ambiguous[scope][key]
            if key in self.scoped.get(scope, {}):
                return self.scoped[scope][key], ()
        if key in self.ambiguous:
            return None, self.ambiguous[key]
        return self.by_alias.get(key), ()


def load_concepts(vault: Path) -> dict[str, Concept]:
    """Read every concept file. A file that cannot be parsed raises and is not rewritten."""
    root = Path(vault)
    directory = root / "concepts"
    found: dict[str, Concept] = {}
    if directory.is_symlink() or not directory.is_dir():
        return found
    for class_dir in sorted(directory.iterdir()):
        if class_dir.is_symlink() or not class_dir.is_dir():
            continue
        for path in sorted(class_dir.glob("*.md")):
            if path.is_symlink() or not path.is_file():
                continue
            concept = _read_concept(path)
            prior = found.get(concept.oid)
            if prior is None:
                found[concept.oid] = concept
                continue
            found[concept.oid] = _prefer_authority(prior, concept)
    return found


def lint_concepts(concepts: dict[str, Concept]) -> list[dict[str, Any]]:
    """Review list. The files are not modified."""
    items: list[dict[str, Any]] = []
    known = set(concepts)
    for concept in concepts.values():
        if concept.class_name not in CLASSES:
            items.append(
                {
                    "kind": "unknown_class",
                    "oid": concept.oid,
                    "detail": concept.class_name,
                }
            )
        if concept.status not in _STATUSES:
            items.append(
                {
                    "kind": "unknown_status",
                    "oid": concept.oid,
                    "detail": concept.status,
                }
            )
        if concept.merged_into and concept.merged_into not in known:
            items.append(
                {
                    "kind": "dangling_oid",
                    "oid": concept.oid,
                    "detail": concept.merged_into,
                }
            )
        if concept.collisions:
            items.append(
                {
                    "kind": "duplicate_oid",
                    "oid": concept.oid,
                    "detail": concept.collisions[0],
                    "paths": [str(concept.path or ""), *concept.collisions],
                }
            )
        for key, value in concept.extras.items():
            if key not in RELATIONS:
                items.append(
                    {
                        "kind": "unknown_relation",
                        "oid": concept.oid,
                        "detail": key,
                    }
                )
            for referent in _relation_oids(value):
                if referent not in known:
                    items.append(
                        {
                            "kind": "dangling_oid",
                            "oid": concept.oid,
                            "detail": referent,
                        }
                    )
    unscoped, scoped = _label_owners(concepts)
    for key, oids in sorted(unscoped.items()):
        if len(oids) > 1:
            items.append(
                {
                    "kind": "duplicate_alias",
                    "oid": oids[0],
                    "detail": key,
                    "oids": list(oids),
                }
            )
    for scope, keys in sorted(scoped.items()):
        for key, oids in sorted(_fold_scope(unscoped, keys).items()):
            if len(oids) > 1:
                items.append(
                    {
                        "kind": "duplicate_alias",
                        "oid": oids[0],
                        "detail": key,
                        "scope": scope,
                        "oids": list(oids),
                    }
                )
    reported: set[frozenset[str]] = set()
    for concept in concepts.values():
        status, chain = _walk(concept.oid, concepts)
        if status == "cycle":
            members = frozenset(chain)
            if members in reported:
                continue
            reported.add(members)
            items.append(
                {
                    "kind": "merge_cycle",
                    "oid": concept.oid,
                    "detail": " ".join(chain),
                    "oids": list(chain),
                }
            )
        elif status == "depth":
            items.append(
                {
                    "kind": "redirect_depth",
                    "oid": concept.oid,
                    "detail": str(len(chain)),
                    "oids": list(chain),
                }
            )
    return items


def project_aliases(concepts: dict[str, Concept]) -> AliasIndex:
    """Alias projection. Duplicate aliases are listed and do not pick a winner.

    Unscoped labels stay globally unique. A scoped label is projected only
    under its own scope, together with any unscoped owner of the same key.
    """
    index = AliasIndex()
    unscoped, scoped = _label_owners(concepts)
    for key, oids in unscoped.items():
        if len(oids) == 1:
            index.by_alias[key] = oids[0]
        else:
            index.ambiguous[key] = tuple(oids)
    for scope, keys in scoped.items():
        for key, oids in _fold_scope(unscoped, keys).items():
            if len(oids) == 1:
                index.scoped.setdefault(scope, {})[key] = oids[0]
            else:
                index.scoped_ambiguous.setdefault(scope, {})[key] = tuple(oids)
    return index


def _label_owners(
    concepts: dict[str, Concept],
) -> tuple[dict[str, list[str]], dict[str, dict[str, list[str]]]]:
    """Active owners per normalised label: unscoped, and per scope."""
    unscoped: dict[str, list[str]] = {}
    scoped: dict[str, dict[str, list[str]]] = {}
    for concept in concepts.values():
        if concept.status != "active":
            continue
        target = _resolve(concept.oid, concepts)
        for label in concept.names():
            key = normalise(label.text)
            if not key:
                continue
            scope = label.scope.strip()
            table = scoped.setdefault(scope, {}) if scope else unscoped
            bucket = table.setdefault(key, [])
            if target not in bucket:
                bucket.append(target)
    return unscoped, scoped


def _fold_scope(
    unscoped: dict[str, list[str]], keys: dict[str, list[str]]
) -> dict[str, list[str]]:
    """Owners of each scoped key inside its scope: unscoped owners first."""
    folded: dict[str, list[str]] = {}
    for key, oids in keys.items():
        merged = list(unscoped.get(key, []))
        for oid in oids:
            if oid not in merged:
                merged.append(oid)
        folded[key] = merged
    return folded


def save_concept(vault: Path, concept: Concept, *, apply: bool = False) -> dict[str, Any]:
    """Write one concept file. Dry-run is the default and an existing path is refused."""
    path = _concept_path(vault, concept)
    preview = {"oid": concept.oid, "path": str(path), "applied": False}
    if not apply:
        return preview
    _refuse_live_overwrite(path)
    _refuse_duplicate_oid(vault, concept, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    concept.path = path
    path.write_text(_render(concept), encoding="utf-8")
    preview["applied"] = True
    return preview


def propose_concept(
    vault: Path,
    *,
    oid: str,
    class_name: str,
    pref_label: str,
    alt_labels: tuple[str, ...] = (),
    definition: str = "",
    apply: bool = False,
) -> dict[str, Any]:
    """Write a candidate anchor. Dry-run is the default."""
    concept = Concept(
        oid=oid,
        class_name=class_name,
        pref_label=pref_label,
        alt_labels=tuple(Label(item) for item in alt_labels),
        status="candidate",
        definition=definition,
    )
    return save_concept(vault, concept, apply=apply)


def merge_concepts(
    vault: Path, source: str, target: str, *, apply: bool = False
) -> dict[str, Any]:
    """Point ``source`` at ``target``. The source file stays on disk."""
    concepts = load_concepts(vault)
    if source not in concepts:
        raise OntologyError(f"unknown concept {source}")
    if target not in concepts:
        raise OntologyError(f"unknown concept {target}")
    if source == target:
        raise OntologyError("a concept cannot merge into itself")
    if _merge_cycles(source, target, concepts):
        raise OntologyError(f"merge cycle: {source} -> {target}")
    incoming = _incoming_hops(source, concepts)
    outgoing = _outgoing_hops(target, concepts)
    if incoming + 1 + outgoing > _MAX_CHAIN:
        raise OntologyError("merge redirect is too deep")
    concept = concepts[source]
    preview = {
        "oid": source,
        "merged_into": target,
        "applied": False,
        "path": str(concept.path) if concept.path else "",
    }
    if not apply:
        return preview
    if concept.path is None:
        raise OntologyError(f"concept {source} has no file")
    concept.merged_into = target
    concept.path.write_text(_render(concept), encoding="utf-8")
    preview["applied"] = True
    return preview


def import_seed(
    seed: Path,
    vault: Path | None = None,
    *,
    apply: bool = False,
) -> dict[str, Any]:
    """Copy an approved seed into a temporary vault.

    ``apply`` writes only the directory given by ``vault``. When that argument
    is omitted, a new temporary directory is created. A directory that already
    holds ``vault.yaml`` is a live vault and is refused. Dry-run writes nothing.
    """
    entries = _seed_entries(seed)
    destination = vault
    created = False
    if apply and destination is None:
        destination = Path(tempfile.mkdtemp(prefix="memfw-concepts-"))
        created = True
    if apply and destination is not None:
        _refuse_live_vault(destination)
    written: list[str] = []
    skipped: list[dict[str, str]] = []
    if apply and destination is not None:
        for entry in entries:
            concept = _concept_from_entry(entry)
            path = _concept_path(destination, concept)
            if path.exists():
                skipped.append({"oid": concept.oid, "reason": "path exists"})
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(_render(concept), encoding="utf-8")
            written.append(concept.oid)
        concepts = load_concepts(destination)
    else:
        concepts = {item.oid: item for item in (_concept_from_entry(row) for row in entries)}
    return {
        "applied": bool(apply),
        "vault": str(destination) if destination is not None else "",
        "created_temp": created,
        "written": written,
        "skipped": skipped,
        "review": lint_concepts(concepts),
        "count": len(entries),
    }


def _resolve(oid: str, concepts: dict[str, Concept]) -> str:
    status, chain = _walk(oid, concepts)
    if status == "ok" and chain:
        return chain[-1]
    if status == "dangling" and len(chain) >= 2 and chain[-1] not in concepts:
        return chain[-2]
    if status == "cycle":
        return oid
    return chain[-1] if chain else oid


def _walk(oid: str, concepts: dict[str, Concept]) -> tuple[str, tuple[str, ...]]:
    """Follow ``merged_into``. Status is ``ok``, ``cycle``, ``depth``, or ``dangling``."""
    chain = [oid]
    seen = {oid}
    current = oid
    for _ in range(_MAX_CHAIN):
        concept = concepts.get(current)
        if concept is None:
            return "dangling", tuple(chain)
        nxt = concept.merged_into
        if not nxt:
            return "ok", tuple(chain)
        if nxt in seen:
            chain.append(nxt)
            return "cycle", tuple(chain)
        chain.append(nxt)
        if nxt not in concepts:
            return "dangling", tuple(chain)
        seen.add(nxt)
        current = nxt
    concept = concepts.get(current)
    if concept is not None and concept.merged_into:
        return "depth", tuple(chain)
    return "ok", tuple(chain)


def _merge_cycles(source: str, target: str, concepts: dict[str, Concept]) -> bool:
    seen = {source}
    current = target
    for _ in range(_MAX_CHAIN + 1):
        if current in seen:
            return True
        seen.add(current)
        concept = concepts.get(current)
        if concept is None or not concept.merged_into:
            return False
        current = concept.merged_into
    return False


def _incoming_hops(oid: str, concepts: dict[str, Concept]) -> int:
    reverse: dict[str, list[str]] = {}
    for concept in concepts.values():
        if concept.merged_into:
            reverse.setdefault(concept.merged_into, []).append(concept.oid)
    best = 0
    stack = [(oid, 0)]
    seen: set[str] = set()
    while stack:
        current, depth = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        best = max(best, depth)
        for pred in reverse.get(current, ()):
            if pred not in seen:
                stack.append((pred, depth + 1))
    return best


def _outgoing_hops(oid: str, concepts: dict[str, Concept]) -> int:
    hops = 0
    current = oid
    seen = {oid}
    while hops <= _MAX_CHAIN:
        concept = concepts.get(current)
        if concept is None or not concept.merged_into:
            return hops
        nxt = concept.merged_into
        if nxt in seen or nxt not in concepts:
            return hops
        seen.add(nxt)
        current = nxt
        hops += 1
    return hops


def _prefer_authority(existing: Concept, incoming: Concept) -> Concept:
    """Keep the authoritative record when two files share an oid."""
    existing_rank = _STATUS_RANK.get(existing.status, -1)
    incoming_rank = _STATUS_RANK.get(incoming.status, -1)
    if incoming_rank > existing_rank:
        winner, loser = incoming, existing
    else:
        winner, loser = existing, incoming
    paths = list(winner.collisions)
    for path in (*loser.collisions, str(loser.path or "")):
        if path and path not in paths:
            paths.append(path)
    winner.collisions = tuple(paths)
    return winner


def _relation_oids(value: Any) -> list[str]:
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    if isinstance(value, list):
        return [item.strip() for item in value if isinstance(item, str) and item.strip()]
    return []


def _read_concept(path: Path) -> Concept:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise OntologyError(f"{path} cannot be read") from exc
    try:
        meta, body = _split(text)
    except yaml.YAMLError as exc:
        raise OntologyError(f"{path} frontmatter cannot be parsed") from exc
    oid = str(meta.get("oid") or "")
    if not oid:
        raise OntologyError(f"{path} has no oid")
    merged = meta.get("merged_into")
    merged_into = str(merged) if isinstance(merged, str) and merged else None
    extras = {key: value for key, value in meta.items() if key not in _CORE_KEYS}
    return Concept(
        oid=oid,
        class_name=str(meta.get("class") or ""),
        pref_label=str(meta.get("pref_label") or ""),
        alt_labels=_labels(meta.get("alt_labels")),
        same_as=_strings(meta.get("same_as")),
        status=str(meta.get("status") or "candidate"),
        merged_into=merged_into,
        definition=body.strip(),
        path=path,
        extras=extras,
    )


def _concept_from_entry(entry: dict[str, Any]) -> Concept:
    oid = str(entry.get("oid") or "")
    if not oid:
        raise OntologyError("seed entry has no oid")
    merged = entry.get("merged_into")
    return Concept(
        oid=oid,
        class_name=str(entry.get("class") or ""),
        pref_label=str(entry.get("pref_label") or oid),
        alt_labels=_labels(entry.get("alt_labels")),
        same_as=_strings(entry.get("same_as")),
        status=str(entry.get("status") or "active"),
        merged_into=str(merged) if isinstance(merged, str) and merged else None,
        definition=str(entry.get("definition") or ""),
    )


def _seed_entries(seed: Path) -> list[dict[str, Any]]:
    loaded = yaml.safe_load(Path(seed).read_text(encoding="utf-8"))
    if not isinstance(loaded, dict) or not isinstance(loaded.get("entries"), list):
        raise OntologyError("seed file has no entries list")
    entries = []
    for item in loaded["entries"]:
        if not isinstance(item, dict):
            raise OntologyError("seed entry is not a mapping")
        entries.append(item)
    return entries


def _labels(value: Any) -> tuple[Label, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise OntologyError("alt_labels must be a list")
    labels: list[Label] = []
    for item in value:
        if isinstance(item, str):
            labels.append(Label(item))
        elif isinstance(item, dict) and isinstance(item.get("label"), str):
            scope = item.get("scope")
            labels.append(Label(item["label"], str(scope) if isinstance(scope, str) else ""))
        else:
            raise OntologyError("alt_labels entry is not a label")
    return tuple(labels)


def _strings(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise OntologyError("same_as must be a list of strings")
    return tuple(value)


def _split(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---\n"):
        raise OntologyError("concept file is missing frontmatter")
    parts = text.split("---\n", 2)
    if len(parts) < 3:
        raise OntologyError("concept file is missing frontmatter")
    try:
        meta = yaml.safe_load(parts[1])
    except yaml.YAMLError as exc:
        raise OntologyError("concept frontmatter cannot be parsed") from exc
    if not isinstance(meta, dict):
        raise OntologyError("concept frontmatter is not a mapping")
    return meta, parts[2]


def _render(concept: Concept) -> str:
    alt: list[Any] = []
    for label in concept.alt_labels:
        if label.scope:
            alt.append({"label": label.text, "scope": label.scope})
        else:
            alt.append(label.text)
    meta = {
        "oid": concept.oid,
        "class": concept.class_name,
        "pref_label": concept.pref_label,
        "alt_labels": alt,
        "same_as": list(concept.same_as),
        "status": concept.status,
        "merged_into": concept.merged_into,
    }
    for key in sorted(concept.extras):
        meta[key] = concept.extras[key]
    dumped = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False).rstrip()
    body = concept.definition.strip()
    return f"---\n{dumped}\n---\n\n{body}\n"


def _slug(oid: str) -> str:
    tail = oid.split(":")[-1]
    slug = normalise(tail).replace(" ", "-")
    slug = re.sub(r"[^a-z0-9-]", "", slug).strip("-")
    if not _SLUG.fullmatch(slug or ""):
        raise OntologyError(f"oid {oid} has no safe slug")
    return slug


def _concept_path(vault: Path, concept: Concept) -> Path:
    if not _CLASS_DIR.fullmatch(concept.class_name or ""):
        raise OntologyError(f"class {concept.class_name!r} is not a safe directory name")
    return Path(vault) / "concepts" / concept.class_name / f"{_slug(concept.oid)}.md"


def _refuse_live_vault(vault: Path) -> None:
    if (Path(vault) / "vault.yaml").is_file():
        raise OntologyError("import-seed refuses a live vault; pass an empty temporary directory")


def _refuse_live_overwrite(path: Path) -> None:
    if path.exists():
        raise OntologyError(f"concept file already exists: {path}")


def _refuse_duplicate_oid(vault: Path, concept: Concept, path: Path) -> None:
    """One oid is one file. A candidate must not replace an authority record."""
    if not Path(vault).exists():
        return
    prior = load_concepts(vault).get(concept.oid)
    if prior is None or prior.path is None:
        return
    if prior.path.resolve() == path.resolve():
        return
    raise OntologyError(f"oid {concept.oid} already exists")


def _frontmatter(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    if not text.startswith("---\n"):
        return {}
    parts = text.split("---\n", 2)
    if len(parts) < 3:
        return {}
    try:
        meta = yaml.safe_load(parts[1])
    except yaml.YAMLError:
        return {}
    return meta if isinstance(meta, dict) else {}
