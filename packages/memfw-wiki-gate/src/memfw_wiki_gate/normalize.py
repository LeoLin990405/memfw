"""Text normalisation and the normalised sentence hash.

One rule everywhere (token checks, loop detection, "unchanged sentence" diffs):

1. Unicode NFKC (full-width digits and Latin letters become ASCII, compatibility
   forms fold);
2. case folding;
3. every run of whitespace becomes one space, leading and trailing space goes.

The sentence hash additionally drops the sentence's closing punctuation and
surrounding quote marks, so ``"Backups run nightly."`` and ``backups run
nightly`` hash the same, while any change of a word, digit or CJK character
gives a new hash.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import Final

_SPACE_RE: Final = re.compile(r"\s+")
#: Characters stripped from both ends before hashing a sentence.
_EDGE: Final = " .,;:!?。，；：！？、…\"'`“”‘’「」『』()[]（）"
#: Where a source text is split into sentences (for loop detection).
_SENTENCE_END_RE: Final = re.compile(r"(?<=[。！？!?;；])|(?<=\.)(?=\s)|\n+")
#: Shortest normalised sentence (in code points) the loop check compares.
LOOP_MIN_CHARS: Final = 12


#: Financial (anti-fraud) CJK numerals and their everyday forms.
_FINANCIAL: Final = str.maketrans("壹贰叁肆伍陆柒捌玖拾佰仟", "一二三四五六七八九十百千")


def ascii_digits(text: str) -> str:
    """Every Unicode decimal digit (``Nd``: ``٤``, ``४``, ``４``) as its ASCII digit.

    ``U+2212 MINUS SIGN`` becomes ``-`` and the financial CJK numerals
    (``壹贰叁…拾佰仟``) their everyday forms, so both sides of the token check
    read one spelling of every number.
    """

    text = text.replace("−", "-").translate(_FINANCIAL)
    if text.isascii():
        return text
    return "".join(
        str(unicodedata.decimal(char)) if unicodedata.category(char) == "Nd" else char
        for char in text
    )


def has_format_character(text: str) -> bool:
    """True when ``text`` holds any ``Cf`` character (ZWJ and ZWNJ included)."""

    return any(unicodedata.category(char) == "Cf" for char in text)


_LETTER_RUN_RE: Final = re.compile(r"[^\W\d_]+")
_CONFUSABLE_SCRIPTS: Final = frozenset({"CYRILLIC", "GREEK"})


def mixed_script_word(text: str) -> bool:
    """True when a word of ``text`` mixes Latin with Cyrillic or Greek letters.

    ``cаuses`` with a Cyrillic ``а`` looks like a lexicon word but is not
    one; such a claim is dropped rather than guessed at. Known cost: a unit
    written with the micro sign (``5µs``, NFKC ``μ``) counts as mixed.
    """

    for match in _LETTER_RUN_RE.finditer(unicodedata.normalize("NFKC", text)):
        scripts = {unicodedata.name(char, "").split(" ", 1)[0] for char in match.group(0)}
        if "LATIN" in scripts and scripts & _CONFUSABLE_SCRIPTS:
            return True
    return False


def matching_copy(text: str, *, keep_spaces: bool) -> str:
    """A copy of ``text`` for lexicon matching only (never shown, never stored).

    NFKD, case folded, with every ``Cf`` (ZWJ, ZWNJ, bidi controls), ``Mn``
    and ``Me`` character removed (combining accents and variation selectors
    are ``Mn``), then NFC again. ``keep_spaces=False`` also removes every
    whitespace character (Chinese markers: ``导 致`` reads as ``导致``);
    ``keep_spaces=True`` collapses whitespace runs to one space (English
    markers need word boundaries).
    """

    decomposed = unicodedata.normalize("NFKD", text).casefold()
    kept = "".join(
        char for char in decomposed if unicodedata.category(char) not in ("Cf", "Mn", "Me")
    )
    kept = unicodedata.normalize("NFC", kept)
    if keep_spaces:
        return _SPACE_RE.sub(" ", kept).strip()
    return _SPACE_RE.sub("", kept)


def normalize_text(text: str) -> str:
    """NFKC, case folded, whitespace collapsed to single spaces and trimmed."""

    return _SPACE_RE.sub(" ", unicodedata.normalize("NFKC", text).casefold()).strip()


def normalize_sentence(text: str) -> str:
    """:func:`normalize_text` without closing punctuation or surrounding quotes."""

    return normalize_text(text).strip(_EDGE)


def sentence_hash(text: str) -> str:
    """``sha256:<hex>`` of :func:`normalize_sentence` (UTF-8)."""

    return "sha256:" + hashlib.sha256(normalize_sentence(text).encode("utf-8")).hexdigest()


def split_sentences(text: str) -> list[str]:
    """The sentences of a source text, normalised; empty pieces left out."""

    pieces = _SENTENCE_END_RE.split(unicodedata.normalize("NFKC", text))
    out = []
    for piece in pieces:
        sentence = normalize_sentence(piece)
        if sentence:
            out.append(sentence)
    return out


def sentence_hashes(text: str, *, min_chars: int = LOOP_MIN_CHARS) -> frozenset[str]:
    """Hashes of every sentence of ``text`` at least ``min_chars`` long once normalised."""

    return frozenset(
        "sha256:" + hashlib.sha256(sentence.encode("utf-8")).hexdigest()
        for sentence in split_sentences(text)
        if len(sentence) >= min_chars
    )


__all__ = [
    "LOOP_MIN_CHARS",
    "ascii_digits",
    "has_format_character",
    "matching_copy",
    "mixed_script_word",
    "normalize_sentence",
    "normalize_text",
    "sentence_hash",
    "sentence_hashes",
    "split_sentences",
]
