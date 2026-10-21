"""Synthetic-only regression for the fixed-seed local determinism probe."""

from __future__ import annotations

import hashlib
import importlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from memfw.core.models import EventKind
from memfw.core.repository import Repository

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
pilot = importlib.import_module("pilot")
mlx_local_transport = importlib.import_module("mlx_local_transport")

_PRIVATE_CANARY = "PRIVATE_BODY_CANARY_6382"
_QUERY_CANARY = "PRIVATE_QUERY_CANARY_194e"


def test_determinism_probe_uses_one_transport_and_records_only_audit_metadata(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = Repository(tmp_path / "private-vault")
    repo.init()
    repo.add(
        "Synthetic title",
        f"Synthetic private body {_PRIVATE_CANARY}; private query context {_QUERY_CANARY}.",
        memory_id="synthetic-memory",
    )
    model_path = tmp_path / "local-model"
    model_path.mkdir()
    monkeypatch.setattr(pilot, "_private_repo", lambda: repo)
    monkeypatch.setattr(pilot, "_load_model_path", lambda _path: model_path)
    monkeypatch.setattr(pilot, "_module_available", lambda _name: True)

    calls: list[dict[str, Any]] = []
    transports: list[object] = []

    class FakeTransport:
        def __init__(self, path: Path) -> None:
            assert path == model_path
            transports.append(self)

        def infer(
            self,
            *,
            model_name: str,
            quantization: str,
            prompt_version: str,
            prompt: str,
            input_json: str,
            temperature: float = 0.0,
            seed: int = 0,
        ) -> Any:
            calls.append(
                {
                    "model_name": model_name,
                    "quantization": quantization,
                    "prompt_version": prompt_version,
                    "prompt": prompt,
                    "input_json": input_json,
                    "temperature": temperature,
                    "seed": seed,
                }
            )
            return mlx_local_transport.LocalInferenceResult(
                text='{"score":0.75,"reason":"synthetic stable"}',
                elapsed_seconds=0.01,
                input_tokens=12,
                output_tokens=5,
                peak_memory_bytes=1234,
                cold_load_seconds=0.02,
                cold_peak_memory_bytes=2345,
                total_seconds=0.03,
                input_truncated_bytes=0,
                raw_input_hash=hashlib.sha256(input_json.encode("utf-8")).hexdigest(),
                submitted_input_hash=hashlib.sha256(input_json.encode("utf-8")).hexdigest(),
            )

    monkeypatch.setattr(mlx_local_transport, "MLXLocalTransport", FakeTransport)
    before_events = repo.events()
    before_documents = [item.model_dump(mode="json") for item in repo.list()]
    before_proposals = repo.list_proposals(status=None)

    result = pilot.determinism_probe(model_path, "synthetic-memory")

    assert len(transports) == 1
    assert len(calls) == 3
    assert all(call["temperature"] == 0.0 and call["seed"] == 0 for call in calls)
    assert _PRIVATE_CANARY in calls[0]["input_json"]
    assert _QUERY_CANARY in calls[0]["input_json"]
    assert result["repeat_count"] == 3
    assert result["valid_repeat_count"] == 3
    serialized_result = json.dumps(result, ensure_ascii=False)
    assert _PRIVATE_CANARY not in serialized_result
    assert _QUERY_CANARY not in serialized_result

    after_events = repo.events()
    added = after_events[len(before_events) :]
    assert len(added) == 3
    assert all(event.kind == EventKind.AGENT_PROPOSAL for event in added)
    for event in added:
        details = event.details
        assert details["operation"] == "local_model_call"
        assert details["kind"] == "inference_audit"
        metadata = json.loads(details["reason"])
        assert set(metadata) == {
            "model",
            "quantization",
            "prompt_version",
            "task",
            "status",
            "input_tokens",
            "output_tokens",
            "peak_memory_bytes",
            "cold_load_seconds",
            "cold_peak_memory_bytes",
            "total_seconds",
            "input_truncated_bytes",
            "raw_input_hash",
            "submitted_input_hash",
            "output_hash",
        }
        assert metadata["input_truncated_bytes"] == 0
        assert (
            metadata["raw_input_hash"]
            == hashlib.sha256(calls[0]["input_json"].encode("utf-8")).hexdigest()
        )
        assert metadata["submitted_input_hash"] == metadata["raw_input_hash"]
        assert _PRIVATE_CANARY not in json.dumps(details, ensure_ascii=False)
        assert _QUERY_CANARY not in json.dumps(details, ensure_ascii=False)

    assert [item.model_dump(mode="json") for item in repo.list()] == before_documents
    assert repo.list_proposals(status=None) == before_proposals
