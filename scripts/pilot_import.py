"""Privacy-first, read-only importer for a small set of explicit pilot formats."""

from __future__ import annotations

import hashlib
import json
import posixpath
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit
from zoneinfo import ZoneInfo

import yaml

_WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")
_MDLINK_RE = re.compile(r"(?<!!)\[[^\]]*\]\(([^)]+\.md)(?:#[^)]*)?\)", re.IGNORECASE)
_FRONTMATTER_RE = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|$)", re.DOTALL)
_H1_RE = re.compile(r"(?m)^#\s+(.+?)\s*#*\s*$")
_TXT_HEADER = "MEMFW_CHAT_EXPORT_V1"
_SUPPORTED = {".md", ".json", ".txt"}
_SHANGHAI = ZoneInfo("Asia/Shanghai")


class ImportFormatError(ValueError):
    """A safe-to-report importer failure with no source content included."""

    def __init__(self, code: str, diagnostic: str) -> None:
        super().__init__(f"{code}: {diagnostic}")
        self.code = code
        self.diagnostic = diagnostic


@dataclass(frozen=True, slots=True)
class ImportRecord:
    """Validated source record ready for the runner to convert to a proposal."""

    record_id: str
    source_id: str | None
    title: str
    body: str
    source_format: str
    source_ref: str
    created_at: datetime
    updated_at: datetime
    references: tuple[str, ...]
    unresolved_references: tuple[str, ...]
    canonical_bytes: int
    tags: tuple[str, ...] = ()
    timestamp_source: str = "metadata"

    def payload(self) -> dict[str, Any]:
        """Return a Repository.add-compatible payload with deterministic candidate ID."""
        return {
            "memory_id": self.record_id,
            "title": self.title,
            "body": self.body,
            "source": f"pilot-import:{self.source_format}",
            "provenance": self.source_ref,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "tags": list(self.tags),
        }


@dataclass(frozen=True, slots=True)
class ImportResult:
    """Whole validated import batch plus body-free aggregate metadata."""

    records: tuple[ImportRecord, ...]
    canonical_bytes: int
    earliest: datetime | None
    latest: datetime | None
    unresolved_references: tuple[str, ...]
    diagnostics: tuple[str, ...]
    skipped_non_utf8: int = 0
    skipped_empty: int = 0
    skipped_invalid_frontmatter: int = 0
    mtime_fallback_records: int = 0
    supported_files: int = 0
    imported_records: int = 0
    skipped_hidden: int = 0
    skipped_read_error: int = 0
    timestamp_metadata_records: int = 0
    timestamp_date_records: int = 0


def _safe_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    return "unsupported"


def _structure_only(value: Any) -> str:
    """Summarize unknown input structure without including keys or values."""
    if isinstance(value, dict):
        types: dict[str, int] = {}
        for item in value.values():
            kind = _safe_type(item)
            types[kind] = types.get(kind, 0) + 1
        summary = json.dumps(types, sort_keys=True)
        return f"top_level=object field_count={len(value)} value_types={summary}"
    if isinstance(value, list):
        types = {}
        for item in value:
            kind = _safe_type(item)
            types[kind] = types.get(kind, 0) + 1
        summary = json.dumps(types, sort_keys=True)
        return f"top_level=array item_count={len(value)} item_types={summary}"
    return f"top_level={_safe_type(value)}"


def _fallback_times(path: Path) -> tuple[datetime, datetime]:
    try:
        stat = path.stat()
    except OSError:
        raise ImportFormatError("SOURCE_READ_FAILED", "source metadata unavailable") from None
    modified = datetime.fromtimestamp(stat.st_mtime, UTC)
    return modified, modified


def _parse_timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=_SHANGHAI) if value.tzinfo is None else value
    if isinstance(value, date):
        return datetime.combine(value, time.min, tzinfo=_SHANGHAI)
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=_SHANGHAI) if parsed.tzinfo is None else parsed


