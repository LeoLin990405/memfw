"""Where the status JSON comes from: an exported file, or one bounded CLI run.

Live mode runs exactly ``[<tailscale>, "status", "--json"]``: no shell, stdin
closed, stderr discarded, proxy variables removed from the environment, a
wall-clock deadline and an output cap. The child runs in its own session and
its whole process group is killed on a timeout or an oversized answer. The
adapter never scans networks or talks to the Tailscale API.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from .status import MAX_STATUS_BYTES, TailnetError

#: The arguments after the executable; never configurable.
STATUS_ARGS: tuple[str, ...] = ("status", "--json")
DEFAULT_TIMEOUT = 15.0
MAX_TIMEOUT = 120.0
#: Environment variables removed before the CLI runs (compared case-insensitively).
PROXY_VARIABLES = frozenset(
    {"http_proxy", "https_proxy", "all_proxy", "no_proxy", "ftp_proxy", "socks_proxy"}
)

Popen = Callable[..., Any]


def read_input(path: Path, *, max_bytes: int = MAX_STATUS_BYTES) -> bytes:
    """An exported status file: a regular file no larger than ``max_bytes``."""

    source = Path(path).expanduser()
    try:
        if not source.is_file():
            raise TailnetError("input_not_a_file")
        with source.open("rb") as handle:
            data = handle.read(max_bytes + 1)
    except OSError as exc:
        raise TailnetError("input_unreadable") from exc
    if len(data) > max_bytes:
        raise TailnetError("status_too_large")
    return data


def child_env(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """``environ`` (default ``os.environ``) without any proxy variable."""

    source = os.environ if environ is None else environ
    return {k: v for k, v in source.items() if k.lower() not in PROXY_VARIABLES}


def resolve_executable(executable: str) -> str:
    """A bare name is looked up on ``PATH``; a path must be an executable file."""

    if not executable or "\x00" in executable:
        raise TailnetError("tailscale_not_found")
    if os.sep in executable or (os.altsep and os.altsep in executable):
        candidate = Path(executable).expanduser()
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
        raise TailnetError("tailscale_not_found")
    found = shutil.which(executable)
    if found is None:
        raise TailnetError("tailscale_not_found")
    return found


def _kill(process: Any) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (OSError, AttributeError):
        try:
            process.kill()
        except OSError:
            pass


def run_tailscale(
    executable: str = "tailscale",
    *,
    timeout: float = DEFAULT_TIMEOUT,
    max_bytes: int = MAX_STATUS_BYTES,
    popen: Popen = subprocess.Popen,
) -> bytes:
    """Stdout of one ``tailscale status --json`` run. Raises :class:`TailnetError`."""

    if not 0 < timeout <= MAX_TIMEOUT:
        raise TailnetError("bad_timeout")
    argv = [resolve_executable(executable), *STATUS_ARGS]
    try:
        process = popen(
            argv,
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=child_env(),
            close_fds=True,
            start_new_session=True,
        )
    except OSError as exc:
        raise TailnetError("tailscale_not_runnable") from exc
    chunks: list[bytes] = []
    state = {"size": 0, "overflow": False}

    def reader() -> None:
        stream = process.stdout
        while True:
            chunk = stream.read(65536)
            if not chunk:
                return
            state["size"] += len(chunk)
            if state["size"] > max_bytes:
                state["overflow"] = True
                _kill(process)
                return
            chunks.append(chunk)

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        _kill(process)
        thread.join(2.0)
        _reap(process)
        raise TailnetError("tailscale_timeout")
    if state["overflow"]:
        _reap(process)
        raise TailnetError("status_too_large")
    try:
        code = process.wait(timeout=max(0.5, min(5.0, timeout)))
    except subprocess.TimeoutExpired as exc:
        _kill(process)
        _reap(process)
        raise TailnetError("tailscale_timeout") from exc
    finally:
        if process.stdout is not None:
            process.stdout.close()
    if code != 0:
        raise TailnetError("tailscale_failed")
    return b"".join(chunks)


def _reap(process: Any) -> None:
    try:
        process.wait(timeout=2.0)
    except (subprocess.TimeoutExpired, OSError):
        pass
    if process.stdout is not None:
        try:
            process.stdout.close()
        except OSError:
            pass


__all__ = [
    "DEFAULT_TIMEOUT",
    "MAX_TIMEOUT",
    "PROXY_VARIABLES",
    "STATUS_ARGS",
    "child_env",
    "read_input",
    "resolve_executable",
    "run_tailscale",
]
