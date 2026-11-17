#!/usr/bin/env python3
"""T6: how far the builtin notes index and the QMD notes adapter agree (not a gate).

For each query it runs memfw's own notes index (``memfw.notes.NotesIndex``,
FTS only, built into a throwaway database) and the ``qmd-notes`` adapter over
the same Knowledge-Hub tree, keeps the top ``k`` of each, and reports the
overlap at note level (paths) and at section level (``kh:<path>#<heading>``):
Jaccard ``|A ∩ B| / |A ∪ B|`` and coverage ``|A ∩ B| / min(|A|, |B|)``. The
builtin side is held to the same visibility rule as the adapter (the note's
privacy level must be in the clearance), so neither side is credited for a
note the other may not return. QMD is either a live loopback endpoint or a
recorded contract fixture (replayed through the adapter's loopback fake).

    python scripts/qmd_notes_consistency.py --notes-root NOTES \\
        (--recording tests/fixtures/qmd_contract/qmd-2.5.3.json | --endpoint URL) \\
        [--collection knowledge-hub] [--legacy-privacy shared] [-k 10] \\
        --query Q [--query Q ...]

Prints one JSON report on stdout. The report holds paths, headings and
numbers only, never note text or the queries' answers. Exit status 0 unless
the inputs are unusable; a low overlap is information, not a failure.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
import tempfile
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

from memfw.notes.index import NoteParseError, NotesIndex, _split_frontmatter
from memfw.ports import RecallBudget
from memfw.ports.privacy import admits, metadata_level

SCHEMA = "memfw-t6-notes-consistency-v1"


def _adapter() -> Any:
    try:
        import memfw_adapter_qmd
    except ModuleNotFoundError:  # a workspace checkout without the member installed
        root = Path(__file__).resolve().parents[1]
        sys.path.insert(0, str(root / "packages" / "adapters" / "memfw-adapter-qmd" / "src"))
        import memfw_adapter_qmd
    return memfw_adapter_qmd


def _visible(root: Path, rel: str, clearance: frozenset[str], legacy: str) -> bool:
    try:
        text = (root / rel).read_text(encoding="utf-8", errors="replace")
        front, _body, _offset = _split_frontmatter(text)
    except (OSError, NoteParseError):
        return False
    return admits(clearance, metadata_level(front, legacy))


def _overlap(a: Sequence[str], b: Sequence[str]) -> dict[str, float | None]:
    left, right = set(a), set(b)
    union = left | right
    shared = left & right
    smaller = min(len(left), len(right))
    return {
        "jaccard": round(len(shared) / len(union), 4) if union else None,
        "coverage": round(len(shared) / smaller, 4) if smaller else None,
        "shared": len(shared),
    }


def _mean(values: Sequence[float | None]) -> float | None:
    present = [value for value in values if value is not None]
    return round(sum(present) / len(present), 4) if present else None


@contextlib.contextmanager
def _endpoint(args: argparse.Namespace) -> Iterator[str]:
    if args.endpoint:
        yield args.endpoint
        return
    _adapter()
    from memfw_adapter_qmd.fake import FakeQmd

    recording = json.loads(Path(args.recording).read_text(encoding="utf-8"))
    with FakeQmd.from_recording(recording) as fake:
        yield fake.url


def report(args: argparse.Namespace) -> dict[str, Any]:
    adapter = _adapter()
    from memfw_adapter_qmd.paths import parse_note_ref

    root = Path(args.notes_root).expanduser()
    clearance = frozenset(part.strip() for part in args.clearance.split(",") if part.strip())
    queries: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="memfw-t6-") as tmp:
        index = NotesIndex(Path(tmp) / "notes.sqlite3")
        index.index(root)
        with _endpoint(args) as url:
            provider = adapter.notes_factory(
                {
                    "endpoint": url,
                    "collection": args.collection,
                    "notes_root": str(root),
                    "legacy_privacy": args.legacy_privacy,
                    "clearance": args.clearance,
                }
            )
            availability = provider.available()
            for query in args.query:
                builtin = [
                    hit
                    for hit in index.search(query, limit=max(args.k * 4, 50), mode="fts")
                    if _visible(root, hit.path, clearance, args.legacy_privacy)
                ][: args.k]
                builtin_sections = [adapter.note_ref(hit.path, hit.heading) for hit in builtin]
                status = "ok"
                qmd_sections: list[str] = []
                try:
                    found = provider.search(
                        query,
                        limit=args.k,
                        budget=RecallBudget(timeout_seconds=args.timeout),
                        tenant="",
                        as_of=None,
                    )
                    qmd_sections = [candidate.ref for candidate in found]
                except Exception as exc:  # noqa: BLE001 - reported as a code, not raised
                    status = "unavailable:" + type(exc).__name__
                parsed = [parse_note_ref(ref) for ref in qmd_sections]
                qmd_paths = list(dict.fromkeys(item[0] for item in parsed if item is not None))
                builtin_paths = list(dict.fromkeys(hit.path for hit in builtin))
                queries.append(
                    {
                        "query_index": len(queries),
                        "qmd_status": status,
                        "builtin": builtin_sections,
                        "qmd": qmd_sections,
                        "notes": _overlap(builtin_paths, qmd_paths),
                        "sections": _overlap(builtin_sections, qmd_sections),
                    }
                )
    return {
        "schema": SCHEMA,
        "gate": False,
        "k": args.k,
        "qmd_source": "endpoint" if args.endpoint else "recording",
        "qmd_available": availability.available,
        "qmd_reason": availability.reason,
        "queries": queries,
        "mean": {
            "notes_jaccard": _mean([q["notes"]["jaccard"] for q in queries]),
            "notes_coverage": _mean([q["notes"]["coverage"] for q in queries]),
            "sections_jaccard": _mean([q["sections"]["jaccard"] for q in queries]),
            "sections_coverage": _mean([q["sections"]["coverage"] for q in queries]),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--notes-root", required=True)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--recording", help="contract fixture to replay (no network)")
    source.add_argument("--endpoint", help="a live loopback qmd mcp --http endpoint")
    parser.add_argument("--collection", default="knowledge-hub")
    parser.add_argument("--clearance", default="public,shared")
    parser.add_argument("--legacy-privacy", default="local-private")
    parser.add_argument("-k", type=int, default=10)
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--query", action="append", required=True)
    args = parser.parse_args(argv)
    if not Path(args.notes_root).is_dir():
        parser.error("--notes-root must be a directory")
    json.dump(report(args), sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
