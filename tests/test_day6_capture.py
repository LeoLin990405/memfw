from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from day6_capture_scan import run_scan  # noqa: E402


def test_capture_scan_reports_packed_stage_and_all_in_separately(tmp_path: Path) -> None:
    report = run_scan(
        tmp_path / "scan",
        count_records=30,
        important_prevalence=0.10,
        thresholds=(0.2, 0.9),
    )
    assert report["labels_are_evaluation_only"] is True
    assert report["cell_count"] == 8
    assert report["rho_capture_values"] == [0.0, 0.3, 0.6, 0.9]
    assert report["h6_eligible_cells"] <= 8
    assert report["h6_passing_eligible_cells"] <= report["h6_eligible_cells"]
    for cell in report["cells"]:
        assert cell["packed_staging_allocated_bytes"] > 0
        assert cell["raw_staging_allocated_bytes"] >= 0
        assert cell["all_in_allocated_bytes"] == sum(cell["allocated_breakdown"].values())
        assert cell["h6_eligible"] == (cell["important_recall"] >= 0.70)
        assert cell["noise_retention"] is None
        assert cell["false_wake_count"] is None
        assert "nonimportant_retention" in cell
