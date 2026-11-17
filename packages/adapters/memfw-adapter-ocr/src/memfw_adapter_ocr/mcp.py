"""``McpOcrBackend``: an OCR server run as a child process, spoken to over MCP stdio.

The operator names the server's command in the adapter settings
(``mcp_command: [...]``); nothing here knows where any server is installed,
imports its code or reads its files beyond what it writes into the private
work directory this adapter hands it. One server process is started per run
and stopped (terminated, then killed) when the run ends.

Protocol: JSON-RPC 2.0, one message per line on stdin/stdout
(``initialize``, ``notifications/initialized``, ``tools/list``,
``tools/call``). A tool's answer is read from ``structuredContent`` or, when
absent, from the first text content as JSON. Requests the server sends are
answered with an empty result (``ping``) or "method not found". The server's
stderr is discarded.

Tools used (discovered with ``tools/list``; a missing one makes the run
unavailable, never a guess):

* image: ``ocr_image(path, lang)`` → ``{"texts": [...], "scores": [...]}``
  and, when the server gives them, ``boxes`` (``[x0, y0, x1, y1]``, pixels,
  top-left origin) with the image ``width`` and ``height``;
* PDF: ``pdf_info(path)`` → ``{"pages", "text_layer_words"}``; then a text
  layer (``words_per_page >= text_layer_min_words``) is read with
  ``pdf_text(path, out_dir)``, which writes a text file whose pages are
  separated by form feeds; otherwise ``ocr_pdf(path, out_dir, ...)`` with the
  page budget (synchronous, the default). Only with ``allow_async_jobs`` (off
  by default: an async job leaves its records, job file and log, in the
  server's own job store) are more than ``sync_max_pages`` pages sent to the
  async trio ``ocr_submit`` / ``ocr_job_status`` / ``ocr_job_result``. Both
  write a Markdown file with one ``## Page N`` section per page.

Every ``out_dir`` is a fresh directory inside the run's private work
directory, and a file the server names is read only if it resolves inside
that directory (and is not a symlink). The server's PDF answers carry no
boxes or confidences, so PDF segments are page-level (``#page=N``).
"""

from __future__ import annotations

import json
import os
import queue
import re
import signal
import subprocess
import threading
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ._ports import Availability
from .backend import Media, OcrBudget, OcrError, OcrLine, OcrPage, OcrResult

PROTOCOL_VERSION = "2025-06-18"
MAX_LINE_BYTES = 8 << 20
MAX_OUTPUT_BYTES = 64 << 20
#: Lines read ahead from the server (each at most MAX_LINE_BYTES) and messages queued to it.
READ_QUEUE = 16
WRITE_QUEUE = 64
#: Requests of its own a server may send while one call of ours is pending.
MAX_SERVER_REQUESTS = 32
_PAGE_HEADING = re.compile(r"^##\s+Page\s+(\d+)\s*$")
_DONE = frozenset({"done", "finished", "completed", "complete", "success", "succeeded"})
_FAILED = frozenset({"failed", "error", "errored", "cancelled", "canceled", "killed"})
#: Environment variables a child never inherits (proxies, credentials).
_DROP_ENV = re.compile(
    r"(?i)^(.*_proxy|all_proxy|no_proxy|.*(_key|_token|_secret|password)|deepseek_.*)$"
)


