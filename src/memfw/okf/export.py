"""Export a memfw vault as an OKF v0.2 knowledge bundle.

OKF is a derived, rebuildable projection: Markdown files plus YAML frontmatter,
with ``index.md`` / ``log.md`` navigation and a machine-readable ``manifest.json``.
The memfw repository remains the authority; this module never writes it.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from memfw.core.epistemic import OPEN_QUESTION_TYPE
from memfw.core.models import MemoryDocument
from memfw.core.repository import Repository
from memfw.ports.privacy import admits, effective_level, parse_clearance

OKF_VERSION = "0.2"
GENERATOR = "memfw-okf-export/1"
FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.S)
RESERVED = {"index.md", "log.md"}


@dataclass(frozen=True, slots=True)
class OkfValidation:
    valid: bool
    concept_count: int
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    type_counts: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class OkfExportResult:
    bundle: Path
    concepts: int
    valid: bool
    manifest: Path
    warnings: list[str] = field(default_factory=list)


def _slug_id(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-")
    return slug or "memory"


def _description(document: MemoryDocument) -> str:
    if document.summary.strip():
        return document.summary.strip().splitlines()[0][:240]
    for line in document.body.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith(("#", "|", "```", ">", "-", "*")):
            return re.sub(r"\s+", " ", stripped)[:240]
    return document.title[:240]


def _type(document: MemoryDocument) -> tuple[str, str | None]:
    for tag in document.tags:
        prefix, _, value = tag.partition(":")
        if prefix == "type" and value:
            return value, value
    return ("Memory", None)


def _concept(document: MemoryDocument, generated_by: str) -> tuple[str, str, dict[str, Any]]:
    concept_type, tag_type = _type(document)
    concept_id = _slug_id(document.id)
    front: dict[str, Any] = {
        "type": concept_type,
        "title": document.title,
        "description": _description(document),
        "resource": f"urn:memfw:{document.id}",
        "tags": list(document.tags),
        "sources": [
            {
                "id": "memfw",
                "resource": f"urn:memfw:{document.id}",
                "title": document.source or "memfw",
            }
        ],
        "generated": {
            "by": generated_by,
            "at": document.updated_at.astimezone(UTC).isoformat(),
        },
        "status": "stable",
        "memfw": {
            "id": document.id,
            "layer": document.layer.value,
            "tier": document.tier.value,
            "source": document.source,
            "provenance": document.provenance,
            "created_at": document.created_at.astimezone(UTC).isoformat(),
            "updated_at": document.updated_at.astimezone(UTC).isoformat(),
            "tag_type": tag_type,
        },
    }
    # Epistemic type and causal grade travel with the concept but stay inside the
    # memfw envelope: the OKF concept schema is unchanged for old bundles, and a
    # consumer that does not know these names still sees them as memfw metadata.
    memfw = front["memfw"]
    if document.epistemic:
        memfw["epistemic"] = document.epistemic
    if document.causal != "none":
        memfw["causal"] = document.causal
    if document.causal_record is not None:
        memfw["causal_record"] = document.causal_record.model_dump(mode="json")
    if document.limitations:
        memfw["limitations"] = list(document.limitations)
    # Only when the memory sets it, so bundles of older memories are unchanged.
    if document.privacy is not None:
        memfw["privacy"] = document.privacy
    if document.experience is not None:
        memfw["experience"] = document.experience.model_dump(mode="json")
    # An open question says what is unknown; a consumer must not read it as a
    # stated memory, so the type and the block travel in the envelope.
    if document.type == OPEN_QUESTION_TYPE:
        memfw["type"] = document.type
        if document.open_question is not None:
            memfw["open_question"] = document.open_question.model_dump(mode="json")
    return concept_id, concept_type, front


def _render(front: dict[str, Any], body: str) -> str:
    header = yaml.safe_dump(front, sort_keys=False, allow_unicode=True, width=100).strip()
    return f"---\n{header}\n---\n\n{body.rstrip()}\n"


def _render_index(concepts: list[dict[str, Any]], bundle_title: str) -> str:
    lines = [
        "---",
        f'okf_version: "{OKF_VERSION}"',
        "---",
        "",
        f"# {bundle_title}",
        "",
        "Generated from the memfw vault. Regenerate with `memfw okf export`.",
        "",
    ]
    by_type: dict[str, list[dict[str, Any]]] = {}
    for concept in concepts:
        by_type.setdefault(str(concept["front"]["type"]), []).append(concept)
    for concept_type in sorted(by_type):
        lines.append(f"## {concept_type}")
        lines.append("")
        for concept in sorted(by_type[concept_type], key=lambda item: item["concept_id"]):
            description = concept["front"].get("description") or ""
            lines.append(
                f"* [{concept['front']['title']}](/{concept['concept_id']}.md) - {description}"
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _render_log(count: int, generated_at: str) -> str:
    return (
        "# memfw OKF Update Log\n\n"
        f"## {generated_at[:10]}\n"
        f"* **Generation**: Rebuilt {count} OKF concepts from the memfw vault.\n"
    )


def export_okf(
    root: Path | str,
    output: Path | str,
    *,
    force: bool = False,
    generated_by: str = "process:memfw-okf-export",
    now: datetime | None = None,
    bundle_title: str | None = None,
    clearance: frozenset[str] | str | None = None,
) -> OkfExportResult:
    """Export ``root`` (a memfw vault) to ``output`` as an OKF v0.2 bundle.

    ``clearance`` (MM0) is the set of privacy levels the bundle may contain
    (``public``, ``shared``, ``local-private``; text is comma separated and
    a malformed value becomes ``{"public"}``). ``None``, the default, exports
    every memory, as before MM0. With a clearance, a memory's level is the
    strictest of its ``privacy`` field (missing → the vault's
    ``legacy_privacy_default``) and its ``privacy:``/``sensitivity:`` tags;
    memories outside it are left out and the manifest records the clearance
    and how many were withheld (never which).
    """
    repository = Repository(Path(root))
    # Resolve first: ``Path(".").with_name`` raises, and the safety checks
    # below must compare real locations, not spellings or symlinks.
    output_path = Path(output).expanduser().resolve()
    _check_output(output_path, repository.root, force=force)
    repository.init()
    documents = repository.list()
    cleared = parse_clearance(clearance) if not isinstance(clearance, frozenset) else clearance
    withheld = 0
    if cleared is not None:
        legacy = repository.legacy_privacy_default()
        kept = [
            document
            for document in documents
            if admits(
                cleared,
                effective_level(
                    document.privacy,
                    document.tags,
                    legacy=legacy,
                    sensitivity=document.top_level_sensitivity,
                ),
            )
        ]
        withheld = len(documents) - len(kept)
        documents = kept
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    generated_at = moment.isoformat().replace("+00:00", "Z")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = output_path.with_name(output_path.name + ".tmp")
    if _is_within(repository.root.resolve(), temp_path):
        raise ValueError(f"refusing to use {temp_path} as scratch: it is or contains the vault")
    if temp_path.is_symlink() or temp_path.is_file():
        temp_path.unlink()  # never follow a symlink into rmtree
    elif temp_path.exists():
        shutil.rmtree(temp_path)
    temp_path.mkdir(parents=True)
    try:
        privacy_note = (
            None if cleared is None else {"clearance": sorted(cleared), "withheld_count": withheld}
        )
        concepts, validation = _write_bundle(
            temp_path,
            repository,
            documents,
            generated_by,
            generated_at,
            bundle_title,
            privacy_note=privacy_note,
        )
        if output_path.exists():
            # Re-check: the output may have appeared since the first check.
            _check_output(output_path, repository.root, force=force)
            backup = _backup_path(output_path, moment)
            if _is_within(repository.root.resolve(), backup):
                raise ValueError(f"refusing to back up {output_path} onto the vault: {backup}")
            output_path.rename(backup)
        temp_path.rename(output_path)
    finally:
        if temp_path.exists():
            shutil.rmtree(temp_path, ignore_errors=True)
    # Report the path as the caller spelled it; the checks above used the real one.
    # ``.`` and ``..`` name no directory of their own (after ``--force`` the
    # working directory is the backup), so those are reported resolved.
    spelled = Path(output)
    reported = spelled if spelled.name and spelled.name != ".." else output_path
    return OkfExportResult(
        bundle=reported,
        concepts=len(concepts),
        valid=validation.valid,
        manifest=reported / "manifest.json",
        warnings=validation.warnings,
    )


def _same_file(left: Path, right: Path) -> bool:
    try:
        return os.path.samefile(left, right)
    except OSError:
        return False


def _is_within(path: Path, parent: Path) -> bool:
    """True when ``path`` is ``parent`` or below it, compared by file identity.

    Text comparison after ``resolve()`` is not enough: on a case-insensitive
    file system (the macOS default) ``vault`` and ``VAULT`` are one directory.
    Each existing ancestor of ``path`` is compared to ``parent`` by
    ``(st_dev, st_ino)``. A ``parent`` that does not exist yet is compared by
    case-folded text, which refuses more, never less.
    """

    if not parent.exists():
        folded = str(parent).casefold()
        return any(str(candidate).casefold() == folded for candidate in (path, *path.parents))
    return any(
        _same_file(candidate, parent) for candidate in (path, *path.parents) if candidate.exists()
    )


def _is_memfw_bundle(path: Path) -> bool:
    try:
        manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(manifest, dict):
        return False
    generator = manifest.get("generator")
    generated_by = manifest.get("generated_by")
    return (isinstance(generator, str) and generator.startswith("memfw-okf-export/")) or (
        isinstance(generated_by, str) and "memfw-okf-export" in generated_by
    )


def _check_output(output_path: Path, vault_root: Path, *, force: bool) -> None:
    """Refuse outputs that would clobber the vault or anything not ours."""

    vault = vault_root.expanduser().resolve()
    if _is_within(vault, output_path):
        raise ValueError(
            f"refusing to export to {output_path}: it is or contains the vault {vault}"
        )
    if _is_within(output_path, vault):
        raise ValueError(f"refusing to export to {output_path}: it is inside the vault {vault}")
    temp_path = output_path.with_name(output_path.name + ".tmp")
    if _is_within(vault, temp_path.resolve()):
        raise ValueError(
            f"refusing to export to {output_path}: its scratch path {temp_path} "
            f"is or contains the vault {vault}"
        )
    if not output_path.exists():
        return
    if not output_path.is_dir():
        raise FileExistsError(
            f"output already exists and is not an OKF bundle directory: {output_path}"
        )
    if not force:
        raise FileExistsError(f"output already exists: {output_path} (use --force)")
    if not _is_memfw_bundle(output_path):
        raise FileExistsError(
            f"refusing to replace {output_path} with --force: it has no manifest.json "
            "written by memfw-okf-export"
        )


def _backup_path(output_path: Path, moment: datetime) -> Path:
    stamp = moment.strftime("%Y%m%dT%H%M%SZ")
    base = output_path.with_name(f"{output_path.name}.bak-{stamp}")
    candidate = base
    counter = 1
    while candidate.exists() or candidate.is_symlink():
        candidate = base.with_name(f"{base.name}-{counter}")
        counter += 1
    return candidate


def _write_bundle(
    temp_path: Path,
    repository: Repository,
    documents: list[MemoryDocument],
    generated_by: str,
    generated_at: str,
    bundle_title: str | None,
    *,
    privacy_note: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], OkfValidation]:
    concepts: list[dict[str, Any]] = []
    for document in documents:
        concept_id, concept_type, front = _concept(document, generated_by)
        concepts.append(
            {
                "concept_id": concept_id,
                "concept_type": concept_type,
                "front": front,
                "body": document.body,
            }
        )
        temp_path.joinpath(f"{concept_id}.md").write_text(
            _render(front, document.body), encoding="utf-8"
        )

    title = bundle_title or f"{repository.root.name} memory"
    temp_path.joinpath("index.md").write_text(_render_index(concepts, title), encoding="utf-8")
    temp_path.joinpath("log.md").write_text(
        _render_log(len(concepts), generated_at), encoding="utf-8"
    )
    manifest: dict[str, Any] = {
        "okf_version": OKF_VERSION,
        "valid": True,
        "concept_count": len(concepts),
        "invalid_count": 0,
        "errors": [],
        "warnings": [],
        "type_counts": dict(Counter(concept["concept_type"] for concept in concepts)),
        "generated_at": generated_at,
        "generator": GENERATOR,
        "generated_by": generated_by,
        "canonical_source": str(repository.root),
    }
    if privacy_note is not None:
        manifest["privacy"] = privacy_note
    temp_path.joinpath("manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    validation = validate_okf_bundle(temp_path)
    manifest["valid"] = validation.valid
    manifest["invalid_count"] = len(validation.errors)
    manifest["errors"] = validation.errors
    manifest["warnings"] = validation.warnings
    temp_path.joinpath("manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    return concepts, validation


def validate_okf_bundle(path: Path | str) -> OkfValidation:
    """Validate an OKF v0.2 bundle: frontmatter parseable, required type, index/log rules."""
    bundle = Path(path)
    errors: list[str] = []
    warnings: list[str] = []
    types: Counter[str] = Counter()
    if not bundle.is_dir():
        return OkfValidation(False, 0, [f"not a directory: {bundle}"])
    concept_count = 0
    for file in sorted(bundle.rglob("*.md")):
        rel = file.relative_to(bundle).as_posix()
        if file.name in RESERVED:
            continue
        concept_count += 1
        try:
            text = file.read_text(encoding="utf-8")
        except OSError as exc:
            errors.append(f"{rel}: {exc}")
            continue
        match = FRONTMATTER_RE.match(text)
        if not match:
            errors.append(f"{rel}: missing YAML frontmatter")
            continue
        try:
            data = yaml.safe_load(match.group(1)) or {}
        except yaml.YAMLError as exc:
            errors.append(f"{rel}: invalid YAML: {exc}")
            continue
        if not isinstance(data, dict):
            errors.append(f"{rel}: frontmatter is not a mapping")
            continue
        concept_type = data.get("type")
        if not isinstance(concept_type, str) or not concept_type.strip():
            errors.append(f"{rel}: missing non-empty type")
            continue
        types[concept_type] += 1
    index = bundle / "index.md"
    if not index.exists():
        warnings.append("bundle root has no index.md")
    else:
        index_text = index.read_text(encoding="utf-8")
        if not index_text.startswith("---\n"):
            warnings.append("bundle root index.md has no frontmatter/okf_version")
        elif (
            f'okf_version: "{OKF_VERSION}"' not in index_text
            and f"okf_version: '{OKF_VERSION}'" not in index_text
        ):
            warnings.append("bundle root index.md does not declare okf_version 0.2")
    manifest_path = bundle / "manifest.json"
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            errors.append(f"manifest.json: invalid JSON: {exc}")
        else:
            declared = manifest.get("concept_count")
            if declared is not None and declared != concept_count:
                errors.append(f"manifest.json: concept_count {declared} != {concept_count}")
    return OkfValidation(
        valid=not errors,
        concept_count=concept_count,
        errors=errors,
        warnings=warnings,
        type_counts=dict(types),
    )
