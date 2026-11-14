"""The optional model part of a handoff pack, with fake backends only."""

from __future__ import annotations

import json
from typing import Any

import pytest

from memfw.conversations.backends import ModelReply, load_backend
from memfw.handoff.schema import MODEL_KEYS, MODEL_LABEL
from memfw.handoff.summarize import (
    INSTRUCTIONS,
    TASK,
    ReplyError,
    SummarizeError,
    summarize,
    validate_reply,
)

SECRET = "pass" + "word=correct-horse-battery-9"
EMAIL = "demo.user" + "@" + "example.com"


class Fake:
    name = "fake"
    local = True

    def __init__(self, reply: str = "", error: Exception | None = None) -> None:
        self.reply = reply
        self.error = error
        self.calls: list[tuple[str, dict[str, Any], int]] = []

    def run(self, task: str, data: dict[str, Any], *, max_tokens: int) -> ModelReply:
        self.calls.append((task, data, max_tokens))
        if self.error:
            raise self.error
        return ModelReply(text=self.reply)


def _valid(**overrides: Any) -> str:
    value: dict[str, Any] = {key: [f"{key} one"] for key in MODEL_KEYS}
    value.update(overrides)
    return json.dumps(value)


def _run(fake: Fake) -> dict[str, Any]:
    return summarize(
        "tests:fake",
        goal="add a flag",
        turns=[{"role": "user", "text": "add a flag"}],
        loader=lambda spec, allow_cloud=False: fake,
    )


def test_valid_reply_is_kept_and_backend_called_once() -> None:
    fake = Fake(_valid())
    part = _run(fake)
    assert part["status"] == "ok" and part["label"] == MODEL_LABEL and part["model"] == "fake"
    assert all(part[key] == [f"{key} one"] for key in MODEL_KEYS)
    assert len(fake.calls) == 1
    task, data, _tokens = fake.calls[0]
    assert task == TASK and data["instructions"] == INSTRUCTIONS
    assert set(data) == {"instructions", "schema", "goal", "turns"}


def test_fenced_json_accepted() -> None:
    assert _run(Fake("```json\n" + _valid() + "\n```"))["status"] == "ok"


@pytest.mark.parametrize(
    ("reply", "reason"),
    [
        ("{not json", "invalid_json"),
        ("[]", "schema"),
        (_valid(extra=["x"]), "schema"),
        (json.dumps({key: [] for key in MODEL_KEYS[:-1]}), "schema"),
        (_valid(done=["x"] * 11), "schema"),
        (_valid(done=["x" * 201]), "schema"),
        (_valid(done="not a list"), "schema"),
        (_valid(done=[3]), "schema"),
        (json.dumps({key: ["y" * 190] * 10 for key in MODEL_KEYS}), "too_large"),
        (_valid(pitfalls=[f"use {SECRET}"]), "secret"),
    ],
)
def test_bad_replies_are_dropped_not_repaired(reply: str, reason: str) -> None:
    part = _run(Fake(reply))
    assert part == {"label": MODEL_LABEL, "status": "dropped", "reason": reason}


def test_personal_data_in_items_is_redacted() -> None:
    part = _run(Fake(_valid(next=[f"ask {EMAIL}"])))
    assert part["status"] == "ok" and EMAIL not in json.dumps(part)


def test_backend_error_is_dropped_without_its_message() -> None:
    part = _run(Fake(error=RuntimeError("boom with detail")))
    assert part == {"label": MODEL_LABEL, "status": "dropped", "reason": "backend_error"}


@pytest.mark.parametrize("spec", [None, "", "extractive", "Extractive"])
def test_extractive_or_missing_spec_refused(spec: str | None) -> None:
    with pytest.raises(SummarizeError):
        summarize(spec, goal="g", turns=[])


def test_loader_errors_become_summarize_errors() -> None:
    def loader(spec: str, allow_cloud: bool = False) -> Any:
        raise ValueError("cannot load backend x:y")

    with pytest.raises(SummarizeError):
        summarize("x:y", goal="g", turns=[], loader=loader)


class Cloud:
    name = "cloud"
    local = False

    def run(self, task: str, data: dict[str, Any], *, max_tokens: int) -> ModelReply:
        raise AssertionError("must not be called")


def make_cloud() -> Cloud:
    return Cloud()


def test_non_local_backend_needs_allow_cloud() -> None:
    spec = f"{__name__}:make_cloud"
    with pytest.raises(SummarizeError):
        summarize(spec, goal="g", turns=[])
    assert load_backend(spec, allow_cloud=True).name == "cloud"


def test_validate_reply_reason() -> None:
    with pytest.raises(ReplyError) as error:
        validate_reply("nope")
    assert error.value.reason == "invalid_json"
    assert validate_reply(_valid())["done"] == ["done one"]
