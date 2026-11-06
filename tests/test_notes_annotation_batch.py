from __future__ import annotations

import csv
from pathlib import Path

from memfw.notes import (
    BatchQuery,
    NotesIndex,
    create_batch,
    import_batch,
    load_cases,
    write_batch,
)


def test_batch_packet_and_import(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    (root / "a.md").write_text("# A\n\ncorpus collocations\n", encoding="utf-8")
    (root / "b.md").write_text("# B\n\nNAS storage\n", encoding="utf-8")
    index_path = tmp_path / "index.sqlite"
    index = NotesIndex(index_path)
    index.index(root)
    rows = create_batch(index, [BatchQuery("q1", "corpus", "answerable")], candidates=3)
    assert rows and any(row.path == "a.md" for row in rows)
    csv_path = tmp_path / "batch.csv"
    write_batch(rows, csv_path=csv_path, annotator="tester")
    with csv_path.open(encoding="utf-8-sig", newline="") as handle:
        rows_in = list(csv.DictReader(handle))
    for row in rows_in:
        if row["candidate_path"] == "a.md":
            row["relevant"] = "1"
            row["answerable"] = "1"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=rows_in[0].keys())
        writer.writeheader()
        writer.writerows(rows_in)
    existing = tmp_path / "golden.yml"
    entries = import_batch(csv_path, db=index_path, out=existing, annotator="tester")
    assert entries
    assert entries[0].expected_paths == ("a.md",)
    cases = load_cases(existing)
    assert cases[0].query == "corpus"
    assert cases[0].answerable is True