def _timestamp_pair(
    created: Any, updated: Any, fallback: tuple[datetime, datetime] | None
) -> tuple[datetime, datetime]:
    created_at = (
        (fallback[0] if fallback is not None else None)
        if created is None
        else _parse_timestamp(created)
    )
    updated_at = (
        (fallback[1] if fallback is not None else None)
        if updated is None
        else _parse_timestamp(updated)
    )
    if created_at is None or updated_at is None:
        raise ImportFormatError("INVALID_TIMESTAMP", "explicit timestamp is invalid")
    if updated_at < created_at:
        raise ImportFormatError("INVALID_TIMESTAMP", "modified time precedes created time")
    return created_at, updated_at


def _metadata_times(metadata: dict[str, Any], path: Path) -> tuple[datetime, datetime, str]:
    """Resolve nested metadata first, then legacy fields, date, and mtime."""
    nested = metadata.get("metadata")
    nested = nested if isinstance(nested, dict) else {}
    has_nested_time = any(
        nested.get(key) is not None for key in ("created_at", "updated_at", "modified")
    )
    if has_nested_time:
        created_value = nested.get("created_at")
        updated_value = nested.get("updated_at")
        modified_value = nested.get("modified")
        date_value = None
    else:
        date_value = metadata.get("date")
        created_value = updated_value = modified_value = None
        if date_value is None:
            created_value = metadata.get("created_at", metadata.get("created"))
            updated_value = metadata.get("updated_at", metadata.get("updated"))
            modified_value = metadata.get("modified")
    chosen_created = created_value if created_value is not None else modified_value
    chosen_updated = updated_value if updated_value is not None else modified_value
    if chosen_created is None:
        chosen_created = date_value
    if chosen_updated is None:
        chosen_updated = date_value
    if chosen_created is None and chosen_updated is not None:
        chosen_created = chosen_updated
    if chosen_updated is None and chosen_created is not None:
        chosen_updated = chosen_created
    source = (
        "mtime"
        if chosen_created is None and chosen_updated is None
        else ("date" if date_value is not None else "metadata")
    )
    fallback = _fallback_times(path) if source == "mtime" else None
    created, updated = _timestamp_pair(chosen_created, chosen_updated, fallback)
    return created, updated, source


def _record_id(relative: str) -> str:
    return "import-" + hashlib.sha256(relative.encode("utf-8")).hexdigest()[:32]


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _parse_markdown(path: Path, raw: str, relative: str) -> dict[str, Any]:
    match = _FRONTMATTER_RE.match(raw)
    if raw.startswith("---") and match is None:
        raise ImportFormatError("INVALID_MARKDOWN", "frontmatter delimiter is incomplete")
    metadata: dict[str, Any] = {}
    body = raw
    if match is not None:
        try:
            parsed_metadata = yaml.safe_load(match.group(1))
        except yaml.YAMLError:
            raise ImportFormatError("INVALID_MARKDOWN", "frontmatter could not be parsed") from None
        if not isinstance(parsed_metadata, dict):
            raise ImportFormatError("INVALID_MARKDOWN", "frontmatter must be a mapping")
        metadata = parsed_metadata
        body = raw[match.end() :]
    title = _text(metadata.get("title"))
    if title is None:
        heading = _H1_RE.search(body)
        title = heading.group(1).strip() if heading else path.stem
    if not body.strip():
        raise ImportFormatError("EMPTY_SOURCE", "source contains no body")
    created, updated, timestamp_source = _metadata_times(metadata, path)
    source_id = _text(metadata.get("id"))
    return {
        "record_id": _record_id(relative),
        "source_id": source_id,
        "title": title,
        "body": body,
        "source_format": "markdown",
        "source_ref": f"markdown:{relative}",
        "source_path": relative,
        "created_at": created,
        "updated_at": updated,
        "link_targets": _WIKILINK_RE.findall(body)
        + [f"mdpath:{target}" for target in _MDLINK_RE.findall(body)],
        "tags": (
            (metadata["tags"],)
            if isinstance(metadata.get("tags"), str)
            else tuple(tag for tag in metadata.get("tags", []) if isinstance(tag, str))
            if isinstance(metadata.get("tags", []), list)
            else ()
        ),
        "timestamp_source": timestamp_source,
    }


