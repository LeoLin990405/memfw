"""From an :class:`~.backend.OcrResult` to a proxy import file, and running one job.

:func:`to_proxy_candidate` is the ONE place that knows the output format:
memfw's proxy import format ``memfw.proxy`` version 1 (MM2a, ``memfw attach
proxy import``; Python form ``memfw.ports.proxies``). MM2a was not on ``main``
when this adapter was written, so the format is mirrored here from MM2a's
documented draft; when ``memfw.ports.proxies`` is importable, every file is
also checked with its ``parse_proxy_import`` before it is written.

Locators are written in canonical units (design v2.1), so no ``coordinates``
block is needed: PDF boxes in points with the origin at the bottom-left of the
page (``{"page": 3, "bbox": [x0, y0, x1, y1]}``), image boxes in pixels with
the origin at the top-left (``{"bbox": [...]}``); a segment without a box is
``{"page": N}`` (PDF) or has no locator (the whole image). A box outside the
page (0.5 unit tolerance) or of zero size is dropped, its text kept; the
number dropped goes into ``coverage.detail``. ``source`` declares the pages and
page sizes (PDF) or the image size, so memfw can check every locator.

:func:`generate_proxy` runs one job: privacy clearance, sha256 of the
original before, a private copy (``0700`` directory, ``0400`` file) the
backend reads, the backend, sha256 of the original after, and the file.
Nothing is uploaded; the copy and the backend's work directory are removed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
from collections.abc import Collection
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ._ports import PRIVACY_LEVELS, field_level, parse_proxy_import
from .backend import Media, OcrBackend, OcrBudget, OcrError, OcrPage, OcrResult

#: MM2a's proxy import format (mirrored; see the module docstring).
PROXY_FORMAT = "memfw.proxy"
PROXY_FORMAT_VERSION = 1
CHUNK = 1 << 20
MAX_SEGMENT_CHARS = 65_536
_MARKER = "<!-- memfw:segment"
#: ``att-<uuid7>``, the attachment id rule.
_ATT_ID = re.compile(r"^att-[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
MEDIA_TYPES: dict[str, Media] = {
    "application/pdf": "pdf",
    "image/png": "image",
    "image/jpeg": "image",
    "image/tiff": "image",
    "image/webp": "image",
    "image/bmp": "image",
}
DEFAULT_CLEARANCE = frozenset({"public", "shared"})
TOLERANCE = 0.5


def _round(value: float) -> float:
    rounded = round(value, 3)
    return 0.0 if rounded == 0 else rounded


def _number(value: float) -> int | float:
    rounded = _round(value)
    return int(rounded) if rounded.is_integer() else rounded


def _to_memfw_frame(
    page: OcrPage, media: Media, bbox: tuple[float, float, float, float]
) -> list[int | float] | None:
    """The box in memfw's canonical frame, or ``None`` when it is unusable."""

    width, height = page.width, page.height
    if width is None or height is None:
        return None
    x0, y0, x1, y1 = bbox
    if media == "pdf":
        if page.unit != "pt":
            return None  # pixel boxes on a PDF need a render DPI the result does not carry
        if page.origin == "top-left":
            y0, y1 = height - y1, height - y0
    else:
        if page.unit != "px":
            return None
        if page.origin == "bottom-left":
            y0, y1 = height - y1, height - y0
    if x0 < -TOLERANCE or y0 < -TOLERANCE or x1 > width + TOLERANCE or y1 > height + TOLERANCE:
        return None
    box = [
        _number(min(max(x0, 0.0), width)),
        _number(min(max(y0, 0.0), height)),
        _number(min(max(x1, 0.0), width)),
        _number(min(max(y1, 0.0), height)),
    ]
    if not (box[0] < box[2] and box[1] < box[3]):
        return None
    return box


def _clean(text: str) -> str:
    """Segment text MM2a accepts: no NUL, no line that opens a segment marker, capped."""

    lines = [line for line in text.replace("\r\n", "\n").replace("\x00", "").split("\n")]
    kept = [line for line in lines if not line.lstrip().startswith(_MARKER)]
    return "\n".join(kept).strip()[:MAX_SEGMENT_CHARS].strip()


