"""Resolve provider refs to memory ids plus filter metadata, or to segments.

A resolver is batch-shaped so an implementation can make one call for many
refs (R6 ``memfw resolve --ids-stdin``). This module touches no memfw
internals. Here:

- ``MemoryPathResolver`` for ``ref_kind="path"`` of the form
  ``memories/<id>.md`` (optionally under ``fleet-memory/``); other path forms
  are left unresolved. It delegates ids to any id resolver.
- ``SchemePathResolver``: path refs that start with a registered scheme
  (``kh:``, ``att:``) go to that scheme's resolver, every other path ref to
  the fallback (a ``MemoryPathResolver``, which leaves ``kh:``/``att:`` refs
  unresolved, as before).
- ``SegmentResolution``: what a scheme resolver returns for a ref that names
  something other than a memory, a note section (``kh:``) or, later, an
  attachment segment (``att:``). The coordinator keeps these apart from
  memory hits (``RecallResult.segments``).
- ``parse_kh_ref``: the ``kh:<path>#<heading>`` grammar the notes providers
  emit (QMD notes, PageIndex).

The vault-reading ``VaultIdResolver`` and the notes-index
``NotesSegmentResolver`` live in ``memfw.recall.builtin``.

**The ``att:`` extension point.** Attachment and proxy resolution belongs to
MM2b. An ``att:<id>#<locator>`` resolver plugs in with
``SchemePathResolver.register("att", resolver)`` (or ``schemes={"att": …}``
on ``memfw.recall.builtin.default_resolvers``): a ``Resolver`` whose
``resolve`` returns ``SegmentResolution(source="attachments", path=<id>,
anchor=<locator>, …)`` for each ref it can show, after its own privacy and
tenant checks, and omits the rest. No ``att:`` resolver is registered by
default, so ``att:`` refs stay unresolved.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable
from urllib.parse import unquote

from .policy import MemoryMeta

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_MEMORY_PATH_RE = re.compile(r"^(?:\./)?(?:fleet-memory/)?memories/([^/]+)\.md$")
_SCHEME_RE = re.compile(r"^[a-z][a-z0-9]{0,15}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")

#: Where a ``SegmentResolution`` comes from: note sections (``kh:``) and
#: attachment segments (``att:``, MM2b).
SEGMENT_SOURCES: tuple[str, ...] = ("notes", "attachments")
#: Longest snippet a segment may carry (the notes index's section cap is 4000).
MAX_SEGMENT_SNIPPET = 8000
_MAX_SEGMENT_FIELD = 1024


@dataclass(frozen=True, slots=True)
class Resolution:
    """A ref that names a memory. ``meta`` is ``None`` when it cannot be read."""

    memory_id: str
    meta: MemoryMeta | None


@dataclass(frozen=True, slots=True)
class SegmentResolution:
    """A ref that names a segment of a non-memory source, already cleared.

    ``source`` is one of ``SEGMENT_SOURCES``; ``path`` and ``anchor`` locate
    the segment (note path and heading; attachment id and locator).
    ``title``, ``anchor`` and ``snippet`` are exactly what that source's own
    search returns for the segment, never more. ``privacy`` is the segment's
    MM0 level (``public``/``shared``/``local-private``) as its resolver read
    it; ``tenant`` is the tenant the source is bound to. The coordinator
    filters both again against the call's policy.
    """

    source: str
    path: str
    anchor: str
    title: str
    snippet: str
    privacy: str
    tenant: str = ""

    @property
    def key(self) -> str:
        """The fusion key: two refs to the same segment fuse into one hit."""

        return f"{self.source}\x00{self.path}\x00{self.anchor}"


def valid_segment(value: object) -> bool:
    """Exactly a ``SegmentResolution`` with string fields inside their caps."""

    if type(value) is not SegmentResolution:
        return False
    fields = (value.path, value.anchor, value.title, value.privacy, value.tenant)
    if not all(type(item) is str and len(item) <= _MAX_SEGMENT_FIELD for item in fields):
        return False
    if type(value.snippet) is not str or len(value.snippet) > MAX_SEGMENT_SNIPPET:
        return False
    return value.source in SEGMENT_SOURCES and bool(value.path)


AnyResolution = Resolution | SegmentResolution


@runtime_checkable
class Resolver(Protocol):
    """Batch ref → resolution. A ref missing from the result is unresolved."""

    def resolve(self, refs: Sequence[str]) -> Mapping[str, AnyResolution]: ...


#: Path refs use the same protocol; this name marks the R6 PR-1 seam.
PathResolver = Resolver


class MemoryPathResolver:
    """``memories/<id>.md`` path refs → ids, then the id resolver."""

    def __init__(self, ids: Resolver) -> None:
        self.ids = ids

    @staticmethod
    def memory_id_of(path: str) -> str | None:
        match = _MEMORY_PATH_RE.fullmatch(path.strip())
        if match is None or not _ID_RE.fullmatch(match.group(1)):
            return None
        return match.group(1)

    def resolve(self, refs: Sequence[str]) -> Mapping[str, AnyResolution]:
        wanted = {ref: self.memory_id_of(ref) for ref in refs}
        ids = sorted({value for value in wanted.values() if value})
        found = self.ids.resolve(ids) if ids else {}
        out: dict[str, AnyResolution] = {}
        for ref, memory_id in wanted.items():
            if memory_id and memory_id in found:
                out[ref] = found[memory_id]
        return out


def ref_scheme(ref: str) -> str | None:
    """``kh`` for ``kh:…``: a short lowercase scheme before the first ``:``.

    ``None`` for anything else (a plain path, ``memories/<id>.md``).
    """

    scheme, sep, _rest = ref.partition(":")
    if not sep or not _SCHEME_RE.fullmatch(scheme):
        return None
    return scheme


class SchemePathResolver:
    """Path refs by scheme: ``<scheme>:…`` → that scheme's resolver, else the fallback.

    A ref whose scheme is registered never reaches the fallback; every other
    ref (an unregistered scheme included) goes to the fallback exactly as
    before, so with no scheme registered this is the fallback alone.
    """

    def __init__(self, fallback: Resolver, schemes: Mapping[str, Resolver] | None = None) -> None:
        self.fallback = fallback
        self.schemes: dict[str, Resolver] = {}
        for scheme, resolver in (schemes or {}).items():
            self.register(scheme, resolver)

    def register(self, scheme: str, resolver: Resolver) -> None:
        if not isinstance(scheme, str) or not _SCHEME_RE.fullmatch(scheme):
            raise ValueError("scheme must be 1-16 lowercase letters or digits")
        if scheme in self.schemes:
            raise ValueError(f"scheme {scheme!r} is already registered")
        self.schemes[scheme] = resolver

    def resolve(self, refs: Sequence[str]) -> Mapping[str, AnyResolution]:
        plain: list[str] = []
        by_scheme: dict[str, list[str]] = {}
        for ref in refs:
            scheme = ref_scheme(ref)
            if scheme is not None and scheme in self.schemes:
                by_scheme.setdefault(scheme, []).append(ref)
            else:
                plain.append(ref)
        out: dict[str, AnyResolution] = {}
        if plain:
            out.update(_only(self.fallback.resolve(plain), plain))
        for scheme in sorted(by_scheme):
            wanted = by_scheme[scheme]
            out.update(_only(self.schemes[scheme].resolve(wanted), wanted))
        return out


def _only(found: Mapping[str, AnyResolution], refs: Sequence[str]) -> dict[str, AnyResolution]:
    """The entries for ``refs`` only: a resolver cannot answer for another's refs."""

    return {ref: found[ref] for ref in refs if ref in found}


def parse_kh_ref(ref: str) -> tuple[str, str] | None:
    """``(path, heading)`` of ``kh:<path>`` / ``kh:<path>#<heading>``, or ``None``.

    The parts are percent-decoded as UTF-8 (strict), the encoding the notes
    providers use. The path must be relative and clean: no empty, ``.`` or
    ``..`` component, no leading ``/``, no backslash, no control character.
    The heading may be empty (the note's first section).
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


__all__ = [
    "MAX_SEGMENT_SNIPPET",
    "SEGMENT_SOURCES",
    "AnyResolution",
    "MemoryPathResolver",
    "PathResolver",
    "Resolution",
    "Resolver",
    "SchemePathResolver",
    "SegmentResolution",
    "parse_kh_ref",
    "ref_scheme",
    "valid_segment",
]
