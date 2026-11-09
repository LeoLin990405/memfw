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

    def names(self) -> tuple[Label, ...]:
        labels = [Label(self.pref_label)]
        labels.extend(self.alt_labels)
        labels.extend(Label(item) for item in self.same_as)
        return tuple(labels)


@dataclass
class AliasIndex:
    """Rebuildable alias → oid projection. Ambiguous keys are not winners."""

    by_alias: dict[str, str] = field(default_factory=dict)
    ambiguous: dict[str, tuple[str, ...]] = field(default_factory=dict)


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
            found[concept.oid] = concept
    return found


def lint_concepts(concepts: dict[str, Concept]) -> list[dict[str, Any]]:
    """Review list. The files are not modified."""
    items: list[dict[str, Any]] = []
    owners: dict[str, list[str]] = {}
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
        if concept.status != "active":
            continue
        target = _resolve(concept.oid, concepts)
        for label in concept.names():
            key = normalise(label.text)
            if not key:
                continue
            owners.setdefault(key, [])
            if target not in owners[key]:
                owners[key].append(target)
    for key, oids in sorted(owners.items()):
        if len(oids) > 1:
            items.append(
                {
                    "kind": "duplicate_alias",
                    "oid": oids[0],
                    "detail": key,
                    "oids": list(oids),
                }
            )
    return items


def project_aliases(concepts: dict[str, Concept]) -> AliasIndex:
    """Alias projection. Duplicate aliases are listed and do not pick a winner."""
    index = AliasIndex()
    owners: dict[str, list[str]] = {}
    for concept in concepts.values():
        if concept.status != "active":
            continue
        target = _resolve(concept.oid, concepts)
        for label in concept.names():
            key = normalise(label.text)
            if not key:
                continue
            bucket = owners.setdefault(key, [])
            if target not in bucket:
                bucket.append(target)
    for key, oids in owners.items():
        if len(oids) == 1:
            index.by_alias[key] = oids[0]
        else:
            index.ambiguous[key] = tuple(oids)
    return index


def save_concept(vault: Path, concept: Concept, *, apply: bool = False) -> dict[str, Any]:
    """Write one concept file. Dry-run is the default and an existing path is refused."""
    path = _concept_path(vault, concept)
    preview = {"oid": concept.oid, "path": str(path), "applied": False}
    if not apply:
        return preview
    _refuse_live_overwrite(path)
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
    seen: set[str] = set()
    current = oid
    for _ in range(16):
        if current in seen:
            return current
        seen.add(current)
        concept = concepts.get(current)
        if concept is None or not concept.merged_into:
            return current
        if concept.merged_into not in concepts:
            return current
        current = concept.merged_into
    return current


def _read_concept(path: Path) -> Concept:
    text = path.read_text(encoding="utf-8")
    meta, body = _split(text)
    oid = str(meta.get("oid") or "")
    if not oid:
        raise OntologyError(f"{path} has no oid")
    merged = meta.get("merged_into")
    merged_into = str(merged) if isinstance(merged, str) and merged else None
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
    meta = yaml.safe_load(parts[1])
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