def _chat_messages(mapping: Any, current_node: Any = None) -> tuple[str, int]:
    if not isinstance(mapping, dict) or not mapping:
        raise ImportFormatError("INVALID_CHAT_JSON", "mapping must be a non-empty object")
    if current_node is not None:
        if not isinstance(current_node, str) or current_node not in mapping:
            raise ImportFormatError(
                "INVALID_CHAT_JSON", "current_node must identify a mapping node"
            )
        chain: list[dict[str, Any]] = []
        seen: set[str] = set()
        node_id: str | None = current_node
        while node_id is not None:
            if node_id in seen:
                raise ImportFormatError("INVALID_CHAT_JSON", "mapping ancestry contains a cycle")
            seen.add(node_id)
            node = mapping.get(node_id)
            if not isinstance(node, dict):
                raise ImportFormatError("INVALID_CHAT_JSON", "mapping ancestry is incomplete")
            chain.append(node)
            parent = node.get("parent")
            if parent is not None and not isinstance(parent, str):
                raise ImportFormatError("INVALID_CHAT_JSON", "mapping parent has an invalid type")
            node_id = parent
        nodes = list(reversed(chain))
    else:
        nodes = [node for node in mapping.values() if isinstance(node, dict)]
        parents = {node.get("parent") for node in nodes if isinstance(node.get("parent"), str)}
        leaves = [key for key in mapping if key not in parents]
        if len(leaves) != 1:
            raise ImportFormatError(
                "AMBIGUOUS_CHAT_BRANCHES",
                "current_node is required when the conversation has multiple branches",
            )
        branch: list[dict[str, Any]] = []
        node_id = leaves[0]
        seen = set()
        while node_id is not None:
            if node_id in seen:
                raise ImportFormatError("INVALID_CHAT_JSON", "mapping ancestry contains a cycle")
            seen.add(node_id)
            node = mapping.get(node_id)
            if not isinstance(node, dict):
                raise ImportFormatError("INVALID_CHAT_JSON", "mapping ancestry is incomplete")
            branch.append(node)
            parent = node.get("parent")
            node_id = parent if isinstance(parent, str) else None
        nodes = list(reversed(branch))
    lines: list[str] = []
    for node in nodes:
        message = node.get("message")
        if not isinstance(message, dict):
            continue
        author = message.get("author")
        content = message.get("content")
        if not isinstance(author, dict) or not isinstance(content, dict):
            continue
        role = author.get("role")
        parts = content.get("parts")
        if not isinstance(role, str) or not isinstance(parts, list):
            continue
        text_parts = [part for part in parts if isinstance(part, str) and part.strip()]
        if text_parts:
            lines.append(f"{role}: {' '.join(text_parts)}")
    if not lines:
        raise ImportFormatError("INVALID_CHAT_JSON", "no text messages found in known schema")
    return "\n".join(lines), len(lines)


def _parse_json(raw: str, relative: str, path: Path) -> list[dict[str, Any]]:
    try:
        value = json.loads(raw)
    except (ValueError, RecursionError):
        raise ImportFormatError("INVALID_JSON", "JSON could not be parsed") from None
    conversations: Any
    if isinstance(value, list):
        conversations = value
    elif isinstance(value, dict) and set(value) == {"conversations"}:
        conversations = value.get("conversations")
    else:
        raise ImportFormatError("STRUCTUREONLY", _structure_only(value))
    if not isinstance(conversations, list) or not conversations:
        raise ImportFormatError("INVALID_CHAT_JSON", "conversations must be a non-empty array")
    result: list[dict[str, Any]] = []
    fallback = _fallback_times(path)
    for index, conversation in enumerate(conversations):
        if not isinstance(conversation, dict):
            raise ImportFormatError("INVALID_CHAT_JSON", "conversation entries must be objects")
        required = {"id", "title", "create_time", "update_time", "mapping"}
        if not required.issubset(conversation):
            raise ImportFormatError("STRUCTUREONLY", _structure_only(conversation))
        source_id = _text(conversation.get("id"))
        title = _text(conversation.get("title"))
        if source_id is None or title is None:
            raise ImportFormatError("INVALID_CHAT_JSON", "id and title must be non-empty strings")
        body, _ = _chat_messages(conversation.get("mapping"), conversation.get("current_node"))
        created, updated = _timestamp_pair(
            conversation.get("create_time"), conversation.get("update_time"), fallback
        )
        result.append(
            {
                "record_id": _record_id(f"{relative}#{index}:{source_id}"),
                "source_id": source_id,
                "title": title,
                "body": body,
                "source_format": "chat-json",
                "source_ref": f"chat-json:{relative}#{source_id}",
                "source_path": relative,
                "created_at": created,
                "updated_at": updated,
                "link_targets": [],
            }
        )
    return result


