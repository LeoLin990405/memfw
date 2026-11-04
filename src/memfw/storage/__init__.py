"""Managed tar.zst cold archive helper with per-memory hard-delete pruning."""

from __future__ import annotations

import io
import os
import tarfile
from pathlib import Path
from typing import TYPE_CHECKING

import zstandard

from ..core.models import Tier

if TYPE_CHECKING:
    from ..core.context import RepositoryHost


def create_cold_archive(
    repository: RepositoryHost, archive_id: str, *, memory_ids: set[str] | None = None
) -> Path:
    """Create a deterministic archive of current cold-tier Markdown documents."""
    if not archive_id.replace("-", "").replace("_", "").isalnum():
        raise ValueError("archive_id may contain only letters, digits, '_' and '-'")
    repository.archives_dir.mkdir(parents=True, exist_ok=True)
    output = repository.archives_dir / f"{archive_id}.tar.zst"
    if output.exists():
        raise FileExistsError(f"Archive already exists: {archive_id}")
    cold_documents = [
        document
        for document in repository.list()
        if document.tier == Tier.COLD
        and not document.is_stub
        and (memory_ids is None or document.id in memory_ids)
    ]
    if not cold_documents:
        raise ValueError("No current cold-tier memories to archive")
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        for document in cold_documents:
            path = repository.memories_dir / f"{document.id}.md"
            data = path.read_bytes()
            info = tarfile.TarInfo(f"memories/{document.id}.md")
            info.size = len(data)
            info.mtime = 0
            tar.addfile(info, io.BytesIO(data))
    compressor = zstandard.ZstdCompressor(level=8, threads=0)
    temporary = output.with_name(output.name + ".tmp")
    temporary.write_bytes(compressor.compress(raw.getvalue()))
    os.replace(temporary, output)
    return output


def remove_memory_from_archives(repository: RepositoryHost, memory_id: str) -> None:
    """Rewrite managed archives without one memory, preserving other archived items."""
    for archive in sorted(repository.archives_dir.glob("*.tar.zst")):
        raw = zstandard.ZstdDecompressor().decompress(archive.read_bytes())
        output = io.BytesIO()
        count = 0
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as source:
            with tarfile.open(fileobj=output, mode="w") as destination:
                for member in source.getmembers():
                    if not member.isfile() or member.name == f"memories/{memory_id}.md":
                        continue
                    stream = source.extractfile(member)
                    if stream is None:
                        continue
                    data = stream.read()
                    clean = tarfile.TarInfo(member.name)
                    clean.size = len(data)
                    clean.mtime = 0
                    destination.addfile(clean, io.BytesIO(data))
                    count += 1
        if count == 0:
            archive.unlink()
        else:
            repository._atomic_write(
                archive,
                zstandard.ZstdCompressor(level=8, threads=0).compress(output.getvalue()),
            )
