"""Public contract checks for conv-v2 defaults, project aliases, prompts, and reply codes.

No network. No DeepSeek API. No API key. These checks do not decide that the
work is done.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from memfw.conversations.backends import reply_issue
from memfw.ports.models import BackendUnavailable, map_project_names, render_tag_prompt

ROOT = Path(__file__).resolve().parents[1]
ADAPTER_SRC = ROOT / "packages" / "adapters" / "memfw-adapter-deepseek" / "src"
if str(ADAPTER_SRC) not in sys.path:  # workspace member, not installed in this venv
    sys.path.insert(0, str(ADAPTER_SRC))

from memfw_adapter_deepseek import DEFAULT_MODEL, Settings  # noqa: E402

VOCAB = {"Northwind": ["north wind", "northwind-app"], "Lumen": ["lumen-notes"]}
_TAG_KEYS = ("project", "task_type", "components", "intent", "outcome")


def _tags(**overrides: Any) -> dict[str, Any]:
    tags: dict[str, Any] = {
        "project": ["Northwind"],
        "task_type": "dev",
        "components": ["worker"],
        "intent": "fix the retry",
        "outcome": "done",
    }
    tags.update(overrides)
    return tags


def _reply(summary: str | None, tags: dict[str, Any]) -> str:
    body: dict[str, Any] = {"tags": tags}
    if summary is not None:
        body = {"summary": summary, "tags": tags}
    return json.dumps(body)


def _valid_reply() -> str:
    return _reply("Fixed the retry.", _tags())


def _issue(text: str, *, finish_reason: str | None = None) -> str:
    """Return the issue code. ``finish_reason`` defaults to empty when omitted."""

    if finish_reason is None:
        code = reply_issue("chunk_summary", text, prompt_version="conv-v2")
    else:
        code = reply_issue(
            "chunk_summary",
            text,
            prompt_version="conv-v2",
            finish_reason=finish_reason,
        )
    assert text not in code
    return code


def test_default_model_is_deepseek_flash() -> None:
    assert DEFAULT_MODEL == "deepseek-flash"
    assert Settings().model == "deepseek-flash"


def test_settings_from_options_refuses_an_api_key() -> None:
    with pytest.raises(BackendUnavailable) as raised:
        Settings.from_options({"api_key": "not-a-real-key"})
    assert raised.value.code == "key_in_config"


def test_settings_from_options_accepts_an_explicit_model() -> None:
    assert Settings.from_options({"model": "deepseek-v4-pro"}).model == "deepseek-v4-pro"


def test_map_project_names_folds_aliases_and_counts_unmapped() -> None:
    for alias in ("north wind", " North  Wind ", "NORTHWIND-APP", "northwind"):
        assert map_project_names([alias], VOCAB) == (["northwind"], 0)
    assert map_project_names(["other-tool"], VOCAB) == (["other-tool"], 1)
    assert map_project_names(["  Other   Tool "], VOCAB) == (["other tool"], 1)
    assert map_project_names(["lumen-notes", "north wind"], VOCAB) == (["lumen", "northwind"], 0)
    assert map_project_names(["other-tool"], {}) == (["other-tool"], 0)


def test_render_tag_prompt_defaults_to_chinese() -> None:
    prompted = render_tag_prompt(
        "chunk_summary",
        {"turns": []},
        language="zh",
        projects=("Northwind",),
    )[0]
    assert "Chinese" in prompted
    assert "Northwind" in prompted
    assert "proper nouns" in prompted
    assert "Chinese" in render_tag_prompt("chunk_summary", {"turns": []})[0]


def test_reply_issue_reports_structural_codes_without_echoing_model_text() -> None:
    missing_intent = _tags()
    del missing_intent["intent"]
    cases = (
        ("not json", "not_json"),
        (_reply(None, _tags()), "missing_summary"),
        (_reply("x" * 801, _tags()), "summary_too_long"),
        (_reply("ok", missing_intent), "missing_tag_key"),
        (_reply("ok", _tags(task_type="coding")), "bad_enum"),
    )
    assert set(missing_intent) == set(_TAG_KEYS) - {"intent"}
    for text, expected in cases:
        assert _issue(text) == expected


def test_reply_issue_url_code_does_not_contain_the_host() -> None:
    raw = "https://example.com/a"
    text = _reply("ok", _tags(project=[raw]))
    code = _issue(text)
    assert code == "item_looks_like_url"
    assert "example.com" not in code
    assert raw not in code


def test_reply_issue_length_is_truncated_even_when_json_is_valid() -> None:
    text = _valid_reply()
    code = _issue(text, finish_reason="length")
    assert code == "truncated"


def test_reply_issue_valid_json_has_no_code() -> None:
    text = _valid_reply()
    assert _issue(text) == ""
    assert _issue(text, finish_reason="") == ""
