"""Map QMD result paths back to memfw references. Standard library only.

QMD does not index a file under its own name. Its indexer stores the
*handelized* path (``handelize`` in QMD's ``store.js``): every run of
characters that is not a letter, a digit or ``$`` becomes ``-``, emoji become
hex code points, ``___`` becomes ``/``, and the file extension is kept. A
result's ``file`` is ``<collection>/<handelized path>``. The mapping is
lossy (``a b.md`` and ``a-b.md`` both become ``a-b.md``), so this adapter never
guesses a name from a result: it handelizes the names it already knows (the
projection manifest's ``memories/<id>.md`` paths, or the files under the
notes root) and looks the result up in that table. A result that is not in the
table, or that two known names map to, is dropped.

:func:`handelize` is a re-implementation of that published rule for QMD
2.5.3 (MIT), checked against outputs of the original in the contract tests;
no QMD code is included. Unicode categories come from Python's
``unicodedata``, which can differ from the JavaScript engine's for code points
added in newer Unicode versions. A name hit by such a difference usually just
fails to match and its results are dropped. In principle it could instead
produce the stored name of a *different* file; the ambiguity check catches
that only when both files are in the table, so it is not ruled out. The risk
is limited to names with recently added characters, and every hit is still
filtered (memory hits by the manifest and the vault; note hits by the note
file's own privacy level).

:func:`note_ref` builds ``kh:<path>#<heading>`` references. Characters a recall
reference may not contain (whitespace, controls, quotes, backslash, angle
brackets, backtick) and the separators ``%``, ``#``, ``)`` and ``]`` are
percent-encoded as UTF-8; everything else, CJK included, is kept.
:func:`parse_note_ref` reverses it.
"""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import unquote

__all__ = [
    "HEADING_RE",
    "encode_ref_part",
    "handelize",
    "note_ref",
    "parse_note_ref",
    "strip_collection",
]

#: The heading rule of memfw's notes index (``memfw.notes.index.HEADING_RE``).
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_EXT_RE = re.compile(r"(\.[a-z0-9]+)$", re.IGNORECASE)
_LAST_EXT_RE = re.compile(r"\.[^.]+$")
#: Characters percent-encoded in a ``kh:`` reference part (besides
#: whitespace and control characters).
_REF_ESCAPE = frozenset("%#\\\"'<>`)]")


def _category(char: str) -> str:
    return unicodedata.category(char)


def _keep(char: str) -> bool:
    """``[\\p{L}\\p{N}$]``: a letter, a number or ``$``."""

    return char == "$" or _category(char)[0] in "LN"


def _emoji_to_hex(segment: str) -> str:
    """``(?:\\p{So}\\p{Mn}?|\\p{Sk})+`` runs → dash-joined hex code points."""

    out: list[str] = []
    index = 0
    size = len(segment)
    while index < size:
        if _category(segment[index]) not in ("So", "Sk"):
            out.append(segment[index])
            index += 1
            continue
        run: list[str] = []
        while index < size:
            category = _category(segment[index])
            if category == "So":
                run.append(segment[index])
                index += 1
                if index < size and _category(segment[index]) == "Mn":
                    index += 1  # matched by the run, not kept
            elif category == "Sk":
                run.append(segment[index])
                index += 1
            else:
                break
        out.append("-".join(f"{ord(char):x}" for char in run))
    return "".join(out)


def _clean(text: str) -> str:
    """Replace each run of non-kept characters with ``-``; strip outer dashes."""

    out: list[str] = []
    in_run = False
    for char in text:
        if _keep(char):
            out.append(char)
            in_run = False
        elif not in_run:
            out.append("-")
            in_run = True
    return "".join(out).strip("-")


def handelize(path: str) -> str | None:
    """QMD 2.5.3's stored path for ``path`` (relative to its collection).

    Returns ``None`` where QMD's ``handelize`` throws (empty path, a file name
    with no usable character, or an empty result): such a file is never in
    QMD's index.
    """

    if not path or not path.strip():
        return None
    segments = [part for part in path.split("/") if part]
    last = segments[-1] if segments else ""
    stem = _LAST_EXT_RE.sub("", last, count=1)
    if not any(char == "$" or _category(char) in ("So", "Sk") or _keep(char) for char in stem):
        return None
    parts = path.replace("___", "/").split("/")
    cleaned: list[str] = []
    for index, segment in enumerate(parts):
        segment = _emoji_to_hex(segment)
        if index == len(parts) - 1:
            match = _EXT_RE.search(segment)
            ext = match.group(1) if match else ""
            name = segment[: -len(ext)] if ext else segment
            cleaned.append(_clean(name) + ext)
        else:
            cleaned.append(_clean(segment))
    result = "/".join(part for part in cleaned if part)
    return result or None


def strip_collection(file: str, collection: str) -> str | None:
    """The collection-relative path of a result ``file``, or ``None`` if foreign.

    Accepts ``<collection>/<path>`` (the ``query`` tool's ``file``) and the
    ``qmd://<collection>/<path>`` resource form.
    """

    if not isinstance(file, str):
        return None
    text = file.removeprefix("qmd://")
    prefix = collection + "/"
    if not text.startswith(prefix):
        return None
    rest = text[len(prefix) :]
    if not rest or rest.startswith("/") or "\\" in rest:
        return None
    if any(part in ("", ".", "..") for part in rest.split("/")):
        return None
    return rest


def encode_ref_part(text: str) -> str:
    """Percent-encode what a recall reference may not contain (see module docstring)."""

    out: list[str] = []
    for char in text:
        if _category(char) == "Cs":
            raise ValueError("lone surrogate in a reference")
        if char in _REF_ESCAPE or char.isspace() or _category(char)[0] in ("C", "Z"):
            out.append("".join(f"%{byte:02X}" for byte in char.encode("utf-8")))
        else:
            out.append(char)
    return "".join(out)


def note_ref(path: str, heading: str = "") -> str:
    """``kh:<path>`` or ``kh:<path>#<heading>``, encoded."""

    ref = "kh:" + encode_ref_part(path)
    if heading:
        ref += "#" + encode_ref_part(heading)
    return ref


def parse_note_ref(ref: str) -> tuple[str, str] | None:
    """``(path, heading)`` of a ``kh:`` reference built by :func:`note_ref`."""

    if not isinstance(ref, str) or not ref.startswith("kh:"):
        return None
    body = ref[3:]
    path, _, heading = body.partition("#")
    if not path:
        return None
    return unquote(path, errors="strict"), unquote(heading, errors="strict")


def heading_at(lines: list[str], line: int, *, body_start: int) -> str:
    """The notes-index heading of 1-based ``line``: the last heading at or above it.

    Lines before ``body_start`` (0-based index of the first body line, after
    the frontmatter) are never headings. ``""`` when there is none, or when
    ``line`` is out of range.
    """

    if not 1 <= line <= len(lines):
        return ""
    for index in range(line - 1, body_start - 1, -1):
        match = HEADING_RE.match(lines[index].rstrip("\r\n"))
        if match:
            return match.group(2).strip()
    return ""
