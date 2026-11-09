"""Fold a label before alias comparison.

NFKC, then case folding, then Traditional-to-Simplified, then one trailing
``@host`` suffix, then hyphen, underscore, and whitespace become a single
space. The character map is OpenCC's preferred one-character form. A
traditional character with several simplified forms uses the first of those
forms, so distinct traditional characters can share a key (for example 發 and
髮). Phrase-level exceptions are not applied.
"""

from __future__ import annotations

import re
import unicodedata

from .t2s import to_simplified

_SEPARATORS = re.compile(r"[\s_\-]+")
_HOST = re.compile(r"@[^\s@]+$")


def normalise(label: str) -> str:
    """Return the comparison key for one alias label."""
    text = unicodedata.normalize("NFKC", str(label)).casefold()
    text = to_simplified(text)
    text = _HOST.sub("", text)
    return _SEPARATORS.sub(" ", text).strip()
