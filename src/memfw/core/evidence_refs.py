"""Parsing of ``evidence`` pointers that may name an attachment (MM3). Pure code.

The write gate (:mod:`memfw.core.epistemic`) classifies an evidence pointer by
its prefix only. This module holds the pure parsing the repository layer
needs to tell whether a pointer names a registered original:

* :func:`canonical_reference` folds the spellings a writer could use to hide a
  prefix or an identifier (Unicode compatibility forms, percent-encoding,
  invisible format characters such as U+200B);
* :func:`parse_attachment_reference` reads ``att:<id>[#locator]``;
* :func:`parse_locator` and :func:`check_locator` read and check a locator
  (design v2.1: PDF in pt with a bottom-left origin, images in px with a
  top-left origin, audio and video in seconds);
* :func:`embedded_hashes`, :func:`embedded_attachment_ids`,
  :func:`sha256_pointer_hex` and :func:`path_pointer` find what a ``sha256:``,
  ``file:`` or other raw pointer could be naming.

Nothing here reads the vault or the blob store.
"""

from __future__ import annotations

import base64
import binascii
import math
import re
import unicodedata
import urllib.parse
from dataclasses import dataclass
from typing import Final

#: The canonical prefix of a pointer to a registered original.
ATTACHMENT_PREFIX: Final = "att:"
#: Most characters a locator may have (it is a URI fragment, not a payload).
MAX_LOCATOR_LENGTH: Final = 512
#: Shortest hex prefix of a ``sha256:`` pointer that is compared against the
#: registered originals' hashes (a shorter one names nothing in particular).
MIN_SHA_PREFIX: Final = 8

# Same shape as ``memfw.attachments.records`` (``att-<uuid7>``); a test keeps
# the two in step. Matched case-insensitively here, on the canonical form.
_ATTACHMENT_ID_RE: Final = re.compile(
    r"att-[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
)
_SCHEME_RE: Final = re.compile(r"^([a-z][a-z0-9+.-]{0,31}):(.*)$", re.DOTALL | re.IGNORECASE)
#: Characters dropped between hex groups before looking for a 64-hex run:
#: whitespace (NBSP folds to a space under NFKC), punctuation used as a group
#: separator, and the Unicode dashes U+2010–U+2015 and minus U+2212.
_HEX_SEPARATORS: Final = frozenset(
    " \t\n\r\f\v,;:/\\|._-'\"()[]{}<>" + "".join(chr(c) for c in range(0x2010, 0x2016)) + "\u2212"
)
_HEX_RUN_RE: Final = re.compile(r"[0-9a-f]{64,}")
#: A run of base64/base64url characters long enough to hold a 32-byte digest
#: (43 characters unpadded).
_B64_RUN_RE: Final = re.compile(r"[A-Za-z0-9+/_-]{43,}")
_B64_DIGEST_CHARS: Final = 43
#: An ``att:`` pointer inside other text (``[shot](att:<id>#bbox=…)``).
_EMBEDDED_ATT_RE: Final = re.compile(
    r"att:(att-[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})"
    r"(?:#([a-z0-9=&,.:+_-]*))?"
)
#: A value that looks like a relative path: a directory part or a file extension.
_RELATIVE_PATH_RE: Final = re.compile(r"(^\.{1,2}/|/|\.[A-Za-z0-9]{1,8}$)")
_DECODE_ROUNDS: Final = 4

#: Locator keys and the media families that accept them.
_PDF: Final = "application/pdf"


def canonical_reference(reference: str) -> str:
    """``reference`` with its disguises removed: NFKC, percent-decoded, no ``Cf``.

    Decoding repeats (a few rounds) so a doubly encoded ``%2561tt:`` folds too.
    Used only to *match* pointers against attachments; the stored evidence
    string is never rewritten.
    """

    text = unicodedata.normalize("NFKC", str(reference))
    for _ in range(_DECODE_ROUNDS):
        decoded = urllib.parse.unquote(text)
        if decoded == text:
            break
        text = unicodedata.normalize("NFKC", decoded)
    text = "".join(char for char in text if unicodedata.category(char) != "Cf")
    return text.strip()