def _parse_txt(raw: str, relative: str, path: Path) -> dict[str, Any]:
    lines = raw.splitlines()
    if len(lines) < 5 or lines[0] != _TXT_HEADER or not lines[1].startswith("Title: "):
        raise ImportFormatError("STRUCTUREONLY", f"top_level=text line_count={len(lines)}")
    if not lines[2].startswith("Created: ") or not lines[3].startswith("Modified: ") or lines[4]:
        raise ImportFormatError("STRUCTUREONLY", f"header={_TXT_HEADER} line_count={len(lines)}")
    title = _text(lines[1][7:])
    body = "\n".join(lines[5:]).strip()
    if title is None or not body:
        raise ImportFormatError("INVALID_CHAT_TEXT", "title and body are required")
    fallback = _fallback_times(path)
    created, updated = _timestamp_pair(lines[2][9:], lines[3][9:], fallback)
    return {
        "record_id": _record_id(relative),
        "source_id": None,
        "title": title,
        "body": body,
        "source_format": "memfw-chat-text-v1",
        "source_ref": f"chat-text-v1:{relative}",
        "source_path": relative,
        "created_at": created,
        "updated_at": updated,
        "link_targets": [],
    }


def _inside_root(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=True).relative_to(root)
    except (OSError, ValueError):
        return False
    return True


