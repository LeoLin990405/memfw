"""Operator recall wiring in ``vault.yaml``: the lifecycle meta-index and the notes index.

Both keys are off by default; without them every output is what it was::

    runtime:
      recall:
        # The lifecycle meta-index (``memfw.projection.load_lifecycle``). In
        # production it lives outside the vault, at ``<base>/status/``.
        lifecycle_source: /srv/memfw/status/memory-meta-index.json
        # The built-in notes index: ``kh:<path>#<heading>`` refs (QMD notes,
        # PageIndex) resolve to its sections in ``mode=two_stage``.
        notes:
          root: /srv/notes               # the Markdown tree the index covers
          index: /srv/notes/.notes.sqlite3
          legacy_privacy: local-private  # level of notes without ``privacy``
          tenant: ""                     # the tenant the notes are bound to

``lifecycle_source``
    Unset: ``two_stage`` reads ``<vault>/status/memory-meta-index.json`` when
    it exists (as before), and ``memory_context`` ``strict`` and the
    open-question queue and labels read the vault alone (as before). Set:
    every one of them joins documents with a ``fleet_path`` tag to that file
    (a missing or unreadable one fails closed: those documents are dropped by
    the strict policy).

``notes``
    Unset: no ``kh:`` resolver; ``kh:`` refs stay unresolved (as before).
    Set: ``memfw.recall.builtin.NotesSegmentResolver`` over ``index`` and
    ``root``; its clearance is the server's (``MEMFW_MCP_CLEARANCE``), else
    ``public,shared``. Sections come back under ``notes`` in the
    ``two_stage`` payload, apart from memory hits.

A ``vault.yaml`` that exists but cannot be read or parsed fails closed
(``Runtime.recall_wiring`` raises ``bad_config``). Relative paths are
relative to the vault root; ``~`` is expanded. A
malformed value raises ``RecallAdapterError`` (``bad_lifecycle_source``,
``bad_notes``, or ``bad_config`` for a ``runtime``/``recall`` that is not a
mapping), so the call fails instead of silently running without it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from memfw.ports.privacy import PRIVACY_LEVELS
from memfw.projection import ProjectionError, lifecycle_source_from_config

from .recall_adapters import RecallAdapterError

_NOTES_KEYS: Final = frozenset({"root", "index", "legacy_privacy", "tenant"})
_MAX_PATH = 4096


@dataclass(frozen=True, slots=True)
class NotesWiring:
    """``runtime.recall.notes``: the notes tree and index ``kh:`` refs resolve against."""

    root: Path
    index: Path
    legacy_privacy: str = "local-private"
    tenant: str = ""


@dataclass(frozen=True, slots=True)
class RecallWiring:
    """The checked ``runtime.recall`` wiring keys (both ``None``: the defaults)."""

    lifecycle_source: Path | None = None
    notes: NotesWiring | None = None


def _path(value: Any, root: Path, code: str) -> Path:
    if not isinstance(value, str) or not value.strip() or len(value) > _MAX_PATH:
        raise RecallAdapterError(code)
    if "\x00" in value:
        raise RecallAdapterError(code)
    path = Path(value.strip()).expanduser()
    return path if path.is_absolute() else Path(root) / path


def _notes(raw: Any, root: Path) -> NotesWiring | None:
    if raw is None:
        return None
    if not isinstance(raw, Mapping) or not set(raw) <= _NOTES_KEYS:
        raise RecallAdapterError("bad_notes")
    legacy = raw.get("legacy_privacy", "local-private")
    if legacy not in PRIVACY_LEVELS:
        raise RecallAdapterError("bad_notes")
    tenant = raw.get("tenant", "")
    if not isinstance(tenant, str):
        raise RecallAdapterError("bad_notes")
    return NotesWiring(
        root=_path(raw.get("root"), root, "bad_notes"),
        index=_path(raw.get("index"), root, "bad_notes"),
        legacy_privacy=str(legacy),
        tenant=tenant,
    )


def parse_wiring(config: Mapping[str, Any], *, root: Path) -> RecallWiring:
    """The ``runtime.recall`` wiring of a parsed ``vault.yaml`` (none without the keys)."""

    section = config.get("runtime")
    if section is None:
        return RecallWiring()
    if not isinstance(section, Mapping):
        raise RecallAdapterError("bad_config")
    recall = section.get("recall")
    if recall is None:
        return RecallWiring()
    if not isinstance(recall, Mapping):
        raise RecallAdapterError("bad_config")
    try:
        lifecycle = lifecycle_source_from_config(config, root)
    except ProjectionError:
        raise RecallAdapterError("bad_lifecycle_source") from None
    return RecallWiring(lifecycle_source=lifecycle, notes=_notes(recall.get("notes"), root))


__all__ = ["NotesWiring", "RecallWiring", "parse_wiring"]
