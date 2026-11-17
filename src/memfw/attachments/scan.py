"""``memfw attach scan``: a dry-run inventory of a configured root (MM1b, design section 8).

The scan walks ``<root>[/<sub dir>]`` without following symlinks and reports
counts, total size, media types, how many files look private (by path only:
nothing is read unless ``hash_files`` is set, and then only to count
duplicate sha256), how many are already registered, and what a bulk
registration would refuse (symlinks, names a location cannot hold). It writes
nothing: no record, no event, no blob, no ``vault_id``.
"""

from __future__ import annotations

import mimetypes
import os
import re
from collections import Counter
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any, Final

from .blobs import hash_stream
from .locations import (
    Location,
    LocationError,
    WalkStats,
    check_relative,
    looks_like_location,
    open_location,
    walk_root,
)

#: Path words that suggest a private document (matched case-insensitively on
#: whole path components and on words inside names). A hint for the report,
#: never a privacy decision: every registered attachment is local-private by default.
PRIVATE_HINTS: Final = (
    "private",
    "personal",
    "secret",
    "confidential",
    "password",
    "passport",
    "id-card",
    "idcard",
    "identity",
    "bank",
    "statement",
    "invoice",
    "receipt",
    "tax",
    "salary",
    "payslip",
    "contract",
    "medical",
    "health",
    "diagnosis",
    "insurance",
    "license",
    "私密",
    "隐私",
    "个人",
    "身份证",
    "护照",
    "银行",
    "流水",
    "发票",
    "收据",
    "合同",
    "工资",
    "病历",
    "体检",
    "保险",
    "税",
)
_WORD_RE: Final = re.compile(r"[a-z0-9]+")


def likely_private(relative: str) -> bool:
    """``relative`` (a location's path) has a word or component that suggests a private file."""

    text = relative.lower()
    words = set(_WORD_RE.findall(text))
    for hint in PRIVATE_HINTS:
        if hint.isascii() and "-" not in hint:
            if hint in words:
                return True
        elif hint in text:
            return True
    return False


def _start(target: str, roots: Mapping[str, Path]) -> tuple[str, tuple[str, ...]]:
    """(root name, start components) for ``<root>:``, ``<root>:<dir>`` or a directory path."""

    if looks_like_location(target, roots):
        name, _, relative = target.partition(":")
        relative = relative.strip("/")
        return name, check_relative(relative) if relative else ()
    raw = Path(target).expanduser()
    if ".." in raw.parts:
        raise LocationError("refusing a path with '..'; give the directory inside the root")
    # Lexical only (never resolved), like ``location_for_path``.
    absolute = PurePosixPath(os.path.abspath(raw))
    best: tuple[int, str, tuple[str, ...]] | None = None
    for name, directory in roots.items():
        for form in {directory, directory.resolve()}:
            root_parts = PurePosixPath(form).parts
            if absolute.parts[: len(root_parts)] == root_parts:
                rest = absolute.parts[len(root_parts) :]
                if best is None or len(root_parts) > best[0]:
                    best = (len(root_parts), name, rest)
    if best is None:
        raise LocationError("the directory is not inside any configured attachments root")
    _, name, rest = best
    return name, check_relative("/".join(rest)) if rest else ()


def scan_root(
    target: str,
    roots: Mapping[str, Path],
    *,
    registered: set[str] = frozenset(),  # type: ignore[assignment]
    max_bytes: int,
    hash_files: bool = False,
    max_entries: int | None = None,
) -> dict[str, Any]:
    """The inventory of ``target`` (see the module docstring). Read-only."""

    name, start = _start(target, roots)
    stats = WalkStats()
    files = 0
    total = 0
    private = 0
    over_limit = 0
    already = 0
    by_type: dict[str, dict[str, int]] = {}
    by_size: dict[int, list[Location]] = {}
    for item in walk_root(name, roots, start=start, stats=stats, max_entries=max_entries):
        files += 1
        total += item.size
        relative = item.location.relative
        kind = mimetypes.guess_type(item.location.name, strict=False)[0] or (
            "application/octet-stream"
        )
        bucket = by_type.setdefault(kind, {"files": 0, "bytes": 0})
        bucket["files"] += 1
        bucket["bytes"] += item.size
        if likely_private(relative):
            private += 1
        if item.size > max_bytes:
            over_limit += 1
        if str(item.location) in registered:
            already += 1
        if hash_files:
            by_size.setdefault(item.size, []).append(item.location)
    report: dict[str, Any] = {
        "root": name,
        "start": "/".join(start),
        "files": files,
        "bytes": total,
        "by_media_type": dict(sorted(by_type.items())),
        "likely_private": private,
        "over_embed_limit": over_limit,
        "already_registered": already,
        "refused_symlinks": stats.symlinks,
        "refused_names": stats.unsafe_names,
        "other_entries": stats.other,
        "unreadable_dirs": stats.unreadable_dirs,
        "truncated": stats.truncated,
        "writes": "none (dry run)",
    }
    if hash_files:
        digests: Counter[str] = Counter()
        for size, locations in by_size.items():
            if len(locations) < 2 or size == 0:
                continue
            for location in locations:
                try:
                    handle, _ = open_location(location, roots)
                except (OSError, LocationError):
                    continue
                with os.fdopen(handle, "rb") as stream:
                    digests[hash_stream(stream)[0]] += 1
        groups = [count for count in digests.values() if count > 1]
        report["duplicates"] = {"groups": len(groups), "files": sum(groups)}
    return report


__all__ = ["PRIVATE_HINTS", "likely_private", "scan_root"]
