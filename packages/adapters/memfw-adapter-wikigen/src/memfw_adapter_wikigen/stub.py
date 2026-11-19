"""A deterministic, model-free ``ModelBackend`` for tests, smoke runs and dry runs.

``memfw_adapter_wikigen.stub:factory`` answers :data:`~.deriver.TASK` without
any model: it reads the prompt's data block back and copies the first
sentence of each source, citing that source (``extractive`` mode), or replays
canned replies (``replies`` option, in order, the last one repeated). It is
``local``, talks to nothing, and reports a fixed digest derived from its
model name, so jobs built for it have a stable ``job_digest``.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ._ports import BackendUnavailable, ModelReply
from .deriver import TASK, estimate_tokens
from .prompt import read_data

_SENTENCE = re.compile(r"[^.!?。！？\n]+[.!?。！？]?")
MAX_SENTENCE = 300


def stub_digest(model: str) -> str:
    return "sha256:" + hashlib.sha256(f"memfw-wikigen-stub/{model}".encode()).hexdigest()


def _first_sentence(text: str) -> str:
    match = _SENTENCE.search(" ".join(text.split()))
    sentence = (match.group(0) if match else text).strip()
    return sentence[:MAX_SENTENCE]


def extractive_reply(data: Mapping[str, Any]) -> dict[str, Any]:
    """One cited sentence per source: the first current one as the summary."""

    conflicted = {
        ref for question in data.get("open_questions", []) for ref in question["conflict_refs"]
    }
    out: dict[str, list[dict[str, Any]]] = {"summary": [], "facts": [], "timeline": []}
    for source in data.get("sources", []):
        text = _first_sentence(source["text"])
        if not text:
            continue
        ref = source["ref"]
        if source["status"] != "current":
            out["timeline"].append({"text": text, "cites": [ref], "kind": "superseded"})
        elif ref in conflicted:
            continue
        elif not out["summary"]:
            out["summary"].append({"text": text, "cites": [ref]})
        else:
            out["facts"].append({"text": text, "cites": [ref]})
    return out


@dataclass
class StubBackend:
    model: str = "stub-extractive"
    digest: str = ""
    replies: Sequence[str] = ()
    local: bool = True
    tasks: tuple[str, ...] = (TASK,)

    def __post_init__(self) -> None:
        if not isinstance(self.model, str) or not re.fullmatch(
            r"[a-z0-9][a-z0-9._-]{0,63}", self.model
        ):
            raise BackendUnavailable("bad_option")
        if not isinstance(self.replies, (list, tuple)) or not all(
            isinstance(reply, str) for reply in self.replies
        ):
            raise BackendUnavailable("bad_option")
        self.replies = tuple(self.replies)
        self.name = f"stub:{self.model}"
        self.digest = self.digest or stub_digest(self.model)
        self.calls: list[dict[str, Any]] = []

    def model_digest(self) -> str:
        return self.digest

    def run(self, task: str, data: dict[str, Any], *, max_tokens: int) -> ModelReply:
        if task != TASK:
            raise ValueError(f"stub backend does not take task {task}")
        prompt = data.get("prompt")
        if not isinstance(prompt, str):
            raise ValueError("data must hold a 'prompt' string")
        self.calls.append(dict(data))
        if self.replies:
            text = self.replies[min(len(self.calls), len(self.replies)) - 1]
        else:
            text = json.dumps(extractive_reply(read_data(prompt)), ensure_ascii=False)
        return ModelReply(text, estimate_tokens(prompt), estimate_tokens(text))


def factory(options: Mapping[str, Any] | None = None) -> StubBackend:
    """The backend for ``backend: "memfw_adapter_wikigen.stub:factory"``."""

    options = dict(options or {})
    if set(options) - {"model", "digest", "replies"}:
        raise BackendUnavailable("bad_option")
    replies = options.pop("replies", ())
    return StubBackend(replies=tuple(replies) if isinstance(replies, list) else replies, **options)


__all__ = ["StubBackend", "extractive_reply", "factory", "stub_digest"]
