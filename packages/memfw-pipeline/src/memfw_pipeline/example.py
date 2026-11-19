"""A small example stage: copy a flat directory of ``*.md`` files.

It is the reference the tests drive and a template for real stages; it is not a
production stage. Options:

- ``source`` (required): an absolute directory of ``*.md`` files (not recursive;
  symlinks and other files are skipped and counted as ``skipped``);
- ``revision_file`` (optional): a file whose first line is the source revision
  (for example a commit id); without it the revision is a digest prefix;
- ``marker`` (optional): an absolute file the idempotent post-publish step
  ``write_marker`` rewrites with the published item count (a stand-in for an
  adapter sync).

The artifact is ``<target>/items/*.md`` plus ``<target>/manifest.json``
(``{"items": N}``).
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .contract import (
    BuildResult,
    DirectoryStage,
    Freshness,
    InputUnavailable,
    PostPublishStep,
    SetupError,
    StageContext,
    StepResult,
    digest_of,
    revision_ok,
)


def _abs_option(options: Mapping[str, Any], key: str, *, required: bool) -> Path | None:
    value = options.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, str) or not os.path.isabs(value):
        raise SetupError("option_invalid")
    return Path(value)


class CopyStage(DirectoryStage):
    name = "example-copy"
    count_glob = "items/*.md"

    def __init__(self, options: Mapping[str, Any]) -> None:
        source = _abs_option(options, "source", required=True)
        assert source is not None
        self.source = source
        self.revision_file = _abs_option(options, "revision_file", required=False)
        self.marker = _abs_option(options, "marker", required=False)

    def _entries(self) -> tuple[list[Path], int]:
        try:
            names = sorted(entry.name for entry in os.scandir(self.source))
        except OSError:
            raise InputUnavailable("source_unreadable") from None
        files: list[Path] = []
        skipped = 0
        for name in names:
            path = self.source / name
            if name.endswith(".md") and path.is_file() and not path.is_symlink():
                files.append(path)
            else:
                skipped += 1
        return files, skipped

    def inputs_digest(self, ctx: StageContext) -> str:
        files, _ = self._entries()
        parts: list[bytes | str] = [self.name, "1"]
        for path in files:
            try:
                parts += [path.name, path.read_bytes()]
            except OSError:
                raise InputUnavailable("source_unreadable") from None
        return digest_of(*parts)

    def freshness(self, ctx: StageContext) -> Freshness:
        if self.revision_file is not None:
            try:
                revision = self.revision_file.read_text(encoding="utf-8").split("\n", 1)[0]
                made = datetime.fromtimestamp(self.revision_file.stat().st_mtime, UTC)
            except (OSError, UnicodeDecodeError):
                raise InputUnavailable("revision_unreadable") from None
            if not revision_ok(revision):
                raise InputUnavailable("revision_invalid")
            return Freshness(revision, made)
        files, _ = self._entries()
        newest = max((p.stat().st_mtime for p in files), default=None)
        as_of = datetime.fromtimestamp(newest, UTC) if newest is not None else None
        return Freshness(self.inputs_digest(ctx)[:16], as_of)

    def build(self, ctx: StageContext, workdir: Path) -> BuildResult:
        files, skipped = self._entries()
        items = workdir / "items"
        items.mkdir(mode=0o700)
        for path in files:
            data = path.read_bytes()
            fd = os.open(items / path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
        manifest = workdir / "manifest.json"
        manifest.write_text(json.dumps({"items": len(files)}) + "\n", encoding="utf-8")
        return BuildResult(
            counts={"items": len(files), "skipped": skipped},
            artifacts=("items", "manifest.json"),
        )

    def post_publish(self, ctx: StageContext) -> Sequence[PostPublishStep]:
        if self.marker is None:
            return ()
        marker = self.marker

        def write_marker(c: StageContext) -> StepResult:
            count = self.live_count(c) or 0
            tmp = marker.with_name(f".{marker.name}.{os.getpid()}-{time.monotonic_ns()}.tmp")
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
            with os.fdopen(os.open(tmp, flags, 0o600), "w", encoding="utf-8") as handle:
                handle.write(json.dumps({"items": count}) + "\n")
            os.replace(tmp, marker)
            return StepResult({"items": count})

        return (PostPublishStep("write_marker", write_marker),)


def factory(options: Mapping[str, Any]) -> CopyStage:
    return CopyStage(options)


__all__ = ["CopyStage", "factory"]
