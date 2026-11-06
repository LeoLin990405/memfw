"""Batch annotation helpers for relatedness edges."""

from __future__ import annotations

import csv
import random
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class RelatedPair:
    pair_id: str
    source: str
    id_a: str
    title_a: str
    path_a: str
    id_b: str
    title_b: str
    path_b: str
    method: str
    score: float


def generate_pairs(
    related_db: Path | str,
    *,
    source: str = "notes",
    count: int = 100,
    positive_ratio: float = 0.5,
    seed: int = 17,
) -> list[RelatedPair]:
    rng = random.Random(seed)
    with sqlite3.connect(related_db) as connection:
        connection.row_factory = sqlite3.Row
        items = {
            (str(row["source"]), str(row["id"])): row
            for row in connection.execute("SELECT * FROM related_items")
        }
        edge_rows = connection.execute(
            "SELECT * FROM related_edges WHERE src_source=? ORDER BY score DESC, src_id, dst_id",
            (source,),
        ).fetchall()
    candidates: list[RelatedPair] = []
    seen: set[tuple[str, str, str, str]] = set()
    positives = max(1, min(count, round(count * positive_ratio)))
    for row in edge_rows:
        key = (
            str(row["src_source"]),
            str(row["src_id"]),
            str(row["dst_source"]),
            str(row["dst_id"]),
        )
        if key in seen:
            continue
        seen.add(key)
        left = items.get((str(row["src_source"]), str(row["src_id"])))
        right = items.get((str(row["dst_source"]), str(row["dst_id"])))
        if left is None or right is None:
            continue
        candidates.append(
            RelatedPair(
                f"pair-{len(candidates) + 1:03d}",
                source,
                str(left["id"]),
                str(left["title"]),
                str(left["path"]),
                str(right["id"]),
                str(right["title"]),
                str(right["path"]),
                str(row["method"]),
                float(row["score"]),
            )
        )
        if len(candidates) >= positives:
            break
    keys = list(items)
    while len(candidates) < count and len(keys) > 1:
        left_key = rng.choice(keys)
        right_key = rng.choice(keys)
        if left_key == right_key:
            continue
        left, right = items[left_key], items[right_key]
        if (left["source"], left["id"], right["source"], right["id"]) in seen:
            continue
        seen.add((left["source"], left["id"], right["source"], right["id"]))
        candidates.append(
            RelatedPair(
                f"pair-{len(candidates) + 1:03d}",
                source,
                str(left["id"]),
                str(left["title"]),
                str(left["path"]),
                str(right["id"]),
                str(right["title"]),
                str(right["path"]),
                "random",
                0.0,
            )
        )
    return candidates


def write_pairs_csv(pairs: list[RelatedPair], path: Path | str) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "pair_id",
                "source",
                "id_a",
                "title_a",
                "path_a",
                "id_b",
                "title_b",
                "path_b",
                "method",
                "score",
                "related",
                "notes",
                "annotator",
                "annotated_at",
            ]
        )
        for pair in pairs:
            writer.writerow(
                [
                    pair.pair_id,
                    pair.source,
                    pair.id_a,
                    pair.title_a,
                    pair.path_a,
                    pair.id_b,
                    pair.title_b,
                    pair.path_b,
                    pair.method,
                    pair.score,
                    "",
                    "",
                    "",
                    "",
                ]
            )


def calibrate(
    csv_path: Path | str, *, thresholds: tuple[float, ...] = (0.2, 0.4, 0.6, 0.8, 1.0, 1.5, 2.0)
) -> dict[str, Any]:
    with Path(csv_path).open(encoding="utf-8-sig", newline="") as handle:
        rows = [row for row in csv.DictReader(handle) if row.get("related") in {"0", "1"}]
    results = []
    for threshold in thresholds:
        predicted = [row for row in rows if float(row["score"]) >= threshold]
        tp = sum(row["related"] == "1" for row in predicted)
        fp = len(predicted) - tp
        positives = sum(row["related"] == "1" for row in rows)
        fn = positives - tp
        precision = tp / len(predicted) if predicted else 0.0
        recall = tp / positives if positives else 0.0
        results.append(
            {
                "threshold": threshold,
                "precision": precision,
                "recall": recall,
                "tp": tp,
                "fp": fp,
                "fn": fn,
            }
        )
    best = max(results, key=lambda row: (row["precision"], row["recall"])) if results else None
    return {"labelled": len(rows), "best": best, "thresholds": results}


__all__ = ["RelatedPair", "calibrate", "generate_pairs", "write_pairs_csv"]
