"""Two reference ``Ingestor`` implementations. Stdlib only.

- ``JsonIngestor`` (``json``): stages through the reference
  ``stage_candidates`` (one JSON candidate per item under
  ``_candidates/json/``). ``items_from_json`` reads a JSON array or JSON
  Lines of ``{"title", "body", "external_ref"?, "metadata"?}`` records.
- ``MarkdownIngestor`` (``markdown``): writes each candidate as a Markdown
  file with a small JSON header under ``_candidates/markdown/``, readable in
  any editor. ``items_from_markdown`` turns ``.md`` files into items (the
  first ``# heading`` is the title, else the file name).

Both keep the ingest contract: a proposal id and a source are required,
nothing outside ``<vault>/_candidates/`` is written, and staging the same
item twice returns the same candidate id and writes nothing new.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

from ..staging import (
    StagingError,
    candidate_directory,
    candidate_id,
    check_ingest_args,
    content_sha256,
    create_exclusive,
    stage_candidates,
)
from ..types import Availability, ExtraValue, IngestItem, StageResult, check_name

#: Largest file ``items_from_json`` / ``items_from_markdown`` read.
MAX_INPUT_BYTES = 8 * 1024 * 1024

_HEADER_OPEN = "<!-- memfw-candidate\n"
_HEADER_CLOSE = "\n-->\n"


def _read_text(path: Path) -> str:
    source = Path(path).expanduser()
    if source.is_symlink() or not source.is_file():
        raise ValueError(f"not a regular file: {source.name}")
    if source.stat().st_size > MAX_INPUT_BYTES:
        raise ValueError(f"file is larger than {MAX_INPUT_BYTES} bytes: {source.name}")
    return source.read_text(encoding="utf-8")


def _item(record: object) -> IngestItem:
    if not isinstance(record, dict):
        raise ValueError("each record must be a JSON object")
    metadata = record.get("metadata", {})
    if not isinstance(metadata, dict) or not all(
        isinstance(key, str) and (value is None or isinstance(value, str | int | float | bool))
        for key, value in metadata.items()
    ):
        raise ValueError("metadata must be an object of scalars")
    external_ref = record.get("external_ref", "")
    if not isinstance(external_ref, str):
        raise ValueError("external_ref must be a string")
    title, body = record.get("title"), record.get("body", "")
    if not isinstance(title, str) or not isinstance(body, str):
        raise ValueError("title and body must be strings")
    typed: dict[str, ExtraValue] = dict(metadata)
    return IngestItem(title=title, body=body, external_ref=external_ref, metadata=typed)


def items_from_json(text: str) -> list[IngestItem]:
    """Items from a JSON array, or from JSON Lines (one object per line)."""

    stripped = text.strip()
    if not stripped:
        return []
    if stripped.startswith("["):
        records = json.loads(stripped)
        if not isinstance(records, list):
            raise ValueError("expected a JSON array")
        return [_item(record) for record in records]
    return [_item(json.loads(line)) for line in stripped.splitlines() if line.strip()]


def items_from_json_file(path: Path) -> list[IngestItem]:
    return items_from_json(_read_text(path))


def items_from_markdown(paths: Sequence[Path]) -> list[IngestItem]:
    """One item per ``.md`` file; ``external_ref`` is the file name."""

    items: list[IngestItem] = []
    for path in paths:
        text = _read_text(path)
        lines = text.splitlines()
        title = Path(path).stem
        body_lines = lines
        for index, line in enumerate(lines):
            if line.strip():
                if line.startswith("# ") and line[2:].strip():
                    title = line[2:].strip()
                    body_lines = lines[index + 1 :]
                break
        items.append(
            IngestItem(
                title=title,
                body="\n".join(body_lines).strip("\n"),
                external_ref=Path(path).name,
                metadata={"format": "markdown"},
            )
        )
    return items


class JsonIngestor:
    """Stages through ``memfw.ports.staging.stage_candidates``."""

    def __init__(self, *, name: str = "json") -> None:
        self.name = check_name(name, what="ingestor")

    def available(self) -> Availability:
        return Availability.ok()

    def stage(
        self,
        vault: Path,
        items: Sequence[IngestItem],
        *,
        proposal_id: str,
        source: str,
    ) -> StageResult:
        return stage_candidates(vault, self.name, items, proposal_id=proposal_id, source=source)


class MarkdownIngestor:
    """Writes ``_candidates/<name>/<candidate id>.md``: a JSON header, then the body.

    Candidate ids are the reference ``candidate_id`` (who, where from, ref and
    content), so the same item is the same candidate in either format.
    """

    def __init__(self, *, name: str = "markdown") -> None:
        self.name = check_name(name, what="ingestor")

    def available(self) -> Availability:
        return Availability.ok()

    def stage(
        self,
        vault: Path,
        items: Sequence[IngestItem],
        *,
        proposal_id: str,
        source: str,
    ) -> StageResult:
        check_ingest_args(proposal_id, source)
        prepared: list[tuple[str, str, bytes]] = []
        seen: set[str] = set()
        duplicates: list[str] = []
        for item in items:
            if not isinstance(item, IngestItem):
                raise StagingError("items must be IngestItem values")
            identifier = candidate_id(self.name, source, item)
            if identifier in seen:
                duplicates.append(identifier)
                continue
            seen.add(identifier)
            digest = content_sha256(item)
            try:
                header = json.dumps(
                    {
                        "candidate_id": identifier,
                        "content_sha256": digest,
                        "ingestor": self.name,
                        "proposal_id": proposal_id,
                        "source": source,
                        "external_ref": item.external_ref,
                        "title": item.title,
                        "metadata": dict(sorted(item.metadata.items())),
                        "status": "staged",
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    indent=2,
                    allow_nan=False,
                )
            except (TypeError, ValueError) as exc:
                raise StagingError("item is not serialisable") from exc
            if "-->" in header:
                raise StagingError("item header cannot contain '-->'")
            text = f"{_HEADER_OPEN}{header}{_HEADER_CLOSE}\n# {item.title}\n\n{item.body}\n"
            prepared.append((identifier, digest, text.encode("utf-8")))
        directory = candidate_directory(vault, self.name)
        staged: list[str] = []
        for identifier, digest, data in prepared:
            if create_exclusive(directory, identifier, ".md", data):
                staged.append(identifier)
            elif read_header(directory / f"{identifier}.md").get("content_sha256") == digest:
                duplicates.append(identifier)
            else:
                raise StagingError("a different candidate already uses this id")
        return StageResult(staged=tuple(staged), duplicates=tuple(duplicates))


def read_header(path: Path) -> dict[str, object]:
    """The JSON header of a Markdown candidate; ``{}`` when it has none."""

    if path.is_symlink() or not path.is_file():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    if not text.startswith(_HEADER_OPEN):
        return {}
    end = text.find(_HEADER_CLOSE, len(_HEADER_OPEN))
    if end < 0:
        return {}
    try:
        header = json.loads(text[len(_HEADER_OPEN) : end])
    except ValueError:
        return {}
    return header if isinstance(header, dict) else {}


__all__ = [
    "MAX_INPUT_BYTES",
    "JsonIngestor",
    "MarkdownIngestor",
    "items_from_json",
    "items_from_json_file",
    "items_from_markdown",
    "read_header",
]