class _Session:
    """One running server: request/response over its pipes, held to a deadline.

    Nothing here can block past ``deadline``: writes go through a writer
    thread (a server that stops reading cannot stall the caller), every wait
    is bounded by the time left, and a watchdog kills the server's process
    group when the deadline passes. A server that sends more than
    :data:`MAX_SERVER_REQUESTS` requests of its own during one call is
    refused (``mcp_flood``).
    """

    def __init__(self, command: Sequence[str], env: Mapping[str, str], deadline: float) -> None:
        self.deadline = deadline
        try:
            self.proc = subprocess.Popen(  # noqa: S603 - the operator's configured command
                list(command),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=dict(env),
                start_new_session=True,
            )
        except OSError:
            raise OcrError("mcp_start_failed") from None
        self._closed = threading.Event()
        self._lines: queue.Queue[bytes | None] = queue.Queue(maxsize=READ_QUEUE)
        self._out: queue.Queue[bytes | None] = queue.Queue(maxsize=WRITE_QUEUE)
        self._write_failed = threading.Event()
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._writer = threading.Thread(target=self._write, daemon=True)
        self._reader.start()
        self._writer.start()
        self._watchdog = threading.Timer(max(0.0, deadline - time.monotonic()), self._kill)
        self._watchdog.daemon = True
        self._watchdog.start()
        self._next = 0

    def _kill(self) -> None:
        """The deadline passed: kill the whole process group (unblocks every pipe)."""

        try:
            os.killpg(self.proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass

    def _read(self) -> None:
        stdout = self.proc.stdout
        assert stdout is not None
        try:
            while not self._closed.is_set():
                line = stdout.readline(MAX_LINE_BYTES + 1)
                if not line:
                    break
                while not self._closed.is_set():
                    try:
                        self._lines.put(line, timeout=0.2)  # bounded: memory stays capped
                        break
                    except queue.Full:
                        continue
        except (OSError, ValueError):
            pass
        try:
            self._lines.put_nowait(None)
        except queue.Full:
            pass

    def _write(self) -> None:
        stdin = self.proc.stdin
        assert stdin is not None
        while True:
            data = self._out.get()
            if data is None:
                return
            try:
                stdin.write(data)
                stdin.flush()
            except (OSError, ValueError):
                self._write_failed.set()
                return

    def _gone(self) -> str:
        """Why the pipes closed: the watchdog at the deadline, or the server itself."""

        return "mcp_timeout" if time.monotonic() >= self.deadline else "mcp_closed"

    def _send(self, message: Mapping[str, Any]) -> None:
        if self._write_failed.is_set():
            raise OcrError(self._gone())
        data = json.dumps(message, ensure_ascii=False).encode("utf-8") + b"\n"
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise OcrError("mcp_timeout")
        try:
            self._out.put(data, timeout=left)
        except queue.Full:
            raise OcrError("mcp_timeout") from None

    def notify(self, method: str, params: Mapping[str, Any] | None = None) -> None:
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = dict(params)
        self._send(message)

    def request(
        self, method: str, params: Mapping[str, Any], *, timeout: float | None = None
    ) -> Any:
        self._next += 1
        ident = self._next
        self._send({"jsonrpc": "2.0", "id": ident, "method": method, "params": dict(params)})
        until = self.deadline if timeout is None else min(self.deadline, time.monotonic() + timeout)
        answered = 0
        while True:
            left = until - time.monotonic()
            if left <= 0:
                raise OcrError("mcp_timeout")
            try:
                line = self._lines.get(timeout=left)
            except queue.Empty:
                raise OcrError("mcp_timeout") from None
            if line is None:
                raise OcrError(self._gone())
            if len(line) > MAX_LINE_BYTES:
                raise OcrError("mcp_message_too_large")
            try:
                message = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                continue  # not protocol output (a stray print); ignore
            if not isinstance(message, dict):
                continue
            if "method" in message and "id" in message:  # a server request
                answered += 1
                if answered > MAX_SERVER_REQUESTS:
                    raise OcrError("mcp_flood")
                self._answer(message)
                continue
            if message.get("id") != ident:
                continue  # a notification or a stale reply
            if "error" in message:
                raise OcrError("mcp_error")
            return message.get("result")

    def _answer(self, message: Mapping[str, Any]) -> None:
        if message.get("method") == "ping":
            self._send({"jsonrpc": "2.0", "id": message["id"], "result": {}})
        else:
            self._send(
                {
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "error": {"code": -32601, "message": "method not found"},
                }
            )

    def _group_alive(self) -> bool:
        try:
            os.killpg(self.proc.pid, 0)
        except ProcessLookupError:
            return False
        except (PermissionError, OSError):
            return True
        return True

    def _wait_group(self, seconds: float) -> bool:
        """Reap the leader and wait until the group is empty; ``True`` when it is."""

        until = time.monotonic() + seconds
        while True:
            try:
                self.proc.wait(timeout=0)
            except subprocess.TimeoutExpired:
                pass
            if not self._group_alive():
                return True
            if time.monotonic() >= until:
                return False
            time.sleep(0.05)

    def close(self) -> None:
        """Stop the server and everything it started (its whole process group).

        The group is always signalled, even when the leader has already
        exited, because an engine child (an OCR worker) may outlive it:
        SIGTERM, then SIGKILL, then a wait until the group is empty.
        :class:`OcrError` ``mcp_orphans`` if anything is still alive.
        """

        proc = self.proc
        self._closed.set()
        self._watchdog.cancel()
        try:
            self._out.put_nowait(None)
        except queue.Full:
            pass
        self._writer.join(timeout=1)
        if self._writer.is_alive():  # stuck writing to a server that stopped reading
            self._kill()
            self._writer.join(timeout=2)
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=2)  # a polite server exits when its stdin closes
        except subprocess.TimeoutExpired:
            pass
        empty = not self._group_alive()
        for sig in (signal.SIGTERM, signal.SIGKILL):
            if empty:
                break
            try:
                os.killpg(proc.pid, sig)
            except ProcessLookupError:
                empty = True
                break
            except (PermissionError, OSError):
                pass
            empty = self._wait_group(2.0 if sig == signal.SIGTERM else 5.0)
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass
        if proc.stdout is not None:
            try:
                proc.stdout.close()
            except OSError:
                pass
        if not empty:
            raise OcrError("mcp_orphans")


