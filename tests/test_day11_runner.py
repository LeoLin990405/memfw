from __future__ import annotations

import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import day11_baseline_comparison as day11
import day11_runner as runner


class FakeEmbedder:
    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        vectors = []
        for text in texts:
            words = text.lower().split()
            vectors.append(
                [float(sum(w.startswith(c) for w in words)) + 0.01 for c in "abcdefghij"]
            )
        return vectors


class FakeAnswerer:
    def answer(self, question: str, contexts: Sequence[str]) -> str:
        return contexts[0].split()[-1] if contexts else "unknown"


class FakeMem0:
    def __init__(self, root: Path, fail_on_add: bool = False) -> None:
        self.root = root
        self.rows: list[dict[str, Any]] = []
        self.fail_on_add = fail_on_add

    def add(self, content: str, *, dia_id: str) -> list[dict[str, Any]]:
        if self.fail_on_add:
            raise RuntimeError("extraction failed")
        text = "User mentioned " + content.split(": ", 1)[1] + " in 2026"
        row = {
            "id": f"m{len(self.rows)}",
            "memory": text,
            "metadata": {"dia_id": dia_id},
            "created_at": f"{len(self.rows):05d}",
            "attributed_to": "user",
        }
        self.rows.append(row)
        return [{"event": "ADD", **row}]

    def search(self, query: str, *, top_k: int) -> list[dict[str, Any]]:
        terms = set(query.lower().split())
        ranked = sorted(self.rows, key=lambda r: -len(terms & set(r["memory"].lower().split())))
        return ranked[:top_k]

    def get_all(self) -> list[dict[str, Any]]:
        return list(self.rows)

    def delete(self, memory_id: str) -> None:
        self.rows = [row for row in self.rows if row["id"] != memory_id]

    def storage_bytes(self) -> int:
        return sum(len(row["memory"]) for row in self.rows)


def _data(tmp_path: Path) -> Path:
    turns = [
        {"dia_id": f"D1:{i}", "speaker": "Ann", "text": f"I adopted animal number {i} today."}
        for i in range(1, 13)
    ]
    sample = {
        "sample_id": "s0",
        "conversation": {"session_1_date_time": "1:00 pm on 8 May, 2023", "session_1": turns},
        "qa": [
            {
                "question": f"Which animal number {c} did Ann adopt?",
                "answer": str(c),
                "evidence": [f"D1:{c}"],
                "category": c,
            }
            for c in range(1, 6)
        ],
    }
    path = tmp_path / "locomo.json"
    path.write_text(json.dumps([sample]), encoding="utf-8")
    return path


def _execute(tmp_path: Path, **overrides: Any) -> dict[str, Any]:
    data = _data(tmp_path)
    manifest = day11.build_manifest(data)
    kwargs: dict[str, Any] = {
        "data_path": data,
        "manifest": manifest,
        "output_dir": tmp_path / "out",
        "base_url": "http://localhost:11434",
        "answer_model": "fake-answer",
        "embedding_model": "fake-embed",
        "top_k": 3,
        "context_token_budget": 64,
        "command": "scripts/day11_baseline_comparison.py --run",
        "work_dir": tmp_path / "work",
        "log": lambda _m: None,
        "mem0_factory": lambda root: FakeMem0(root),
        "embedder": FakeEmbedder(),
        "answerer": FakeAnswerer(),
        "digests": {"fake-answer": "sha256:a", "fake-embed": "sha256:e"},
    }
    kwargs.update(overrides)
    return runner.execute(**kwargs)


