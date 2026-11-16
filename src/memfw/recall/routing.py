"""Query routing: exact-looking queries prefer lexical (FTS) providers.

The classifier is a pure function of the query text and never logs it. An
exact query (quotes, many digits, an id/hash/path/URL/version, an acronym,
a proper name, or a very short query) goes to the lexical providers only
when at least one is in the route; everything else goes to every provider.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

RouteKind = Literal["exact", "semantic"]

_QUOTED = re.compile(r"[\"“”「」『』'`].+?[\"“”「」『』'`]")
_HEX = re.compile(r"\b[0-9a-fA-F]{12,64}\b")
_LONG_NUMBER = re.compile(r"\d{6,}")
_VERSION = re.compile(r"\bv?\d+\.\d+(?:\.\d+)*\b")
_ACRONYM = re.compile(r"\b[A-Z][A-Z0-9]{3,}\b")
_URL = re.compile(r"\b[a-z][a-z0-9+.-]*://", re.IGNORECASE)
_PATHLIKE = re.compile(
    r"(?:^|\s)(?:~|\.{1,2})?/?[\w.-]+/[\w./-]+|\b[\w-]+\.(?:md|py|json|ya?ml|toml)\b"
)
_NAME_TOKEN = re.compile(r"^[A-Z][a-z]+(?:[-'][A-Z]?[a-z]+)?$")
_SHORT_QUERY_CHARS = 8
_DIGIT_RATIO = 0.3


def classify_query(query: str) -> RouteKind:
    """``exact`` for lookup-style queries, ``semantic`` otherwise."""

    text = query.strip()
    if not text:
        return "semantic"
    if _QUOTED.search(text):
        return "exact"
    if _URL.search(text) or _HEX.search(text) or _LONG_NUMBER.search(text):
        return "exact"
    if _VERSION.search(text) or _ACRONYM.search(text) or _PATHLIKE.search(text):
        return "exact"
    visible = [char for char in text if not char.isspace()]
    digits = sum(char.isdigit() for char in visible)
    if visible and digits / len(visible) >= _DIGIT_RATIO:
        return "exact"
    tokens = text.split()
    if 1 <= len(tokens) <= 3 and all(_NAME_TOKEN.fullmatch(token) for token in tokens):
        return "exact"
    if len(visible) <= _SHORT_QUERY_CHARS and len(tokens) <= 2:
        return "exact"
    return "semantic"


@dataclass(frozen=True, slots=True)
class RouteDecision:
    kind: RouteKind
    providers: tuple[str, ...]


class DefaultRouter:
    """Exact → lexical providers (if any), semantic → all, in the given order."""

    def route(
        self, query: str, providers: Sequence[str], *, lexical: frozenset[str]
    ) -> RouteDecision:
        kind = classify_query(query)
        names = tuple(providers)
        if kind == "exact":
            preferred = tuple(name for name in names if name in lexical)
            if preferred:
                return RouteDecision(kind, preferred)
        return RouteDecision(kind, names)


class AllProvidersRouter:
    """Every provider for every query (used when routing is turned off)."""

    def route(
        self, query: str, providers: Sequence[str], *, lexical: frozenset[str]
    ) -> RouteDecision:
        del lexical
        return RouteDecision(classify_query(query), tuple(providers))


__all__ = ["AllProvidersRouter", "DefaultRouter", "RouteDecision", "RouteKind", "classify_query"]
