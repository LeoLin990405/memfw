"""``PdftotextBackend``: poppler's ``pdftotext -bbox-layout`` for text-layer PDFs (fallback).

The executable's absolute path comes from the settings (``pdftotext: /...``,
and optionally ``pdfinfo: /...``); nothing searches ``PATH``. One line of the
output is one segment, with its box converted by :mod:`.proxy` from
poppler's frame (points, top-left origin) to memfw's (points, bottom-left
origin). A scanned PDF gives pages without lines; the proxy's ``coverage``
still names every page read and notes the pages without text.
"""

from __future__ import annotations

import html
import os
import re
import subprocess
import threading
from pathlib import Path

from ._ports import Availability
from .backend import Media, OcrBudget, OcrError, OcrLine, OcrPage, OcrResult
from .mcp import child_env

MAX_OUTPUT_BYTES = 64 << 20
_PAGE = re.compile(r'<page width="([0-9.]+)" height="([0-9.]+)">(.*?)</page>', re.S)
_LINE = re.compile(
    r'<line xMin="([0-9.]+)" yMin="([0-9.]+)" xMax="([0-9.]+)" yMax="([0-9.]+)">(.*?)</line>',
    re.S,
)
_WORD = re.compile(r"<word [^>]*>(.*?)</word>", re.S)


def parse_bbox_layout(text: str) -> list[OcrPage]:
    """Pages of ``pdftotext -bbox-layout`` output: one line per segment, top-left frame."""

    pages: list[OcrPage] = []
    for number, match in enumerate(_PAGE.finditer(text), 1):
        width, height = float(match.group(1)), float(match.group(2))
        lines: list[OcrLine] = []
        for line in _LINE.finditer(match.group(3)):
            words = [html.unescape(w).strip() for w in _WORD.findall(line.group(5))]
            joined = " ".join(w for w in words if w)
            if joined:
                box = tuple(float(line.group(i)) for i in range(1, 5))
                lines.append(OcrLine(joined, bbox=box))  # type: ignore[arg-type]
        pages.append(
            OcrPage(
                page=number,
                lines=tuple(lines),
                width=width,
                height=height,
                unit="pt",
                origin="top-left",
            )
        )
    return pages


def tool_version(executable: str) -> str:
    try:
        done = subprocess.run(  # noqa: S603 - the operator's configured executable
            [executable, "-v"], capture_output=True, timeout=10, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    match = re.search(rb"version ([0-9][0-9A-Za-z.+-]{0,31})", done.stdout + done.stderr)
    return match.group(1).decode() if match else "unknown"


def _run_capped(argv: list[str], *, timeout: float, cap: int, workdir: Path) -> bytes:
    """Run ``argv`` and read at most ``cap`` bytes of stdout (killed when over, or late)."""

    try:
        proc = subprocess.Popen(  # noqa: S603 - the operator's configured executable
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            env=child_env(None, workdir),
            cwd=workdir,
        )
    except OSError:
        raise OcrError("pdftotext_failed") from None
    late = threading.Event()

    def stop() -> None:
        late.set()
        proc.kill()

    timer = threading.Timer(timeout, stop)
    timer.daemon = True
    timer.start()
    chunks: list[bytes] = []
    size = 0
    try:
        assert proc.stdout is not None
        while chunk := proc.stdout.read(1 << 16):
            size += len(chunk)
            if size > cap:
                proc.kill()
                raise OcrError("pdftotext_output_too_large")
            chunks.append(chunk)
        code = proc.wait()
    finally:
        timer.cancel()
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        if proc.stdout is not None:
            proc.stdout.close()
    if late.is_set():
        raise OcrError("pdftotext_timeout")
    if code != 0:
        raise OcrError("pdftotext_failed")
    return b"".join(chunks)


_PAGES = re.compile(rb"^Pages:\s+(\d+)\s*$", re.M)


class PdftotextBackend:
    """An :class:`~.backend.OcrBackend` for PDFs with a text layer (``kind: extract``).

    ``pdfinfo`` (optional, absolute path) gives the page count, so only the
    first ``max_pages`` pages are converted (``-l``) and coverage can say how
    many there are. Without it, a PDF longer than ``max_pages`` is refused
    (``page_budget``). Output is read with a byte cap (:data:`MAX_OUTPUT_BYTES`).
    """

    name = "pdftotext"

    def __init__(self, executable: str, *, pdfinfo: str = "") -> None:
        if not isinstance(executable, str) or not Path(executable).is_absolute():
            raise OcrError("pdftotext_not_absolute")
        if pdfinfo and (not isinstance(pdfinfo, str) or not Path(pdfinfo).is_absolute()):
            raise OcrError("pdfinfo_not_absolute")
        self.executable = executable
        self.pdfinfo = pdfinfo

    def __repr__(self) -> str:
        return "PdftotextBackend()"

    def available(self) -> Availability:
        for tool in filter(None, (self.executable, self.pdfinfo)):
            if not Path(tool).is_file() or not os.access(tool, os.X_OK):
                return Availability.missing("pdftotext_missing")
        return Availability.ok()

    def _page_count(self, path: Path, budget: OcrBudget, workdir: Path) -> int | None:
        if not self.pdfinfo:
            return None
        info = _run_capped(
            [self.pdfinfo, str(path)], timeout=budget.timeout_seconds, cap=1 << 20, workdir=workdir
        )
        match = _PAGES.search(info)
        if match is None or int(match.group(1)) < 1:
            raise OcrError("pdftotext_unreadable")
        return int(match.group(1))

    def extract(self, path: Path, *, media: Media, budget: OcrBudget, workdir: Path) -> OcrResult:
        if media != "pdf":
            raise OcrError("pdftotext_pdf_only")
        total = self._page_count(path, budget, workdir)
        last = min(total, budget.max_pages) if total is not None else budget.max_pages + 1
        raw = _run_capped(
            [
                self.executable,
                "-q",
                "-bbox-layout",
                "-enc",
                "UTF-8",
                "-f",
                "1",
                "-l",
                str(last),
                str(path),
                "-",
            ],
            timeout=budget.timeout_seconds,
            cap=MAX_OUTPUT_BYTES,
            workdir=workdir,
        )
        pages = parse_bbox_layout(raw.decode("utf-8", errors="replace"))
        if not pages:
            raise OcrError("pdftotext_no_pages")
        if total is None:
            if len(pages) > budget.max_pages:
                raise OcrError("page_budget")  # longer than the budget, length unknown
            total = len(pages)
        return OcrResult(
            tool="pdftotext",
            version=tool_version(self.executable),
            media="pdf",
            total_pages=total,
            pages=tuple(pages[: budget.max_pages]),
            kind="extract",
        )


__all__ = ["PdftotextBackend", "parse_bbox_layout", "tool_version"]
