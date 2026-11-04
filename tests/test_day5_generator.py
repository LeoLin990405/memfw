from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from gen_day5 import _corr, generate_dataset


def test_rho_scan_changes_only_intensity_and_hits_requested_binary_correlation() -> None:
    generated = {
        rho: generate_dataset(count=200, seed=20261018, rho=rho) for rho in (0, 0.3, 0.6, 0.9)
    }
    base_records, labels, base_accesses = generated[0.0]
    truth = [float(row.importance == "important") for row in labels]
    for rho, (records, other_labels, accesses) in generated.items():
        assert other_labels == labels
        assert accesses == base_accesses
        assert [
            (row.memory_id, row.timestamp, row.text, row.access_count, row.last_accessed_at)
            for row in records
        ] == [
            (row.memory_id, row.timestamp, row.text, row.access_count, row.last_accessed_at)
            for row in base_records
        ]
        realized = _corr([row.capture_intensity for row in records], truth)
        assert abs(realized - rho) < 1e-8
        assert all(0.05 <= row.capture_intensity <= 0.95 for row in records)


def test_access_history_is_heavy_tailed_and_has_varying_recency() -> None:
    records, _labels, events = generate_dataset(count=200, seed=20261018, rho=0.6)
    counts = sorted(row.access_count for row in records)
    assert len(events) == sum(counts)
    assert counts[-1] >= 4 * (sum(counts) / len(counts))
    assert len({row.last_accessed_at for row in records}) > 190
    assert len({row.timestamp for row in records}) == len(records)
