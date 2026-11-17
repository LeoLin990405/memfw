"""Privacy levels and clearance for the vault's exits (MM0). Standard library only.

A memory, note, attachment or proxy has one of three privacy levels, from
least to most restricted: ``public``, ``shared``, ``local-private``. Anything
else is ``unknown``, which no clearance ever admits (fail closed).

The level a reader acts on is the strictest of:

* the ``privacy`` field (case-insensitive). A missing field takes the
  ``legacy_default`` (``local-private`` unless the vault sets
  ``legacy_privacy_default``); a present but unrecognised value is
  ``unknown``;
* every ``privacy:<value>`` tag: ``public`` → public, ``shared`` and
  ``fleet`` → shared, ``local-private`` → local-private, the recall "no
  restriction" spellings (``none``, ``null``, empty) add nothing, anything
  else → unknown;
* every ``sensitivity:<value>`` tag, read like
  ``memfw.projection.tag_privacy_class``: ``private`` → local-private, any
  other value → unknown.

Tags can only make a memory stricter, never looser. A clearance is the set of
levels an exit may return. ``None`` means the exit has no clearance configured
and filters nothing (the behaviour before MM0).

``memfw/index/read_path.py`` holds a standard-library copy of these rules for
the read-only HTTP service, which runs without ``memfw`` installed;
``tests/test_mm0_privacy_clearance.py`` checks the two agree.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Final, Literal

PrivacyLevel = Literal["public", "shared", "local-private", "unknown"]

#: The levels a document may carry, least to most restricted.
PRIVACY_LEVELS: Final[tuple[str, ...]] = ("public", "shared", "local-private")
_RANK: Final[dict[str, int]] = {"public": 0, "shared": 1, "local-private": 2, "unknown": 3}

#: Level of a document with no ``privacy`` field when the vault sets nothing.
LEGACY_PRIVACY_FALLBACK: Final[str] = "local-private"
#: ``vault.yaml`` key for the level of documents with no ``privacy`` field.
LEGACY_PRIVACY_KEY: Final[str] = "legacy_privacy_default"
#: The clearance the design gives remote exits (HTTP, OKF). Not applied by
#: default yet: an exit filters only when a clearance is configured.
REMOTE_CLEARANCE: Final[frozenset[str]] = frozenset({"public", "shared"})
#: What a malformed clearance becomes: the narrowest non-empty one, so a typo
#: can never widen access.
MALFORMED_CLEARANCE: Final[frozenset[str]] = frozenset({"public"})

_TAG_NO_RESTRICTION: Final[frozenset[str]] = frozenset({"", "none", "null"})
_TAG_SHARED: Final[frozenset[str]] = frozenset({"shared", "fleet"})


#: ``vault.yaml`` key: the level stamped on new writes that set none (unset: none stamped).
NEW_PRIVACY_KEY: Final[str] = "new_privacy_default"


def is_looser(after: str, before: str) -> bool:
    """True when ``after`` admits more readers than ``before`` (``unknown`` is strictest)."""

    return _RANK.get(after, _RANK["unknown"]) < _RANK.get(before, _RANK["unknown"])


def imported_value(raw: Any) -> str | None:
    """A ``privacy`` value carried by a candidate or import: absent → ``None``.

    A present value that is not one of the three levels becomes
    ``local-private`` (fail closed) instead of an error.
    """

    if raw is None:
        return None
    level = field_level(raw)
    return LEGACY_PRIVACY_FALLBACK if level == "unknown" else level


def privacy_value(raw: Any) -> str | None:
    """A write-side ``privacy`` value in canonical form, or ``None`` when absent.

    Accepts the three levels in any case and surrounding space. Raises
    ``ValueError`` for anything else, so a write never stores an unknown level.
    """

    if raw is None:
        return None
    if isinstance(raw, str):
        value = raw.strip().lower()
        if value in PRIVACY_LEVELS:
            return value
    raise ValueError("privacy must be one of: " + ", ".join(PRIVACY_LEVELS))


def field_level(raw: Any) -> PrivacyLevel:
    """Level of a present ``privacy`` field value. Unrecognised is ``unknown``."""

    if isinstance(raw, str):
        value = raw.strip().lower()
        if value in PRIVACY_LEVELS:
            return value  # type: ignore[return-value]
    return "unknown"


def legacy_default(raw: Any) -> str:
    """``legacy_privacy_default`` from vault config; missing or invalid → ``local-private``."""

    if isinstance(raw, str) and raw.strip().lower() in PRIVACY_LEVELS:
        return raw.strip().lower()
    return LEGACY_PRIVACY_FALLBACK


def legacy_default_from_config(config: Mapping[str, Any] | None) -> str:
    """The legacy default a parsed ``vault.yaml`` mapping sets."""

    if not isinstance(config, Mapping):
        return LEGACY_PRIVACY_FALLBACK
    return legacy_default(config.get(LEGACY_PRIVACY_KEY))


def tag_levels(tags: Iterable[Any]) -> list[PrivacyLevel]:
    """Levels the ``privacy:*`` and ``sensitivity:*`` tags impose (any case)."""

    levels: list[PrivacyLevel] = []
    for tag in tags:
        text = str(tag)
        lowered = text.lower()
        if lowered.startswith("privacy:"):
            value = lowered.split(":", 1)[1].strip()
            if value in _TAG_NO_RESTRICTION:
                continue
            if value == "public":
                levels.append("public")
            elif value in _TAG_SHARED:
                levels.append("shared")
            elif value == "local-private":
                levels.append("local-private")
            else:
                levels.append("unknown")
        elif lowered.startswith("sensitivity:"):
            value = lowered.split(":", 1)[1].strip()
            levels.append("local-private" if value == "private" else "unknown")
    return levels


def strictest(levels: Iterable[str]) -> PrivacyLevel:
    """The most restricted of ``levels``; an unrecognised entry is ``unknown``."""

    best: PrivacyLevel = "public"
    for level in levels:
        current: PrivacyLevel = level if level in _RANK else "unknown"  # type: ignore[assignment]
        if _RANK[current] > _RANK[best]:
            best = current
    return best


def effective_level(
    privacy: Any,
    tags: Iterable[Any] = (),
    *,
    present: bool | None = None,
    legacy: str = LEGACY_PRIVACY_FALLBACK,
) -> PrivacyLevel:
    """The level a reader acts on (strictest wins, see the module docstring).

    ``present`` says whether the source carried a ``privacy`` key; by default
    a ``None`` value means it did not. A present ``None`` (``privacy:`` with no
    value) is ``unknown``.
    """

    has_field = (privacy is not None) if present is None else present
    base = field_level(privacy) if has_field else legacy_default(legacy)
    if isinstance(tags, (str, bytes)) or not isinstance(tags, Iterable):
        tags = ()
    return strictest([base, *tag_levels(tags)])


def metadata_level(
    metadata: Mapping[str, Any], legacy: str = LEGACY_PRIVACY_FALLBACK
) -> PrivacyLevel:
    """Level of one parsed frontmatter mapping (its ``privacy`` field and ``tags`` list).

    A ``privacy`` key with no value reads as absent, like ``MemoryDocument``.
    """

    tags = metadata.get("tags")
    return effective_level(
        metadata.get("privacy"),
        tags if isinstance(tags, list) else (),
        legacy=legacy,
    )


def parse_clearance(value: str | Iterable[str] | None) -> frozenset[str] | None:
    """An exit's clearance. ``None`` or blank → ``None`` (no filtering configured).

    A string is comma separated; tokens are case-insensitive. A value with any
    token outside the three levels, or with none at all after a non-blank
    input, is malformed and becomes ``MALFORMED_CLEARANCE``.
    """

    if value is None:
        return None
    if isinstance(value, str):
        if not value.strip():
            return None
        tokens = value.split(",")
    else:
        tokens = list(value)
    cleaned = {str(token).strip().lower() for token in tokens if str(token).strip()}
    if not cleaned or not cleaned <= set(PRIVACY_LEVELS):
        return MALFORMED_CLEARANCE
    return frozenset(cleaned)


def clearance_is_valid(value: str | Iterable[str] | None) -> bool:
    """True when ``value`` is unset or names only the three levels."""

    if value is None or (isinstance(value, str) and not value.strip()):
        return True
    tokens = value.split(",") if isinstance(value, str) else list(value)
    cleaned = {str(token).strip().lower() for token in tokens if str(token).strip()}
    return bool(cleaned) and cleaned <= set(PRIVACY_LEVELS)


def admits(clearance: frozenset[str] | None, level: str) -> bool:
    """Whether an exit with ``clearance`` may return something at ``level``."""

    if clearance is None:
        return True
    return level != "unknown" and level in clearance


__all__ = [
    "LEGACY_PRIVACY_FALLBACK",
    "LEGACY_PRIVACY_KEY",
    "MALFORMED_CLEARANCE",
    "NEW_PRIVACY_KEY",
    "PRIVACY_LEVELS",
    "REMOTE_CLEARANCE",
    "PrivacyLevel",
    "admits",
    "clearance_is_valid",
    "effective_level",
    "field_level",
    "imported_value",
    "is_looser",
    "legacy_default",
    "legacy_default_from_config",
    "metadata_level",
    "parse_clearance",
    "privacy_value",
    "strictest",
    "tag_levels",
]
