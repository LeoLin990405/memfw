"""Fifth review round for ``memfw history``: each test fails on 186075b.

Secret-shaped values are assembled at runtime.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from memfw.history import SourceRoot, TranscriptIndex, has_secret, redact

AT_VALUE = "P" + "@ssw0rd"


@pytest.mark.parametrize(
    ("text", "hidden"),
    [
        ("DB_PASSWORD=" + AT_VALUE + "!", "ssw0rd"),
        ("password: " + AT_VALUE, "ssw0rd"),
        ("password=ab" + "@cdef1234", "cdef1234"),
        ("token: x" + "@abcdefghijklmn", "abcdefghijklmn"),
    ],
)
def test_values_with_an_at_sign_are_caught(text: str, hidden: str) -> None:
    assert has_secret(text)
    assert hidden not in redact(text)


def test_env_line_with_an_at_sign_is_not_indexed(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "s.jsonl").write_text(
        json.dumps({"session_id": "s", "text": "DB_PASSWORD=" + AT_VALUE + "!"})
        + "\n"
        + json.dumps({"session_id": "s", "text": "clean"})
        + "\n",
        "utf-8",
    )
    index = TranscriptIndex(tmp_path / "h.sqlite", [SourceRoot.create("r", root)])
    assert index.refresh()["skipped_secret"] == 1
    assert "ssw0rd" not in index.db.read_bytes().decode("utf-8", errors="replace")


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "redis://:" + "P#ss!w0rd99" + "@cache.example.net:6379/0",
            "redis://[REDACTED:credentials]@cache.example.net:6379/0",
        ),
        (
            "amqp://:" + "hunter2abc" + "@mq.example.net/vh",
            "amqp://[REDACTED:credentials]@mq.example.net/vh",
        ),
        (
            "postgres://:" + "hunter2abc" + "@db.example.net/app",
            "postgres://[REDACTED:credentials]@db.example.net/app",
        ),
        (
            "https://bob:" + "pw1234" + "@db.example.com:5432/app",
            "https://[REDACTED:credentials]@db.example.com:5432/app",
        ),
        (
            "git clone https://x-access-token:"
            + "gh"
            + "p_"
            + "A1b2C3d4" * 5
            + "@github.com/org/repo.git",
            "git clone https://[REDACTED:credentials]@github.com/org/repo.git",
        ),
        (
            "DATABASE_URL=postgres://app:" + "hunter2abc" + "@db.example.net:5432/app",
            "DATABASE_URL=postgres://[REDACTED:credentials]@db.example.net:5432/app",
        ),
    ],
)
def test_url_userinfo_including_an_empty_user(url: str, expected: str) -> None:
    assert has_secret(url)
    assert redact(url) == expected


@pytest.mark.parametrize(
    "text",
    [
        "token_type: bearer",
        "password_hash = bcrypt.hash(pw)",
        "api_key_header: X-API-Key",
        "secret_name: my-app-secret",
        "password_reset_url: https://example.org/reset",
        "secret_ref: vault-path",
        "Run git with --token option for auth.",
        "Pass --secret file.txt to the tool.",
        "Use --password ./secrets.env",
    ],
)
def test_descriptive_suffixes_and_flag_filenames_are_not_secrets(text: str) -> None:
    assert not has_secret(text)


@pytest.mark.parametrize(
    "text",
    [
        "SECRET_KEY_BASE=" + "abcdef" + "123456",
        "DB_PASSWORD_PROD=" + "hunter2" + "abc",
        "api_key_v2: " + "abcdef" + "123456",
        "--password " + "hunter2" + "abc",
        "--password correcthorsebattery",
    ],
)
def test_round4_detection_is_kept(text: str) -> None:
    assert has_secret(text)
