"""Deterministic Markdown serialization and version-object storage."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, cast

import yaml

from ..core.common import sha256_text
from ..core.context import RepositoryHost
from ..core.errors import IntegrityError
from ..core.models import MemoryDocument

_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_BODY_MARKERS = (b"\n---\n\n", b"\r\n---\r\n\r\n")


def _split_raw_markdown(raw: bytes) -> tuple[bytes, bytes]:
    for marker in _BODY_MARKERS:
        offset = raw.find(marker)
        if offset >= 0:
            end = offset + len(marker)
            return raw[:end], raw[end:]
    raise IntegrityError("Markdown has no frontmatter/body separator")


class MarkdownStorageMixin:
    def _current_path(self: RepositoryHost, memory_id: str) -> Path:
        self._check_id(memory_id)
        return self.memories_dir / f"{memory_id}.md"

    def _history_path(self: RepositoryHost, memory_id: str, content_hash: str) -> Path:
        self._check_id(memory_id)
        if not _HASH_RE.fullmatch(content_hash):
            raise IntegrityError("Invalid content hash")
        return self.history_dir / memory_id / f"{content_hash}.md"

    def _history_metadata_path(self: RepositoryHost, memory_id: str, content_hash: str) -> Path:
        return self._history_path(memory_id, content_hash).with_suffix(".meta.json")

    def _body_object_path(self: RepositoryHost, body_hash: str) -> Path:
        if not _HASH_RE.fullmatch(body_hash):
            raise IntegrityError("Invalid body hash")
        return self.history_dir / "bodies" / f"{body_hash}.txt"

    def _has_version(self: RepositoryHost, memory_id: str, content_hash: str) -> bool:
        current = self._current_path(memory_id)
        return (
            (current.exists() and hashlib.sha256(current.read_bytes()).hexdigest() == content_hash)
            or self._history_path(memory_id, content_hash).exists()
            or self._history_metadata_path(memory_id, content_hash).exists()
        )

    def _read_version(self: RepositoryHost, memory_id: str, content_hash: str) -> bytes:
        """Reconstruct an exact retained version from a full snapshot or metadata delta."""
        current = self._current_path(memory_id)
        if current.exists():
            current_raw = current.read_bytes()
            if hashlib.sha256(current_raw).hexdigest() == content_hash:
                return current_raw
        full_path = self._history_path(memory_id, content_hash)
        if full_path.exists():
            raw = full_path.read_bytes()
        else:
            delta_path = self._history_metadata_path(memory_id, content_hash)
            try:
                delta = json.loads(delta_path.read_text(encoding="utf-8"))
                if delta.get("schema_version") not in {2, 3}:
                    base_hash = delta["base_hash"]
                    base_doc = self.parse(
                        self._read_version(memory_id, base_hash).decode("utf-8"),
                        expected_id=memory_id,
                    )
                    values = base_doc.model_dump(mode="json", exclude={"body"})
                    values.update(delta["metadata_changes"])
                    doc = MemoryDocument.model_validate({**values, "body": base_doc.body})
                    raw = self.render(doc).encode("utf-8")
                    if hashlib.sha256(raw).hexdigest() != content_hash:
                        raise IntegrityError(f"Historical version hash mismatch: {content_hash}")
                    return raw
                if delta.get("schema_version") == 3:
                    header = str(delta["frontmatter"]).encode("utf-8")
                    body_hash = str(delta["body_hash"])
                    body = cast(Any, self)._current_body_by_hash(body_hash)
                    if body is None:
                        body = cast(Any, self)._archived_body_by_hash(memory_id, body_hash)
                    if body is None:
                        body = cast(
                            bytes, cast(Any, self)._body_object_path(body_hash).read_bytes()
                        )
                    raw = header + cast(bytes, body)
                    if hashlib.sha256(raw).hexdigest() != content_hash:
                        raise IntegrityError(f"Historical version hash mismatch: {content_hash}")
                    return raw
                metadata = delta["metadata"]
                body_hash = delta["body_hash"]
                body = cast(Any, self)._current_body_by_hash(body_hash)
                if body is None:
                    body = cast(Any, self)._archived_body_by_hash(memory_id, body_hash)
                if body is None:
                    body = cast(
                        str,
                        cast(Any, self)._body_object_path(body_hash).read_text(encoding="utf-8"),
                    )
                doc = MemoryDocument.model_validate({**metadata, "body": body})
                raw = self.render(doc).encode("utf-8")
            except Exception as exc:
                raise IntegrityError(
                    f"Invalid metadata history delta {content_hash}: {exc}"
                ) from exc
        if hashlib.sha256(raw).hexdigest() != content_hash:
            raise IntegrityError(f"Historical version hash mismatch: {content_hash}")
        return raw

    def _current_body_by_hash(
        self: RepositoryHost, body_hash: str, *, exclude_memory: str | None = None
    ) -> bytes | None:
        if not self.memories_dir.exists():
            return None
        for path in self.memories_dir.glob("*.md"):
            if path.stem == exclude_memory:
                continue
            try:
                raw = path.read_bytes()
                _, body = _split_raw_markdown(raw)
                self.parse(raw.decode("utf-8"), expected_id=path.stem)
            except (OSError, IntegrityError):
                continue
            if hashlib.sha256(body).hexdigest() == body_hash:
                return body
        return None

    def _archived_body_by_hash(
        self: RepositoryHost, memory_id: str, body_hash: str
    ) -> bytes | None:
        if not self.archives_dir.exists():
            return None
        import io
        import tarfile

        import zstandard

        for archive_path in sorted(self.archives_dir.glob("*.tar.zst")):
            try:
                tar_bytes = zstandard.ZstdDecompressor().decompress(archive_path.read_bytes())
                with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:") as archive:
                    member = archive.extractfile(f"memories/{memory_id}.md")
                    if member is None:
                        continue
                    raw = member.read()
                    _, body = _split_raw_markdown(raw)
                    self.parse(raw.decode("utf-8"), expected_id=memory_id)
                    if hashlib.sha256(body).hexdigest() == body_hash:
                        return body
            except Exception:
                continue
        return None

    def _body_source_hash(self: RepositoryHost, memory_id: str, content_hash: str) -> str:
        path = self._history_metadata_path(memory_id, content_hash)
        if not path.exists():
            return content_hash
        try:
            delta = json.loads(path.read_text(encoding="utf-8"))
            if delta.get("schema_version") == 3:
                # Schema 3 stores complete original frontmatter plus a direct
                # body hash. Its base_hash is provenance only; retaining that
                # metadata file would create an extra rollback slot.
                return content_hash
            if delta.get("schema_version") == 2:
                return content_hash
            # Day 4 history chains stored full Markdown bases by version hash.
            return str(delta["base_hash"])
        except (OSError, KeyError, ValueError, TypeError) as exc:
            raise IntegrityError(f"Invalid metadata history delta {content_hash}: {exc}") from exc

    @staticmethod
    def render(document: MemoryDocument) -> str:
        metadata = document.model_dump(mode="json", exclude={"body"})
        # Fixed field order makes identical inputs produce identical Markdown bytes.
        ordered = {
            key: metadata[key]
            for key in (
                "id",
                "title",
                "created_at",
                "updated_at",
                "layer",
                "tier",
                "source",
                "provenance",
                "anchor",
                "tags",
                "version",
                "summary",
                "is_stub",
                "archive_ref",
                "archived_hash",
                "strength",
                "reference_count",
                "last_access_interval_days",
                "manually_marked",
                "group_key",
            )
        }
        frontmatter = yaml.safe_dump(ordered, allow_unicode=True, sort_keys=False).rstrip()
        body = document.body.rstrip("\n")
        return f"---\n{frontmatter}\n---\n\n{body}\n"

    @staticmethod
    def parse(markdown: str, *, expected_id: str | None = None) -> MemoryDocument:
        markdown = markdown.replace("\r\n", "\n")
        if not markdown.startswith("---\n"):
            raise IntegrityError("Markdown is missing YAML frontmatter")

        class UniqueKeyLoader(yaml.SafeLoader):
            pass

        def unique_mapping(
            loader: yaml.SafeLoader, node: yaml.nodes.MappingNode, deep: bool = False
        ) -> dict[Any, Any]:
            mapping: dict[Any, Any] = {}
            for key_node, value_node in node.value:
                key = loader.construct_object(key_node, deep=deep)
                if key in mapping:
                    raise IntegrityError(f"Duplicate frontmatter key: {key}")
                mapping[key] = loader.construct_object(value_node, deep=deep)
            return mapping

        UniqueKeyLoader.add_constructor(
            yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping
        )
        try:
            _, raw, body = markdown.split("---\n", 2)
            metadata = yaml.load(raw, Loader=UniqueKeyLoader)
        except (ValueError, yaml.YAMLError) as exc:
            raise IntegrityError(f"Invalid Markdown frontmatter: {exc}") from exc
        if not isinstance(metadata, dict):
            raise IntegrityError("Frontmatter must be a YAML mapping")
        if expected_id is not None and metadata.get("id") != expected_id:
            raise IntegrityError("Frontmatter id does not match filename")
        body = body.lstrip("\n")
        if body.endswith("\n"):
            body = body[:-1]
        try:
            return MemoryDocument.model_validate({**metadata, "body": body})
        except Exception as exc:
            raise IntegrityError(f"Invalid memory document: {exc}") from exc

    def _write_version(
        self: RepositoryHost,
        memory_id: str,
        markdown: str,
        *,
        base_hash: str | None = None,
    ) -> str:
        digest = sha256_text(markdown)
        path = self._history_path(memory_id, digest)
        delta_path = self._history_metadata_path(memory_id, digest)
        path.parent.mkdir(parents=True, exist_ok=True)
        full_path = self._history_path(memory_id, digest)
        delta_path = self._history_metadata_path(memory_id, digest)
        if full_path.exists() or delta_path.exists():
            if self._read_version(memory_id, digest).decode("utf-8") != markdown:
                raise IntegrityError("SHA-256 collision in Markdown history")
            return digest
        self.parse(markdown, expected_id=memory_id)
        # Preserve the old body only when a retained historical metadata record
        # refers to it and the current body is about to change. Objects are
        # content-addressed across memories, so GC can keep shared references.
        current = self._current_path(memory_id)
        if not current.exists():
            return digest
        if current.exists():
            old_raw = current.read_bytes()
            _, old_body = _split_raw_markdown(old_raw)
            _, new_body = _split_raw_markdown(markdown.encode("utf-8"))
            if old_body != new_body:
                body_hash = hashlib.sha256(old_body).hexdigest()
                if (
                    cast(Any, self)._body_is_referenced(body_hash)
                    and cast(Any, self)._current_body_by_hash(body_hash, exclude_memory=memory_id)
                    is None
                    and cast(Any, self)._archived_body_by_hash(memory_id, body_hash) is None
                ):
                    body_path = cast(Any, self)._body_object_path(body_hash)
                    if not body_path.exists():
                        self._atomic_write(body_path, old_body)
        raw_markdown = markdown.encode("utf-8")
        frontmatter, body_bytes = _split_raw_markdown(raw_markdown)
        body_hash = hashlib.sha256(body_bytes).hexdigest()
        delta = {
            "schema_version": 3,
            "body_hash": body_hash,
            "frontmatter": frontmatter.decode("utf-8"),
        }
        if base_hash is not None:
            dependency_hash = base_hash
            # Metadata-only updates share a stable full-version base rather
            # than growing a one-version-per-update dependency chain.
            current_raw = current.read_bytes()
            _, current_body = _split_raw_markdown(current_raw)
            if current_body == body_bytes:
                dependency_hash = self._body_source_hash(memory_id, base_hash)
            delta["base_hash"] = dependency_hash
        self._atomic_write(
            delta_path,
            (
                json.dumps(delta, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
            ).encode(),
        )
        if (
            cast(Any, self)._current_body_by_hash(body_hash) is None
            and cast(Any, self)._archived_body_by_hash(memory_id, body_hash) is None
        ):
            body_path = cast(Any, self)._body_object_path(body_hash)
            if not body_path.exists():
                self._atomic_write(body_path, body_bytes)
        # Initial/current bodies are not duplicated in .history. Historical
        # bodies are materialized only on a later body replacement.
        return digest

    def _body_is_referenced(self: RepositoryHost, body_hash: str) -> bool:
        for delta_path in (
            self.history_dir.glob("*/*.meta.json") if self.history_dir.exists() else ()
        ):
            try:
                if json.loads(delta_path.read_text(encoding="utf-8")).get("body_hash") == body_hash:
                    return True
            except (OSError, ValueError, TypeError):
                continue
        return False

    def _preserve_shared_body_before_erasure(self: RepositoryHost, memory_id: str) -> None:
        current_path = self._current_path(memory_id)
        if not current_path.exists():
            return
        self.parse(current_path.read_text(encoding="utf-8"), expected_id=memory_id)
        _, body = _split_raw_markdown(current_path.read_bytes())
        body_hash = hashlib.sha256(body).hexdigest()
        history_dirs = self.history_dir.glob("*") if self.history_dir.exists() else ()
        referenced_elsewhere = False
        for directory in history_dirs:
            if not directory.is_dir() or directory.name == memory_id or directory.name == "bodies":
                continue
            for delta_path in directory.glob("*.meta.json"):
                try:
                    if (
                        json.loads(delta_path.read_text(encoding="utf-8")).get("body_hash")
                        == body_hash
                    ):
                        referenced_elsewhere = True
                        break
                except (OSError, ValueError, TypeError):
                    continue
            if referenced_elsewhere:
                break
        if (
            referenced_elsewhere
            and cast(Any, self)._current_body_by_hash(body_hash, exclude_memory=memory_id) is None
        ):
            body_path = cast(Any, self)._body_object_path(body_hash)
            if not body_path.exists():
                self._atomic_write(body_path, body)

    def _prune_history(self: RepositoryHost, memory_id: str) -> None:
        try:
            config = yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
            keep = max(
                1,
                int(config.get("history_keep", 2)),
                int(config.get("archive_keep_history", 1)),
            )
        except (OSError, TypeError, ValueError, yaml.YAMLError):
            keep = 2
        refs = [
            event.after_hash
            for event in self.events()
            if event.memory_id == memory_id and event.after_hash is not None
        ]
        retained = set(refs[-keep:])
        current = self._current_path(memory_id)
        if current.exists():
            retained.add(hashlib.sha256(current.read_bytes()).hexdigest())
        retained |= {self._body_source_hash(memory_id, item) for item in tuple(retained)}
        history = self.history_dir / memory_id
        if history.exists():
            for candidate in history.iterdir():
                content_hash = candidate.name.removesuffix(".meta.json").removesuffix(".md")
                if content_hash not in retained:
                    candidate.unlink()
        cast(Any, self)._prune_body_objects()

    def _prune_body_objects(self: RepositoryHost) -> None:
        body_dir = self.history_dir / "bodies"
        if not body_dir.exists():
            return
        referenced: set[str] = set()
        for delta_path in self.history_dir.glob("*/*.meta.json"):
            try:
                value = json.loads(delta_path.read_text(encoding="utf-8"))
                if isinstance(value.get("body_hash"), str):
                    referenced.add(value["body_hash"])
            except (OSError, ValueError, TypeError):
                continue
        for path in tuple(body_dir.glob("*.txt")):
            digest = path.stem
            if (
                digest not in referenced
                or cast(Any, self)._current_body_by_hash(digest) is not None
            ):
                path.unlink(missing_ok=True)
