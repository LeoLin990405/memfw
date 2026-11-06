"""Batch annotation packets for fast manual relevance labeling.

A packet is a CSV plus a human-readable Markdown sheet. The CSV is the source
of truth for import. Human labels are explicit: candidate rows are not turned
into relevant/irrelevant labels until a person fills the columns.
"""

from __future__ import annotations

import csv
import datetime as dt
import json
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .annotation import AnnotationEntry, GoldenAnnotator
from .index import NoteHit, NotesIndex

RELEVANT_TRUE = {"1", "true", "yes", "y", "x", "✓", "是", "相关"}
RELEVANT_FALSE = {"0", "false", "no", "n", "否", "不相关", ""}


@dataclass(frozen=True)
class BatchQuery:
    query_id: str
    query: str
    kind: str = "unspecified"


@dataclass(frozen=True)
class BatchCandidate:
    query_id: str
    query: str
    path: str
    title: str = ""
    heading: str = ""
    source: str = ""


def load_batch_queries(path: Path | str) -> list[BatchQuery]:
    source = Path(path)
    if source.suffix in {".yaml", ".yml"}:
        payload = yaml.safe_load(source.read_text(encoding="utf-8"))
        rows = payload.get("queries", payload) if isinstance(payload, dict) else payload
    elif source.suffix == ".jsonl":
        rows = [
            json.loads(line)
            for line in source.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    elif source.suffix == ".json":
        payload = json.loads(source.read_text(encoding="utf-8"))
        rows = payload.get("queries", payload) if isinstance(payload, dict) else payload
    else:
        raise ValueError("batch query file must be .yaml, .jsonl, or .json")
    if not isinstance(rows, list):
        raise ValueError("batch query file must contain a list")
    out: list[BatchQuery] = []
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict) or not str(row.get("query") or "").strip():
            continue
        out.append(
            BatchQuery(
                query_id=str(row.get("id") or row.get("query_id") or f"q{index:03d}"),
                query=str(row["query"]).strip(),
                kind=str(row.get("kind") or "unspecified"),
            )
        )
    return out


def create_batch(
    index: NotesIndex,
    queries: Iterable[BatchQuery],
    *,
    candidates: int = 8,
    mode: str = "fts",
) -> list[BatchCandidate]:
    rows: list[BatchCandidate] = []
    for query in queries:
        hits: list[NoteHit] = index.search(
            query.query,
            limit=candidates,
            mode=mode,
        )
        if not hits:
            rows.append(
                BatchCandidate(
                    query_id=query.query_id,
                    query=query.query,
                    path="",
                    source="no_candidates",
                )
            )
            continue
        for hit in hits:
            rows.append(
                BatchCandidate(
                    query_id=query.query_id,
                    query=query.query,
                    path=hit.path,
                    title=hit.title,
                    heading=hit.heading,
                    source=hit.source,
                )
            )
    return rows


def write_batch(
    rows: list[BatchCandidate],
    *,
    csv_path: Path | str,
    md_path: Path | str | None = None,
    meta_path: Path | str | None = None,
    annotator: str = "",
) -> dict[str, Any]:
    csv_out = Path(csv_path)
    csv_out.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "query_id",
        "query",
        "candidate_path",
        "title",
        "heading",
        "relevant",
        "answerable",
        "extra_paths",
        "notes",
        "annotator",
        "annotated_at",
    ]
    with csv_out.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "query_id": row.query_id,
                    "query": row.query,
                    "candidate_path": row.path,
                    "title": row.title,
                    "heading": row.heading,
                    "relevant": "",
                    "answerable": "",
                    "extra_paths": "",
                    "notes": "",
                    "annotator": annotator,
                    "annotated_at": dt.date.today().isoformat(),
                }
            )
    if md_path is not None:
        md = Path(md_path)
        md.parent.mkdir(parents=True, exist_ok=True)
        grouped: dict[str, list[BatchCandidate]] = defaultdict(list)
        for row in rows:
            grouped[row.query_id].append(row)
        lines = [
            "# Annotation batch",
            "",
            "Fill the CSV columns. `relevant=1` marks a candidate that directly answers the query.",
            "Use `answerable=0` for a query that the vault cannot answer.",
            "`extra_paths` can contain other vault paths separated by `;`.",
            "",
        ]
        for query_id, group in grouped.items():
            lines += [f"## {query_id}: {group[0].query}", ""]
            for candidate in group:
                if candidate.path:
                    lines.append(
                        f"- [ ] `{candidate.path}` — {candidate.title} / {candidate.heading}"
                    )
                else:
                    lines.append("- [ ] no candidate returned")
            lines.append("")
        md.write_text("\n".join(lines), encoding="utf-8")
    meta = {
        "created_at": dt.datetime.now().isoformat(timespec="seconds"),
        "rows": len(rows),
        "queries": len({row.query_id for row in rows}),
        "csv": str(csv_out),
        "markdown": str(md_path) if md_path else None,
    }
    if meta_path is not None:
        Path(meta_path).parent.mkdir(parents=True, exist_ok=True)
        Path(meta_path).write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return meta


def _truthy(value: str) -> bool:
    return value.strip().lower() in RELEVANT_TRUE


def _falsey(value: str) -> bool:
    return value.strip().lower() in RELEVANT_FALSE


def import_batch(
    csv_path: Path | str,
    *,
    db: Path | str,
    out: Path | str,
    annotator: str,
) -> list[AnnotationEntry]:
    grouped: dict[str, dict[str, Any]] = {}
    with Path(csv_path).open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            query_id = str(row.get("query_id") or "").strip()
            query = str(row.get("query") or "").strip()
            if not query_id or not query:
                continue
            item = grouped.setdefault(
                query_id,
                {
                    "query": query,
                    "kind": str(row.get("kind") or "").strip(),
                    "paths": [],
                    "answerable": None,
                    "notes": "",
                },
            )
            candidate = str(row.get("candidate_path") or "").strip()
            relevant = str(row.get("relevant") or "").strip()
            if candidate and _truthy(relevant):
                item["paths"].append(candidate)
            extra = str(row.get("extra_paths") or "").strip()
            if extra:
                item["paths"].extend(
                    part.strip() for part in extra.replace(",", ";").split(";") if part.strip()
                )
            answerable = str(row.get("answerable") or "").strip()
            if answerable:
                item["answerable"] = _truthy(answerable)
            notes = str(row.get("notes") or "").strip()
            if notes:
                item["notes"] = notes
    session = GoldenAnnotator(db, out, annotator=annotator)
    for query_id in sorted(grouped):
        item = grouped[query_id]
        paths = list(dict.fromkeys(item["paths"]))
        answerable = item["answerable"]
        if answerable is False or not paths:
            kind = "no_answer"
            paths = []
            notes = item["notes"] or "batch marked no answer"
        else:
            kind = (
                item["kind"]
                if item["kind"] in {"answerable", "chinese", "mixed", "title", "concept"}
                else "answerable"
            )
            notes = item["notes"]
        session.add(item["query"], paths, kind=kind, notes=notes)
    return session.entries


__all__ = [
    "BatchCandidate",
    "BatchQuery",
    "create_batch",
    "import_batch",
    "load_batch_queries",
    "write_batch",
]
