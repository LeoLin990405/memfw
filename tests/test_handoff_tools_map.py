"""Which tool calls count as file edits, per agent (synthetic summaries only)."""

from __future__ import annotations

import pytest

from memfw.handoff.tools_map import canonical_agent, files_from_calls, pairs
from memfw.history.parse import render

SECRET = "pass" + "word=correct-horse-battery-9"


def _call(name: str, args: object) -> dict[str, str]:
    return {"name": name, "summary": render(args)[:200]}


@pytest.mark.parametrize(
    ("agent", "call", "expected"),
    [
        (
            "claude",
            _call("Edit", {"file_path": "/work/demo/a.py", "old_string": "x"}),
            "/work/demo/a.py",
        ),
        (
            "claude",
            _call("NotebookEdit", {"notebook_path": "/work/demo/n.ipynb"}),
            "/work/demo/n.ipynb",
        ),
        (
            "codex",
            _call("apply_patch", {"input": "*** Begin Patch\n*** Add File: docs/new.md\n+x"}),
            "docs/new.md",
        ),
        (
            "opencode",
            _call("edit", {"filePath": "/work/demo/b.ts", "oldString": "y"}),
            "/work/demo/b.ts",
        ),
        ("gemini", _call("replace", {"file_path": "/work/demo/c.go"}), "/work/demo/c.go"),
        (
            "qwen",
            _call("notebook_edit", {"notebook_path": "/work/demo/q.ipynb", "cell_id": "c1"}),
            "/work/demo/q.ipynb",
        ),
        ("qwen", _call("edit", {"file_path": "/work/demo/q.py"}), "/work/demo/q.py"),
        (
            "kimi",
            _call("StrReplaceFile", {"path": "/work/demo/d.rs", "old": "z"}),
            "/work/demo/d.rs",
        ),
        (
            "cursor",
            _call("edit_file", {"target_file": "/work/demo/e.py", "code_edit": "q"}),
            "/work/demo/e.py",
        ),
        ("mystery-cli", _call("save_and_write", {"path": "/work/demo/f.txt"}), "/work/demo/f.txt"),
    ],
)
def test_edit_tools_per_agent_and_generic_fallback(
    agent: str, call: dict[str, str], expected: str
) -> None:
    assert files_from_calls(agent, [call]) == [expected]


def test_read_only_tools_contribute_nothing() -> None:
    calls = [
        _call("Read", {"file_path": "/work/demo/a.py"}),
        _call("Bash", {"command": "cat /work/demo/a.py"}),
        _call("grep", {"path": "/work/demo", "pattern": "x"}),
    ]
    assert files_from_calls("claude", calls) == []


def test_dedupe_order_and_limit() -> None:
    calls = [_call("Write", {"file_path": f"/work/demo/f{i % 5}.py"}) for i in range(20)]
    assert files_from_calls("claude", calls) == [f"/work/demo/f{i}.py" for i in range(5)]
    assert files_from_calls("claude", calls, limit=2) == ["/work/demo/f0.py", "/work/demo/f1.py"]


def test_multiple_patch_headers() -> None:
    patch = "*** Begin Patch\n*** Update File: src/a.py\n@@\n*** Delete File: src/b.py\n"
    assert files_from_calls("codex", [_call("apply_patch", {"input": patch})]) == [
        "src/a.py",
        "src/b.py",
    ]


def test_value_cut_by_the_store_is_not_trusted() -> None:
    long_tail = "/work/demo/" + "d" * 300 + "/file.py"
    summary = render({"old_string": "x" * 150, "file_path": long_tail})[:200]
    assert len(summary) == 200
    assert files_from_calls("claude", [{"name": "Edit", "summary": summary}]) == []
    assert (
        files_from_calls("claude", [{"name": "Edit", "summary": "file_path: /work/demo/x…"}]) == []
    )


def test_secret_shaped_path_dropped_and_home_user_masked() -> None:
    calls = [
        {"name": "Write", "summary": f"file_path: /work/demo/{SECRET}.txt"},
        _call("Write", {"file_path": "/" + "Users" + "/someone/work/demo/a.py"}),
    ]
    files = files_from_calls("claude", calls)
    assert len(files) == 1 and "someone" not in files[0] and files[0].endswith("/work/demo/a.py")


def test_aliases_and_pairs() -> None:
    assert canonical_agent("Claude-Code") == "claude"
    assert canonical_agent("zcode") == "glm"
    assert canonical_agent("not-registered") == "not-registered"
    assert pairs("file_path: /a; b c; content: x; y") == {"file_path": "/a; b c", "content": "x; y"}
