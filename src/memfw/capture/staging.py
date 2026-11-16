"""Write staged memory candidates under ``<vault>/_candidates/remember/``.

This is the one staging writer shared by every Ingestor: the MCP
``memory_remember`` tool and ``memfw okf import``. A candidate is never
authoritative; ``memfw candidate-apply`` is the only path from here into the
vault. The file layout is what :func:`memfw.capture.review.read_candidate`
reads: the content hash is the sha256 of the body, and every string in the
frontmatter is one double-quoted YAML line, so untrusted text cannot end the
frontmatter block early.
"""

from __future__ import annotations

import copy
import hashlib
import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from yaml.nodes import Node

#: Where staged memory candidates live, relative to the vault root.
CANDIDATE_DIR = Path("_candidates") / "remember"
#: Largest candidate body an Ingestor may stage.
MAX_CANDIDATE_BYTES = 64 * 1024


class _SingleLine(str):
    """A string rendered as one double-quoted YAML scalar."""


class _CandidateDumper(yaml.SafeDumper):
    """Dumper that keeps every string on one escaped line."""


def _represent_single_line(dumper: yaml.SafeDumper, data: _SingleLine) -> Node:
    return dumper.represent_scalar("tag:yaml.org,2002:str", str(data), style='"')


_CandidateDumper.add_representer(_SingleLine, _represent_single_line)


def single_line(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): single_line(item) for key, item in value.items()}
    if isinstance(value, list):
        return [single_line(item) for item in value]
    if isinstance(value, str):
        return _SingleLine(value)
    return value


def body_content_hash(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def candidate_document(
    *,
    title: str,
    text: str,
    proposal_id: str,
    tenant: str,
    tags: list[str],
    related: list[str],
    suggestions: list[dict[str, Any]],
    source: str = "candidate",
    extra: dict[str, Any] | None = None,
) -> str:
    """Frontmatter `candidate-apply` can read: hash is sha256 of the body.

    ``extra`` adds top-level keys after the standard ones (an Ingestor's own
    provenance block); it may not replace a standard key.
    """
    content_hash = body_content_hash(text)
    # The candidate is recorded now; ``recorded_at`` is the TTL clock.
    created_at = datetime.now(UTC).isoformat()
    metadata = {
        "type": "candidate",
        "lifecycle": "active",
        "proposal_id": proposal_id,
        "source": source,
        "tenant": tenant,
        "tags": list(tags),
        "related": list(related),
        "created_at": created_at,
        "recorded_at": created_at,
        "content_hash": content_hash,
        "suggestions": copy.deepcopy(suggestions),
        "authority": False,
    }
    header: dict[str, Any] = {
        "name": title,
        "title": title,
        "type": "candidate",
        "lifecycle": "active",
        "proposal_id": proposal_id,
        "source": source,
        "tenant": tenant,
        "tags": list(tags),
        "related": list(related),
        "created_at": created_at,
        "recorded_at": created_at,
        "content_hash": content_hash,
        "suggestions": copy.deepcopy(suggestions),
        "authority": False,
        "metadata": metadata,
    }
    for key, value in (extra or {}).items():
        if key in header:
            raise ValueError(f"extra key {key!r} would replace a candidate field")
        header[key] = copy.deepcopy(value)
    rendered = yaml.dump(
        single_line(header),
        Dumper=_CandidateDumper,
        allow_unicode=True,
        sort_keys=False,
        width=10**9,
    )
    if "---\n" in rendered:
        raise ValueError("candidate frontmatter is not safe to publish")
    return f"---\n{rendered}---\n\n{text}\n"


def open_candidate_directory(root: Path) -> int:
    """Open `<root>/_candidates/remember` without following a swapped symlink."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    root_fd = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    current = root_fd
    try:
        for part in CANDIDATE_DIR.parts:
            try:
                os.mkdir(part, 0o777, dir_fd=current)
            except FileExistsError:
                pass
            next_fd = os.open(part, flags, dir_fd=current)
            if current != root_fd:
                os.close(current)
            current = next_fd
        return current
    except BaseException:
        if current != root_fd:
            os.close(current)
        raise
    finally:
        os.close(root_fd)


@dataclass(frozen=True, slots=True)
class PublishedCandidate:
    """Identity of a file :func:`publish_candidate` may have linked into place."""

    filename: str
    device: int
    inode: int


def publish_candidate(
    root: Path,
    filename: str,
    text: str,
    *,
    created: list[PublishedCandidate] | None = None,
) -> None:
    """Write `filename` with O_EXCL plus an atomic link. Never overwrites.

    The same pattern as `/stage`: a unique temporary file is published with
    `os.link`, which fails if the destination already exists.

    When ``created`` is given, the file's identity is appended *before* the
    link, so a caller rolling back (even after an interrupt that lands right
    after ``os.link`` returns) can pass it to :func:`unpublish_candidate`,
    which removes the file only if it is still that inode.
    """
    directory_fd = open_candidate_directory(root)
    temporary_name = f".{filename}.{uuid.uuid4().hex}.tmp"
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory_fd,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            descriptor = None
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if created is not None:
            info = os.stat(temporary_name, dir_fd=directory_fd, follow_symlinks=False)
            created.append(PublishedCandidate(filename, info.st_dev, info.st_ino))
        os.link(temporary_name, filename, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary_name, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        os.close(directory_fd)


def unpublish_candidate(root: Path, published: PublishedCandidate) -> bool:
    """Remove a candidate this process published, if it is still that inode.

    A file another writer put at the same name has a different inode and is
    left alone. Returns True when a file was removed.
    """
    directory_fd = open_candidate_directory(root)
    try:
        try:
            info = os.stat(published.filename, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        if (info.st_dev, info.st_ino) != (published.device, published.inode):
            return False
        os.unlink(published.filename, dir_fd=directory_fd)
        return True
    finally:
        os.close(directory_fd)


__all__ = [
    "CANDIDATE_DIR",
    "PublishedCandidate",
    "unpublish_candidate",
    "MAX_CANDIDATE_BYTES",
    "body_content_hash",
    "candidate_document",
    "open_candidate_directory",
    "publish_candidate",
    "single_line",
]