def child_env(extra: Mapping[str, str] | None, workdir: Path) -> dict[str, str]:
    """A small environment: ``PATH``, ``HOME``, locale, a private ``TMPDIR``, plus ``extra``.

    Proxy variables and anything that looks like a credential are never passed,
    from the parent or from ``extra``.
    """

    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "LC_ALL")}
    env["TMPDIR"] = str(workdir)
    for key, value in (extra or {}).items():
        env[str(key)] = str(value)
    return {k: v for k, v in env.items() if not _DROP_ENV.match(k)}


def tool_payload(result: Any) -> Any:
    """A tool call's answer: ``structuredContent``, else the first text content as JSON."""

    if not isinstance(result, Mapping):
        raise OcrError("mcp_bad_result")
    if result.get("isError") is True:
        raise OcrError("mcp_tool_error")
    structured = result.get("structuredContent")
    if isinstance(structured, Mapping):
        inner = structured.get("result", structured)
        if isinstance(inner, Mapping):
            return inner
    for item in result.get("content") or ():
        if isinstance(item, Mapping) and item.get("type") == "text":
            try:
                return json.loads(str(item.get("text", "")))
            except ValueError:
                raise OcrError("mcp_bad_result") from None
    raise OcrError("mcp_bad_result")


def _inside(base: Path, named: Any) -> Path:
    """``named`` (a path the server returned) if it is a regular file inside ``base``."""

    if not isinstance(named, str) or not named:
        raise OcrError("mcp_no_output")
    path = Path(named)
    if not path.is_absolute():
        path = base / path
    if path.is_symlink() or not path.resolve().is_relative_to(base.resolve()):
        raise OcrError("mcp_output_outside_workdir")
    if not path.is_file():
        raise OcrError("mcp_no_output")
    if path.stat().st_size > MAX_OUTPUT_BYTES:
        raise OcrError("mcp_output_too_large")
    return path


def parse_page_markdown(text: str) -> dict[int, list[str]]:
    """``{page: [lines]}`` from Markdown with one ``## Page N`` section per page."""

    pages: dict[int, list[str]] = {}
    current: int | None = None
    for raw in text.splitlines():
        match = _PAGE_HEADING.match(raw.strip())
        if match:
            current = int(match.group(1))
            pages.setdefault(current, [])
            continue
        if current is not None and raw.strip():
            pages[current].append(raw.strip())
    return pages