def _span(read: list[int]) -> str:
    if read == list(range(read[0], read[-1] + 1)):
        return f"{read[0]}-{read[-1]}" if len(read) > 1 else f"{read[0]}"
    return ",".join(str(n) for n in read)


def to_proxy_candidate(
    result: OcrResult,
    *,
    attachment_id: str,
    source_sha256: str,
    media_type: str,
    privacy: str,
    created_at: datetime,
    language: str = "",
) -> dict[str, Any]:
    """The ``memfw.proxy`` v1 file for one result (see the module docstring).

    :class:`OcrError` ``no_text`` when the engine found no text at all (a proxy
    needs at least one segment).
    """

    media = result.media
    segments: list[dict[str, Any]] = []
    dropped = 0
    read: list[int] = []
    empty: list[int] = []
    for page in result.pages:
        if page.page is not None:
            read.append(page.page)
        found = False
        for line in page.lines:
            text = _clean(line.text)
            if not text:
                continue
            found = True
            box = _to_memfw_frame(page, media, line.bbox) if line.bbox is not None else None
            if line.bbox is not None and box is None:
                dropped += 1
            locator: dict[str, Any] = {}
            if page.page is not None:
                locator["page"] = page.page
            if box is not None:
                locator["bbox"] = box
            segment: dict[str, Any] = {"text": text}
            if locator:
                segment["locator"] = locator
            if line.confidence is not None:
                segment["confidence"] = round(line.confidence, 4)
            segments.append(segment)
        if page.page is not None and not found:
            empty.append(page.page)
    if not segments:
        raise OcrError("no_text")
    read.sort()
    if media == "pdf":
        full = read == list(range(1, result.total_pages + 1))
        detail = f"pages {_span(read)}/{result.total_pages}"
        if empty:
            detail += f"; no text on {len(empty)} page(s)"
    else:
        full, detail = True, "image"
    if dropped:
        detail += f"; {dropped} box(es) dropped"
    source: dict[str, Any] = {}
    if media == "pdf":
        source["pages"] = result.total_pages
        sizes: dict[int, list[int | float]] = {}
        for p in result.pages:
            if p.page is not None and p.unit == "pt" and p.width and p.height:
                sizes[p.page] = [_number(p.width), _number(p.height)]
        if len(sizes) == result.total_pages:
            listed = [sizes[n] for n in sorted(sizes)]
            if all(size == listed[0] for size in listed):
                source["page_size"] = listed[0]
            else:
                source["page_sizes"] = listed
    else:
        first = result.pages[0] if result.pages else None
        if first is not None and first.width is not None and first.height is not None:
            source["width"], source["height"] = _number(first.width), _number(first.height)
    candidate: dict[str, Any] = {
        "format": PROXY_FORMAT,
        "version": PROXY_FORMAT_VERSION,
        "kind": result.kind,
        "source_sha256": source_sha256,
        "attachment_id": attachment_id,
        "coverage": {"status": "full" if full else "partial", "detail": detail[:200]},
        "producer": {"tool": result.tool, "version": result.version},
        "created_at": created_at.astimezone(UTC).isoformat(),
        "privacy": privacy,
    }
    if language:
        candidate["language"] = language
    if source:
        candidate["source"] = source
    candidate["segments"] = segments
    return candidate


def check_format(candidate: dict[str, Any], media_type: str) -> None:
    """Validate with MM2a's own parser when it is installed (``memfw.ports.proxies``)."""

    if parse_proxy_import is None:
        return
    try:
        parse_proxy_import(candidate, media_type=media_type)
    except ValueError:
        raise OcrError("proxy_format") from None


# -- running a job -----------------------------------------------------------------


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _regular(path: Path) -> None:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        raise OcrError("source_missing") from None
    if stat.S_ISLNK(info.st_mode):
        raise OcrError("symlink_refused")
    if not stat.S_ISREG(info.st_mode):
        raise OcrError("not_a_file")