def import_pilot(source_root: Path | str) -> ImportResult:
    """Read, validate, and normalize supported files without modifying the source."""
    try:
        root = Path(source_root).expanduser().resolve(strict=True)
    except OSError:
        raise ImportFormatError("INVALID_ROOT", "source root is unavailable") from None
    if not root.is_dir():
        raise ImportFormatError("INVALID_ROOT", "source root must be a directory")
    source_paths: list[Path] = []
    skipped_hidden = 0
    supported_files = 0
    for candidate in root.rglob("*"):
        if candidate.is_file() and candidate.suffix.lower() in _SUPPORTED:
            supported_files += 1
        if any(part.startswith(".") for part in candidate.relative_to(root).parts):
            if candidate.is_file() and candidate.suffix.lower() in _SUPPORTED:
                skipped_hidden += 1
            continue
        if candidate.is_symlink() and not _inside_root(candidate, root):
            raise ImportFormatError("PATH_REJECTED", "symlink resolves outside source root")
        if candidate.is_file():
            if not _inside_root(candidate, root):
                raise ImportFormatError("PATH_REJECTED", "source path resolves outside source root")
            if candidate.suffix.lower() in _SUPPORTED:
                source_paths.append(candidate)
    if supported_files == 0:
        raise ImportFormatError("EMPTY_PILOT", "no supported source files found")

    parsed: list[dict[str, Any]] = []
    skipped_non_utf8 = skipped_empty = skipped_invalid_frontmatter = skipped_read_error = 0
    for path in sorted(source_paths, key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        try:
            data = path.read_bytes()
            raw = data.decode("utf-8")
        except UnicodeDecodeError:
            skipped_non_utf8 += 1
            continue
        except OSError:
            skipped_read_error += 1
            continue
        if not raw.strip():
            skipped_empty += 1
            continue
        try:
            if path.suffix.lower() == ".md":
                entries = [_parse_markdown(path, raw, relative)]
            elif path.suffix.lower() == ".json":
                entries = _parse_json(raw, relative, path)
            else:
                entries = [_parse_txt(raw, relative, path)]
        except ImportFormatError as exc:
            if exc.code == "INVALID_MARKDOWN":
                skipped_invalid_frontmatter += 1
                continue
            if exc.code == "EMPTY_SOURCE":
                skipped_empty += 1
                continue
            raise
        parsed.extend(entries)

    if not parsed:
        return ImportResult(
            records=(),
            canonical_bytes=0,
            earliest=None,
            latest=None,
            unresolved_references=(),
            diagnostics=("validated_records=0",),
            skipped_non_utf8=skipped_non_utf8,
            skipped_empty=skipped_empty,
            skipped_invalid_frontmatter=skipped_invalid_frontmatter,
            supported_files=supported_files,
            imported_records=0,
            skipped_hidden=skipped_hidden,
            skipped_read_error=skipped_read_error,
        )

    ids = [item["record_id"] for item in parsed]
    source_ids = [item["source_id"] for item in parsed if item["source_id"] is not None]
    if len(ids) != len(set(ids)) or len(source_ids) != len(set(source_ids)):
        raise ImportFormatError("DUPLICATE_ID", "duplicate deterministic record or source ID")
    aliases: dict[str, set[str]] = {}
    for item in parsed:
        for alias in (item["source_id"], item["title"], item["record_id"]):
            if isinstance(alias, str) and alias:
                aliases.setdefault(alias.casefold(), set()).add(item["record_id"])
        source_path = Path(item["source_path"])
        for alias in (
            source_path.as_posix(),
            source_path.with_suffix("").as_posix(),
            source_path.stem,
        ):
            aliases.setdefault(alias.casefold(), set()).add(item["record_id"])

    records: list[ImportRecord] = []
    unresolved: list[str] = []
    for item in parsed:
        references: list[str] = []
        missing: list[str] = []
        for target in item["link_targets"]:
            if target.startswith("mdpath:"):
                link = unquote(target.removeprefix("mdpath:")).strip()
                parsed_link = urlsplit(link)
                if parsed_link.scheme or parsed_link.netloc or link.startswith("/"):
                    continue
                relative_link = posixpath.normpath(
                    posixpath.join(Path(item["source_path"]).parent.as_posix(), parsed_link.path)
                )
                if relative_link == ".." or relative_link.startswith("../"):
                    continue
                path_matches = aliases.get(relative_link.casefold(), set())
                if len(path_matches) == 1:
                    references.append(next(iter(path_matches)))
                continue
            normalized = target.strip().replace("\\", "/")
            source_parent = Path(item["source_path"]).parent
            candidates = (
                (source_parent / normalized).as_posix(),
                (source_parent / normalized).with_suffix("").as_posix(),
                normalized,
                Path(normalized).with_suffix("").as_posix(),
                Path(normalized).stem,
            )
            matches: set[str] = set()
            for alias in candidates:
                matches = aliases.get(alias.casefold(), set())
                if matches:
                    break
            if len(matches) == 1:
                references.append(next(iter(matches)))
            else:
                missing.append(target)
                unresolved.append(target)
        clean = {
            key: value for key, value in item.items() if key not in {"link_targets", "source_path"}
        }
        canonical = json.dumps(
            {
                **clean,
                "created_at": clean["created_at"].isoformat(),
                "updated_at": clean["updated_at"].isoformat(),
                "references": references,
                "unresolved_references": missing,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        records.append(
            ImportRecord(
                **clean,
                references=tuple(references),
                unresolved_references=tuple(missing),
                canonical_bytes=len(canonical),
            )
        )
    times = [time for record in records for time in (record.created_at, record.updated_at)]
    return ImportResult(
        records=tuple(records),
        canonical_bytes=sum(record.canonical_bytes for record in records),
        earliest=min(times) if times else None,
        latest=max(times) if times else None,
        unresolved_references=tuple(unresolved),
        diagnostics=(f"validated_records={len(records)}",),
        skipped_non_utf8=skipped_non_utf8,
        skipped_empty=skipped_empty,
        skipped_invalid_frontmatter=skipped_invalid_frontmatter,
        mtime_fallback_records=sum(record.timestamp_source == "mtime" for record in records),
        supported_files=supported_files,
        imported_records=len(records),
        skipped_hidden=skipped_hidden,
        skipped_read_error=skipped_read_error,
        timestamp_metadata_records=sum(record.timestamp_source == "metadata" for record in records),
        timestamp_date_records=sum(record.timestamp_source == "date" for record in records),
    )
