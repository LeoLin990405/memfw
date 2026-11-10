"""Turn a user query into a quoted FTS5 OR expression.

``query_terms`` is the one term builder for both index tokenizers. The
readonly service keeps a stdlib copy of the trigram rules in
``scripts/memfw_readonly_service.py`` so that path runs without ``memfw``;
for a unicode61 index it calls ``query_terms`` itself, because the terms
must be segmented by the same jieba call that segmented the stored text.
``tests/test_fts_term_query.py`` checks the two paths agree.
"""

from __future__ import annotations

import re
from collections.abc import Callable

_TERM = re.compile(r"\w+", re.UNICODE)
MAX_TERMS = 16


def jieba_segmenter() -> Callable[[str], list[str]]:
    """Segmenter used to store and to query a unicode61 index."""

    try:
        import jieba  # type: ignore[import-untyped]
    except ImportError as exc:
        raise RuntimeError(
            "SQLite FTS5 trigram is unavailable and jieba is required for the unicode61 fallback; "
            "install the declared jieba dependency"
        ) from exc
    return lambda text: [token for token in jieba.lcut(text, cut_all=False) if token.strip()]


def query_terms(query: str, tokenizer: str = "trigram") -> list[str]:
    """Terms ``fts_query_from_terms`` would quote for an index of ``tokenizer``.

    A trigram index stores raw text, so terms are the ``\\w+`` regex tokens.
    A unicode61 index stores jieba segments, so the query is segmented the
    same way. Both then use the same length, casefold-dedupe and
    ``MAX_TERMS`` rules.
    """

    if tokenizer == "unicode61":
        return _choose(jieba_segmenter()(query))
    return _choose(_TERM.findall(query))


def fts_match(query: str, tokenizer: str = "trigram") -> str | None:
    """Quoted OR match for ``query`` against an index of ``tokenizer``."""

    return fts_query_from_terms(query_terms(query, tokenizer))


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


def _choose(tokens: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for token in tokens:
        key = token.casefold()
        if len(token) < 2 or key in seen:
            continue
        seen.add(key)
        out.append(token)
        if len(out) == MAX_TERMS:
            break
    return out


def fts_terms(query: str) -> list[str]:
    """Return the regex tokens ``fts_query`` would quote, in order."""

    return query_terms(query, "trigram")


def fts_query(query: str) -> str | None:
    """Build the match string from regex tokens. No CJK word segmentation."""

    return fts_query_from_terms(_TERM.findall(query))
