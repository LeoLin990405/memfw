"""Fourth review round for ``memfw history``: each test fails on e2b0b6b.

Fixtures are synthetic and live under ``tmp_path``; secret-shaped values are
assembled at runtime.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest

import memfw.history.index as index_module
from memfw.history import HistoryError, SourceRoot, TranscriptIndex, has_secret, redact

VALUE = "hunter2" + "abc"
TOKEN = "abcdef" + "123456"
GHP = "gh" + "p_" + "A1b2C3d4" * 5


def _line(**fields: Any) -> str:
    return json.dumps(fields) + "\n"


def _setup(tmp_path: Path) -> tuple[Path, TranscriptIndex]:
    root = tmp_path / "root"
    root.mkdir()
    return root, TranscriptIndex(tmp_path / "h.sqlite", [SourceRoot.create("r", root)])


# -- blocker: long dotted / underscored keys -------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "spring.security.oauth2.client.registration.google.client-secret=GOCSPX-" + TOKEN,
        "spring.datasource.hikari.data-source-properties.password=" + VALUE,
        "SPRING_DATASOURCE_HIKARI_CONNECTION_PASSWORD=" + VALUE,
        "my_app.config.services.payment.gateway.provider.api_key=" + TOKEN,
        "MY_VERY_LONG_SERVICE_NAME_PRODUCTION_DB_PASSWORD=" + VALUE,
    ],
)
def test_long_prefixed_keys_are_caught(text: str) -> None:
    assert has_secret(text)
    redacted = redact(text)
    assert VALUE not in redacted and TOKEN not in redacted


@pytest.mark.parametrize(
    "text",
    [
        "a" * 1_048_576,
        "a." * 524_288,
        (" " + "a" * 127) * 8192,
        (" " + "a." * 63 + "password") * 7500,
        " password_a_b_c_d_e" * 55_000,
        " password1234567" * 65_000,
        " --password " * 87_000,
        "password =>" * 95_000,
        "password: |\n" * 87_000,
    ],
)
def test_wider_key_patterns_stay_linear(text: str) -> None:
    started = time.perf_counter()
    has_secret(text)
    assert time.perf_counter() - started < 1.0


# -- suffixes and separators -------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "SECRET_KEY_BASE=" + TOKEN,
        "DB_PASSWORD_PROD=" + VALUE,
        "password1=" + VALUE,
        "api_key_v2: " + TOKEN,
        "--password " + VALUE,
        'password => "' + VALUE + '"',
        "PASSWORD: |\n  " + VALUE,
    ],
)
def test_key_suffixes_and_other_separators(text: str) -> None:
    assert has_secret(text)
    assert VALUE not in redact(text) and TOKEN not in redact(text)


@pytest.mark.parametrize(
    "text",
    ["tokens: abcdef", "--token-file path/x", "use --password-stdin", "pass: ok"],
)
def test_suffix_rules_keep_benign_shapes_clean(text: str) -> None:
    assert not has_secret(text)


# -- URL credentials keep host, port and path ----------------------------------------------


def test_url_credentials_mask_only_the_userinfo() -> None:
    url = "https://" + "bob" + ":" + "pw1234" + "@db.example.com:5432/app"
    assert redact(url) == "https://[REDACTED:credentials]@db.example.com:5432/app"
    clone = "git clone https://x-access-token:" + GHP + "@github.com/org/repo.git"
    assert redact(clone) == "git clone https://[REDACTED:credentials]@github.com/org/repo.git"
    email = "name+sk-" + "ABCDEFGHIJKLMNOPQRSTUVWX" + "@example" + ".com"
    assert "example" not in redact(email)


# -- failures bookkeeping ------------------------------------------------------------------------


def test_failure_rows_follow_the_file_and_show_in_stats(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, index = _setup(tmp_path)
    (root / "ok.jsonl").write_text(_line(session_id="o", text="fine"), "utf-8")
    bad = root / "b.jsonl"
    bad.write_text(_line(session_id="b", text="BOOM"), "utf-8")
    original = index_module.parse_line

    def exploding(line: str, *, jsonl: bool) -> Any:
        if "BOOM" in line:
            raise RuntimeError("synthetic parser failure")
        return original(line, jsonl=jsonl)

    monkeypatch.setattr(index_module, "parse_line", exploding)
    index.refresh()
    index.refresh()
    assert index.stats()["files_failing"] == 1
    bad.unlink()
    index.refresh()
    assert index.stats()["files_failing"] == 0
    with sqlite3.connect(index.db) as connection:
        assert connection.execute("SELECT COUNT(*) FROM failures").fetchone()[0] == 0


# -- read's stale drop during a refresh --------------------------------------------------------


def test_stale_drop_during_a_refresh_is_a_history_error(tmp_path: Path) -> None:
    root, index = _setup(tmp_path)
    path = root / "s.jsonl"
    path.write_text(_line(session_id="s", text="alpha words"), "utf-8")
    index.refresh()
    ref = index.list(session="s")["entries"][0]["ref"]
    path.write_text(_line(session_id="s", text="omega words"), "utf-8")
    writer = sqlite3.connect(index.db, timeout=0)
    try:
        writer.execute("BEGIN IMMEDIATE")
        started = time.perf_counter()
        with pytest.raises(HistoryError, match="changed since indexing"):
            index.read(ref)
        assert time.perf_counter() - started < 5.0
    finally:
        writer.rollback()
        writer.close()
    assert index.refresh()["files_indexed"] == 1
