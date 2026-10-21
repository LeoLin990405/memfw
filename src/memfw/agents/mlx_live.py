"""Offline, injected local-MLX inference adapter; imports no MLX runtime."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from importlib.resources import files
from typing import Literal, Protocol, cast

from pydantic import TypeAdapter

from ..core.models import Layer, MemoryDocument
from . import DeterministicJudge, JsonObject, _digest, _memory_payload
from .models import JsonValue, MergeProposal, Proposal


class LocalInferenceResult(Protocol):
    text: str
    elapsed_seconds: float
    cold_load_seconds: float | None
    total_seconds: float | None
    input_tokens: int
    output_tokens: int
    peak_memory_bytes: int
    cold_peak_memory_bytes: int
    input_truncated_bytes: int
    raw_input_hash: str
    submitted_input_hash: str


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


class InferenceReceipt(Protocol):
    provider: str
    model_name: str
    quantization: str
    prompt_version: str
    task: str
    status: Literal["completed", "failed"]
    elapsed_seconds: float | None
    cold_load_seconds: float | None
    total_seconds: float | None
    input_tokens: int | None
    output_tokens: int | None
    peak_memory_bytes: int | None
    cold_peak_memory_bytes: int | None
    input_hash: str
    raw_input_hash: str
    submitted_input_hash: str
    input_truncated_bytes: int | None
    output_hash: str


Telemetry = Callable[[InferenceReceipt], None]
ResponseValidator = Callable[[JsonObject], None]
PrivateDiagnostic = Callable[[str | None, BaseException | None], None]
QueryShapeDiagnostic = Callable[[Mapping[str, object]], None]


class LocalMLXQueryError(ValueError):
    """Sanitized query failure with a stable machine-readable code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"local MLX query generation failed ({code})")


class _Receipt:
    def __init__(
        self,
        *,
        model_name: str,
        quantization: str,
        prompt_version: str,
        task: str,
        result: LocalInferenceResult | None,
        status: Literal["completed", "failed"],
        input_hash: str,
        raw_input_hash: str,
        submitted_input_hash: str,
        input_truncated_bytes: int | None,
        output_hash: str,
    ) -> None:
        self.provider = "mlx"
        self.model_name = model_name
        self.quantization = quantization
        self.prompt_version = prompt_version
        self.task = task
        self.status = status
        self.elapsed_seconds = result.elapsed_seconds if result else None
        self.cold_load_seconds = getattr(result, "cold_load_seconds", None) if result else None
        self.total_seconds = getattr(result, "total_seconds", None) if result else None
        self.input_tokens = result.input_tokens if result else None
        self.output_tokens = result.output_tokens if result else None
        self.peak_memory_bytes = result.peak_memory_bytes if result else None
        self.cold_peak_memory_bytes = (
            getattr(result, "cold_peak_memory_bytes", None) if result else None
        )
        self.input_hash = input_hash
        self.raw_input_hash = raw_input_hash
        self.submitted_input_hash = submitted_input_hash
        self.input_truncated_bytes = input_truncated_bytes
        self.output_hash = output_hash


