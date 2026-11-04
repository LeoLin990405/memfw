from __future__ import annotations

import hashlib
import importlib
import json
import os
import socket
import sys
import types
from pathlib import Path
from typing import Any

import pytest
from filelock import Timeout

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
mlx_local_transport = importlib.import_module("mlx_local_transport")
MLXLocalTransport = mlx_local_transport.MLXLocalTransport
_network_disabled = mlx_local_transport._network_disabled


class FakePart:
    text = '{"score":0.7}'
    prompt_tokens = 17
    generation_tokens = 5
    peak_memory = 0.65


class FakeTokenizer:
    def __init__(self) -> None:
        self.messages: list[dict[str, str]] = []
        self.chat_options: dict[str, object] = {}

    def apply_chat_template(
        self,
        messages: list[dict[str, str]],
        *,
        tokenize: bool,
        add_generation_prompt: bool,
    ) -> str:
        self.messages = messages
        self.chat_options = {
            "tokenize": tokenize,
            "add_generation_prompt": add_generation_prompt,
        }
        return "CHAT_TEMPLATE:" + messages[0]["content"]

    def encode(self, text: str) -> list[int]:
        return list(range(len(text.split())))


class CharacterTokenizer(FakeTokenizer):
    def encode(self, text: str) -> list[int]:
        return list(range(len(text)))


def local_model(tmp_path: Path, *, bits: int = 4) -> Path:
    model = tmp_path / "model"
    model.mkdir()
    (model / "config.json").write_text(
        json.dumps({"quantization": {"bits": bits}}), encoding="utf-8"
    )
    (model / "weights.safetensors").write_bytes(b"fake checkpoint marker")
    return model


def fake_runtime(
    monkeypatch: pytest.MonkeyPatch, tokenizer: FakeTokenizer | None = None
) -> dict[str, Any]:
    calls: dict[str, Any] = {"load": [], "stream": [], "sampler": [], "seed": []}
    tokenizer = tokenizer or FakeTokenizer()
    mlx = types.ModuleType("mlx")
    mlx.__path__ = []
    core = types.ModuleType("mlx.core")

    class Random:
        @staticmethod
        def seed(value: int) -> None:
            calls["seed"].append(value)

    core.random = Random()  # type: ignore[attr-defined]
    core.reset_peak_memory = lambda: None  # type: ignore[attr-defined]
    core.get_peak_memory = lambda: 700_000_000  # type: ignore[attr-defined]
    mlx.core = core  # type: ignore[attr-defined]

    mlx_lm = types.ModuleType("mlx_lm")
    mlx_lm.__path__ = []

    def load(path: str, *, trust_remote_code: bool, lazy: bool) -> tuple[object, FakeTokenizer]:
        calls["offline_env"] = {
            key: os.environ.get(key)
            for key in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_HUB_DISABLE_TELEMETRY")
        }
        calls["load"].append((path, trust_remote_code, lazy))
        return object(), tokenizer

    mlx_lm.load = load  # type: ignore[attr-defined]
    generate = types.ModuleType("mlx_lm.generate")

    def stream_generate(
        _model: object,
        _tokenizer: FakeTokenizer,
        *,
        prompt: str,
        max_tokens: int,
        sampler: object,
    ) -> list[FakePart]:
        calls["stream"].append((prompt, max_tokens, sampler))
        return [FakePart()]

    generate.stream_generate = stream_generate  # type: ignore[attr-defined]
    sample_utils = types.ModuleType("mlx_lm.sample_utils")

    def make_sampler(*, temp: float) -> object:
        calls["sampler"].append(temp)
        return {"temperature": temp}

    sample_utils.make_sampler = make_sampler  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mlx", mlx)
    monkeypatch.setitem(sys.modules, "mlx.core", core)
    monkeypatch.setitem(sys.modules, "mlx_lm", mlx_lm)
    monkeypatch.setitem(sys.modules, "mlx_lm.generate", generate)
    monkeypatch.setitem(sys.modules, "mlx_lm.sample_utils", sample_utils)
    calls["tokenizer"] = tokenizer
    return calls


