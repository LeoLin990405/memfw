"""parse_reply stays aligned with main on the seed-7 sample.

The generator follows the 4000-case probe (seed 7): random replies, random
prompt versions, dropped keys, an extra key, truncated JSON, a fence, or a
blank. It does not import anything outside this repository. Main's
``parse_reply`` is executed from commit b7ca370 in a subprocess.

An omitted tag key is filled. A project or component item that looks like a
URL or a path is dropped and the reply is kept. Both match main. Any
disagreement fails this test.
"""

from __future__ import annotations

import json
import random
import subprocess
import sys
from pathlib import Path
from typing import Any

from memfw.conversations.backends import parse_reply

ROOT = Path(__file__).resolve().parents[1]
MAIN = "b7ca370410a56faf3ba14b35e3a08a4d49eda588"
TASKS = ("chunk_summary", "session_summary", "memory_claims")
VERSIONS = ("conv-v1", "conv-v2")
TAG_KEYS = ("project", "task_type", "components", "intent", "outcome")


def _cases() -> list[tuple[str, str, str]]:
    """The same 4000 replies as the seed-7 probe. A path is built, not a home literal."""

    rng = random.Random(7)
    home = "/" + "Users/" + "x/y"
    values: list[Any] = [
        None,
        "",
        "  ",
        "dev",
        "DEV",
        "Review",
        "bogus",
        3,
        [],
        ["memfw", "Conductor 修复"],
        ["x" * 300],
        "x" * 900,
        "梯度下降的实现细节。",
        ["https://e.com/a"],
        [home],
        {"a": 1},
        "done",
        "ongoing",
        "unknown",
        "success",
        ["ok", 5],
        "sk-abcdefghijklmnopqrstuvwxyz123456",
    ]
    cases: list[tuple[str, str, str]] = []
    for _ in range(4000):
        task = rng.choice(TASKS)
        version = rng.choice(VERSIONS)
        if task == "memory_claims":
            obj: dict[str, Any] = {
                "claims": rng.choice([["a claim"], [], ["x"] * 6, "nope", [1], ["好的结论"]])
            }
        else:
            obj = {"summary": rng.choice(values)}
            if version == "conv-v2":
                sampled = []
                for _ in range(3):
                    picked = rng.sample(TAG_KEYS, rng.randint(0, 5))
                    sampled.append({key: rng.choice(values) for key in picked})
                obj["tags"] = rng.choice([{}, None, "t", *sampled])
                if rng.random() < 0.1 and isinstance(obj["tags"], dict):
                    obj["tags"]["extra"] = 1
            else:
                if rng.random() < 0.5:
                    obj["intent"] = rng.choice(values)
                if rng.random() < 0.5:
                    obj["outcome"] = rng.choice(values)
        for key in list(obj):
            if rng.random() < 0.08:
                del obj[key]
        if rng.random() < 0.05:
            obj["junk"] = 1
        text = json.dumps(obj, ensure_ascii=False)
        roll = rng.random()
        if roll < 0.05:
            text = text[: len(text) // 2]
        elif roll < 0.08:
            text = "```json\n" + text + "\n```"
        elif roll < 0.1:
            text = "   "
        cases.append((task, version, text))
    return cases


def _canon(value: Any) -> Any:
    if isinstance(value, str) and value.startswith("EXC:"):
        return value
    return json.loads(json.dumps(value, default=str))


def _current(cases: list[tuple[str, str, str]]) -> list[Any]:
    out = []
    for task, version, text in cases:
        try:
            parsed = parse_reply(task, text, prompt_version=version)
        except Exception as exc:  # a crash is a disagreement, not a pass
            parsed = "EXC:" + type(exc).__name__
        out.append(_canon(parsed))
    return out


def _main(cases: list[tuple[str, str, str]], tmp: Path) -> list[Any]:
    archive = tmp / "main-src"
    archive.mkdir()
    tar_path = tmp / "main.tar"
    with tar_path.open("wb") as handle:
        subprocess.run(
            ["git", "archive", MAIN, "src/memfw"],
            cwd=ROOT,
            check=True,
            stdout=handle,
        )
    subprocess.run(["tar", "-xf", str(tar_path), "-C", str(archive)], check=True)
    script = r"""
import json, sys
sys.path.insert(0, sys.argv[1])
import memfw
from memfw.conversations.backends import parse_reply
assert memfw.__file__.startswith(sys.argv[1]), memfw.__file__
cases = json.loads(sys.stdin.read())
out = []
for task, version, text in cases:
    try:
        parsed = parse_reply(task, text, prompt_version=version)
    except Exception as exc:
        parsed = "EXC:" + type(exc).__name__
    out.append(parsed)
json.dump(out, sys.stdout, default=str)
"""
    completed = subprocess.run(
        [sys.executable, "-c", script, str(archive / "src")],
        input=json.dumps(cases),
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stderr[-2000:])
    return [_canon(item) for item in json.loads(completed.stdout)]


def test_parse_reply_matches_main(tmp_path: Path) -> None:
    cases = _cases()
    assert len(cases) == 4000
    current = _current(cases)
    baseline = _main(cases, tmp_path)
    assert len(baseline) == 4000
    mismatched = [
        index
        for index, (got, expected) in enumerate(zip(current, baseline, strict=True))
        if got != expected
    ]
    assert not mismatched, f"mismatched={len(mismatched)} first={mismatched[:5]}"
