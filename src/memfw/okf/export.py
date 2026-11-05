"""Export a memfw vault as an OKF v0.2 knowledge bundle.

OKF is a derived, rebuildable projection: Markdown files plus YAML frontmatter,
with ``index.md`` / ``log.md`` navigation and a machine-readable ``manifest.json``.
The memfw repository remains the authority; this module never writes it.
"""

from __future__ import annotations

import json
import re
import shutil
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from memfw.core.models import MemoryDocument
from memfw.core.repository import Repository

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
) -> OkfExportResult:
    """Export ``root`` (a memfw vault) to ``output`` as an OKF v0.2 bundle."""
    repository = Repository(Path(root))
    repository.init()
    documents = repository.list()
    generated_at = (now or datetime.now(UTC)).astimezone(UTC).isoformat().replace("+00:00", "Z")
    output_path = Path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = output_path.with_name(output_path.name + ".tmp")
    if temp_path.exists():
        shutil.rmtree(temp_path)
    temp_path.mkdir(parents=True)

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

    if output_path.exists():
        if not force:
            shutil.rmtree(temp_path)
            raise FileExistsError(f"output already exists: {output_path} (use force=True)")
        backup = output_path.with_name(
            output_path.name + f".bak-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
        )
        output_path.rename(backup)
        temp_path.rename(output_path)
    else:
        temp_path.rename(output_path)
    return OkfExportResult(
        bundle=output_path,
        concepts=len(concepts),
        valid=validation.valid,
        manifest=output_path / "manifest.json",
        warnings=validation.warnings,
    )


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