def test_run_with_fake_clients_writes_complete_schema(tmp_path: Path) -> None:
    report = _execute(tmp_path)
    assert report["status"] == "completed", report["failure"]
    assert report["schema_version"] == "day11-baseline-report-v2"
    settings = report["settings"]
    aligned = {key: settings[key] for key in day11.ALIGNMENT_FIELDS}
    assert day11.validate_alignment(aligned, aligned)["validated"] is True
    results = report["results"]
    for name in runner.SYSTEMS:
        for key in ("retention_100pct", "retention_5pct"):
            cell = results["conditions"][name][key]
            assert 0.0 <= cell["evidence_recall"] <= 1.0
            assert cell["write_latency"]["p50_seconds"] is not None
            assert cell["search_latency"]["p95_seconds"] is not None
            assert cell["storage_bytes_on_disk"] >= 0
        assert results["conditions"][name]["retention_100pct"]["storage_bytes_on_disk"] > 0
    full = results["conditions"]["mem0"]["retention_100pct"]["retained_memory_count"]
    small = results["conditions"]["mem0"]["retention_5pct"]["retained_memory_count"]
    assert small < full
    for family in ("recall", "answer_score"):
        for value in results["statistics"][family].values():
            assert "p_holm" in value and isinstance(value["significant_at_0_05"], bool)
    audit = results["provenance_audit"]["systems"]
    assert audit["memfw"]["label_counts"] == {"verbatim_source_text": audit["memfw"]["sample_size"]}
    assert audit["mem0"]["flagged_count"] == audit["mem0"]["sample_size"]
    written = json.loads((tmp_path / "out/day11_baseline_comparison.json").read_text())
    assert written["status"] == "completed"
    markdown = (tmp_path / "out/day11_baseline_comparison.md").read_text()
    assert "Implications for the workshop paper" in markdown
    assert "Limitations" in markdown


def test_mem0_add_errors_are_recorded_not_hidden(tmp_path: Path) -> None:
    report = _execute(tmp_path, mem0_factory=lambda root: FakeMem0(root, fail_on_add=True))
    assert report["status"] == "completed"
    assert len(report["results"]["mem0_add_errors"]) == 12
    assert report["results"]["conditions"]["mem0"]["retention_100pct"]["evidence_recall"] == 0.0


def test_failure_is_recorded_honestly_without_metrics(tmp_path: Path) -> None:
    def broken(_root: Path) -> runner.Mem0Like:
        raise ConnectionRefusedError("ollama unreachable")

    report = _execute(tmp_path, mem0_factory=broken)
    assert report["status"] == "failed"
    assert report["results"] is None
    assert report["failure"]["stage"] == "init_systems"
    assert report["failure"]["error_type"] == "ConnectionRefusedError"
    assert "no simulated numbers" in (tmp_path / "out/day11_baseline_comparison.md").read_text()


def test_non_loopback_endpoint_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        runner.require_loopback("https://api.openai.com")
    report = _execute(tmp_path, base_url="http://example.com:11434")
    assert report["status"] == "failed"
    assert report["failure"]["stage"] == "preflight"


def test_mem0_config_is_fully_local(tmp_path: Path) -> None:
    config = runner.mem0_config(tmp_path, "http://127.0.0.1:11434", "a", "e")
    assert config["llm"]["provider"] == "ollama"
    assert config["embedder"]["provider"] == "ollama"
    assert config["vector_store"]["provider"] == "qdrant"
    assert config["vector_store"]["config"]["path"].startswith(str(tmp_path))


def test_scoring_helpers() -> None:
    assert runner.token_f1("the red car", "red car") == 1.0
    assert runner.answer_score("unknown", "x", 5) == 1.0
    assert runner.answer_score("blue", None, 5) == 0.0
    assert runner.holm({"a": 0.01, "b": 0.04}) == {"a": 0.02, "b": 0.04}
    assert runner.percentile([1.0, 2.0, 3.0], 0.5) == 2.0
    items = [{"memory": "one two three"}, {"memory": "four"}]
    assert runner.fit_context(items, 1) == [{"memory": "four"}]


def test_audit_rules() -> None:
    verbatim = runner.audit_memory("adopted a dog", "I adopted a dog today", "Ann", {}, None)
    assert verbatim["label"] == "verbatim_source_text"
    flagged = runner.audit_memory("Ann adopted a dog in 2026", "I adopted a dog", "Ann", {}, "user")
    assert flagged["flagged"] is True and flagged["novel_numbers"] is True
    labelled = runner.audit_memory(
        "Likes dogs", "I adopted a dog", "Ann", {"derived": True}, "user"
    )
    assert labelled["label"] == "derived_labelled"
    assert runner.audit_memory("x", None, None, {}, None)["label"] == "missing_source_link"