def split_scheme(reference: str) -> tuple[str, str] | None:
    """``(scheme, rest)`` of the canonical form, scheme lowercased; ``None`` without one."""

    match = _SCHEME_RE.match(canonical_reference(reference))
    if match is None:
        return None
    return match.group(1).lower(), match.group(2).strip()


@dataclass(frozen=True)
class Locator:
    """A checked locator. Unset parts are ``None``.

    ``bbox`` is ``(x0, y0, x1, y1)``: pt with a bottom-left origin on a PDF
    page, px with a top-left origin on an image or a video frame. ``t`` is a
    ``(start, end)`` time range in seconds (``end`` equals ``start`` for one
    instant); ``frame`` is one keyframe time in seconds.
    """

    page: int | None = None
    bbox: tuple[float, float, float, float] | None = None
    t: tuple[float, float] | None = None
    frame: float | None = None

    @property
    def keys(self) -> frozenset[str]:
        return frozenset(
            key
            for key, value in (
                ("page", self.page),
                ("bbox", self.bbox),
                ("t", self.t),
                ("frame", self.frame),
            )
            if value is not None
        )


@dataclass(frozen=True)
class AttachmentReference:
    """One ``att:`` pointer: the attachment id (lowercase) and its locator text."""

    attachment_id: str
    locator: str | None


