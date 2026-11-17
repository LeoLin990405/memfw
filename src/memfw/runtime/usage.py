"""The ``runtime:`` section of ``vault.yaml`` and the ``memory_used`` signal (C5, C6).

Two switches, both off the default path (no section, no variable: exactly
the behaviour before them):

``runtime.usage.enabled`` (``MEMFW_USAGE_SIGNAL``), default ``false``
    Registers the ``memory_used`` MCP tool and wires
    ``memfw.usage.SqliteUsageSink``. Off, the tool is not listed (``tools/list``
    is byte-identical) and the Runtime's sink is ``NullUsageSink``.

``runtime.search.record_access`` (``MEMFW_RECORD_ACCESS``), default ``true``
    Whether ``memory_search`` ``hybrid``/``vector`` and ``two_stage`` append a
    ``memory_access`` event per returned hit and warm a cold or stub hit, as
    they always have. ``false`` is the session-safe setting: no search mode
    writes the vault (see ``docs/ports-and-recall.md`` section 4.5).

A set variable wins over ``vault.yaml`` (``1``/``true``/``yes``/``on`` or
``0``/``false``/``no``/``off``; any other value refuses to start). The
section is read on first use and then kept for the Runtime's life (restart
the server to pick up a change), but only once it was read: while the vault
is missing or ``vault.yaml`` is unreadable, every use reads again and gets the
closed answer (usage off, access not recorded). A value that is not a boolean
also fails closed.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from memfw.core.errors import MemfwError
from memfw.core.repository import Repository
from memfw.ports.usage import APP_RULE, MAX_USAGE_IDS, MEMORY_ID_RULE, SESSION_REF_RULE, UsageSignal

_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})


def env_switch(environ: Mapping[str, str], name: str) -> bool | None:
    """``name`` as a boolean, or ``None`` when unset or empty; anything else raises."""

    raw = environ.get(name, "").strip().lower()
    if not raw:
        return None
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    raise ValueError(f"{name} must be one of 1, true, yes, on, 0, false, no, off")


@dataclass(frozen=True, slots=True)
class RuntimeOptions:
    """The resolved switches (see the module docstring)."""

    usage_enabled: bool = False
    record_access: bool = True


def _switch(section: Any, group: str, key: str, default: bool, closed: bool) -> bool:
    if not isinstance(section, Mapping):
        return default if section is None else closed
    part = section.get(group)
    if part is None:
        return default
    if not isinstance(part, Mapping):
        return closed
    value = part.get(key, default)
    return value if isinstance(value, bool) else closed


def options_from_config(config: Mapping[str, Any]) -> RuntimeOptions:
    """``RuntimeOptions`` from a ``vault.yaml`` mapping (no ``runtime:``: the defaults)."""

    section = config.get("runtime")
    return RuntimeOptions(
        usage_enabled=_switch(section, "usage", "enabled", False, False),
        record_access=_switch(section, "search", "record_access", True, False),
    )


#: What a missing vault or an unreadable ``vault.yaml`` gives: closed on both switches.
CLOSED = RuntimeOptions(usage_enabled=False, record_access=False)


def read_options(repository: Repository | None) -> tuple[RuntimeOptions, bool]:
    """The vault's options and whether they may be cached.

    No vault, or a ``vault.yaml`` that cannot be read or validated, gives
    ``CLOSED`` (usage off, access not recorded) and ``False``: the caller
    reads again next time instead of keeping a guess.
    """

    if repository is None:
        return CLOSED, False
    try:
        config = repository.read_config()
    except (MemfwError, OSError, ValueError):
        return CLOSED, False
    return options_from_config(config), True


def checked_used_request(ids: object, session_ref: object) -> tuple[tuple[str, ...], str]:
    """``memory_used`` arguments: well-formed distinct ids (malformed ones dropped), the ref.

    Raises ``ValueError`` on the shape only (never on whether an id exists):
    ``ids`` must be a list of 1 to ``MAX_USAGE_IDS`` strings and
    ``session_ref`` a token matching ``SESSION_REF_RULE``.
    """

    if not isinstance(ids, list) or not all(isinstance(item, str) for item in ids):
        raise ValueError("ids must be a list of memory ids")
    if not ids:
        raise ValueError("ids required")
    if len(ids) > MAX_USAGE_IDS:
        raise ValueError(f"at most {MAX_USAGE_IDS} ids")
    if not isinstance(session_ref, str) or not SESSION_REF_RULE.fullmatch(session_ref):
        raise ValueError(
            "session_ref must be 1-200 characters: letters, digits and _ . : @ + = - (no spaces)"
        )
    wanted = tuple(dict.fromkeys(item for item in ids if MEMORY_ID_RULE.fullmatch(item)))
    return wanted, session_ref


def signal_app(app: str) -> str:
    """The calling app as a signal records it: lower case, or ``""`` when unnamed or odd."""

    name = (app or "").strip().lower()
    return name if APP_RULE.fullmatch(name) else ""


def readable_ids(repository: Repository, wanted: tuple[str, ...], visible: Any) -> tuple[str, ...]:
    """The ids of ``wanted`` that exist and ``visible`` keeps; the rest silently dropped.

    Read-only: ``Repository.get`` parses the file and warms nothing.
    """

    kept: list[str] = []
    for memory_id in wanted:
        try:
            document = repository.get(memory_id)
        except (MemfwError, OSError, ValueError):
            continue
        if visible(document):
            kept.append(memory_id)
    return tuple(kept)


__all__ = [
    "CLOSED",
    "RuntimeOptions",
    "UsageSignal",
    "checked_used_request",
    "env_switch",
    "options_from_config",
    "read_options",
    "readable_ids",
    "signal_app",
]