def test_chat_template_temperature_seed_tokens_and_decimal_peak_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = fake_runtime(monkeypatch)
    transport = MLXLocalTransport(local_model(tmp_path))
    result = transport.infer(
        model_name="local-test",
        quantization="4bit",
        prompt_version="v1",
        prompt="Return JSON.",
        input_json='{"id":"one"}',
        temperature=0.0,
        seed=23,
    )

    tokenizer = calls["tokenizer"]
    assert tokenizer.messages == [
        {"role": "user", "content": 'Return JSON.\n\nINPUT_JSON:\n{"id":"one"}'}
    ]
    assert tokenizer.chat_options == {"tokenize": False, "add_generation_prompt": True}
    assert calls["stream"][0][0].startswith("CHAT_TEMPLATE:")
    assert calls["stream"][0][1] == 256
    assert calls["sampler"] == [0.0]
    assert calls["seed"] == [23]
    assert result.text == FakePart.text
    assert result.input_tokens == 17
    assert result.output_tokens == 5
    assert result.peak_memory_bytes == 700_000_000
    assert result.cold_peak_memory_bytes == 700_000_000
    assert result.total_seconds >= result.elapsed_seconds
    assert result.cold_load_seconds >= 0
    assert calls["offline_env"] == {
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "HF_HUB_DISABLE_TELEMETRY": "1",
    }


def test_oversized_json_trims_only_body_and_reports_hashes_and_byte_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = fake_runtime(monkeypatch)
    transport = MLXLocalTransport(local_model(tmp_path), max_input_tokens=24)
    input_json = json.dumps(
        {
            "id": "keep-id",
            "hash": "keep-hash",
            "system_instructions": "keep these instructions",
            "body": "private-body " * 300,
            "title": "keep-title",
        },
        separators=(",", ":"),
    )
    result = transport.infer(
        model_name="local-test",
        quantization="4bit",
        prompt_version="v1",
        prompt="Return JSON.",
        input_json=input_json,
    )

    submitted_text = calls["stream"][0][0]
    submitted_json = submitted_text.split("INPUT_JSON:\n", 1)[1]
    submitted = json.loads(submitted_json)
    assert submitted["id"] == "keep-id"
    assert submitted["hash"] == "keep-hash"
    assert submitted["system_instructions"] == "keep these instructions"
    assert submitted["title"] == "keep-title"
    assert len(submitted["body"]) < len("private-body " * 300)
    assert len(calls["tokenizer"].encode(submitted_text)) <= 24
    assert result.input_truncated_bytes == len(input_json.encode()) - len(submitted_json.encode())
    assert result.input_truncated_bytes > 0
    assert result.raw_input_hash == hashlib.sha256(input_json.encode()).hexdigest()
    assert result.submitted_input_hash == hashlib.sha256(submitted_json.encode()).hexdigest()


def test_oversized_non_body_fields_are_rejected_without_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = fake_runtime(monkeypatch)
    transport = MLXLocalTransport(local_model(tmp_path), max_input_tokens=24)
    input_json = json.dumps({"id": "keep-id", "title": "too-large " * 300})

    with pytest.raises(ValueError, match="configured local input token limit"):
        transport.infer(
            model_name="local-test",
            quantization="4bit",
            prompt_version="v1",
            prompt="Return JSON.",
            input_json=input_json,
        )

    assert calls["stream"] == []