def _number(text: str, what: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise ValueError(f"{what} must be a number, got {text!r}") from None
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{what} must be a finite non-negative number")
    return value


def parse_locator(text: str) -> Locator:
    """Parse ``page=3&bbox=x0,y0,x1,y1``, ``t=12.5,20``, ``frame=t:65`` and so on.

    Raises ``ValueError`` on an unknown or repeated key, a malformed value, an
    empty box or an inverted time range.
    """

    if not text:
        raise ValueError("empty locator")
    if len(text) > MAX_LOCATOR_LENGTH:
        raise ValueError(f"locator is longer than {MAX_LOCATOR_LENGTH} characters")
    seen: dict[str, str] = {}
    for part in text.split("&"):
        key, sep, value = part.partition("=")
        key = key.strip().lower()
        if not sep or not key or not value.strip():
            raise ValueError(f"locator part must be key=value, got {part!r}")
        if key in seen:
            raise ValueError(f"locator repeats {key!r}")
        seen[key] = value.strip()
    unknown = sorted(set(seen) - {"page", "bbox", "t", "frame"})
    if unknown:
        raise ValueError("unknown locator key: " + ", ".join(unknown))
    page: int | None = None
    if "page" in seen:
        if not seen["page"].isdigit() or int(seen["page"]) < 1:
            raise ValueError("page must be a positive integer")
        page = int(seen["page"])
    bbox: tuple[float, float, float, float] | None = None
    if "bbox" in seen:
        parts = seen["bbox"].split(",")
        if len(parts) != 4:
            raise ValueError("bbox must be x0,y0,x1,y1")
        x0, y0, x1, y1 = (_number(item.strip(), "bbox") for item in parts)
        if not (x0 < x1 and y0 < y1):
            raise ValueError("bbox must have x0 < x1 and y0 < y1")
        bbox = (x0, y0, x1, y1)
    t: tuple[float, float] | None = None
    if "t" in seen:
        parts = seen["t"].split(",")
        if len(parts) not in (1, 2):
            raise ValueError("t must be start or start,end in seconds")
        start = _number(parts[0].strip(), "t")
        end = _number(parts[1].strip(), "t") if len(parts) == 2 else start
        if end < start:
            raise ValueError("t must have start <= end")
        t = (start, end)
    frame: float | None = None
    if "frame" in seen:
        prefix, sep, value = seen["frame"].partition(":")
        if prefix.strip().lower() != "t" or not sep:
            raise ValueError("frame must be t:<seconds>")
        frame = _number(value.strip(), "frame")
    return Locator(page=page, bbox=bbox, t=t, frame=frame)


def _allowed_keys(media_type: str) -> tuple[frozenset[str], ...]:
    """The key sets a locator on this media type may use (one of them, exactly)."""

    if media_type == _PDF:
        return (frozenset({"page"}), frozenset({"page", "bbox"}))
    if media_type.startswith("image/"):
        return (frozenset({"bbox"}),)
    if media_type.startswith("audio/"):
        return (frozenset({"t"}),)
    if media_type.startswith("video/"):
        return (frozenset({"t"}), frozenset({"frame"}), frozenset({"frame", "bbox"}))
    return ()


def check_locator(
    locator: Locator,
    media_type: str,
    *,
    pages: int | None = None,
    width: float | None = None,
    height: float | None = None,
    duration: float | None = None,
) -> None:
    """Refuse (``ValueError``) a locator that does not fit the original.

    The keys must suit the media type (PDF: ``page`` [+ ``bbox``]; image:
    ``bbox``; audio: ``t``; video: ``t``, or ``frame`` [+ ``bbox``]); any
    other media type takes no locator. Bounds are checked against whichever
    of ``pages``/``width``/``height``/``duration`` the record carries (MM1a
    records carry none of them yet, so only the shape is checked).
    """

    allowed = _allowed_keys(media_type)
    if locator.keys not in allowed:
        if not allowed:
            raise ValueError(f"a {media_type} attachment takes no locator")
        spelled = " or ".join("+".join(sorted(keys)) for keys in allowed)
        raise ValueError(f"a {media_type} locator uses {spelled}")
    if locator.page is not None and pages is not None and locator.page > pages:
        raise ValueError(f"page {locator.page} is past the last page ({pages})")
    if locator.bbox is not None:
        _, _, x1, y1 = locator.bbox
        if width is not None and x1 > width:
            raise ValueError("bbox is wider than the original")
        if height is not None and y1 > height:
            raise ValueError("bbox is taller than the original")
    if duration is not None:
        ends = [locator.t[1]] if locator.t is not None else []
        if locator.frame is not None:
            ends.append(locator.frame)
        if any(end > duration for end in ends):
            raise ValueError("time is past the end of the original")


def is_attachment_reference(reference: str) -> bool:
    """Whether the canonical form of ``reference`` starts with ``att:`` (any case)."""

    scheme = split_scheme(reference)
    return scheme is not None and scheme[0] == "att"


def parse_attachment_reference(reference: str) -> AttachmentReference | None:
    """``att:<id>[#locator]`` → :class:`AttachmentReference`; ``None`` for another scheme.

    The id is matched case-insensitively and returned lowercase. Raises
    ``ValueError`` when the pointer is ``att:`` but the id is malformed. The
    locator text is returned unparsed (see :func:`parse_locator`).
    """

    scheme = split_scheme(reference)
    if scheme is None or scheme[0] != "att":
        return None
    rest = scheme[1]
    identifier, sep, locator = rest.partition("#")
    identifier = identifier.strip().lower()
    if not _ATTACHMENT_ID_RE.fullmatch(identifier):
        raise ValueError(f"att: must name an attachment id (att-<uuid7>), got {rest!r}")
    if sep and not locator.strip():
        raise ValueError("att: has an empty locator after '#'")
    return AttachmentReference(identifier, locator.strip() if sep else None)


def _folded(reference: str) -> str:
    return canonical_reference(reference).lower()


def _digest_from_base64(text: str) -> str | None:
    """The hex of ``text`` read as base64 or base64url, when it decodes to 32 bytes."""

    # Padding is tolerated in any amount (``…=``, ``…==``) and then redone.
    value = text.strip().rstrip("=")
    if not value or len(value) > 64:
        return None
    # base64url → base64, then a strict decode (no character is skipped).
    standard = value.replace("-", "+").replace("_", "/")
    padded = standard + "=" * (-len(standard) % 4)
    try:
        raw = base64.b64decode(padded.encode("ascii"), validate=True)
    except (binascii.Error, ValueError, UnicodeEncodeError):
        return None
    return raw.hex() if len(raw) == 32 else None


def embedded_hashes(reference: str) -> frozenset[str]:
    """Every sha256 digest the pointer *could* be spelling (lowercase hex).

    Best effort across encodings, on the canonical form:

    * hex: separators between hex groups are dropped (:data:`_HEX_SEPARATORS`)
      and every 64-character window of a hex run counts, so
      ``data:e3b0c442 98fc…``, ``e3b0-c442-…`` and a digest glued to other hex
      are all found;
    * base64/base64url: ``=`` is dropped anywhere, and every 43-character
      window of a base64 run counts when it decodes to 32 bytes, so
      ``sha256-<b64>`` (SRI), ``sha256=<b64>``, ``sha-256=:<b64>:``
      (Content-Digest) and a bare ``result:<b64>`` are found.

    The result is a candidate set, not a claim: the caller compares it with
    the registered originals' hashes, so a candidate that names no original
    changes nothing. Not found: a digest split across several pointers, or
    encrypted or otherwise transformed.
    """

    canonical = canonical_reference(reference)
    found: set[str] = set()
    compact = "".join(char for char in canonical.lower() if char not in _HEX_SEPARATORS)
    for match in _HEX_RUN_RE.finditer(compact):
        run = match.group(0)
        found.update(run[start : start + 64] for start in range(len(run) - 63))
    for match in _B64_RUN_RE.finditer(canonical.replace("=", "")):
        run = match.group(0)
        for start in range(len(run) - _B64_DIGEST_CHARS + 1):
            digest = _digest_from_base64(run[start : start + _B64_DIGEST_CHARS])
            if digest is not None:
                found.add(digest)
    return frozenset(found)


def embedded_attachment_references(reference: str) -> tuple[AttachmentReference, ...]:
    """``att:`` pointers inside a pointer that is not itself ``att:`` (link syntax etc.)."""

    scheme = split_scheme(reference)
    if scheme is not None and scheme[0] == "att":
        return ()
    return tuple(
        AttachmentReference(identifier, locator or None)
        for identifier, locator in _EMBEDDED_ATT_RE.findall(_folded(reference))
    )


def embedded_attachment_ids(reference: str) -> frozenset[str]:
    """Every ``att-<uuid7>`` in the canonical form (lowercase)."""

    return frozenset(_ATTACHMENT_ID_RE.findall(_folded(reference)))


def sha256_pointer_hex(reference: str) -> str | None:
    """The hex digits of a ``sha256:`` pointer (separators dropped), or ``None``.

    ``None`` too when the value is not hex or is shorter than
    :data:`MIN_SHA_PREFIX` characters.
    """

    scheme = split_scheme(reference)
    if scheme is None or scheme[0] != "sha256":
        return None
    value = scheme[1].split("#", 1)[0]
    # Any separator between hex groups (space, tab, comma, slash, colon …).
    digits = "".join(char for char in value.lower() if char.isalnum())
    if len(digits) >= MIN_SHA_PREFIX and all(char in "0123456789abcdef" for char in digits):
        return digits
    # ``sha256:<base64>`` / ``<base64url>`` of the 32-byte digest.
    return _digest_from_base64("".join(value.split()))


def path_pointer(reference: str) -> str | None:
    """The local path a pointer names, if it looks like one; ``None`` otherwise.

    ``file:/p``, ``file:///p``, ``file://localhost/p`` and ``file:~/p`` give
    the path; any other scheme gives one when its value is an absolute or
    ``~`` path (``data:/tmp/x.png``) or looks like a relative one (a directory
    part or a file extension: ``data:copies/s.png``, ``dataset:./s.png``).
    A relative value is returned as is; the caller decides what it is
    relative to.
    """

    scheme = split_scheme(reference)
    if scheme is None:
        return None
    name, rest = scheme
    rest = rest.split("#", 1)[0].split("?", 1)[0].strip()
    if rest.startswith("//"):
        authority, _, path = rest[2:].partition("/")
        if authority not in ("", "localhost"):
            return None
        rest = "/" + path
    if not rest:
        return None
    if name == "file":
        return rest
    if rest.startswith(("/", "~")) or _RELATIVE_PATH_RE.search(rest):
        return rest
    return None


__all__ = [
    "ATTACHMENT_PREFIX",
    "MAX_LOCATOR_LENGTH",
    "MIN_SHA_PREFIX",
    "AttachmentReference",
    "Locator",
    "canonical_reference",
    "check_locator",
    "embedded_attachment_ids",
    "embedded_attachment_references",
    "embedded_hashes",
    "is_attachment_reference",
    "parse_attachment_reference",
    "parse_locator",
    "path_pointer",
    "sha256_pointer_hex",
    "split_scheme",
]
