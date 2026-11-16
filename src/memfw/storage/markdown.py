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
from ..core.markdown_parse import _OPTIONAL_DEFAULT_FIELDS, parse_memory_markdown
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


def _observed_default_fields(document: MemoryDocument) -> tuple[str, ...]:
    """Optional frontmatter fields the source file spelled out at their default."""

    return document._observed_default_fields


def _observed_default_fields_from_metadata(metadata: Any) -> tuple[str, ...]:
    """Same, for a retained frontmatter mapping that is not a live document."""

    if not isinstance(metadata, dict):
        return ()
    return tuple(key for key in _OPTIONAL_DEFAULT_FIELDS if key in metadata)


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
                    # ``model_dump`` above materialises every default, so the
                    # reconstructed file must inherit which optional fields the
                    # base version actually carried; otherwise a legacy delta
                    # re-renders to different bytes than the version it stores.
                    doc._observed_default_fields = _observed_default_fields(base_doc)
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
                # ``metadata`` is the original frontmatter, so its keys say
                # exactly which optional defaults that version spelled out.
                doc._observed_default_fields = _observed_default_fields_from_metadata(metadata)
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

        own_member = f"memories/{memory_id}.md"

        def matching_body(raw: bytes, expected_id: str) -> bytes | None:
            try:
                _, body = _split_raw_markdown(raw)
                self.parse(raw.decode("utf-8"), expected_id=expected_id)
            except Exception:
                return None
            return body if hashlib.sha256(body).hexdigest() == body_hash else None

        archives: list[tarfile.TarFile] = []
        try:
            for archive_path in sorted(self.archives_dir.glob("*.tar.zst")):
                try:
                    tar_bytes = zstandard.ZstdDecompressor().decompress(archive_path.read_bytes())
                    archives.append(tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:"))
                except Exception:
                    continue
            # The memory's own archived file is the usual source. Body objects
            # are content-addressed across memories, so another memory's
            # archived file holding the same bytes is an equally exact source:
            # pruning may have dropped the object while that memory was live.
            for archive in archives:
                try:
                    member = archive.extractfile(own_member)
                except Exception:
                    continue
                if member is None:
                    continue
                body = matching_body(member.read(), memory_id)
                if body is not None:
                    return body
            for archive in archives:
                try:
                    members = archive.getmembers()
                except Exception:
                    continue
                for info in members:
                    name = info.name
                    if (
                        name == own_member
                        or not info.isfile()
                        or not name.startswith("memories/")
                        or not name.endswith(".md")
                        or "/" in name.removeprefix("memories/")
                    ):
                        continue
                    try:
                        member = archive.extractfile(info)
                    except Exception:
                        continue
                    if member is None:
                        continue
                    other_id = name.removeprefix("memories/").removesuffix(".md")
                    body = matching_body(member.read(), other_id)
                    if body is not None:
                        return body
        finally:
            for archive in archives:
                archive.close()
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
                "type",
                "tenant",
                "related",
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
                "valid_from",
                "valid_to",
                "superseded_by",
            )
        }
        # Files written before ``recorded_at`` existed keep their bytes.
        if metadata.get("recorded_at"):
            ordered["recorded_at"] = metadata["recorded_at"]
        # Empty alias targets stay off the file so existing bytes do not change.
        for key in ("about", "applies_to"):
            if metadata.get(key):
                ordered[key] = metadata[key]
        # Same for verification state: the default (unconfirmed, no evidence)
        # is not written, so files that were never verified keep their bytes.
        if metadata.get("status", "unconfirmed") != "unconfirmed":
            ordered["status"] = metadata["status"]
        if metadata.get("evidence"):
            ordered["evidence"] = metadata["evidence"]
        # Epistemic/causal fields are absent from older files and stay off the
        # file while they hold their defaults, so existing bytes do not change.
        if metadata.get("epistemic"):
            ordered["epistemic"] = metadata["epistemic"]
        if metadata.get("causal", "none") != "none":
            ordered["causal"] = metadata["causal"]
        if metadata.get("causal_record"):
            ordered["causal_record"] = metadata["causal_record"]
        if metadata.get("limitations"):
            ordered["limitations"] = metadata["limitations"]
        if metadata.get("experience"):
            ordered["experience"] = metadata["experience"]
        # Files written before ``type``, ``tenant``, ``related`` and the temporal
        # fields existed keep their bytes too: the default carries no
        # information, so it stays off the file exactly like ``recorded_at`` and
        # the alias targets. A file re-rendered by this version must not gain
        # lines its own version never wrote.
        #
        # The other direction matters just as much: a file written on or after
        # 2026-11-06 carries all six lines even at their default, and an
        # unconditional drop would rewrite such a file and change its
        # ``content_hash`` for no reason. ``parse`` records which of the six the
        # source file had, so those come back exactly as they were.
        observed = set(getattr(document, "_observed_default_fields", ()))
        for key in _OPTIONAL_DEFAULT_FIELDS:
            if key not in observed and ordered.get(key) in (None, "", []):
                ordered.pop(key, None)
        frontmatter = yaml.safe_dump(ordered, allow_unicode=True, sort_keys=False).rstrip()
        body = document.body.rstrip("\n")
        return f"---\n{frontmatter}\n---\n\n{body}\n"

    @staticmethod
    def parse(markdown: str, *, expected_id: str | None = None) -> MemoryDocument:
        return parse_memory_markdown(markdown, expected_id=expected_id)

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
                # This memory's own archive is a sufficient body source only
                # for its own history. Another memory's history may reference
                # the same object, and that memory cannot rely on an archive
                # it does not own (the archive can later be erased or thawed).
                if (
                    cast(Any, self)._body_is_referenced(body_hash)
                    and cast(Any, self)._current_body_by_hash(body_hash, exclude_memory=memory_id)
                    is None
                    and (
                        cast(Any, self)._body_referenced_by_other(body_hash, memory_id)
                        or cast(Any, self)._archived_body_by_hash(memory_id, body_hash) is None
                    )
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

    def _body_referenced_by_other(self: RepositoryHost, body_hash: str, memory_id: str) -> bool:
        """Whether a retained version of a memory other than ``memory_id`` uses the body."""

        history_dirs = self.history_dir.glob("*") if self.history_dir.exists() else ()
        for directory in history_dirs:
            if not directory.is_dir() or directory.name == memory_id or directory.name == "bodies":
                continue
            for delta_path in directory.glob("*.meta.json"):
                try:
                    if (
                        json.loads(delta_path.read_text(encoding="utf-8")).get("body_hash")
                        == body_hash
                    ):
                        return True
                except (OSError, ValueError, TypeError, AttributeError):
                    continue
        return False

    def _preserve_shared_body_before_erasure(self: RepositoryHost, memory_id: str) -> None:
        current_path = self._current_path(memory_id)
        if not current_path.exists():
            return
        self.parse(current_path.read_text(encoding="utf-8"), expected_id=memory_id)
        _, body = _split_raw_markdown(current_path.read_bytes())
        body_hash = hashlib.sha256(body).hexdigest()
        if (
            cast(Any, self)._body_referenced_by_other(body_hash, memory_id)
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
