from __future__ import annotations

import csv
from pathlib import Path

from memfw.related_annotation import calibrate


def test_calibrate_relatedness(tmp_path: Path) -> None:
    path = tmp_path / "pairs.csv"
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["pair_id", "score", "related"])
        writer.writerow(["p1", "2.0", "1"])
        writer.writerow(["p2", "1.5", "0"])
        writer.writerow(["p3", "0.9", "1"])
        writer.writerow(["p4", "0.1", ""])
    report = calibrate(path, thresholds=(0.5, 1.0, 2.0))
    assert report["labelled"] == 3
    assert report["best"]["threshold"] == 2.0
    assert report["best"]["precision"] == 1.0
