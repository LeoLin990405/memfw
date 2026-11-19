"""Citation reference grammar (ports 0.4.0, W1-1). Standard library only.

A ``Citation`` (``memfw.ports.types``) may carry a ``ref``: one pointer in one
of four forms, each a spelling memfw already uses elsewhere.

======================  ===========================  ==============================
``ref_kind``            form                         points at
======================  ===========================  ==============================
``memory``              ``mem:<id>`` /               a memory (that version when
                        ``mem:<id>@v<n>``            ``@v<n>`` is given)
``note_segment``        ``kh:<path>`` /              one section of a note; the
                        ``kh:<path>#<heading>``      grammar of
                                                     ``memfw.recall.parse_kh_ref``
``attachment_segment``  ``att:<prx-id>:<n>``         segment ``n`` of an attachment
                                                     proxy (``SEGMENT_REF_RE``)
``wiki_page``           ``wiki:<page_key>`` /        a wiki page (or one section);
                        ``wiki:<page_key>#<sec>``    generated, never evidence
======================  ===========================  ==============================

Anything else, a conversation pointer (``conv:``) included, is not a citation
ref. A ref must be canonical: it must equal :func:`canonical_ref` of itself
(``kh:`` parts percent-encoded as ``note_ref`` does, no ``#`` without a
heading, no surrounding heading space), so one source has one spelling. Memory
ids starting with ``wiki-`` are reserved for wiki page records and are never
memory refs (``wiki_as_memory``). The kh grammar is a standard-library copy of
``memfw.recall.parse_kh_ref`` (``memfw.ports`` imports nothing else from
memfw); ``tests/test_wiki_ports.py`` checks the two agree.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Final, Literal, get_args
from urllib.parse import unquote

from .proxies import SEGMENT_REF_RE

CitationRefKind = Literal["memory", "note_segment", "attachment_segment", "wiki_page"]
CITATION_REF_KINDS: Final[tuple[str, ...]] = get_args(CitationRefKind)

#: Longest citation ref, the same bound as ``Candidate.ref``.
MAX_CITATION_REF_CHARS: Final = 512

#: The vault's memory id rule (``memfw.ports.types.MEMORY_ID_RE``; a test keeps them equal).
_MEMORY_ID_RE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_VERSION_RE: Final = re.compile(r"^[1-9][0-9]{0,8}$")
_CONTROL_RE: Final = re.compile(r"[\x00-\x1f\x7f]")

#: ``wiki-`` plus the first 8 bytes of a sha256, in lowercase hex.
WIKI_PAGE_KEY_RE: Final = re.compile(r"^wiki-[0-9a-f]{16}$")
#: The fixed page sections, in page order (``memfw-wiki-page/1``).
WIKI_SECTIONS: Final[tuple[str, ...]] = ("summary", "facts", "timeline", "relations", "open")

_PREFIXES: Final[tuple[tuple[str, CitationRefKind], ...]] = (
    ("mem:", "memory"),
    ("kh:", "note_segment"),
    ("att:", "attachment_segment"),
    ("wiki:", "wiki_page"),
)


#: Memory ids with this prefix are wiki page records (W1-3), never memory refs.
WIKI_RECORD_PREFIX: Final = "wiki-"

#: Characters ``note_ref`` percent-encodes besides spaces, control and separator
#: characters (the rule of the QMD notes adapter's ``encode_ref_part``).
_REF_ESCAPE: Final = frozenset("%#\\\"'<>`)]")


class CitationRefError(ValueError):
    """A citation ref that matches none of the four forms.

    ``code`` is ``malformed``, ``not_canonical`` or ``wiki_as_memory``.
    """

    def __init__(self, message: str, code: str = "malformed") -> None:
        super().__init__(message)
        self.code = code


def _encode_part(text: str) -> str:
    out: list[str] = []
    for char in text:
        category = unicodedata.category(char)
        if char in _REF_ESCAPE or char.isspace() or category[0] in ("C", "Z"):
            out.append("".join(f"%{byte:02X}" for byte in char.encode("utf-8")))
        else:
            out.append(char)
    return "".join(out)


def note_ref(path: str, heading: str = "") -> str:
    """The canonical ``kh:<path>[#<heading>]`` (encoded like the notes providers)."""

    ref = "kh:" + _encode_part(path)
    heading = heading.strip()
    if heading:
        ref += "#" + _encode_part(heading)
    return ref


def parse_memory_ref(ref: str) -> tuple[str, int | None] | None:
    """``(memory_id, version)`` of ``mem:<id>`` / ``mem:<id>@v<n>``, or ``None``."""

    if not isinstance(ref, str) or not ref.startswith("mem:"):
        return None
    body = ref[4:]
    memory_id, sep, raw_version = body.partition("@v")
    if not _MEMORY_ID_RE.fullmatch(memory_id):
        return None
    if not sep:
        return memory_id, None
    if not _VERSION_RE.fullmatch(raw_version):
        return None
    return memory_id, int(raw_version)


def parse_note_ref(ref: str) -> tuple[str, str] | None:
    """``(path, heading)`` of ``kh:<path>`` / ``kh:<path>#<heading>``, or ``None``.

    Same rules as ``memfw.recall.parse_kh_ref``: percent-decoded as strict
    UTF-8, a relative clean path (no empty, ``.`` or ``..`` part, no leading
    ``/``, no backslash, no control character), the heading stripped.
    """

    if not isinstance(ref, str) or not ref.startswith("kh:"):
        return None
    raw_path, _sep, raw_heading = ref[3:].partition("#")
    try:
        path = unquote(raw_path, errors="strict")
        heading = unquote(raw_heading, errors="strict")
    except UnicodeDecodeError:
        return None
    if not path or path.startswith("/") or "\\" in path or _CONTROL_RE.search(path):
        return None
    if any(part in ("", ".", "..") for part in path.split("/")):
        return None
    if _CONTROL_RE.search(heading):
        return None
    return path, heading.strip()


def parse_attachment_ref(ref: str) -> str | None:
    """The ``<prx-id>:<n>`` segment ref of ``att:<prx-id>:<n>``, or ``None``."""

    if not isinstance(ref, str) or not ref.startswith("att:"):
        return None
    segment = ref[4:]
    return segment if SEGMENT_REF_RE.fullmatch(segment) else None


def parse_wiki_ref(ref: str) -> tuple[str, str] | None:
    """``(page_key, section)`` of ``wiki:<page_key>[#<section>]`` (section may be ``""``)."""

    if not isinstance(ref, str) or not ref.startswith("wiki:"):
        return None
    page_key, sep, section = ref[5:].partition("#")
    if not WIKI_PAGE_KEY_RE.fullmatch(page_key):
        return None
    if sep and section not in WIKI_SECTIONS:
        return None
    return page_key, section


def canonical_ref(ref: str) -> str | None:
    """The one spelling of a valid ref, or ``None`` when ``ref`` is not one."""

    memory = parse_memory_ref(ref)
    if memory is not None:
        memory_id, version = memory
        return f"mem:{memory_id}" + ("" if version is None else f"@v{version}")
    note = parse_note_ref(ref)
    if note is not None:
        return note_ref(*note)
    if parse_attachment_ref(ref) is not None:
        return ref
    page = parse_wiki_ref(ref)
    if page is not None:
        return f"wiki:{page[0]}" + (f"#{page[1]}" if page[1] else "")
    return None


def citation_ref_kind(ref: object) -> CitationRefKind:
    """The kind of a citation ref. Raises :class:`CitationRefError` for anything else."""

    if type(ref) is not str or not ref or len(ref) > MAX_CITATION_REF_CHARS:
        raise CitationRefError("citation ref must be a string of 1 to 512 characters")
    for prefix, kind in _PREFIXES:
        if ref.startswith(prefix):
            parsed: object = {
                "memory": parse_memory_ref,
                "note_segment": parse_note_ref,
                "attachment_segment": parse_attachment_ref,
                "wiki_page": parse_wiki_ref,
            }[kind](ref)
            if parsed is None:
                raise CitationRefError(f"malformed {kind} citation ref")
            if kind == "memory" and ref[4:].startswith(WIKI_RECORD_PREFIX):
                raise CitationRefError(
                    "wiki- ids are wiki page records, not memories", "wiki_as_memory"
                )
            if canonical_ref(ref) != ref:
                raise CitationRefError("citation ref is not in canonical form", "not_canonical")
            return kind
    raise CitationRefError("citation ref must start with mem:, kh:, att: or wiki:")


__all__ = [
    "CITATION_REF_KINDS",
    "MAX_CITATION_REF_CHARS",
    "WIKI_PAGE_KEY_RE",
    "WIKI_RECORD_PREFIX",
    "WIKI_SECTIONS",
    "CitationRefError",
    "canonical_ref",
    "note_ref",
    "CitationRefKind",
    "citation_ref_kind",
    "parse_attachment_ref",
    "parse_memory_ref",
    "parse_note_ref",
    "parse_wiki_ref",
]
