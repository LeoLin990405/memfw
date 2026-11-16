"""Import an OKF v0.2 bundle as staged memory candidates (an Ingestor).

The importer is the inverse of :mod:`memfw.okf.export`, with one rule the
export does not need: **it never writes the authoritative vault.** Every
concept becomes a candidate under ``<vault>/_candidates/remember/``, written by
the shared staging writer (:mod:`memfw.storage.candidate_staging`), and a human applies
it with ``memfw candidate-apply`` like any other candidate.

Bundle content is untrusted data. The bundle is read once, through directory
file descriptors opened with ``O_NOFOLLOW`` at every component, into an
in-memory snapshot; nothing is read through a path again. The snapshot is
refused as a whole if it has a symlink that leaves the bundle, a symlinked
concept or manifest, a non-regular file, invalid UTF-8, a file over its size
cap, or frontmatter with YAML anchors or aliases (alias bombs, recursive
nodes). It is then checked by :func:`validate_okf_bundle` on a private copy of
those same bytes. Staging is all-or-nothing: every candidate document is built
before the first one is published, and a failure while publishing removes the
files this run already wrote.

Re-importing is idempotent: the ``proposal_id`` is a hash of the bundle id,
the concept id and the body hash. A concept whose body already exists in the
vault (a bundle memfw exported from this same vault) is skipped and reported.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from yaml.events import AliasEvent
from yaml.nodes import Node

from memfw.core.errors import MemfwError
from memfw.core.repository import Repository
from memfw.storage.candidate_files import (
    candidate_is_expired,
    read_candidate,
    staged_candidate_paths,
)
from memfw.storage.candidate_staging import (
    CANDIDATE_DIR,
    MAX_CANDIDATE_BYTES,
    PublishedCandidate,
    body_content_hash,
    candidate_document,
    publish_candidate,
    unpublish_candidate,
)

from .export import FRONTMATTER_RE, RESERVED, OkfValidation, validate_okf_bundle

#: A concept file may not be larger than a candidate body may be.
MAX_CONCEPT_BYTES = MAX_CANDIDATE_BYTES
#: ``manifest.json``, ``index.md`` and ``log.md`` grow with the bundle.
MAX_NAVIGATION_BYTES = 1024 * 1024
#: Entries (files and directories) one bundle may hold.
MAX_BUNDLE_ENTRIES = 10_000
#: Largest rendered candidate file; frontmatter cannot grow past this.
MAX_RENDERED_BYTES = 2 * MAX_CANDIDATE_BYTES
#: OKF frontmatter keys the export writes and the importer maps.
KNOWN_FIELDS = frozenset(
    {"type", "title", "description", "resource", "tags", "sources", "generated", "status", "memfw"}
)
_TITLE_MAX = 200
_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
_FILE_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)


class OkfImportError(MemfwError):
    """The bundle was refused; nothing was staged."""

    def __init__(self, message: str, errors: list[str] | None = None) -> None:
        super().__init__(message)
        self.errors = list(errors or [])


@dataclass(slots=True)
class OkfImportItem:
    concept: str
    doc_id: str
    title: str
    status: str
    proposal_id: str | None = None
    content_hash: str | None = None
    candidate: str | None = None
    memory_id: str | None = None
    reason: str | None = None
    unknown_fields: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "concept": self.concept,
            "doc_id": self.doc_id,
            "title": self.title,
            "status": self.status,
            "proposal_id": self.proposal_id,
            "content_hash": self.content_hash,
            "candidate": self.candidate,
            "memory_id": self.memory_id,
            "reason": self.reason,
            "unknown_fields": list(self.unknown_fields),
        }


@dataclass(slots=True)
class OkfImportResult:
    bundle: Path
    root: Path
    source: str
    dry_run: bool
    validation: OkfValidation
    items: list[OkfImportItem] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in self.items:
            counts[item.status] = counts.get(item.status, 0) + 1
        return dict(sorted(counts.items()))

    def as_dict(self) -> dict[str, Any]:
        return {
            "bundle": str(self.bundle),
            "root": str(self.root),
            "source": self.source,
            "dry_run": self.dry_run,
            "authority": False,
            "valid": self.validation.valid,
            "concept_count": self.validation.concept_count,
            "counts": self.counts,
            "items": [item.as_dict() for item in self.items],
            "warnings": list(self.warnings),
        }


@dataclass(slots=True)
class _Snapshot:
    """The bytes of every file the importer reads, keyed by POSIX relative path."""

    base: Path
    files: dict[str, bytes] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def concepts(self) -> list[str]:
        return sorted(
            rel
            for rel in self.files
            if rel.endswith(".md") and rel.rsplit("/", 1)[-1] not in RESERVED
        )


class _NoAliasLoader(yaml.SafeLoader):
    """A safe loader that refuses anchors and aliases before any node is built.

    Without aliases a document cannot expand past its own size (no alias bombs)
    and cannot refer to itself (no recursive nodes).
    """

    def compose_node(self, parent: Node | None, index: Any) -> Node | None:
        if self.check_event(AliasEvent):
            raise yaml.YAMLError("YAML aliases are not accepted in an imported bundle")
        event = self.peek_event()  # type: ignore[no-untyped-call]
        if getattr(event, "anchor", None) is not None:
            raise yaml.YAMLError("YAML anchors are not accepted in an imported bundle")
        return super().compose_node(parent, index)


def _slug(text: str, limit: int = 48) -> str:
    value = re.sub(r"[^a-z0-9_-]+", "-", text.lower()).strip("-")
    return value[:limit].strip("-") or "memory"


def _cap_for(name: str) -> int | None:
    """Size cap of a file the importer reads, or None for files it never reads."""
    if name.endswith(".md"):
        return MAX_NAVIGATION_BYTES if name in RESERVED else MAX_CONCEPT_BYTES
    if name == "manifest.json":
        return MAX_NAVIGATION_BYTES
    return None


def _read_fd(descriptor: int, cap: int) -> bytes | None:
    """Read at most ``cap`` bytes; None when the file is larger."""
    chunks: list[bytes] = []
    remaining = cap + 1
    while remaining > 0:
        chunk = os.read(descriptor, min(remaining, 65536))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    data = b"".join(chunks)
    return None if len(data) > cap else data


def _symlink_error(base: Path, rel: str, name: str) -> str | None:
    # Classified by path only for the message; a symlink is never followed.
    target = Path(os.path.realpath(base / rel))
    if target != base and base not in target.parents:
        return f"{rel}: symlink escapes the bundle"
    if _cap_for(name) is not None:
        return f"{rel}: symlinked concept or manifest is not imported"
    return None


def _walk(snapshot: _Snapshot, dir_fd: int, prefix: str, entries: list[int]) -> None:
    for name in sorted(os.listdir(dir_fd)):
        entries[0] += 1
        if entries[0] > MAX_BUNDLE_ENTRIES:
            raise OkfImportError(f"bundle has more than {MAX_BUNDLE_ENTRIES} entries")
        rel = f"{prefix}{name}"
        info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        if stat.S_ISLNK(info.st_mode):
            error = _symlink_error(snapshot.base, rel, name)
            if error:
                snapshot.errors.append(error)
            continue
        if stat.S_ISDIR(info.st_mode):
            try:
                child = os.open(name, _DIR_FLAGS, dir_fd=dir_fd)
            except OSError as exc:
                snapshot.errors.append(f"{rel}: cannot open directory: {exc.strerror}")
                continue
            try:
                _walk(snapshot, child, f"{rel}/", entries)
            finally:
                os.close(child)
            continue
        if not stat.S_ISREG(info.st_mode):
            snapshot.errors.append(f"{rel}: not a regular file")
            continue
        cap = _cap_for(name)
        if cap is None:
            continue  # other files are never read
        if info.st_size > cap:
            snapshot.errors.append(f"{rel}: {info.st_size} bytes exceeds the {cap}-byte cap")
            continue
        try:
            descriptor = os.open(name, _FILE_FLAGS, dir_fd=dir_fd)
        except OSError as exc:
            snapshot.errors.append(f"{rel}: cannot open: {exc.strerror}")
            continue
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                snapshot.errors.append(f"{rel}: not a regular file")
                continue
            data = _read_fd(descriptor, cap)
        finally:
            os.close(descriptor)
        if data is None:
            snapshot.errors.append(f"{rel}: exceeds the {cap}-byte cap")
            continue
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            snapshot.errors.append(f"{rel}: not valid UTF-8")
            continue
        snapshot.files[rel] = data


def _snapshot(bundle: Path | str) -> _Snapshot:
    base = Path(bundle)
    if not base.is_dir():
        return _Snapshot(base, errors=[f"not a directory: {base}"])
    base = base.resolve(strict=True)
    snapshot = _Snapshot(base)
    root_fd = os.open(base, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        _walk(snapshot, root_fd, "", [0])
    except OkfImportError as exc:
        snapshot.errors.append(str(exc))
    finally:
        os.close(root_fd)
    return snapshot


def check_bundle_paths(bundle: Path | str) -> list[str]:
    """Path-safety errors for ``bundle``; an empty list means it is safe to read.

    Refused: symlinks whose target leaves the bundle, symlinked concept or
    manifest files, non-regular files, Markdown concepts over
    :data:`MAX_CONCEPT_BYTES` or not UTF-8, navigation files over
    :data:`MAX_NAVIGATION_BYTES`, and bundles over :data:`MAX_BUNDLE_ENTRIES`.
    """
    return _snapshot(bundle).errors


def _parse_concept(rel: str, data: bytes) -> tuple[dict[str, Any], str]:
    """Frontmatter and body of one concept, re-checked on the bytes read."""
    text = data.decode("utf-8")
    match = FRONTMATTER_RE.match(text)
    if match is None:
        raise OkfImportError(f"{rel}: missing YAML frontmatter")
    try:
        front = yaml.load(match.group(1), Loader=_NoAliasLoader) or {}  # noqa: S506
    except yaml.YAMLError as exc:
        raise OkfImportError(f"{rel}: invalid YAML: {exc}") from exc
    except RecursionError as exc:
        raise OkfImportError(f"{rel}: frontmatter nests too deeply") from exc
    if not isinstance(front, dict):
        raise OkfImportError(f"{rel}: frontmatter is not a mapping")
    concept_type = front.get("type")
    if not isinstance(concept_type, str) or not concept_type.strip():
        raise OkfImportError(f"{rel}: missing non-empty type")
    return front, text[match.end() :].strip()


def _validate_snapshot(snapshot: _Snapshot) -> OkfValidation:
    """Run the OKF validator over a private copy of exactly the bytes read."""
    with tempfile.TemporaryDirectory(prefix="memfw-okf-import-") as scratch:
        copy_root = Path(scratch) / "bundle"
        copy_root.mkdir()
        for rel, data in snapshot.files.items():
            target = copy_root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        return validate_okf_bundle(copy_root)


def _json_safe(value: Any) -> Any:
    """Plain JSON data, so YAML scalars (dates, odd keys) render safely."""
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value
    return None


def _manifest(snapshot: _Snapshot) -> dict[str, Any]:
    data = snapshot.files.get("manifest.json")
    if data is None:
        return {}
    try:
        parsed = json.loads(data.decode("utf-8"))
    except (ValueError, RecursionError) as exc:
        raise OkfImportError("bundle refused: unreadable manifest.json", [str(exc)]) from exc
    return parsed if isinstance(parsed, dict) else {}


def _bundle_id(base: Path, manifest: dict[str, Any]) -> str:
    """Stable identity of the bundle across re-exports of the same vault."""
    for key in ("bundle_id", "canonical_source"):
        value = _text(manifest.get(key))
        if value:
            return f"{key}:{value}"
    return f"name:{base.name}"


def _tags(front: dict[str, Any], concept_type: str, warnings: list[str], rel: str) -> list[str]:
    raw = front.get("tags")
    tags: list[str] = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, (str, int, float)) and not isinstance(item, bool):
                text = str(item).strip()
                if text and "\n" not in text and text not in tags:
                    tags.append(text)
            else:
                warnings.append(f"{rel}: dropped non-scalar tag")
    elif raw is not None:
        warnings.append(f"{rel}: tags is not a list; ignored")
    # Inverse of the export: OKF ``type`` comes from the first ``type:`` tag,
    # and ``Memory`` is the fallback for a memory that had none.
    if concept_type != "Memory" and not any(tag.startswith("type:") for tag in tags):
        tags.insert(0, f"type:{concept_type}")
    return tags


def _title(front: dict[str, Any], doc_id: str) -> str:
    title = _text(front.get("title")) or doc_id
    title = re.sub(r"\s+", " ", title).strip()
    return title[:_TITLE_MAX] or "memory"


def _proposal_id(bundle_id: str, doc_id: str, content_hash: str) -> str:
    digest = hashlib.sha256(f"{bundle_id}\0{doc_id}\0{content_hash}".encode()).hexdigest()
    return f"okf-{digest[:32]}"


def _vault_hashes(repository: Repository) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for document in repository.list():
        hashes.setdefault(body_content_hash(document.body.strip()), document.id)
    return hashes


def _live_staged(root: Path) -> tuple[dict[str, Path], dict[str, Path]]:
    """Pending, unexpired candidates by proposal_id and by content hash."""
    by_proposal: dict[str, Path] = {}
    by_hash: dict[str, Path] = {}
    for path in staged_candidate_paths(root):
        try:
            candidate = read_candidate(path)
        except MemfwError:
            continue
        if candidate.status != "pending" or candidate_is_expired(candidate):
            continue
        if candidate.proposal_id:
            by_proposal.setdefault(candidate.proposal_id, path)
        if candidate.content_hash:
            by_hash.setdefault(candidate.content_hash, path)
    return by_proposal, by_hash


def _free_filename(directory: Path, stem: str, taken: set[str]) -> str:
    """``stem.md``, or ``stem-N.md`` when an applied/expired candidate holds the name."""
    for attempt in range(1, 100):
        name = f"{stem}.md" if attempt == 1 else f"{stem}-{attempt}.md"
        if name not in taken and not os.path.lexists(directory / name):
            taken.add(name)
            return name
    raise OkfImportError(f"no free candidate file name for {stem}")


@dataclass(slots=True)
class _Planned:
    item: OkfImportItem
    filename: str
    document: str


def _render(
    rel: str,
    front: dict[str, Any],
    body: str,
    item: OkfImportItem,
    *,
    concept_type: str,
    source: str,
    tenant: str,
    bundle_name: str,
    bundle_key: str,
    warnings: list[str],
) -> str:
    tags = _tags(front, concept_type, warnings, rel)
    provenance: dict[str, Any] = {
        "bundle": bundle_name,
        "bundle_key": bundle_key,
        "concept": rel,
        "doc_id": item.doc_id,
        "type": concept_type,
        "untrusted": True,
    }
    for key in ("description", "resource", "status", "sources", "generated"):
        if key in front:
            provenance[key] = _json_safe(front[key])
    raw_memfw = front.get("memfw")
    if isinstance(raw_memfw, dict) and raw_memfw:
        # layer, tier, epistemic, timestamps and the original source travel
        # here for the reviewer; candidate-apply writes title, body and tags.
        provenance["memfw"] = _json_safe(raw_memfw)
    if item.unknown_fields:
        provenance["unknown_fields"] = _json_safe({key: front[key] for key in item.unknown_fields})
    assert item.proposal_id is not None
    document = candidate_document(
        title=item.title,
        text=body,
        proposal_id=item.proposal_id,
        tenant=tenant,
        tags=tags,
        related=[],
        suggestions=[],
        source=source,
        extra={"okf_import": provenance},
    )
    size = len(document.encode("utf-8"))
    if size > MAX_RENDERED_BYTES:
        raise OkfImportError(f"{rel}: candidate would be {size} bytes (cap {MAX_RENDERED_BYTES})")
    return document


def import_okf(
    bundle: Path | str,
    root: Path | str,
    *,
    dry_run: bool = True,
    tenant: str = "",
) -> OkfImportResult:
    """Stage every concept of ``bundle`` as a candidate in the vault at ``root``.

    ``dry_run`` (the default) reports what would be staged and writes nothing.
    Raises :class:`OkfImportError` when the bundle is unsafe or invalid; then
    nothing is staged.
    """
    snapshot = _snapshot(bundle)
    if snapshot.errors:
        raise OkfImportError("bundle refused: unsafe paths", snapshot.errors)

    parsed: dict[str, tuple[dict[str, Any], str]] = {}
    parse_errors: list[str] = []
    for rel in snapshot.concepts():
        try:
            parsed[rel] = _parse_concept(rel, snapshot.files[rel])
        except OkfImportError as exc:
            parse_errors.append(str(exc))
    if parse_errors:
        raise OkfImportError("bundle refused: OKF validation failed", parse_errors)
    validation = _validate_snapshot(snapshot)
    if not validation.valid:
        raise OkfImportError("bundle refused: OKF validation failed", validation.errors)

    repository = Repository(Path(root))
    if not repository.events_path.exists() and not repository.config_path.exists():
        raise OkfImportError(f"not a memfw vault: {root}; run `memfw init {root}` first")
    vault_root = repository.root
    candidate_dir = vault_root / CANDIDATE_DIR

    base = snapshot.base
    bundle_id = _bundle_id(base, _manifest(snapshot))
    source = f"okf:{_slug(base.name, 64)}"
    bundle_key = hashlib.sha256(bundle_id.encode()).hexdigest()[:16]
    result = OkfImportResult(
        bundle=base,
        root=vault_root,
        source=source,
        dry_run=dry_run,
        validation=validation,
        warnings=list(validation.warnings),
    )

    vault_hashes = _vault_hashes(repository)
    staged_proposals, staged_hashes = _live_staged(vault_root)
    seen_hashes: dict[str, str] = {}
    taken: set[str] = set()
    planned: list[_Planned] = []

    # Phase 1: decide and render every candidate. Nothing is written yet.
    render_errors: list[str] = []
    for rel, (front, body) in parsed.items():
        raw_memfw = front.get("memfw")
        memfw_block: dict[str, Any] = raw_memfw if isinstance(raw_memfw, dict) else {}
        doc_id = _text(memfw_block.get("id")) or rel.removesuffix(".md")
        concept_type = str(front["type"]).strip()
        unknown = sorted(str(key) for key in front if key not in KNOWN_FIELDS)
        item = OkfImportItem(
            concept=rel,
            doc_id=doc_id,
            title=_title(front, doc_id),
            status="",
            unknown_fields=unknown,
        )
        result.items.append(item)

        if not body:
            item.status, item.reason = "skipped", "empty_body"
            continue
        content_hash = body_content_hash(body)
        item.content_hash = content_hash
        item.proposal_id = _proposal_id(bundle_id, doc_id, content_hash)

        if content_hash in vault_hashes:
            item.status, item.reason = "duplicate", "same content hash as a vault memory"
            item.memory_id = vault_hashes[content_hash]
            continue
        if item.proposal_id in staged_proposals:
            item.status, item.reason = "already_staged", "proposal_id already staged"
            item.candidate = str(staged_proposals[item.proposal_id])
            continue
        if content_hash in staged_hashes:
            item.status, item.reason = "duplicate", "same content hash as a pending candidate"
            item.candidate = str(staged_hashes[content_hash])
            continue
        if content_hash in seen_hashes:
            item.status = "duplicate"
            item.reason = f"same content hash as {seen_hashes[content_hash]} in this bundle"
            continue
        seen_hashes[content_hash] = rel

        try:
            document = _render(
                rel,
                front,
                body,
                item,
                concept_type=concept_type,
                source=source,
                tenant=tenant,
                bundle_name=base.name,
                bundle_key=bundle_key,
                warnings=result.warnings,
            )
        except OkfImportError as exc:
            render_errors.append(str(exc))
            continue
        except (ValueError, TypeError, RecursionError) as exc:
            render_errors.append(f"{rel}: cannot render candidate: {exc}")
            continue
        filename = _free_filename(candidate_dir, f"{_slug(item.title)}-{item.proposal_id}", taken)
        item.candidate = str(candidate_dir / filename)
        item.status = "would_stage"
        planned.append(_Planned(item, filename, document))
    if render_errors:
        raise OkfImportError("bundle refused: candidates could not be built", render_errors)
    if dry_run:
        return result

    # Phase 2: publish. Any failure or interrupt, even one that lands right
    # after a link, removes every file this run linked into place.
    created: list[PublishedCandidate] = []
    completed = False
    try:
        for plan in planned:
            try:
                publish_candidate(vault_root, plan.filename, plan.document, created=created)
            except FileExistsError:
                plan.item.status, plan.item.reason = "already_staged", "candidate file exists"
                continue
            plan.item.status = "staged"
        completed = True
    except Exception as exc:
        raise OkfImportError("staging failed; nothing was staged", [str(exc)]) from exc
    finally:
        if not completed:
            for entry in created:
                try:
                    unpublish_candidate(vault_root, entry)
                except OSError:
                    pass
            for plan in planned:
                if plan.item.status == "staged":
                    plan.item.status = "would_stage"
    return result


__all__ = [
    "KNOWN_FIELDS",
    "MAX_BUNDLE_ENTRIES",
    "MAX_CONCEPT_BYTES",
    "MAX_NAVIGATION_BYTES",
    "MAX_RENDERED_BYTES",
    "OkfImportError",
    "OkfImportItem",
    "OkfImportResult",
    "check_bundle_paths",
    "import_okf",
]
