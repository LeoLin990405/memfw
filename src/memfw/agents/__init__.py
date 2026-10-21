"""Deterministic mock judges. No provider SDK or network client is used here."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Literal, Protocol, cast

from pydantic import TypeAdapter

from ..core.models import Layer, MemoryDocument
from .models import (
    JsonValue,
    MergeProposal,
    Proposal,
    canonical_json,
    canonical_payload_hash,
    mock_signature,
)

Mode = Literal["mock", "live"]
JsonObject = dict[str, JsonValue]


class JudgeAgent(Protocol):
    def score_salience(self, memories: list[MemoryDocument]) -> list[Proposal]: ...
    def find_near_duplicates(self, memories: list[MemoryDocument]) -> list[MergeProposal]: ...
    def suggest_layer(self, memory: MemoryDocument) -> Proposal: ...
    def wake_intensity(self, signal: str) -> float: ...


class MemoryAgent(Protocol):
    """Compatibility protocol retained for earlier agent consumers."""

    def propose(self, context: str) -> Proposal: ...


class InjectedTransport(Protocol):
    def __call__(
        self, provider: str, request: Mapping[str, JsonValue]
    ) -> Mapping[str, JsonValue]: ...


def _default_clock() -> datetime:
    return datetime(2000, 1, 1, tzinfo=UTC)


def _digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _memory_payload(memory: MemoryDocument) -> JsonObject:
    dumped = memory.model_dump(mode="json")
    return {key: value for key, value in dumped.items()}


class DeterministicJudge:
    """Shared implementation used by provider-named adapters in mock mode."""

    provider = "mock"
    provider_env = ""

    def __init__(
        self,
        *,
        mode: Mode = "mock",
        model_name: str | None = None,
        ngram_size: int = 3,
        duplicate_threshold: float = 0.8,
        clock: Callable[[], datetime] = _default_clock,
        transport: InjectedTransport | None = None,
        env_key: str | None = None,
    ) -> None:
        if ngram_size < 1:
            raise ValueError("ngram_size must be positive")
        if not 0 <= duplicate_threshold <= 1:
            raise ValueError("duplicate_threshold must be between zero and one")
        if mode not in ("mock", "live"):
            raise ValueError("mode must be mock or live")
        if mode == "live":
            if transport is None:
                raise ValueError("live mode requires an injected transport")
            required_env = self.provider_env if env_key is None else env_key
            if required_env and not os.environ.get(required_env):
                raise ValueError("live mode requires the configured provider environment key")
        self.mode = mode
        self.model_name = model_name or f"{self.provider}-mock-v1"
        self.ngram_size = ngram_size
        self.duplicate_threshold = duplicate_threshold
        self.clock = clock
        self.transport = transport
        self.env_key = env_key

    def _input_hash(self, input_value: object) -> str:
        return _digest(
            {
                "provider": self.provider,
                "model": self.model_name,
                "ngram_size": self.ngram_size,
                "duplicate_threshold": self.duplicate_threshold,
                "input": input_value,
            }
        )

    def _request(self, task: str, input_value: object) -> JsonObject:
        input_hash = self._input_hash(input_value)
        request: JsonObject = {
            "task": task,
            "input_hash": input_hash,
            "input": TypeAdapter(JsonValue).validate_python(input_value),
        }
        if self.mode == "mock":
            request["mock_digest"] = _digest([self.provider, self.model_name, task, input_hash])
            return request
        assert self.transport is not None
        response = self.transport(self.provider, request)
        return {key: value for key, value in response.items()}

    @staticmethod
    def _mock_seed(response: JsonObject) -> str:
        seed = response.get("mock_digest")
        if not isinstance(seed, str):
            raise ValueError("mock response is missing its deterministic digest")
        return seed

    def _proposal(
        self,
        *,
        kind: Literal["salience", "merge", "layer", "forget", "anchor"],
        target_ids: tuple[str, ...],
        target_hashes: dict[str, str],
        output: JsonObject,
        reason: str,
        input_hash: str,
    ) -> Proposal:
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("clock must return a timezone-aware datetime")
        now = now.astimezone(UTC)
        payload_hash = canonical_payload_hash(output)
        proposal_id = _digest(
            [self.provider, self.model_name, kind, target_ids, target_hashes, input_hash, output]
        )[:32]
        signature = mock_signature(proposal_id, input_hash, payload_hash, now, target_hashes)
        return Proposal(
            proposal_id=proposal_id,
            kind=kind,
            target_ids=target_ids,
            target_hashes=target_hashes,
            agent_name=self.provider,
            model_name=self.model_name,
            input_hash=input_hash,
            output=output,
            reason=reason,
            payload_hash=payload_hash,
            actor=f"agent:{self.provider}",
            signature=signature,
            created_at=now,
            updated_at=now,
        )

    def score_salience(self, memories: list[MemoryDocument]) -> list[Proposal]:
        serialized = [_memory_payload(memory) for memory in memories]
        proposals: list[Proposal] = []
        for memory, item in zip(memories, serialized, strict=True):
            request = self._request("score_salience", item)
            input_hash = self._input_hash(item)
            if self.mode == "mock":
                seed = self._mock_seed(request)
                score = int(seed[:8], 16) / 0xFFFFFFFF
            else:
                value = request.get("score")
                if (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not 0.0 <= value <= 1.0
                ):
                    raise ValueError("live score_salience response requires score in [0, 1]")
                score = float(value)
            output: JsonObject = {"score": round(score, 8), "input_hash": _digest(item)}
            proposals.append(
                self._proposal(
                    kind="salience",
                    target_ids=(memory.id,),
                    target_hashes={memory.id: _digest(item)},
                    output=output,
                    reason=(
                        "Deterministic mock score; no semantic judgement was performed."
                        if self.mode == "mock"
                        else "Provider score response; human review is required."
                    ),
                    input_hash=input_hash,
                )
            )
        return proposals

    @staticmethod
    def _ngrams(memory: MemoryDocument, size: int) -> set[str]:
        text = " ".join((memory.title, memory.summary, memory.body)).casefold()
        compact = " ".join(text.split())
        if not compact:
            return set()
        if len(compact) < size:
            return {compact}
        return {compact[index : index + size] for index in range(len(compact) - size + 1)}

    @classmethod
    def _ngram_bitsets(
        cls, memories: list[MemoryDocument], size: int
    ) -> tuple[dict[str, int], dict[str, int]]:
        """Project n-gram sets to deterministic integer bitsets for exact Jaccard."""

        gram_sets = {memory.id: cls._ngrams(memory, size) for memory in memories}
        vocabulary = sorted({gram for values in gram_sets.values() for gram in values})
        positions = {gram: index for index, gram in enumerate(vocabulary)}
        byte_count = (len(vocabulary) + 7) // 8
        masks: dict[str, int] = {}
        counts: dict[str, int] = {}
        for memory_id, values in gram_sets.items():
            packed = bytearray(byte_count)
            for gram in values:
                position = positions[gram]
                packed[position >> 3] |= 1 << (position & 7)
            masks[memory_id] = int.from_bytes(packed, "little")
            counts[memory_id] = len(values)
        return masks, counts

    def find_near_duplicates(self, memories: list[MemoryDocument]) -> list[MergeProposal]:
        input_value = [_memory_payload(memory) for memory in memories]
        input_hash = self._input_hash(input_value)
        if self.mode == "live":
            response = self._request("find_near_duplicates", input_value)
            pairs = response.get("merge_pairs")
            if not isinstance(pairs, list):
                raise ValueError("live find_near_duplicates response requires merge_pairs list")
            known_ids = {memory.id for memory in memories}
            proposals: list[MergeProposal] = []
            for raw_pair in pairs:
                if not isinstance(raw_pair, dict):
                    raise ValueError("merge_pairs entries must be objects")
                representative_id = raw_pair.get("representative_id")
                raw_duplicate_ids = raw_pair.get("duplicate_ids")
                basis = raw_pair.get("match_basis", "provider-supplied")
                raw_disposition = raw_pair.get("disposition", "retain")
                confidence = raw_pair.get("confidence")
                if (
                    not isinstance(representative_id, str)
                    or representative_id not in known_ids
                    or not isinstance(raw_duplicate_ids, list)
                    or not raw_duplicate_ids
                    or not all(
                        isinstance(item, str) and item in known_ids for item in raw_duplicate_ids
                    )
                    or representative_id in raw_duplicate_ids
                    or not isinstance(basis, str)
                    or not isinstance(raw_disposition, str)
                    or raw_disposition not in {"delete", "retain"}
                    or isinstance(confidence, bool)
                    or not isinstance(confidence, (int, float))
                    or not 0.0 <= confidence <= 1.0
                ):
                    raise ValueError("live merge_pairs entry has invalid targets or fields")
                duplicate_ids = cast(list[str], raw_duplicate_ids)
                disposition = cast(Literal["delete", "retain"], raw_disposition)
                targets: tuple[str, ...] = (representative_id, *duplicate_ids)
                output_data: JsonObject = {
                    "representative_id": representative_id,
                    "duplicate_ids": cast(JsonValue, duplicate_ids),
                    "match_basis": basis,
                    "disposition": disposition,
                    "confidence": float(confidence),
                }
                proposal = self._proposal(
                    kind="merge",
                    target_ids=targets,
                    target_hashes={
                        memory.id: _digest(_memory_payload(memory))
                        for memory in memories
                        if memory.id in targets
                    },
                    output=output_data,
                    reason="Provider response; human review is required.",
                    input_hash=input_hash,
                )
                proposals.append(
                    MergeProposal(
                        representative_id=representative_id,
                        duplicate_ids=tuple(duplicate_ids),
                        match_basis=basis,
                        disposition=disposition,
                        confidence=float(confidence),
                        proposal=proposal,
                    )
                )
            return proposals
        masks, gram_counts = self._ngram_bitsets(memories, self.ngram_size)
        output: list[MergeProposal] = []
        ordered = sorted(memories, key=lambda memory: memory.id)
        for index, left_memory in enumerate(ordered):
            for right_memory in ordered[index + 1 :]:
                intersection = (masks[left_memory.id] & masks[right_memory.id]).bit_count()
                union_count = (
                    gram_counts[left_memory.id] + gram_counts[right_memory.id] - intersection
                )
                score = intersection / union_count if union_count else 0.0
                if score < self.duplicate_threshold:
                    continue
                representative_memory, duplicate_memory = (
                    (left_memory, right_memory)
                    if left_memory.id < right_memory.id
                    else (right_memory, left_memory)
                )
                pair_output: JsonObject = {
                    "representative_id": representative_memory.id,
                    "duplicate_ids": [duplicate_memory.id],
                    "similarity": round(score, 8),
                    "match_basis": f"character-{self.ngram_size}-gram-jaccard",
                    "disposition": "retain",
                }
                proposal = self._proposal(
                    kind="merge",
                    target_ids=(representative_memory.id, duplicate_memory.id),
                    target_hashes={
                        representative_memory.id: _digest(_memory_payload(representative_memory)),
                        duplicate_memory.id: _digest(_memory_payload(duplicate_memory)),
                    },
                    output=pair_output,
                    reason="Character n-gram similarity only; human review is required.",
                    input_hash=input_hash,
                )
                output.append(
                    MergeProposal(
                        representative_id=representative_memory.id,
                        duplicate_ids=(duplicate_memory.id,),
                        match_basis=f"character-{self.ngram_size}-gram-jaccard",
                        confidence=round(score, 8),
                        proposal=proposal,
                    )
                )
        return output

    def suggest_layer(self, memory: MemoryDocument) -> Proposal:
        input_value = _memory_payload(memory)
        input_hash = self._input_hash(input_value)
        request = self._request("suggest_layer", input_value)
        if self.mode == "mock":
            seed = self._mock_seed(request)
            choices = (Layer.CURRENT, Layer.CONSOLIDATED, Layer.ANCHOR)
            selected = choices[int(seed[:8], 16) % len(choices)]
        else:
            raw_layer = request.get("layer")
            try:
                live_layer = Layer(raw_layer) if isinstance(raw_layer, str) else None
            except ValueError:
                live_layer = None
            if live_layer is None:
                raise ValueError("live suggest_layer response requires a valid layer")
            selected = live_layer
        output: JsonObject = {"layer": selected.value, "input_hash": _digest(input_value)}
        return self._proposal(
            kind="layer",
            target_ids=(memory.id,),
            target_hashes={memory.id: _digest(input_value)},
            output=output,
            reason=(
                "Deterministic mock suggestion; no semantic judgement was performed."
                if self.mode == "mock"
                else "Provider layer response; human review is required."
            ),
            input_hash=input_hash,
        )

    def wake_intensity(self, signal: str) -> float:
        request = self._request("wake_intensity", signal)
        if self.mode == "mock":
            seed = self._mock_seed(request)
            return int(seed[:8], 16) / 0xFFFFFFFF
        intensity = request.get("intensity")
        if (
            isinstance(intensity, bool)
            or not isinstance(intensity, (int, float))
            or not 0.0 <= intensity <= 1.0
        ):
            raise ValueError("live wake_intensity response requires intensity in [0, 1]")
        return float(intensity)


class ClaudeJudge(DeterministicJudge):
    provider = "claude"
    provider_env = "ANTHROPIC_API_KEY"


class OpenAIJudge(DeterministicJudge):
    provider = "openai"
    provider_env = "OPENAI_API_KEY"


class DeepSeekJudge(DeterministicJudge):
    provider = "deepseek"
    provider_env = "DEEPSEEK_API_KEY"


class MLXJudge(DeterministicJudge):
    provider = "mlx"
    # Local MLX inference has no provider credential. Live mode still requires
    # an explicitly injected transport, so this adapter never creates a client.
    provider_env = ""


from .mlx_live import (  # noqa: E402, I001
    LocalInference,
    LocalInferenceResult,
    LocalMLXJudge,
    Telemetry,
)


__all__ = [
    "ClaudeJudge",
    "DeepSeekJudge",
    "DeterministicJudge",
    "InjectedTransport",
    "JudgeAgent",
    "MLXJudge",
    "LocalInference",
    "LocalInferenceResult",
    "LocalMLXJudge",
    "MemoryAgent",
    "OpenAIJudge",
    "Proposal",
    "Telemetry",
    "MergeProposal",
    "mock_signature",
]
