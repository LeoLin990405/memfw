"""The printed launchd plist: single-pass placeholders and an absolute memfw path."""

from __future__ import annotations

import plistlib
import sys
from pathlib import Path

import pytest

from memfw.conversations import schedule
from memfw.conversations.schedule import render
from memfw.conversations.store import ConversationError


def test_placeholder_inside_a_value_is_not_reexpanded(tmp_path: Path) -> None:
    vault = tmp_path / "__LOG_DIR__" / "__INTERVAL__"
    vault.mkdir(parents=True)
    logs = tmp_path / "logs__MEMFW_BIN__"
    plist = plistlib.loads(
        render(vault=vault, memfw_bin="/opt/__VAULT_ROOT__/memfw", log_dir=logs).encode()
    )
    assert plist["ProgramArguments"][0] == "/opt/__VAULT_ROOT__/memfw"
    assert plist["ProgramArguments"][4] == str(vault.resolve())
    assert plist["StandardOutPath"] == f"{logs}/conversations-worker.log"
    assert plist["StartInterval"] == 900


def test_values_are_still_xml_escaped(tmp_path: Path) -> None:
    vault = tmp_path / "a&b<c>"
    vault.mkdir()
    text = render(vault=vault, memfw_bin="/opt/x&y", log_dir=tmp_path / "l<o>g")
    assert "&amp;" in text and "&lt;" in text
    plist = plistlib.loads(text.encode())
    assert plist["ProgramArguments"][0] == "/opt/x&y"
    assert plist["ProgramArguments"][4] == str(vault.resolve())


def test_bin_defaults_to_absolute_running_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = tmp_path / "bin" / "memfw"
    real.parent.mkdir()
    real.write_text("#!/bin/sh\n")
    real.chmod(0o755)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["bin/memfw", "conversations", "schedule"])
    plist = plistlib.loads(render(vault=tmp_path).encode())
    binary = plist["ProgramArguments"][0]
    assert Path(binary).is_absolute()
    assert binary == str(real.resolve())


def test_bin_falls_back_to_resolved_path_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = tmp_path / "real" / "memfw"
    real.parent.mkdir()
    real.write_text("#!/bin/sh\n")
    real.chmod(0o755)
    linkdir = tmp_path / "path"
    linkdir.mkdir()
    (linkdir / "memfw").symlink_to(real)
    monkeypatch.setattr(sys, "argv", ["-c"])
    monkeypatch.setenv("PATH", str(linkdir))
    plist = plistlib.loads(render(vault=tmp_path).encode())
    assert plist["ProgramArguments"][0] == str(real.resolve())


def test_bin_missing_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["-c"])
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    with pytest.raises(ConversationError, match="--bin"):
        render(vault=tmp_path)


def test_default_log_dir_is_under_user_logs() -> None:
    assert schedule.default_log_dir() == Path.home() / "Library" / "Logs" / "memfw"


def test_argv0_named_like_memfw_but_not_memfw_is_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    impostor = tmp_path / "bin" / "memfw-dev-helper"
    impostor.parent.mkdir()
    impostor.write_text("#!/bin/sh\n")
    impostor.chmod(0o755)
    monkeypatch.setattr(sys, "argv", [str(impostor)])
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    with pytest.raises(ConversationError, match="--bin"):
        render(vault=tmp_path)
