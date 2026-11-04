from __future__ import annotations

import json
import sys
from collections import Counter
from datetime import UTC, datetime
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from typing import Any, cast

import pytest
from filelock import FileLock

from memfw.agents import DeterministicJudge
from memfw.core.models import Layer, MemoryDocument, Tier

_SPEC = spec_from_file_location(
    "day3_synthetic_generator", Path(__file__).parents[1] / "scripts" / "gen_synthetic.py"
)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)
THEMES = cast(tuple[tuple[str, float], ...], _MODULE.THEMES)
generate_dataset = cast(Any, _MODULE.generate_dataset)
write_dataset = cast(Any, _MODULE.write_dataset)

_SCRIPTS = Path(__file__).parents[1] / "scripts"
sys.path.insert(0, str(_SCRIPTS))
from day3_experiment import run_experiment  # noqa: E402


def test_generator_is_fixed_seed_bilingual_and_spans_thirty_days() -> None:
    first = generate_dataset()
    second = generate_dataset()

    assert first == second
    assert len(first) == 1_000
    assert len({item.theme for item in first}) == 7
    assert first[-1].timestamp[:10] != first[0].timestamp[:10]
    assert all("\n" in item.text and item.duplicate_group for item in first[:400])
    assert all(item.duplicate_group is None for item in first[400:])


def test_generator_writes_parseable_capture_jsonl_with_end_boundary(tmp_path: Path) -> None:
    data_path = write_dataset(tmp_path / "synthetic.jsonl")
    stream_path = tmp_path / "synthetic.capture.jsonl"
    rows = [json.loads(line) for line in stream_path.read_text(encoding="utf-8").splitlines()]

    assert data_path.exists()
    assert len(rows) == 1_001
    assert rows[0]["source_id"] == "d3-0000"
    assert all(isinstance(row["timestamp"], str) for row in rows)
    assert rows[-1] == {"timestamp": rows[-1]["timestamp"], "type": "end"}


def test_importance_tracks_topic_strength_and_capture_follows_awake_windows() -> None:
    records = generate_dataset()
    counts: dict[str, Counter[str]] = {theme: Counter() for theme, _strength in THEMES}
    for item in records:
        counts[item.theme][item.importance] += 1

    high_theme, _ = THEMES[0]
    low_theme, _ = THEMES[-1]
    high_fraction = counts[high_theme]["important"] / sum(counts[high_theme].values())
    low_fraction = counts[low_theme]["important"] / sum(counts[low_theme].values())
    awake_intensity = [item.capture_intensity for item in records if item.awake]
    asleep_intensity = [item.capture_intensity for item in records if not item.awake]

    assert high_fraction > low_fraction
    assert min(awake_intensity) > max(asleep_intensity)


def test_mock_similarity_separates_near_pairs_from_nonduplicate_records() -> None:
    records = generate_dataset()
    judge = DeterministicJudge(duplicate_threshold=0.8)

    def document(index: int) -> MemoryDocument:
        row = records[index]
        timestamp = datetime(2026, 1, 1, tzinfo=UTC)
        return MemoryDocument(
            id=row.memory_id,
            title=row.theme,
            layer=Layer.CURRENT,
            tier=Tier.HOT,
            created_at=timestamp,
            updated_at=timestamp,
            body=row.text,
        )

    duplicate_proposals = judge.find_near_duplicates([document(0), document(1)])
    assert duplicate_proposals
    assert duplicate_proposals[0].confidence >= judge.duplicate_threshold

    nonduplicate_scores = []
    for offset in range(20):
        left = document(600 + offset * 2)
        right = document(601 + offset * 2)
        left_grams = judge._ngrams(left, judge.ngram_size)
        right_grams = judge._ngrams(right, judge.ngram_size)
        nonduplicate_scores.append(len(left_grams & right_grams) / len(left_grams | right_grams))
    assert sum(score < judge.duplicate_threshold for score in nonduplicate_scores) >= 16


def test_experiment_root_lock_rejects_concurrent_run_before_writes(tmp_path: Path) -> None:
    work_root = tmp_path / "exclusive-run"
    work_root.mkdir()
    with FileLock(str(work_root / ".experiment.lock")):
        with pytest.raises(RuntimeError, match="already in use"):
            run_experiment(records=generate_dataset(count=1), work_root=work_root)

    assert not (work_root / "base").exists()


def test_small_policy_and_capture_experiment_is_reproducible(tmp_path: Path) -> None:
    records = generate_dataset(count=20)
    work_root = tmp_path / "repeatability"
    first = run_experiment(records=records, work_root=work_root)
    second = run_experiment(records=records, work_root=work_root)

    canonical_first = json.dumps(first, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    canonical_second = json.dumps(second, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    assert canonical_first == canonical_second