def _size(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or not value > 0:
        return None
    return float(value)


def _confidence(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return min(1.0, max(0.0, float(value))) if value == value else None


def _boxes(raw: Any, count: int) -> list[tuple[float, float, float, float]] | None:
    """``[x0, y0, x1, y1]`` per text, or ``None`` when absent or malformed (text is kept)."""

    if not isinstance(raw, list) or len(raw) != count:
        return None
    out: list[tuple[float, float, float, float]] = []
    for box in raw:
        if not isinstance(box, list | tuple) or len(box) != 4:
            return None
        if not all(isinstance(v, int | float) and not isinstance(v, bool) for v in box):
            return None
        x0, y0, x1, y1 = (float(v) for v in box)
        if x0 > x1 or y0 > y1:
            return None
        out.append((x0, y0, x1, y1))
    return out


def _number(value: Any, code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise OcrError(code)
    return value


class McpOcrBackend:
    """An :class:`~.backend.OcrBackend` over an MCP OCR server (child process)."""

    name = "mcp"

    def __init__(
        self,
        command: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        lang: str = "en",
        engine: str = "",
        dpi: int = 200,
        text_layer_min_words: int = 10,
        sync_max_pages: int = 5,
        poll_seconds: float = 2.0,
        allow_async_jobs: bool = False,
    ) -> None:
        if (
            not isinstance(command, Sequence)
            or isinstance(command, str)
            or not command
            or not all(isinstance(part, str) and part for part in command)
        ):
            raise OcrError("bad_mcp_command")
        if not Path(command[0]).is_absolute():
            raise OcrError("mcp_command_not_absolute")
        self.command = tuple(command)
        self.env = dict(env or {})
        self.lang = lang
        self.engine = engine
        self.dpi = dpi
        self.text_layer_min_words = text_layer_min_words
        self.sync_max_pages = sync_max_pages
        self.poll_seconds = poll_seconds
        self.allow_async_jobs = allow_async_jobs is True
        self.version = "unknown"

    def __repr__(self) -> str:
        return "McpOcrBackend()"

    def available(self) -> Availability:
        executable = Path(self.command[0])
        if not executable.is_file() or not os.access(executable, os.X_OK):
            return Availability.missing("mcp_command_missing")
        return Availability.ok()

    # -- one run --------------------------------------------------------------------

    def _open(self, workdir: Path, deadline: float) -> tuple[_Session, set[str]]:
        session = _Session(self.command, child_env(self.env, workdir), deadline)
        try:
            init = session.request(
                "initialize",
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "memfw-adapter-ocr", "version": _version()},
                },
                timeout=60,
            )
            info = init.get("serverInfo") if isinstance(init, Mapping) else None
            if isinstance(info, Mapping) and isinstance(info.get("version"), str):
                self.version = re.sub(r"[^0-9A-Za-z.+-]", "", info["version"])[:32] or "unknown"
            session.notify("notifications/initialized")
            listed = session.request("tools/list", {}, timeout=60)
            tools = listed.get("tools") if isinstance(listed, Mapping) else None
            if not isinstance(tools, list):
                raise OcrError("mcp_bad_tools")
            names = {str(t.get("name")) for t in tools if isinstance(t, Mapping)}
        except BaseException:
            session.close()
            raise
        return session, names

    def _call(self, session: _Session, names: set[str], tool: str, args: Mapping[str, Any]) -> Any:
        if tool not in names:
            raise OcrError(f"mcp_tool_missing:{tool}")
        return tool_payload(session.request("tools/call", {"name": tool, "arguments": dict(args)}))

    def extract(self, path: Path, *, media: Media, budget: OcrBudget, workdir: Path) -> OcrResult:
        deadline = time.monotonic() + budget.timeout_seconds
        session, names = self._open(workdir, deadline)
        try:
            if media == "image":
                return self._image(session, names, path)
            return self._pdf(session, names, path, budget, workdir)
        finally:
            session.close()

    def _result(self, media: Media, total: int, pages: list[OcrPage], kind: str) -> OcrResult:
        return OcrResult(
            tool="mcp",
            version=self.version,
            media=media,
            total_pages=total,
            pages=tuple(pages),
            kind="extract" if kind == "extract" else "ocr",
        )

    def _image(self, session: _Session, names: set[str], path: Path) -> OcrResult:
        answer = self._call(session, names, "ocr_image", {"path": str(path), "lang": self.lang})
        if not isinstance(answer, Mapping) or not isinstance(answer.get("texts"), list):
            raise OcrError("mcp_bad_result")
        texts: list[Any] = answer["texts"]
        raw_scores = answer.get("scores")
        scores: list[Any] = raw_scores if isinstance(raw_scores, list) else []
        boxes = _boxes(answer.get("boxes"), len(texts))
        width, height = _size(answer.get("width")), _size(answer.get("height"))
        if width is None or height is None:
            boxes = None  # boxes without a size cannot be checked: drop them, keep the text
        lines: list[OcrLine] = []
        for index, text in enumerate(texts):
            if not isinstance(text, str) or not text.strip():
                continue
            score = scores[index] if index < len(scores) else None
            lines.append(
                OcrLine(
                    text=text.strip(),
                    bbox=boxes[index] if boxes is not None else None,
                    confidence=_confidence(score),
                )
            )
        page = OcrPage(
            page=None,
            lines=tuple(lines),
            width=width if boxes is not None else None,
            height=height if boxes is not None else None,
            unit="px",
            origin="top-left",
        )
        return self._result("image", 1, [page], "ocr")

    def _pdf(
        self, session: _Session, names: set[str], path: Path, budget: OcrBudget, workdir: Path
    ) -> OcrResult:
        info = self._call(session, names, "pdf_info", {"path": str(path)})
        if not isinstance(info, Mapping):
            raise OcrError("mcp_bad_result")
        total = _number(info.get("pages"), "mcp_bad_pages")
        if total < 1:
            raise OcrError("mcp_bad_pages")
        words = _number(info.get("text_layer_words", 0), "mcp_bad_result")
        reading = min(total, budget.max_pages)
        if words / total >= self.text_layer_min_words and "pdf_text" in names:
            out = workdir / "pdf_text"
            out.mkdir(mode=0o700)
            answer = self._call(
                session, names, "pdf_text", {"path": str(path), "out_dir": str(out)}
            )
            file = _inside(out, answer.get("text_path") if isinstance(answer, Mapping) else None)
            chunks = file.read_text(encoding="utf-8", errors="replace").split("\f")
            pages = [
                OcrPage(
                    page=n,
                    lines=tuple(
                        OcrLine(line.strip()) for line in chunk.splitlines() if line.strip()
                    ),
                )
                for n, chunk in enumerate(chunks[:reading], 1)
            ]
            return self._result("pdf", total, pages, "extract")
        out = workdir / "ocr"
        out.mkdir(mode=0o700)
        args: dict[str, Any] = {
            "path": str(path),
            "out_dir": str(out),
            "dpi": self.dpi,
            "lang": self.lang,
            "max_pages": reading if reading < total else 0,
        }
        if self.engine:
            args["engine"] = self.engine
        # Async jobs leave records (job files, logs) in the server's own job store,
        # so they are used only when the operator allows it.
        asynchronous = self.allow_async_jobs and {"ocr_submit", "ocr_job_status"} <= names
        if asynchronous and (reading > self.sync_max_pages or "ocr_pdf" not in names):
            named = self._submit_and_wait(session, names, args)
        else:
            left = max(1, int(session.deadline - time.monotonic()))
            answer = self._call(session, names, "ocr_pdf", {**args, "timeout": left})
            named = answer.get("output_file") if isinstance(answer, Mapping) else None
        file = _inside(out, named)
        parsed = parse_page_markdown(file.read_text(encoding="utf-8", errors="replace"))
        pages = [
            OcrPage(page=n, lines=tuple(OcrLine(line) for line in parsed[n]))
            for n in sorted(parsed)
            if 1 <= n <= reading
        ]
        return self._result("pdf", total, pages, "ocr")

    def _submit_and_wait(self, session: _Session, names: set[str], args: Mapping[str, Any]) -> Any:
        started = self._call(session, names, "ocr_submit", args)
        job = started.get("job_id") if isinstance(started, Mapping) else None
        if not isinstance(job, str) or not job:
            raise OcrError("mcp_bad_result")
        while True:
            status = self._call(session, names, "ocr_job_status", {"job_id": job})
            if not isinstance(status, Mapping):
                raise OcrError("mcp_bad_result")
            state = str(status.get("status", "")).lower()
            if state in _FAILED:
                raise OcrError("mcp_job_failed")
            if state in _DONE or status.get("running") is False or status.get("output_file"):
                named = status.get("output_file")
                if not named and "ocr_job_result" in names:
                    result = self._call(session, names, "ocr_job_result", {"job_id": job})
                    named = result.get("output_file") if isinstance(result, Mapping) else None
                return named
            if time.monotonic() + self.poll_seconds >= session.deadline:
                raise OcrError("mcp_timeout")
            time.sleep(self.poll_seconds)


def _version() -> str:
    from . import __version__

    return __version__


__all__ = ["PROTOCOL_VERSION", "McpOcrBackend", "child_env", "parse_page_markdown", "tool_payload"]
