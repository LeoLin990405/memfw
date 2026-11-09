"""Turn a user query into a quoted FTS5 OR expression.

The readonly service keeps an equivalent copy in
``scripts/memfw_readonly_service.py`` because that process must not import
``memfw``. ``tests/test_fts_term_query.py`` checks the two copies agree.
"""

from __future__ import annotations

import re

_TERM = re.compile(r"\w+", re.UNICODE)
MAX_TERMS = 16


def fts_query_from_terms(terms: list[str]) -> str | None:
    """Quote terms and join them with OR.

    Tokens shorter than two characters are dropped. Comparison is casefolded,
    and the first spelling is kept. At most ``MAX_TERMS`` terms are used.
    ``None`` means the caller should return an empty result. ``AND``, ``NEAR``,
    ``*`` and quotes never reach FTS as syntax.
    """

    seen: set[str] = set()
    chosen: list[str] = []
    for token in terms:
        key = token.casefold()
        if len(token) < 2 or key in seen:
            continue
        seen.add(key)
        chosen.append(token)
        if len(chosen) == MAX_TERMS:
            break
    if not chosen:
        return None
    return " OR ".join('"' + term.replace('"', '""') + '"' for term in chosen)


def fts_terms(query: str) -> list[str]:
    """Return the regex tokens ``fts_query`` would quote, in order."""

    seen: set[str] = set()
    out: list[str] = []
    for token in _TERM.findall(query):
        key = token.casefold()
        if len(token) < 2 or key in seen:
            continue
        seen.add(key)
        out.append(token)
        if len(out) == MAX_TERMS:
            break
    return out


def fts_query(query: str) -> str | None:
    """Build the match string from regex tokens. No CJK word segmentation."""

    return fts_query_from_terms(_TERM.findall(query))
