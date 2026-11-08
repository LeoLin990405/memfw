"""Cross-model annotation candidates with a strict human fallback.

LLM labels are accepted only when two independent judges agree, confidence is
high, and the externally measured human kappa exceeds the configured gate. They
never enter the human golden file and never train anything.
"""

from __future__ import annotations

import datetime as dt
import json
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import yaml

from . import http as notes_http


@dataclass(frozen=True)
class JudgeVerdict:
    answerable: bool
    relevant_paths: tuple[str, ...]
    confidence: float
    model: str


class Judge(Protocol):
    name: str

    def judge(self, query: str, candidates: Sequence[Mapping[str, str]]) -> JudgeVerdict: ...


class OpenAIJudge:
    """Minimal OpenAI-compatible chat judge. Caller supplies endpoint/key."""

    def __init__(
        self,
        endpoint: str,
        *,
        model: str,
        api_key: str | None = None,
        timeout: float = 120.0,
    ) -> None:
        self.endpoint = endpoint
        self.name = model
        self.api_key = api_key
        self.timeout = timeout

    def judge(self, query: str, candidates: Sequence[Mapping[str, str]]) -> JudgeVerdict:
        lines = [
            f"Query: {query}",
            "Candidates:",
        ]
        for index, candidate in enumerate(candidates, start=1):
            lines.append(
                f"[{index}] {candidate.get('path', '')} | {candidate.get('title', '')} "
                f"| {candidate.get('heading', '')}\n"
                f"{candidate.get('content', '')[:500]}"
            )
        prompt = "\n\n".join(lines) + (
            '\n\nReturn JSON: {"answerable": boolean, "relevant": [candidate numbers], '
            '"confidence": number 0..1}. Only direct answers.'
        )
        body = json.dumps(
            {
                "model": self.name,
                "messages": [
                    {"role": "system", "content": "Independent retrieval annotator. JSON only."},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0,
                "max_tokens": 800,
            }
        ).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(self.endpoint, data=body, headers=headers)
        with notes_http.open_http(request, timeout=self.timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        text = payload["choices"][0]["message"].get("content") or ""
        if text.strip().startswith("```"):
            text = text.strip().strip("`")
            if text.startswith("json"):
                text = text[4:].lstrip()
        parsed = json.loads(text)
        indices = parsed.get("relevant") or []
        paths = []
        for value in indices:
            try:
                position = int(value)
            except (TypeError, ValueError):
                continue
            if 1 <= position <= len(candidates):
                paths.append(str(candidates[position - 1].get("path", "")))
        confidence = float(parsed.get("confidence") or 0.0)
        return JudgeVerdict(
            answerable=bool(parsed.get("answerable")),
            relevant_paths=tuple(dict.fromkeys(paths)),
            confidence=max(0.0, min(confidence, 1.0)),
            model=self.name,
        )


@dataclass(frozen=True)
class AILabel:
    query: str
    expected_paths: tuple[str, ...]
    answerable: bool
    confidence: float
    judges: tuple[str, ...]
    created_at: str


@dataclass
class CrossAnnotationResult:
    accepted: list[AILabel] = field(default_factory=list)
    human_queue: list[dict[str, Any]] = field(default_factory=list)
    disagreements: int = 0
    human_kappa: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "accepted": len(self.accepted),
            "human_queue": len(self.human_queue),
            "disagreements": self.disagreements,
            "human_kappa": self.human_kappa,
            "auto_ratio": len(self.accepted) / (len(self.accepted) + len(self.human_queue))
            if self.accepted or self.human_queue
            else 0.0,
        }


def cross_annotate(
    items: Sequence[tuple[str, Sequence[Mapping[str, str]]]],
    judge_a: Judge,
    judge_b: Judge,
    *,
    human_kappa: float,
    kappa_gate: float = 0.745,
    min_confidence: float = 0.8,
    now: str | None = None,
) -> CrossAnnotationResult:
    """Judge each query twice; only unanimous, high-confidence labels are accepted."""
    result = CrossAnnotationResult(human_kappa=human_kappa)
    if human_kappa <= kappa_gate:
        result.human_queue = [
            {"query": query, "reason": "human_kappa_below_gate"} for query, _ in items
        ]
        return result
    timestamp = now or dt.datetime.now().isoformat(timespec="seconds")
    for query, candidates in items:
        try:
            a = judge_a.judge(query, candidates)
            b = judge_b.judge(query, candidates)
        except Exception as exc:  # noqa: BLE001 - fallback boundary
            result.human_queue.append({"query": query, "reason": f"judge_error: {exc}"})
            continue
        same = a.answerable == b.answerable and set(a.relevant_paths) == set(b.relevant_paths)
        confidence = min(a.confidence, b.confidence)
        if same and confidence >= min_confidence:
            result.accepted.append(
                AILabel(
                    query=query,
                    expected_paths=a.relevant_paths,
                    answerable=a.answerable,
                    confidence=confidence,
                    judges=(a.model, b.model),
                    created_at=timestamp,
                )
            )
        else:
            result.disagreements += 1
            result.human_queue.append(
                {
                    "query": query,
                    "reason": "judge_disagreement" if not same else "low_confidence",
                    "judge_a": a.relevant_paths,
                    "judge_b": b.relevant_paths,
                    "confidence": confidence,
                }
            )
    return result


def write_annotation_files(
    result: CrossAnnotationResult,
    *,
    ai_path: Path | str,
    human_queue_path: Path | str,
) -> None:
    ai = Path(ai_path)
    ai.parent.mkdir(parents=True, exist_ok=True)
    ai.write_text(
        yaml.safe_dump(
            {
                "annotator": "ai-cross-model",
                "authority": "candidate-only; not human golden",
                "items": [
                    {
                        "query": item.query,
                        "expected_paths": list(item.expected_paths),
                        "answerable": item.answerable,
                        "confidence": item.confidence,
                        "judges": list(item.judges),
                        "created_at": item.created_at,
                    }
                    for item in result.accepted
                ],
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    queue = Path(human_queue_path)
    queue.parent.mkdir(parents=True, exist_ok=True)
    queue.write_text(
        yaml.safe_dump({"items": result.human_queue}, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


__all__ = [
    "AILabel",
    "CrossAnnotationResult",
    "Judge",
    "JudgeVerdict",
    "OpenAIJudge",
    "cross_annotate",
    "write_annotation_files",
]
