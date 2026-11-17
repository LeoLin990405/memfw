"""The adapter's own OCR engine interface: :class:`OcrBackend` and its result types.

A backend turns one local file (a private copy of the original) into pages of
text lines. It never sees the vault, never chooses which file to read, and
never uploads anything. Two implementations ship: ``mcp`` (an OCR server run
as a child process over the MCP stdio protocol) and ``pdftotext`` (poppler,
text-layer PDFs). Anything else that satisfies the protocol can be swapped in
(``backend: "module:factory"`` in the settings).

Coordinates in a result are in the engine's own frame and say so (``unit``,
``origin``); :mod:`.proxy` converts them to memfw's locator frame (PDF: points,
bottom-left origin; image: pixels, top-left origin).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

from ._ports import Availability

Media = Literal["pdf", "image"]
Unit = Literal["pt", "px"]
Origin = Literal["bottom-left", "top-left"]


class OcrError(RuntimeError):
    """A backend or the adapter refused or failed. ``code`` is a short code, never text."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class OcrBudget:
    """Limits for one run: wall time (seconds) and pages."""

    timeout_seconds: float = 600.0
    max_pages: int = 200

    def __post_init__(self) -> None:
        if not (isinstance(self.timeout_seconds, int | float) and self.timeout_seconds > 0):
            raise OcrError("bad_budget")
        if isinstance(self.max_pages, bool) or not isinstance(self.max_pages, int):
            raise OcrError("bad_budget")
        if self.max_pages < 1:
            raise OcrError("bad_budget")


@dataclass(frozen=True)
class OcrLine:
    """One line (or block) of text; ``bbox`` is ``(x0, y0, x1, y1)`` in the page's frame."""

    text: str
    bbox: tuple[float, float, float, float] | None = None
    confidence: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise OcrError("bad_line")
        if self.bbox is not None:
            if len(self.bbox) != 4 or not all(_finite(v) for v in self.bbox):
                raise OcrError("bad_bbox")
            x0, y0, x1, y1 = self.bbox
            if x0 > x1 or y0 > y1:
                raise OcrError("bad_bbox")
        if self.confidence is not None and not (
            _finite(self.confidence) and 0.0 <= self.confidence <= 1.0
        ):
            raise OcrError("bad_confidence")


@dataclass(frozen=True)
class OcrPage:
    """One page (PDF, 1-based ``page``) or the whole image (``page`` is ``None``).

    ``width`` and ``height`` are the page size in ``unit``; they are needed
    only when a line has a ``bbox``.
    """

    page: int | None
    lines: tuple[OcrLine, ...]
    width: float | None = None
    height: float | None = None
    unit: Unit = "pt"
    origin: Origin = "bottom-left"

    def __post_init__(self) -> None:
        if self.page is not None and (
            isinstance(self.page, bool) or not isinstance(self.page, int) or self.page < 1
        ):
            raise OcrError("bad_page")
        if self.unit not in ("pt", "px") or self.origin not in ("bottom-left", "top-left"):
            raise OcrError("bad_frame")
        for value in (self.width, self.height):
            if value is not None and not (_finite(value) and value > 0):
                raise OcrError("bad_page_size")
        if any(line.bbox is not None for line in self.lines) and (
            self.width is None or self.height is None
        ):
            raise OcrError("bbox_without_page_size")


@dataclass(frozen=True)
class OcrResult:
    """What a backend read. ``total_pages`` is the document's page count (1 for an image)."""

    tool: str
    version: str
    media: Media
    total_pages: int
    pages: tuple[OcrPage, ...] = field(default_factory=tuple)
    #: ``ocr`` (recognised from pixels) or ``extract`` (read from a text layer).
    kind: Literal["ocr", "extract"] = "ocr"

    def __post_init__(self) -> None:
        if isinstance(self.total_pages, bool) or not isinstance(self.total_pages, int):
            raise OcrError("bad_result")
        if self.total_pages < 1 or self.media not in ("pdf", "image"):
            raise OcrError("bad_result")
        if self.kind not in ("ocr", "extract"):
            raise OcrError("bad_result")
        numbers = [p.page for p in self.pages]
        if self.media == "image":
            if numbers not in ([], [None]):
                raise OcrError("bad_result")
        elif any(n is None or n > self.total_pages for n in numbers) or len(set(numbers)) != len(
            numbers
        ):
            raise OcrError("bad_result")


@runtime_checkable
class OcrBackend(Protocol):
    """An OCR engine. ``extract`` reads ``path`` (a private copy) and nothing else.

    ``workdir`` is a private, empty, temporary directory the backend may write
    into (and nowhere else). ``budget`` bounds time and pages; a document over
    the page budget is either refused (:class:`OcrError` ``page_budget``) or
    read in part, the result's pages saying which.
    """

    name: str

    def available(self) -> Availability: ...

    def extract(
        self, path: Path, *, media: Media, budget: OcrBudget, workdir: Path
    ) -> OcrResult: ...


def _finite(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def page_numbers(result: OcrResult) -> Sequence[int]:
    return sorted(p.page for p in result.pages if p.page is not None)


__all__ = [
    "Media",
    "OcrBackend",
    "OcrBudget",
    "OcrError",
    "OcrLine",
    "OcrPage",
    "OcrResult",
    "Origin",
    "Unit",
    "page_numbers",
]
