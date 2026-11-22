"""Conv-v2 chunk_summary replies keep omitted tag keys (rev 32).

An absent tag key is filled and the reply is kept. A present empty
``task_type`` is not that default. No network. No DeepSeek API. No API key.

These checks do not decide that the work is done.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from memfw.conversations.backends import (
    defaulted_tag_keys,
    dropped_tag_items,
    parse_reply,
    reply_issue,
)
from memfw.ports.models import ModelReply

_SUMMARY = "Kept the chunk."
_TAG_ORDER = ("project", "task_type", "components", "intent", "outcome")
_DEFAULTS: dict[str, Any] = {
    "project": [],
    "task_type": "other",
    "components": [],
    "intent": "",
    "outcome": "unknown",
}


def _tags(**overrides: Any) -> dict[str, Any]:
    tags: dict[str, Any] = {
        "project": ["uploader"],
        "task_type": "dev",
        "components": ["retry"],
        "intent": "fix the retry",
        "outcome": "done",
    }
    tags.update(overrides)
    return tags


def _reply(tags: Any, summary: str = _SUMMARY) -> str:
    return json.dumps({"summary": summary, "tags": tags})


def _issue(text: str, *, finish_reason: str | None = None) -> str:
    if finish_reason is None:
        return reply_issue("chunk_summary", text, prompt_version="conv-v2")
    return reply_issue(
        "chunk_summary",
        text,
        prompt_version="conv-v2",
        finish_reason=finish_reason,
    )


def _parsed(text: str) -> Any:
    return parse_reply("chunk_summary", text, prompt_version="conv-v2")


def _assert_names_omit_summary(names: tuple[str, ...]) -> None:
    assert _SUMMARY not in names
    assert _SUMMARY not in " ".join(names)


def test_all_omitted_tag_keys_are_kept_and_named_in_order() -> None:
    text = _reply({})
    assert _issue(text) == ""
    parsed = _parsed(text)
    assert parsed is not None
    assert parsed["summary"] == _SUMMARY
    assert parsed["tags"] == _DEFAULTS
    names = defaulted_tag_keys(text)
    assert names == _TAG_ORDER
    _assert_names_omit_summary(names)


def test_defaulted_tag_keys_follow_canonical_order_not_json_order() -> None:
    text = _reply({"intent": "fix the retry", "task_type": "dev"})
    names = defaulted_tag_keys(text)
    assert names == ("project", "components", "outcome")
    _assert_names_omit_summary(names)
    assert _issue(text) == ""
    parsed = _parsed(text)
    assert parsed is not None
    assert parsed["tags"]["project"] == []
    assert parsed["tags"]["components"] == []
    assert parsed["tags"]["outcome"] == "unknown"
    assert parsed["tags"]["task_type"] == "dev"
    assert parsed["tags"]["intent"] == "fix the retry"


@pytest.mark.parametrize("key", _TAG_ORDER)
def test_one_absent_tag_key_is_kept_and_named(key: str) -> None:
    tags = _tags()
    del tags[key]
    text = _reply(tags)
    names = defaulted_tag_keys(text)
    assert names == (key,)
    _assert_names_omit_summary(names)
    assert _issue(text) == ""
    parsed = _parsed(text)
    assert parsed is not None
    assert parsed["tags"][key] == _DEFAULTS[key]
    for other in _TAG_ORDER:
        if other != key:
            assert parsed["tags"][other] == tags[other]


def test_present_default_values_are_not_listed_as_absent() -> None:
    text = _reply(dict(_DEFAULTS))
    assert defaulted_tag_keys(text) == ()
    assert _issue(text) == ""
    parsed = _parsed(text)
    assert parsed is not None
    assert parsed["tags"]["project"] == []
    assert parsed["tags"]["task_type"] == "other"
    assert parsed["tags"]["components"] == []
    assert parsed["tags"]["intent"] == ""
    assert parsed["tags"]["outcome"] == "unknown"


def test_present_empty_task_type_is_bad_enum_not_the_default() -> None:
    text = _reply(_tags(task_type=""))
    assert _issue(text) == "bad_enum"
    assert _parsed(text) is None
    assert defaulted_tag_keys(text) == ()


def test_task_type_coding_is_bad_enum() -> None:
    text = _reply(_tags(task_type="coding"))
    assert _issue(text) == "bad_enum"
    assert _parsed(text) is None


def test_unknown_tag_key_extra_is_bad_shape() -> None:
    tags = _tags()
    tags["extra"] = "x"
    text = _reply(tags)
    assert _issue(text) == "bad_shape"
    assert _parsed(text) is None


@pytest.mark.parametrize("tags", ["t", None])
def test_tags_that_are_not_an_object_are_bad_shape(tags: str | None) -> None:
    text = _reply(tags)
    code = _issue(text)
    assert code == "bad_shape"
    assert code != ""
    assert _parsed(text) is None


def test_project_url_is_dropped_and_the_reply_is_kept() -> None:
    """Rev 32 supplement: drop the location item. Do not reject the reply."""

    raw = "https://example.com/a"
    text = _reply(_tags(project=["uploader", raw], components=["retry", "~/notes"]))
    assert _issue(text) == ""
    parsed = _parsed(text)
    assert parsed is not None
    assert parsed["summary"] == _SUMMARY
    assert parsed["tags"]["project"] == ["uploader"]
    assert parsed["tags"]["components"] == ["retry"]
    assert dropped_tag_items(text) == 2
    blob = json.dumps(parsed)
    assert raw not in blob and "notes" not in blob


def test_finish_reason_length_is_truncated_even_when_json_parses() -> None:
    text = _reply(_tags())
    assert _parsed(text) is not None
    assert _issue(text, finish_reason="length") == "truncated"


def test_fully_valid_reply_is_kept_and_defaults_nothing() -> None:
    text = _reply(_tags())
    assert _issue(text) == ""
    parsed = _parsed(text)
    assert parsed is not None
    assert parsed["summary"] == _SUMMARY
    assert parsed["tags"]["project"] == ["uploader"]
    assert parsed["tags"]["task_type"] == "dev"
    assert parsed["tags"]["components"] == ["retry"]
    assert parsed["tags"]["intent"] == "fix the retry"
    assert parsed["tags"]["outcome"] == "done"
    assert defaulted_tag_keys(text) == ()


def test_model_reply_finish_reason_defaults_to_empty() -> None:
    assert ModelReply("ok", 1, 2).finish_reason == ""
    assert ModelReply("ok", 1, 2, "stop").finish_reason == "stop"
