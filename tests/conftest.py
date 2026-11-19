"""Test-wide guards: no network, and no access to the real per-user data dirs."""

from __future__ import annotations

import socket
from typing import Any

import pytest

from tests.real_home_guard import guarding


@pytest.fixture(autouse=True)
def guard_real_home() -> Any:
    """Fail any test that opens, connects to or lists a path under the real data dirs.

    ``tests.real_home_guard`` has the dirs, the audit hook and
    ``inspecting_real_home`` (for before/after snapshot fixtures only).
    """

    with guarding():
        yield


@pytest.fixture(autouse=True)
def disable_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def denied(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Tests must not open network sockets")

    for name in ("connect", "connect_ex"):
        monkeypatch.setattr(socket.socket, name, denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)
