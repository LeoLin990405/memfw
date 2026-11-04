"""Offline-only, lazy MLX-LM inference transport for the Day 8 pilot.

This module has no import-time MLX dependency and never resolves a model name
through a remote hub. The caller must supply an existing local model directory.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import Any, Protocol

from filelock import FileLock


@dataclass(frozen=True, slots=True)
class LocalInferenceResult:
    text: str
    elapsed_seconds: float
    input_tokens: int
    output_tokens: int
    peak_memory_bytes: int
    cold_load_seconds: float = 0.0
    cold_peak_memory_bytes: int = 0
    total_seconds: float = 0.0
    input_truncated_bytes: int = 0
    raw_input_hash: str = ""
    submitted_input_hash: str = ""


class LocalInference(Protocol):
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
    ) -> LocalInferenceResult: ...


def _replace_attribute(target: object, name: str, value: object) -> None:
    setattr(target, name, value)


@contextmanager
def _network_disabled() -> Iterator[None]:
    """Fail closed for socket connection attempts during local model work."""
    old_connect = socket.socket.connect
    old_connect_ex = socket.socket.connect_ex
    old_create_connection = socket.create_connection
    old_getaddrinfo = socket.getaddrinfo

    def denied(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("network access is disabled for the local MLX pilot")

    _replace_attribute(socket.socket, "connect", denied)
    _replace_attribute(socket.socket, "connect_ex", denied)
    _replace_attribute(socket, "create_connection", denied)
    _replace_attribute(socket, "getaddrinfo", denied)
    try:
        yield
    finally:
        _replace_attribute(socket.socket, "connect", old_connect)
        _replace_attribute(socket.socket, "connect_ex", old_connect_ex)
        _replace_attribute(socket, "create_connection", old_create_connection)
        _replace_attribute(socket, "getaddrinfo", old_getaddrinfo)


class MLXLocalTransport:
    """A single-call-at-a-time local MLX-LM transport."""

    def __init__(self, model_path: Path | str, *, max_input_tokens: int = 8192) -> None:
        if (
            isinstance(max_input_tokens, bool)
            or not isinstance(max_input_tokens, int)
            or max_input_tokens <= 0
        ):
            raise ValueError("max_input_tokens must be a positive integer")
        self.max_input_tokens = max_input_tokens
        self.model_path = Path(model_path).expanduser().resolve()
        if not self.model_path.is_dir():
            raise ValueError("model path must be an existing local directory")
        self._config_path = self.model_path / "config.json"
        self._weights = tuple(self.model_path.glob("*.safetensors"))
        if not self._config_path.is_file() or not self._weights:
            raise ValueError(
                "local model directory must contain config.json and safetensors weights"
            )
        try:
            self._config = json.loads(self._config_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError("local model config.json is unreadable") from exc
        quant = self._config.get("quantization")
        if not isinstance(quant, dict) or quant.get("bits") != 4:
            raise ValueError("local model config must identify a 4-bit quantized checkpoint")
        self._process_lock = FileLock(
            str(Path(tempfile.gettempdir()) / f"memfw-local-mlx-{os.getuid()}.lock")
        )
        self._model: Any = None
        self._tokenizer: Any = None
        self._mx: Any = None
        self._stream_generate: Any = None
        self._make_sampler: Any = None

    @staticmethod
    def _require_load_below_limit() -> None:
        try:
            load_average = os.getloadavg()[0]
        except (AttributeError, OSError) as exc:
            raise RuntimeError("OS load average is unavailable; refusing MLX inference") from exc
        if load_average >= 9.0:
            raise RuntimeError("OS one-minute load average is at least 9; defer MLX inference")

    def _load(self) -> tuple[Any, Any, Any, Any, Any]:
        if self._model is None:
            os.environ["HF_HUB_OFFLINE"] = "1"
            os.environ["TRANSFORMERS_OFFLINE"] = "1"
            os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
            with _network_disabled():
                try:
                    # Resolve optional native runtime modules only inside the offline guard.
                    mx = import_module("mlx.core")
                    load = vars(import_module("mlx_lm"))["load"]
                    stream_generate = vars(import_module("mlx_lm.generate"))["stream_generate"]
                    make_sampler = vars(import_module("mlx_lm.sample_utils"))["make_sampler"]
                except ImportError as exc:
                    raise RuntimeError(
                        "mlx and mlx-lm are not installed in this environment"
                    ) from exc
                self._model, self._tokenizer = load(
                    str(self.model_path), trust_remote_code=False, lazy=False
                )
                self._mx = mx
                self._stream_generate = stream_generate
                self._make_sampler = make_sampler
        return self._model, self._tokenizer, self._mx, self._stream_generate, self._make_sampler

    @staticmethod
    def _shrink_text(value: str) -> str:
        if len(value) <= 2:
            return ""
        return value[: len(value) // 2] + "…"

    @staticmethod
    def _shrink_json(value: Any) -> Any:
        """Trim only body/summary text while retaining valid JSON and metadata."""
        if isinstance(value, dict):
            copied = dict(value)
            for key in ("body", "summary"):
                if key in copied and isinstance(copied[key], str) and copied[key]:
                    copied[key] = MLXLocalTransport._shrink_text(copied[key])
                    return copied
            for key, child in copied.items():
                if isinstance(child, (dict, list)):
                    smaller = MLXLocalTransport._shrink_json(child)
                    if smaller != child:
                        copied[key] = smaller
                        return copied
            return copied
        if isinstance(value, list):
            for index, child in enumerate(value):
                if isinstance(child, (dict, list)):
                    smaller = MLXLocalTransport._shrink_json(child)
                    if smaller != child:
                        return [*value[:index], smaller, *value[index + 1 :]]
        return value

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
    ) -> LocalInferenceResult:
        if temperature != 0.0:
            raise ValueError("the Day 8 pilot requires temperature=0.0")
        if not model_name or not prompt_version:
            raise ValueError("model_name and prompt_version are required for audit metadata")
        expected_bits = 4 if "4" in quantization.casefold() else None
        if expected_bits != self._config["quantization"].get("bits"):
            raise ValueError("requested quantization does not match the local checkpoint metadata")
        with self._process_lock:
            self._require_load_below_limit()
            call_started = time.perf_counter()
            cold_started = time.perf_counter()
            model, tokenizer, mx, stream_generate, make_sampler = self._load()
            cold_load_seconds = time.perf_counter() - cold_started
            cold_peak_memory = int(mx.get_peak_memory())
            self._require_load_below_limit()
            mx.random.seed(seed)
            mx.reset_peak_memory()
            started = time.perf_counter()
            try:
                parsed_input = json.loads(input_json)
            except (TypeError, ValueError) as exc:
                raise ValueError("input_json must contain valid JSON") from exc
            if not hasattr(tokenizer, "apply_chat_template"):
                raise RuntimeError(
                    "model tokenizer has no chat template; refusing plain-text fallback"
                )
            transmitted_json = json.dumps(
                parsed_input, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            raw_input_hash = hashlib.sha256(input_json.encode("utf-8")).hexdigest()
            original_bytes = len(transmitted_json.encode("utf-8"))

            def format_prompt(serialized: str) -> str:
                return str(
                    tokenizer.apply_chat_template(
                        [{"role": "user", "content": f"{prompt}\n\nINPUT_JSON:\n{serialized}"}],
                        tokenize=False,
                        add_generation_prompt=True,
                    )
                )

            formatted_prompt = format_prompt(transmitted_json)
            prompt_tokens = len(tokenizer.encode(formatted_prompt))
            while prompt_tokens > self.max_input_tokens:
                smaller = self._shrink_json(parsed_input)
                candidate = json.dumps(
                    smaller, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                )
                if candidate == transmitted_json:
                    raise ValueError("prompt exceeds the configured local input token limit")
                parsed_input = smaller
                transmitted_json = candidate
                formatted_prompt = format_prompt(transmitted_json)
                prompt_tokens = len(tokenizer.encode(formatted_prompt))
            input_truncated_bytes = max(0, original_bytes - len(transmitted_json.encode("utf-8")))
            chunks: list[str] = []
            output_tokens = peak_memory = 0
            with _network_disabled():
                for part in stream_generate(
                    model,
                    tokenizer,
                    prompt=formatted_prompt,
                    max_tokens=256,
                    sampler=make_sampler(temp=0.0),
                ):
                    chunk = getattr(part, "text", None)
                    if isinstance(chunk, str):
                        chunks.append(chunk)
                    prompt_tokens = int(
                        getattr(part, "prompt_tokens", prompt_tokens) or prompt_tokens
                    )
                    output_tokens = int(
                        getattr(part, "generation_tokens", output_tokens) or output_tokens
                    )
                    peak_gb = getattr(part, "peak_memory", None)
                    if isinstance(peak_gb, (int, float)):
                        peak_memory = max(peak_memory, int(peak_gb * 1_000_000_000))
            if not output_tokens:
                output_tokens = len(tokenizer.encode("".join(chunks)))
            peak_memory = max(peak_memory, int(mx.get_peak_memory()))
            return LocalInferenceResult(
                text="".join(chunks),
                elapsed_seconds=time.perf_counter() - started,
                input_tokens=prompt_tokens,
                output_tokens=output_tokens,
                peak_memory_bytes=peak_memory,
                cold_load_seconds=cold_load_seconds,
                cold_peak_memory_bytes=cold_peak_memory,
                total_seconds=time.perf_counter() - call_started,
                input_truncated_bytes=input_truncated_bytes,
                raw_input_hash=raw_input_hash,
                submitted_input_hash=hashlib.sha256(transmitted_json.encode("utf-8")).hexdigest(),
            )