def _private_copy(source: Path, target: Path) -> str:
    digest = hashlib.sha256()
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(target, flags, 0o600)
    with open(source, "rb") as src, os.fdopen(fd, "wb") as dst:
        while chunk := src.read(CHUNK):
            digest.update(chunk)
            dst.write(chunk)
    os.chmod(target, 0o400)
    return digest.hexdigest()


def check_request(
    *, attachment_id: str, sha256: str, media_type: str, privacy: str, clearance: Collection[str]
) -> tuple[Media, str]:
    """``(media, privacy level)`` or :class:`OcrError`; the clearance must admit the level."""

    if not isinstance(attachment_id, str) or not _ATT_ID.fullmatch(attachment_id):
        raise OcrError("bad_attachment_id")
    if not isinstance(sha256, str) or not _SHA256.fullmatch(sha256):
        raise OcrError("bad_sha256")
    media = MEDIA_TYPES.get(str(media_type).strip().lower())
    if media is None:
        raise OcrError("unsupported_media_type")
    level = field_level(privacy)
    if level not in PRIVACY_LEVELS:
        raise OcrError("bad_privacy")
    if level not in clearance:
        raise OcrError("privacy_not_cleared")
    return media, level


def generate_proxy(
    source: Path,
    *,
    backend: OcrBackend,
    attachment_id: str,
    sha256: str,
    media_type: str,
    privacy: str,
    clearance: Collection[str] = DEFAULT_CLEARANCE,
    budget: OcrBudget | None = None,
    now: datetime | None = None,
    language: str = "",
) -> dict[str, Any]:
    """Run ``backend`` on a private copy of ``source`` and return the proxy candidate.

    Refused before anything runs: a bad id, hash or media type, a privacy level
    the clearance does not admit (by default ``local-private`` is not), a
    symlink or non-regular source, an original whose sha256 is not ``sha256``,
    or an unavailable backend. Refused after the run: an original that
    changed meanwhile (``source_changed``).
    """

    media, level = check_request(
        attachment_id=attachment_id,
        sha256=sha256,
        media_type=media_type,
        privacy=privacy,
        clearance=clearance,
    )
    source = Path(source)
    _regular(source)
    if sha256_file(source) != sha256:
        raise OcrError("sha_mismatch")
    availability = backend.available()
    if not availability.available:
        raise OcrError(availability.reason or "backend_unavailable")
    moment = now or datetime.now(UTC)
    tmp = Path(tempfile.mkdtemp(prefix="memfw-ocr-"))
    try:
        os.chmod(tmp, 0o700)
        suffix = ".pdf" if media == "pdf" else (Path(source).suffix.lower()[:8] or ".img")
        copy = tmp / f"original{suffix}"
        if _private_copy(source, copy) != sha256:
            raise OcrError("source_changed")
        work = tmp / "work"
        work.mkdir(mode=0o700)
        result = backend.extract(copy, media=media, budget=budget or OcrBudget(), workdir=work)
        if not isinstance(result, OcrResult) or result.media != media:
            raise OcrError("bad_result")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    _regular(source)
    if sha256_file(source) != sha256:
        raise OcrError("source_changed")
    kind = str(media_type).strip().lower()
    candidate = to_proxy_candidate(
        result,
        attachment_id=attachment_id,
        source_sha256=sha256,
        media_type=kind,
        privacy=level,
        created_at=moment,
        language=language,
    )
    check_format(candidate, kind)
    return candidate


def write_proxy(out: Path, candidate: dict[str, Any]) -> Path:
    """Write the candidate as JSON (``0600``); an existing file is never replaced."""

    payload = json.dumps(candidate, ensure_ascii=False, indent=1, sort_keys=True).encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(out, flags, 0o600)
    except FileExistsError:
        raise OcrError("out_exists") from None
    with os.fdopen(fd, "wb") as handle:
        handle.write(payload + b"\n")
        handle.flush()
        os.fsync(handle.fileno())
    return Path(out)


__all__ = [
    "DEFAULT_CLEARANCE",
    "MEDIA_TYPES",
    "PROXY_FORMAT",
    "PROXY_FORMAT_VERSION",
    "check_format",
    "check_request",
    "generate_proxy",
    "sha256_file",
    "to_proxy_candidate",
    "write_proxy",
]