class LocalMLXJudge(DeterministicJudge):
    """Live local inference judge. The transport is injected and defaults to none."""

    provider = "mlx"

    def __init__(
        self,
        *,
        inference: LocalInference,
        model_name: str,
        quantization: str,
        prompt_version: str = "v1",
        clock: Callable[[], datetime] | None = None,
        telemetry: Telemetry | None = None,
        diagnostic_callback: PrivateDiagnostic | None = None,
        query_shape_callback: QueryShapeDiagnostic | None = None,
        ngram_size: int = 3,
        duplicate_threshold: float = 0.8,
    ) -> None:
        if not model_name or not quantization:
            raise ValueError("model_name and quantization are required")
        if prompt_version != "v1":
            raise ValueError("unsupported prompt version")
        super().__init__(
            mode="mock",
            model_name=model_name,
            ngram_size=ngram_size,
            duplicate_threshold=duplicate_threshold,
            clock=clock or (lambda: datetime.now(UTC)),
        )
        self.inference = inference
        self.quantization = quantization
        self.prompt_version = prompt_version
        self.telemetry = telemetry
        self.diagnostic_callback = diagnostic_callback
        self.query_shape_callback = query_shape_callback

    def _input_hash(self, input_value: object) -> str:
        return _digest(
            {
                "provider": self.provider,
                "model": self.model_name,
                "quantization": self.quantization,
                "prompt_version": self.prompt_version,
                "ngram_size": self.ngram_size,
                "duplicate_threshold": self.duplicate_threshold,
                "input": input_value,
            }
        )

    def _infer(self, task: str, value: object, validator: ResponseValidator) -> JsonObject:
        input_json = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        prompt_name = f"{task}-{self.prompt_version}.txt"
        prompt = (files("memfw.agents") / "prompts" / prompt_name).read_text(encoding="utf-8")
        input_hash = self._input_hash(value)
        raw_input_hash = hashlib.sha256(input_json.encode("utf-8")).hexdigest()
        result: LocalInferenceResult | None = None
        try:
            result = self.inference.infer(
                model_name=self.model_name,
                quantization=self.quantization,
                prompt_version=self.prompt_version,
                prompt=prompt,
                input_json=input_json,
                temperature=0.0,
                seed=0,
            )
            parsed: object = self._parse_response(result.text, task)
            if not isinstance(parsed, dict) or not all(isinstance(key, str) for key in parsed):
                raise ValueError("local MLX response must be one JSON object")
            if task == "query" and self.query_shape_callback is not None:
                try:
                    self.query_shape_callback(self._query_shape(parsed))
                except Exception:
                    raise LocalMLXQueryError("diagnostic_logging_failure") from None
            response: JsonObject = cast(JsonObject, TypeAdapter(JsonValue).validate_python(parsed))
            validator(response)
            if not self._valid_metrics(result):
                raise ValueError("invalid inference metrics")
            if task == "query" and self.diagnostic_callback is not None:
                self._notify_private_diagnostic(result.text, None)
        except Exception as exc:
            self._emit_receipt(task, result, input_hash, raw_input_hash, "failed")
            if task == "query":
                code = getattr(
                    exc,
                    "code",
                    "transport_failure" if result is None else "invalid_response",
                )
                if not isinstance(code, str):
                    code = "invalid_response"
                safe = LocalMLXQueryError(code)
                if self.diagnostic_callback is not None and code != "diagnostic_logging_failure":
                    self._notify_private_diagnostic(
                        getattr(result, "text", None) if result is not None else None, exc
                    )
                raise safe from None
            raise ValueError("local MLX inference or response validation failed") from None
        self._emit_receipt(task, result, input_hash, raw_input_hash, "completed")
        return response

    def _notify_private_diagnostic(self, response: str | None, error: BaseException | None) -> None:
        if self.diagnostic_callback is None:
            return
        try:
            self.diagnostic_callback(response, error)
        except Exception:
            raise LocalMLXQueryError("diagnostic_logging_failure") from None

    @staticmethod
    def _query_shape(value: object) -> Mapping[str, object]:
        """Describe known query-envelope structure without retaining any payload values."""
        if not isinstance(value, dict):
            return {"root_type": type(value).__name__}
        rows = value.get("queries")
        shape: dict[str, object] = {
            "root_type": "object",
            "queries_present": "queries" in value,
            "queries_type": type(rows).__name__,
        }
        if isinstance(rows, list):
            row_types: dict[str, int] = {}
            field_types: dict[str, dict[str, int]] = {
                "target_id": {},
                "query": {},
            }
            blank_queries = 0
            for row in rows:
                row_type = type(row).__name__
                row_types[row_type] = row_types.get(row_type, 0) + 1
                if not isinstance(row, dict):
                    continue
                for field in field_types:
                    item = row.get(field)
                    item_type = "missing" if field not in row else type(item).__name__
                    counts = field_types[field]
                    counts[item_type] = counts.get(item_type, 0) + 1
                    if field == "query" and isinstance(item, str) and not item.strip():
                        blank_queries += 1
            shape.update(
                {
                    "query_count": len(rows),
                    "row_types": row_types,
                    "known_field_types": field_types,
                    "blank_query_count": blank_queries,
                }
            )
        return shape

    @staticmethod
    def _parse_response(text: str, task: str) -> object:
        """Parse strict JSON, or recover one JSON value from common model wrapping."""
        candidates: list[str] = [text.strip()]
        if task == "query":
            candidates.extend(
                match.group(1).strip()
                for match in re.finditer(r"```(?:json)?\s*(.*?)```", text, re.I | re.S)
            )
            # Scan balanced JSON regions in prose; strings and escapes are respected.
            start: int | None = None
            depth = 0
            quoted = False
            escaped = False
            for index, char in enumerate(text):
                if quoted:
                    if escaped:
                        escaped = False
                    elif char == "\\":
                        escaped = True
                    elif char == '"':
                        quoted = False
                    continue
                if char == '"':
                    quoted = True
                elif char in "[{":
                    if depth == 0:
                        start = index
                    depth += 1
                elif char in "]}":
                    depth -= 1
                    if depth == 0 and start is not None:
                        candidates.append(text[start : index + 1])
                        start = None
                    elif depth < 0:
                        depth = 0
        parsed_candidates: dict[str, object] = {}
        for candidate in candidates:
            try:
                parsed = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            parsed_candidates.setdefault(
                json.dumps(parsed, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                parsed,
            )
        if task == "query" and len(parsed_candidates) > 1:
            raise LocalMLXQueryError("ambiguous_json")
        if parsed_candidates:
            parsed = next(iter(parsed_candidates.values()))
            if task == "query" and isinstance(parsed, list):
                return {"queries": parsed}
            if task == "query" and isinstance(parsed, dict) and "query" in parsed:
                return {"queries": [parsed]}
            return parsed
        raise LocalMLXQueryError("invalid_json") if task == "query" else ValueError("invalid JSON")

    @staticmethod
    def _valid_metrics(result: LocalInferenceResult) -> bool:
        values = (
            result.elapsed_seconds,
            result.input_tokens,
            result.output_tokens,
            result.peak_memory_bytes,
        )
        optional_load_time = getattr(result, "cold_load_seconds", None)
        optional_total_time = getattr(result, "total_seconds", None)
        optional_cold_peak = getattr(result, "cold_peak_memory_bytes", None)
        return (
            all(
                not isinstance(value, bool)
                and isinstance(value, (int, float))
                and math.isfinite(value)
                for value in values
            )
            and result.elapsed_seconds >= 0
            and (
                optional_load_time is None
                or (
                    not isinstance(optional_load_time, bool)
                    and isinstance(optional_load_time, (int, float))
                    and math.isfinite(optional_load_time)
                    and optional_load_time >= 0
                )
            )
            and (
                optional_total_time is None
                or (
                    not isinstance(optional_total_time, bool)
                    and isinstance(optional_total_time, (int, float))
                    and math.isfinite(optional_total_time)
                    and optional_total_time >= 0
                )
            )
            and (
                optional_cold_peak is None
                or (
                    isinstance(optional_cold_peak, int)
                    and not isinstance(optional_cold_peak, bool)
                    and optional_cold_peak >= 0
                )
            )
            and result.input_tokens >= 0
            and result.output_tokens >= 0
            and result.peak_memory_bytes >= 0
        )

    def _emit_receipt(
        self,
        task: str,
        result: LocalInferenceResult | None,
        input_hash: str,
        raw_input_hash: str,
        status: Literal["completed", "failed"],
    ) -> None:
        if self.telemetry is None:
            return
        metric_result = result if result is not None and self._valid_metrics(result) else None
        output_text = getattr(result, "text", None) if result is not None else None
        output_hash = (
            hashlib.sha256(output_text.encode("utf-8")).hexdigest()
            if isinstance(output_text, str)
            else "unknown"
        )
        submitted_input_hash: str = raw_input_hash
        result_raw_hash: str = raw_input_hash
        if result is not None:
            candidate_submitted_hash = getattr(result, "submitted_input_hash", None)
            candidate_raw_hash = getattr(result, "raw_input_hash", None)
            if self._is_sha256(candidate_submitted_hash):
                submitted_input_hash = cast(str, candidate_submitted_hash)
            if self._is_sha256(candidate_raw_hash):
                result_raw_hash = cast(str, candidate_raw_hash)
        truncated_bytes = getattr(result, "input_truncated_bytes", 0) if result else 0
        if (
            isinstance(truncated_bytes, bool)
            or not isinstance(truncated_bytes, int)
            or truncated_bytes < 0
        ):
            truncated_bytes = None
        self.telemetry(
            _Receipt(
                model_name=self.model_name,
                quantization=self.quantization,
                prompt_version=self.prompt_version,
                task=task,
                result=metric_result,
                status=status,
                input_hash=input_hash,
                raw_input_hash=result_raw_hash,
                submitted_input_hash=submitted_input_hash,
                input_truncated_bytes=truncated_bytes,
                output_hash=output_hash,
            )
        )

    @staticmethod
    def _is_sha256(value: object) -> bool:
        return (
            isinstance(value, str)
            and len(value) == 64
            and all(character in "0123456789abcdef" for character in value)
        )

    @staticmethod
    def _reason(response: Mapping[str, JsonValue]) -> str:
        reason = response.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("local MLX response requires a non-empty reason")
        return reason.strip()

    @classmethod
    def _validate_score(cls, response: JsonObject) -> None:
        score = response.get("score")
        if isinstance(score, bool) or not isinstance(score, (int, float)) or not 0 <= score <= 1:
            raise ValueError("invalid score")
        cls._reason(response)

    @classmethod
    def _validate_layer(cls, response: JsonObject) -> None:
        layer = response.get("layer")
        if not isinstance(layer, str):
            raise ValueError("invalid layer")
        Layer(layer)
        cls._reason(response)

    @classmethod
    def _validate_wake(cls, response: JsonObject) -> None:
        intensity = response.get("intensity")
        if (
            isinstance(intensity, bool)
            or not isinstance(intensity, (int, float))
            or not 0 <= intensity <= 1
        ):
            raise ValueError("invalid intensity")
        cls._reason(response)

    @classmethod
    def _validate_merges(cls, response: JsonObject, known_ids: set[str]) -> None:
        pairs = response.get("merge_pairs")
        if not isinstance(pairs, list):
            raise ValueError("missing merge pairs")
        for pair in pairs:
            if not isinstance(pair, dict):
                raise ValueError("invalid merge pair")
            representative = pair.get("representative_id")
            duplicates = pair.get("duplicate_ids")
            basis = pair.get("match_basis")
            disposition = pair.get("disposition")
            confidence = pair.get("confidence")
            if (
                not isinstance(representative, str)
                or representative not in known_ids
                or not isinstance(duplicates, list)
                or not duplicates
                or not all(isinstance(item, str) and item in known_ids for item in duplicates)
                or len(set(duplicates)) != len(duplicates)
                or representative in duplicates
                or not isinstance(basis, str)
                or not basis.strip()
                or disposition not in {"retain", "delete"}
                or isinstance(confidence, bool)
                or not isinstance(confidence, (int, float))
                or not 0 <= confidence <= 1
            ):
                raise ValueError("invalid merge fields")
            cls._reason(pair)

    @classmethod
    def _validate_queries(cls, response: JsonObject, ids: set[str], count: int) -> None:
        items = response.get("queries")
        if not isinstance(items, list):
            raise LocalMLXQueryError("invalid_schema")
        if not items:
            raise LocalMLXQueryError("empty_output")
        for item in items:
            if isinstance(item, str) and len(ids) == 1 and item.strip():
                continue
            if isinstance(item, dict) and "query" in item and len(ids) == 1:
                query = item.get("query")
                if isinstance(query, str) and query.strip():
                    continue
            if not isinstance(item, dict):
                raise LocalMLXQueryError("invalid_schema")
            target_id = item.get("target_id")
            query = item.get("query")
            if (
                not isinstance(target_id, str)
                or (len(ids) != 1 and target_id not in ids)
                or not isinstance(query, str)
                or not query.strip()
            ):
                raise LocalMLXQueryError("invalid_schema")

    def score_salience(self, memories: list[MemoryDocument]) -> list[Proposal]:
        proposals: list[Proposal] = []
        for memory in memories:
            value = _memory_payload(memory)
            response = self._infer("salience", value, self._validate_score)
            score = response.get("score")
            if (
                isinstance(score, bool)
                or not isinstance(score, (int, float))
                or not 0 <= score <= 1
            ):
                raise ValueError("local MLX score must be in [0, 1]")
            input_hash = self._input_hash(value)
            proposals.append(
                self._proposal(
                    kind="salience",
                    target_ids=(memory.id,),
                    target_hashes={memory.id: _digest(value)},
                    output={"score": round(float(score), 8), "input_hash": _digest(value)},
                    reason=self._reason(response),
                    input_hash=input_hash,
                )
            )
        return proposals

    def suggest_layer(self, memory: MemoryDocument) -> Proposal:
        value = _memory_payload(memory)
        response = self._infer("layer", value, self._validate_layer)
        raw_layer = response.get("layer")
        if not isinstance(raw_layer, str):
            raise ValueError("local MLX response requires a valid layer")
        try:
            layer = Layer(raw_layer)
        except (ValueError, TypeError) as exc:
            raise ValueError("local MLX response requires a valid layer") from exc
        return self._proposal(
            kind="layer",
            target_ids=(memory.id,),
            target_hashes={memory.id: _digest(value)},
            output={"layer": layer.value, "input_hash": _digest(value)},
            reason=self._reason(response),
            input_hash=self._input_hash(value),
        )

    def wake_intensity(self, signal: str) -> float:
        response = self._infer("wake", {"signal": signal}, self._validate_wake)
        intensity = response.get("intensity")
        if (
            isinstance(intensity, bool)
            or not isinstance(intensity, (int, float))
            or not 0 <= intensity <= 1
        ):
            raise ValueError("local MLX intensity must be in [0, 1]")
        self._reason(response)
        return float(intensity)

    def find_near_duplicates(self, memories: list[MemoryDocument]) -> list[MergeProposal]:
        values = [_memory_payload(memory) for memory in memories]
        known = {memory.id: memory for memory in memories}
        response = self._infer(
            "merge", values, lambda result: self._validate_merges(result, set(known))
        )
        pairs = response.get("merge_pairs")
        if not isinstance(pairs, list):
            raise ValueError("local MLX merge response requires merge_pairs")
        input_hash = self._input_hash(values)
        output: list[MergeProposal] = []
        for pair in pairs:
            if not isinstance(pair, dict):
                raise ValueError("local MLX merge pair must be an object")
            representative = pair.get("representative_id")
            duplicates = pair.get("duplicate_ids")
            basis = pair.get("match_basis")
            disposition = pair.get("disposition")
            confidence = pair.get("confidence")
            if (
                not isinstance(representative, str)
                or representative not in known
                or not isinstance(duplicates, list)
                or not duplicates
                or not all(isinstance(item, str) and item in known for item in duplicates)
                or len(set(duplicates)) != len(duplicates)
                or representative in duplicates
                or not isinstance(basis, str)
                or not basis.strip()
                or disposition not in {"retain", "delete"}
                or isinstance(confidence, bool)
                or not isinstance(confidence, (int, float))
                or not 0 <= confidence <= 1
            ):
                raise ValueError("local MLX merge pair has invalid targets or fields")
            representative_id = representative
            duplicate_ids = cast(list[str], duplicates)
            match_basis = basis
            merge_disposition = cast(Literal["retain", "delete"], disposition)
            merge_confidence = cast(float, confidence)
            targets = (representative_id, *duplicate_ids)
            reason = self._reason(pair)
            output_data: JsonObject = {
                "representative_id": representative_id,
                "duplicate_ids": cast(JsonValue, duplicate_ids),
                "match_basis": match_basis.strip(),
                "disposition": merge_disposition,
                "confidence": float(merge_confidence),
            }
            proposal = self._proposal(
                kind="merge",
                target_ids=targets,
                target_hashes={item: _digest(_memory_payload(known[item])) for item in targets},
                output=output_data,
                reason=reason,
                input_hash=input_hash,
            )
            output.append(
                MergeProposal(
                    representative_id=representative_id,
                    duplicate_ids=tuple(duplicate_ids),
                    match_basis=match_basis.strip(),
                    disposition=merge_disposition,
                    confidence=float(merge_confidence),
                    proposal=proposal,
                )
            )
        return output

    def suggest_queries(self, memories: list[MemoryDocument], count: int) -> list[dict[str, str]]:
        """Return at most count retrieval queries bound to supplied memory IDs."""
        if count < 0 or count > 100:
            raise ValueError("query count must be between 0 and 100")
        if count == 0 or not memories:
            return []
        inputs = {"count": count, "memories": [_memory_payload(item) for item in memories]}
        response = self._infer(
            "query",
            inputs,
            lambda result: self._validate_queries(
                result, {memory.id for memory in memories}, count
            ),
        )
        raw_queries = response.get("queries")
        if not isinstance(raw_queries, list):
            return []
        known_ids = {memory.id for memory in memories}
        single_id = memories[0].id if len(memories) == 1 else None
        queries: list[dict[str, str]] = []
        for item in raw_queries[:count]:
            if isinstance(item, str) and single_id is not None and item.strip():
                queries.append({"target_id": single_id, "query": item.strip()})
                continue
            if not isinstance(item, dict):
                continue
            target_id = item.get("target_id")
            query = item.get("query")
            if single_id is not None:
                target_id = single_id
            if (
                isinstance(target_id, str)
                and target_id in known_ids
                and isinstance(query, str)
                and query.strip()
            ):
                queries.append({"target_id": target_id, "query": query.strip()})
        return queries


__all__ = [
    "InferenceReceipt",
    "LocalInference",
    "LocalInferenceResult",
    "LocalMLXJudge",
    "LocalMLXQueryError",
    "Telemetry",
]
