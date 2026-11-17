"""``PdftotextBackend``: poppler's ``pdftotext -bbox-layout`` for text-layer PDFs (fallback).

The executable's absolute path comes from the settings (``pdftotext: /...``);
nothing searches ``PATH``. One line of the output is one segment, with its box
converted by :mod:`.proxy` from poppler's frame (points, top-left origin) to
memfw's (points, bottom-left origin). A scanned PDF gives pages without lines;
the proxy's ``coverage`` still names every page read and ``empty_pages``
lists the ones without text.
"""

from __future__ import annotations

import html
import os
import re
import subprocess
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


class PdftotextBackend:
    """An :class:`~.backend.OcrBackend` for PDFs with a text layer (``kind: extract``)."""

    name = "pdftotext"

    def __init__(self, executable: str) -> None:
        if not isinstance(executable, str) or not Path(executable).is_absolute():
            raise OcrError("pdftotext_not_absolute")
        self.executable = executable

    def __repr__(self) -> str:
        return "PdftotextBackend()"

    def available(self) -> Availability:
        if not Path(self.executable).is_file() or not os.access(self.executable, os.X_OK):
            return Availability.missing("pdftotext_missing")
        return Availability.ok()

    def extract(self, path: Path, *, media: Media, budget: OcrBudget, workdir: Path) -> OcrResult:
        if media != "pdf":
            raise OcrError("pdftotext_pdf_only")
        try:
            done = subprocess.run(  # noqa: S603 - the operator's configured executable
                [self.executable, "-q", "-bbox-layout", "-enc", "UTF-8", str(path), "-"],
                capture_output=True,
                timeout=budget.timeout_seconds,
                env=child_env(None, workdir),
                cwd=workdir,
                stdin=subprocess.DEVNULL,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise OcrError("pdftotext_timeout") from None
        except OSError:
            raise OcrError("pdftotext_failed") from None
        if done.returncode != 0:
            raise OcrError("pdftotext_failed")
        if len(done.stdout) > MAX_OUTPUT_BYTES:
            raise OcrError("pdftotext_output_too_large")
        pages = parse_bbox_layout(done.stdout.decode("utf-8", errors="replace"))
        if not pages:
            raise OcrError("pdftotext_no_pages")
        return OcrResult(
            tool="pdftotext",
            version=tool_version(self.executable),
            media="pdf",
            total_pages=len(pages),
            pages=tuple(pages[: budget.max_pages]),
            kind="extract",
        )


__all__ = ["PdftotextBackend", "parse_bbox_layout", "tool_version"]
