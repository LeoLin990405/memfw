"""Test-wide guard against accidental network use."""

from __future__ import annotations

import socket
from typing import Any

import pytest


@pytest.fixture(autouse=True)
def disable_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def denied(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Tests must not open network sockets")

    for name in ("connect", "connect_ex"):
        monkeypatch.setattr(socket.socket, name, denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)