def test_long_unspaced_chinese_body_respects_character_token_limit_and_utf8_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = fake_runtime(monkeypatch, CharacterTokenizer())
    transport = MLXLocalTransport(local_model(tmp_path), max_input_tokens=190)
    input_json = json.dumps(
        {"id": "固定标识", "body": "很长的中文正文没有空格" * 300},
        ensure_ascii=False,
        separators=(",", ":"),
    )

    result = transport.infer(
        model_name="local-test",
        quantization="4bit",
        prompt_version="v1",
        prompt="Return JSON.",
        input_json=input_json,
    )

    submitted_text = calls["stream"][0][0]
    submitted_json = submitted_text.split("INPUT_JSON:\n", 1)[1]
    submitted = json.loads(submitted_json)
    assert len(calls["tokenizer"].encode(submitted_text)) <= 190
    assert submitted["id"] == "固定标识"
    assert len(submitted["body"]) < len("很长的中文正文没有空格" * 300)
    assert result.input_truncated_bytes == len(input_json.encode("utf-8")) - len(
        submitted_json.encode("utf-8")
    )
    assert result.input_truncated_bytes > 0


@pytest.mark.parametrize("case", ["missing_model", "missing_config", "missing_weights", "bad_bits"])
def test_bad_local_checkpoint_fails_before_any_network_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    attempted: list[str] = []

    def deny(*_args: object, **_kwargs: object) -> None:
        attempted.append("network")
        raise AssertionError("network should not be attempted")

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket.socket, "connect_ex", deny)
    monkeypatch.setattr(socket, "getaddrinfo", deny)
    model = tmp_path / "model"
    if case != "missing_model":
        model.mkdir()
    if case == "missing_model":
        pass
    elif case == "missing_weights":
        (model / "config.json").write_text(
            json.dumps({"quantization": {"bits": 4}}), encoding="utf-8"
        )
    elif case == "bad_bits":
        (model / "config.json").write_text(
            json.dumps({"quantization": {"bits": 8}}), encoding="utf-8"
        )
        (model / "weights.safetensors").write_bytes(b"marker")
    else:
        (model / "weights.safetensors").write_bytes(b"marker")

    with pytest.raises(ValueError):
        MLXLocalTransport(model)
    assert attempted == []


def test_network_guard_denies_all_supported_socket_paths_and_restores() -> None:
    methods = ("connect", "connect_ex", "create_connection", "getaddrinfo")
    originals = {
        name: getattr(socket if name in methods[2:] else socket.socket, name) for name in methods
    }
    with _network_disabled():
        for name in methods:
            target = socket if name in methods[2:] else socket.socket
            with pytest.raises(RuntimeError, match="network access is disabled"):
                getattr(target, name)("example.invalid", 443)
    for name, original in originals.items():
        target = socket if name in methods[2:] else socket.socket
        assert getattr(target, name) is original


def test_transport_instances_share_a_process_global_file_lock(tmp_path: Path) -> None:
    model = local_model(tmp_path)
    first = MLXLocalTransport(model)
    second = MLXLocalTransport(model)
    assert first._process_lock.lock_file == second._process_lock.lock_file
    first._process_lock.acquire(timeout=0)
    try:
        with pytest.raises(Timeout):
            second._process_lock.acquire(timeout=0)
    finally:
        first._process_lock.release()


def test_high_os_load_blocks_before_loading_or_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = fake_runtime(monkeypatch)
    monkeypatch.setattr(mlx_local_transport.os, "getloadavg", lambda: (9.0, 8.0, 7.0))
    transport = MLXLocalTransport(local_model(tmp_path))
    with pytest.raises(RuntimeError, match="load average"):
        transport.infer(
            model_name="local-test",
            quantization="4bit",
            prompt_version="v1",
            prompt="Return JSON.",
            input_json="{}",
        )
    assert calls["load"] == []
    assert calls["stream"] == []


def test_os_load_is_checked_again_after_model_load_before_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = fake_runtime(monkeypatch)
    loads = iter((1.0, 9.0))
    monkeypatch.setattr(mlx_local_transport.os, "getloadavg", lambda: (next(loads), 1.0, 1.0))
    transport = MLXLocalTransport(local_model(tmp_path))

    with pytest.raises(RuntimeError, match="load average"):
        transport.infer(
            model_name="local-test",
            quantization="4bit",
            prompt_version="v1",
            prompt="Return JSON.",
            input_json="{}",
        )

    assert len(calls["load"]) == 1
    assert calls["stream"] == []
