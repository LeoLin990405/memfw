"""Check ① of the design (2.4): token consistency.

Every *checkable token* in a claim must appear in the text of a source the
claim cites. The check is a necessary condition only: it catches a changed
number, version, date, id, path, quote or English name, not a sentence whose
meaning is reversed (that is the NLI check's job, W1-5).

Both sides are read after NFKC, with every Unicode decimal digit (``Nd``:
``٤٠``, ``४०``, full-width) mapped to ASCII, ``U+2212`` to ``-`` and the
financial CJK numerals (``壹贰叁肆伍陆柒捌玖拾佰仟``) to everyday ones.

Token kinds, extracted in this order (a span taken by an earlier kind is not
read again by a later one):

========== =============================================== ==========================
kind       claim side                                      supported when
========== =============================================== ==========================
``quote``  text in ``"…"``, ``“…”``, ``「…」``, ``『…』``,     its normalised text is in a
           backticks; the quote's own numbers, dates, …    source, at word boundaries
           are checked as tokens too                       where it starts/ends Latin,
                                                           never cutting a numeral
``path``   URLs, ``/abs``, ``~/x``, ``./x``, ``a/b.ext``,   in a source, whole or as a
           ``a/b/c`` (closing punctuation not included;     leading part (next char is
           a relative path is not part of an absolute one)
                                                           ``/``, space, punctuation
                                                           or the end)
``date``   ``2026-11-19``, ``2026/11/19``, ``2026.11.19``,  a source date with every
           ``2026年11月19日``, ``2026年11月``, ``11月19日``,     component the claim names
           ``Nov 19, 2026``, ``19 November 2026``           (year, month, day) equal
``time``   ``02:00``, ``3:00:15``                          the same clock time
``version`` ``v1.2``, ``1.2.3``, ``1.2.3.4``, ``1.2.3-rc1``  a source version or
                                                           decimal, ``v`` ignored
``id``     ``mem-2026…``, ``prx-ab12``, ``W1-2``, ``N76``,   substring at token
           ``H7``, ``#42``, hex runs of 7+ with a digit     boundaries, case folded
           and a letter
``number`` Arabic numbers (``1,000`` = ``1000``,            the same value among the
           ``1.50`` = ``1.5``, ``-5`` is not ``5``) and      source's numbers (Arabic
           CJK numerals before a counter (``三台``,          or CJK numerals anywhere)
           ``三 台``) or closing a sentence (``是三。``)      with a compatible unit
``name``   ALLCAPS words (``NAS``), CamelCase (``OpenAI``)  the word, case folded, at
           and capitalised Latin words not at a sentence    word boundaries
           start
========== =============================================== ==========================

Source texts go through the same extraction (quotes left open, so a number
inside a quoted source passage still counts). A source's numbers are its
number tokens and every CJK numeral run in it, each with the unit after it.
Units (``天``/days, ``小时``/hours, ``%``, weeks, months, sizes, …) are
compared: a claim number with a unit needs that unit in the source (``30 天``
is backed by neither ``30 hours`` nor a bare ``30``); a unitless claim number
is backed by the value with any unit. Scientific notation (``1e3``) is one
number. The parts of a source's dates and times back only a claim number with
a date or time unit (``月``, ``日``, ``号``, ``点``, ``时``, ``分``, ``年``, a
month name, am/pm, o'clock) or after the word "year" (``the year 2026``; a
bare ``2026`` is not backed by a date's year), so
``11 月`` is backed by ``2026-11-01`` and ``11 snapshots`` is not. The parts
of its versions are *not* numbers, so ``4 snapshots`` is not backed by
``restic 0.16.4``. Known limit: ``02:00 pm`` is read as the clock time
``2:00`` (the ``pm`` is not applied).

CJK numerals versus Arabic digits (decision): the two scripts are equivalent.
A claim's ``3`` is supported by a source's ``三`` and the other way round.
On the claim side a CJK numeral is a number token only when a counter follows
(``三台``, ``两次``, ``十二个``, whitespace allowed) or it closes a sentence
after a non-CJK character or a copula / quantity word (``是三。``), because
bare ``一`` is part of ordinary words (``一致``, ``统一。``); on the source
side every CJK numeral run counts. English
number words (``three``) and CJK-numeral dates (``二〇二六年``) are not
recognised on the claim side (they pass unchecked; NLI and the human review
cover them).
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Final

from .normalize import ascii_digits, normalize_text

TOKEN_KINDS: Final[tuple[str, ...]] = (
    "quote",
    "path",
    "date",
    "time",
    "version",
    "id",
    "number",
    "name",
)

_MONTHS: Final[dict[str, int]] = {
    name: index
    for index, names in enumerate(
        (
            ("jan", "january"),
            ("feb", "february"),
            ("mar", "march"),
            ("apr", "april"),
            ("may",),
            ("jun", "june"),
            ("jul", "july"),
            ("aug", "august"),
            ("sep", "sept", "september"),
            ("oct", "october"),
            ("nov", "november"),
            ("dec", "december"),
        ),
        start=1,
    )
    for name in names
}
_MONTH_ALT: Final = "|".join(sorted(_MONTHS, key=len, reverse=True))

_QUOTE_RE: Final = re.compile(
    r'"([^"\n]{1,300})"|“([^”\n]{1,300})”|「([^」\n]{1,300})」'
    r"|『([^』\n]{1,300})』|`([^`\n]{1,300})`"
)
_URL_RE: Final = re.compile(r"\b[a-z][a-z0-9+.-]{1,15}://[^\s　-〿＀-￯]+", re.I)
_PATH_RE: Final = re.compile(
    r"(?<![\w/.~-])(?:"
    r"(?:~|\.{1,2})?/[\w.@+-]+(?:/[\w.@+-]+)*/?"  # /abs, ~/x, ./x, ../x
    r"|[\w.@+-]+(?:/[\w.@+-]+){2,}/?"  # a/b/c
    r"|[\w.@+-]+(?:/[\w.@+-]+)*/[\w@+-]+\.[A-Za-z0-9]{1,8}"  # a/b.ext
    r")(?![\w/])",
    re.ASCII,
)
_DATE_PATTERNS: Final[tuple[tuple[re.Pattern[str], str], ...]] = (
    (re.compile(r"(?<!\d)(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?!\d)", re.ASCII), "ymd"),
    (re.compile(r"(?<!\d)(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]", re.ASCII), "ymd"),
    (re.compile(r"(?<!\d)(\d{4})\s*年\s*(\d{1,2})\s*月", re.ASCII), "ym"),
    (re.compile(r"(?<!\d)(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]", re.ASCII), "md"),
    (
        re.compile(
            r"\b(" + _MONTH_ALT + r")\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?(?!\d)",
            re.I | re.ASCII,
        ),
        "Mdy",
    ),
    (
        re.compile(
            r"(?<!\d)(\d{1,2})\s+(" + _MONTH_ALT + r")\.?,?\s+(\d{4})(?!\d)", re.I | re.ASCII
        ),
        "dMy",
    ),
)
_TIME_RE: Final = re.compile(r"(?<![\d:.])(\d{1,2}):(\d{2})(?::(\d{2}))?(?![\d:])", re.ASCII)
_VERSION_RE: Final = re.compile(
    r"(?<![\w.])(?:[vV](\d+(?:\.\d+){1,3})|(\d+(?:\.\d+){2,3}))(?:[-+]([0-9A-Za-z]+(?:\.[0-9A-Za-z]+)*))?(?!\w|\.\d)",
    re.ASCII,
)
_ID_RE: Final = re.compile(
    r"(?<![\w-])(?:"
    r"#\d+"
    r"|[A-Za-z][A-Za-z0-9]*(?:[-_][A-Za-z0-9]+)+"
    r"|[A-Za-z]{1,6}\d+[A-Za-z0-9]*"
    r"|[0-9a-f]{7,64}"
    r")(?![\w])",
    re.ASCII,
)
#: An Arabic number; a leading minus is part of it (``-5`` is not ``5``).
#: Scientific notation (``1e3``, ``2.5E-4``) is one number, not ``1`` and a word.
_ARABIC_RE: Final = re.compile(
    r"(?<![\w.])-?(?:\d{1,3}(?:,\d{3})+(?:\.\d+)?(?!\d)|\d+(?:\.\d+)?)"
    r"(?:[eE][+-]?\d+(?![\w.]))?",
    re.ASCII,
)
#: "year" right before a bare number (``the year 2026``) makes it a date's year.
_YEAR_WORD_RE: Final = re.compile(r"(?<![a-z])year\s*$", re.I)
_CJK_DIGITS: Final[dict[str, int]] = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}
_CJK_UNITS: Final[dict[str, int]] = {"十": 10, "百": 100, "千": 1000}
_CJK_BIG: Final[dict[str, int]] = {"万": 10_000, "亿": 100_000_000}
_CJK_NUM_CLASS: Final = "零〇一二两三四五六七八九十百千万亿"
#: Counters after which a CJK numeral in a claim is a quantity.
_COUNTERS: Final = (
    "个|台|次|天|日|周|星期|月|年|小时|分钟|秒|毫秒|条|句|页|篇|份|项|件|位|人|名|种|类|层|步|轮|"
    "核|线程|进程|倍|成|组|批|套|块|张|节|段|章|行|列|字|岁|号|点|处|版|部|本|元|度|分"
)
_CJK_QTY_RE: Final = re.compile("([" + _CJK_NUM_CLASS + "]+)(?=\\s*(?:" + _COUNTERS + "))")
#: A CJK numeral closing a sentence (``数量是三。``): after a non-CJK character or
#: a copula / quantity word, so ``统一。`` is not the number one.
_CJK_END_RE: Final = re.compile(
    "(?:(?<=[是为共有计约达至到第])|(?<![\u3400-\u9fff\uf900-\ufaff]))"
    "([" + _CJK_NUM_CLASS + "]+)(?=\\s*(?:[。！？!?；;.]|$))"
)
_CJK_RUN_RE: Final = re.compile("[" + _CJK_NUM_CLASS + "]+")
_WORD_RE: Final = re.compile(r"(?<![\w])[A-Za-z][A-Za-z0-9]*(?:'[a-z]+)?(?![\w])", re.ASCII)
_SENTENCE_START_RE: Final = re.compile(r"(?:^|[.!?;:。！？\n]\s*|[(\[\"'“‘]\s*)$")
#: Latin words a capital does not make a name.
_NOT_NAMES: Final = frozenset({"i", "ok"})
#: The unit after a number: ``(pattern, unit, clock)``. ``unit`` is compared
#: when both sides name one (``30 天`` is not backed by ``30 hours``); ``clock``
#: marks a date or time unit, the only kind a source's date and time parts
#: back (``11 月`` by ``2026-11-01``, never ``11 snapshots``). First match wins.
_UNITS: Final[tuple[tuple[re.Pattern[str], str | None, bool], ...]] = tuple(
    (re.compile(r"\s*(?:" + pattern + r")", re.I), unit, clock)
    for pattern, unit, clock in (
        (r"%|percent\b|per\s+cent\b|个百分点|百分点", "percent", False),
        (r"个月|months?\b", "month", False),
        (r"小时|hours?\b|hrs?\b", "hour", False),
        (r"分钟|minutes?\b|mins?\b", "minute", False),
        (r"毫秒|ms\b|milliseconds?\b", "millisecond", False),
        (r"秒|seconds?\b|secs?\b", "second", False),
        (r"天|days?\b", "day", False),
        (r"周|星期|weeks?\b", "week", False),
        (r"years?\b", "year", False),
        (r"次|times\b", "times", False),
        (r"[kmgtp]i?b\b|bytes?\b", "bytes", False),
        (r"年|月|日|号|点|时|分", None, True),
        (r"o'clock\b|[ap]\.?m\.?(?![a-z])", None, True),
        (r"(?:" + _MONTH_ALT + r")\b\.?", None, True),
    )
)
_SIZE_RE: Final = re.compile(r"\s*([kmgtp]i?b)\b", re.I)


@dataclass(frozen=True, slots=True)
class Token:
    """One checkable token: its kind and normalised value (never shown in reports)."""

    kind: str
    value: str
    date: tuple[int | None, int | None, int | None] | None = None
    unit: str | None = None
    clock: bool = False


def _mask(text: str, start: int, end: int) -> str:
    return text[:start] + " " * (end - start) + text[end:]


def _number_value(raw: str) -> str | None:
    """Canonical decimal string: thousands commas dropped, trailing zeros dropped."""

    digits = raw.replace(",", "")
    try:
        value = Decimal(digits)
    except InvalidOperation:
        return None
    if not value.is_finite():
        return None
    return format(value.normalize(), "f")


def cjk_number(run: str) -> int | None:
    """The value of a CJK numeral run (``十二`` 12, ``三百零五`` 305, ``二〇二六`` 2026)."""

    run = ascii_digits(run)  # financial numerals (壹贰叁…) as everyday ones
    if not run or any(char not in _CJK_NUM_CLASS for char in run):
        return None
    if all(char in _CJK_DIGITS for char in run):  # digit by digit: 二〇二六
        if len(run) == 1:
            return _CJK_DIGITS[run]
        if "两" in run:
            return None
        return int("".join(str(_CJK_DIGITS[char]) for char in run))
    total = section = 0
    digit: int | None = None
    for char in run:
        if char in _CJK_DIGITS:
            if digit is not None and _CJK_DIGITS[char] != 0:
                return None  # two digits in a row inside a positional numeral
            digit = _CJK_DIGITS[char] if _CJK_DIGITS[char] != 0 else None
        elif char in _CJK_UNITS:
            section += (1 if digit is None else digit) * _CJK_UNITS[char]
            digit = None
        else:  # 万 / 亿
            section += 0 if digit is None else digit
            if section == 0:
                return None
            total += section * _CJK_BIG[char]
            section = 0
            digit = None
    return total + section + (0 if digit is None else digit)


def _date(kind: str, groups: tuple[str | None, ...]) -> tuple[int | None, int | None, int | None]:
    def num(value: str | None) -> int | None:
        return None if value is None else int(value)

    if kind == "ymd":
        return num(groups[0]), num(groups[1]), num(groups[2])
    if kind == "ym":
        return num(groups[0]), num(groups[1]), None
    if kind == "md":
        return None, num(groups[0]), num(groups[1])
    if kind == "Mdy":
        month = _MONTHS[str(groups[0]).lower()]
        return num(groups[2]), month, num(groups[1])
    month = _MONTHS[str(groups[1]).lower()]  # dMy
    return num(groups[2]), month, num(groups[0])


def _valid_date(date: tuple[int | None, int | None, int | None]) -> bool:
    _year, month, day = date
    return (month is None or 1 <= month <= 12) and (day is None or 1 <= day <= 31)


def _at_sentence_start(text: str, start: int) -> bool:
    return bool(_SENTENCE_START_RE.search(text[:start]))


def _time(match: re.Match[str]) -> str | None:
    hour, minute = int(match.group(1)), int(match.group(2))
    second = None if match.group(3) is None else int(match.group(3))
    if hour > 24 or minute > 59 or (second is not None and second > 59):
        return None
    return f"{hour}:{minute:02d}" + ("" if second is None else f":{second:02d}")


def _unit_after(text: str, end: int) -> tuple[str | None, bool]:
    """The unit following a number that ends at ``end``: ``(unit, clock)``."""

    for pattern, unit, clock in _UNITS:
        match = pattern.match(text, end)
        if match is None:
            continue
        if unit == "bytes":
            size = _SIZE_RE.match(text, end)
            return (size.group(1).lower().replace("i", "") if size else "b"), False
        return unit, clock
    return None, False


def _number(value: str, text: str, end: int, start: int | None = None) -> Token:
    unit, clock = _unit_after(text, end)
    if not clock and start is not None and _YEAR_WORD_RE.search(text[:start]):
        clock = True  # ``the year 2026`` names a date's year; a bare ``2026`` does not
    return Token("number", value, unit=unit, clock=clock)


def extract_tokens(text: str, *, quotes: bool = True) -> list[Token]:
    """The checkable tokens of a claim, in kind order, then text order.

    ``quotes=False`` (used for source texts) leaves quoted text to the later
    kinds, so a number inside a quote in a source still counts as a number.
    """

    work = ascii_digits(unicodedata.normalize("NFKC", text))
    tokens: list[Token] = []
    inner_tokens: list[Token] = []
    for match in _QUOTE_RE.finditer(work) if quotes else ():
        inner = next(group for group in match.groups() if group is not None)
        value = normalize_text(inner)
        if value:
            tokens.append(Token("quote", value))
            # The quote's own numbers, dates, paths, … are checked too: a quote
            # cut out of a longer numeral (「十天」 from 三十天) must not pass.
            inner_tokens.extend(extract_tokens(inner, quotes=False))
        work = _mask(work, match.start(), match.end())
    for match in _URL_RE.finditer(work):
        value = normalize_text(match.group(0)).rstrip(".,;:!?").rstrip("/")
        tokens.append(Token("path", value))
        work = _mask(work, match.start(), match.end())
    for pattern, kind in _DATE_PATTERNS:
        for match in pattern.finditer(work):
            date = _date(kind, match.groups())
            if not _valid_date(date):
                continue
            tokens.append(Token("date", "-".join("?" if p is None else str(p) for p in date), date))
            work = _mask(work, match.start(), match.end())
    for match in _TIME_RE.finditer(work):
        clock = _time(match)
        if clock is not None:
            tokens.append(Token("time", clock))
            work = _mask(work, match.start(), match.end())
    for match in _PATH_RE.finditer(work):
        # A sentence's closing punctuation is not part of the path.
        value = normalize_text(match.group(0)).rstrip(".,;:!?").rstrip("/")
        tokens.append(Token("path", value))
        work = _mask(work, match.start(), match.end())
    for match in _VERSION_RE.finditer(work):
        core = match.group(1) or match.group(2)
        suffix = match.group(3)
        value = core + (f"-{suffix.lower()}" if suffix else "")
        tokens.append(Token("version", value))
        work = _mask(work, match.start(), match.end())
    for match in _ID_RE.finditer(work):
        raw = match.group(0)
        if raw.isdigit():
            continue  # a plain number, read below
        if re.fullmatch(r"[0-9a-f]{7,64}", raw) and not (
            re.search(r"\d", raw) and re.search(r"[a-f]", raw)
        ):
            continue
        if not re.search(r"\d", raw):
            continue  # hyphenated words (``read-only``) are not ids
        tokens.append(Token("id", raw.casefold()))
        work = _mask(work, match.start(), match.end())
    for match in _ARABIC_RE.finditer(work):
        amount = _number_value(match.group(0))
        if amount is not None:
            tokens.append(_number(amount, work, match.end(), match.start()))
        work = _mask(work, match.start(), match.end())
    for pattern in (_CJK_QTY_RE, _CJK_END_RE):
        for match in pattern.finditer(work):
            number = cjk_number(match.group(1))
            if number is not None:
                tokens.append(_number(str(number), work, match.end(), match.start()))
            else:
                tokens.append(Token("quote", normalize_text(match.group(1))))
            work = _mask(work, match.start(), match.end())
    for match in _WORD_RE.finditer(work):
        word = match.group(0)
        letters = re.sub(r"[^A-Za-z]", "", word)
        if len(letters) < 2 or word.casefold() in _NOT_NAMES:
            continue
        allcaps = letters.isupper()
        camel = bool(re.search(r"[a-z][A-Z]", word))
        capital = word[0].isupper() and not _at_sentence_start(work, match.start())
        if allcaps or camel or capital:
            tokens.append(Token("name", word.casefold()))
    return tokens + inner_tokens


#: ``/`` on the left too: a relative path is not backed by an absolute one.
_PATH_LEFT: Final = r"(?<![a-z0-9_.~@+\-/])"
#: Characters a quote edge that is a digit or CJK numeral must not continue into.
_NUMERAL_EDGE: Final = "0-9" + _CJK_NUM_CLASS
_PATH_RIGHT: Final = r"(?![a-z0-9_@+\-~]|\.[a-z0-9_])"


def _ascii_word(char: str) -> bool:
    return bool(char) and char.isascii() and (char.isalnum() or char == "_")


@dataclass(slots=True)
class SourceIndex:
    """What a source text offers the token check (built once per source)."""

    text: str
    #: Each number value and the units it appears with (``None``: no unit).
    numbers: dict[str, set[str | None]] = field(default_factory=dict)
    #: Values of the source's date and time parts (back clock numbers only).
    clock_numbers: set[str] = field(default_factory=set)
    dates: list[tuple[int | None, int | None, int | None]] = field(default_factory=list)
    versions: set[str] = field(default_factory=set)
    times: set[str] = field(default_factory=set)

    @classmethod
    def build(cls, raw: str) -> SourceIndex:
        """The same extraction as for claims (quotes left open), plus every CJK numeral.

        Date and time components back only a claim number with a date or
        time unit (a claim's ``11 月`` is backed by a source's ``2026-11-01``,
        ``11 snapshots`` is not); version parts back no number (a claim's
        ``4 snapshots`` is not backed by ``restic 0.16.4``).
        """

        prepared = ascii_digits(unicodedata.normalize("NFKC", raw))
        index = cls(text=normalize_text(prepared))
        for token in extract_tokens(raw, quotes=False):
            if token.kind == "number":
                index.numbers.setdefault(token.value, set()).add(token.unit)
                if "." in token.value:
                    index.versions.add(token.value)
            elif token.kind == "date":
                assert token.date is not None
                index.dates.append(token.date)
                index.clock_numbers.update(str(part) for part in token.date if part is not None)
            elif token.kind == "time":
                index.times.add(token.value)
                index.clock_numbers.update(str(int(part)) for part in token.value.split(":"))
            elif token.kind == "version":
                index.versions.add(token.value)
                index.versions.add(token.value.split("-", 1)[0])
        for match in _CJK_RUN_RE.finditer(prepared):
            number = cjk_number(match.group(0))
            if number is not None:
                unit, _clock = _unit_after(prepared, match.end())
                index.numbers.setdefault(str(number), set()).add(unit)
        return index

    def _contains(self, value: str, *, word: bool) -> bool:
        if not value:
            return False
        if not word:
            return value in self.text
        pattern = r"(?<![0-9a-z_])" + re.escape(value) + r"(?![0-9a-z_]|-[0-9a-z])"
        return re.search(pattern, self.text) is not None

    def _bounded(self, value: str, left: str, right: str) -> bool:
        if not value:
            return False
        return re.search(left + re.escape(value) + right, self.text) is not None

    def supports(self, token: Token) -> bool:
        if token.kind == "quote":
            # Word boundaries where the quote starts or ends with a Latin
            # letter or digit: "keep it" is not in "keep items".
            # A quote edge that is a digit or CJK numeral must not continue a
            # longer numeral in the source: 「十天」 is not in 三十天.
            first, last = token.value[:1], token.value[-1:]
            left = r"(?<![0-9a-z_])" if _ascii_word(first) else ""
            right = r"(?![0-9a-z_])" if _ascii_word(last) else ""
            if first and (first.isdigit() or first in _CJK_NUM_CLASS):
                left += "(?<![" + _NUMERAL_EDGE + "])"
            if last and (last.isdigit() or last in _CJK_NUM_CLASS):
                right += "(?![" + _NUMERAL_EDGE + "])"
            return self._bounded(token.value, left, right)
        if token.kind == "path":
            # A whole path or a whole leading part of one: ``/srv/a`` is in
            # ``/srv/a/b``, ``/srv/backup/rest`` is not in ``/srv/backup/restic``.
            return self._bounded(token.value, _PATH_LEFT, _PATH_RIGHT)
        if token.kind == "date":
            assert token.date is not None
            return any(
                all(
                    want is None or (have is not None and have == want)
                    for want, have in zip(token.date, date, strict=True)
                )
                for date in self.dates
            )
        if token.kind == "time":
            return token.value in self.times
        if token.kind == "version":
            return token.value in self.versions
        if token.kind == "id":
            return self._contains(token.value, word=True)
        if token.kind == "number":
            units = self.numbers.get(token.value)
            # A claim number with a unit needs the same unit in the source; a
            # unitless claim number is backed by the value with any unit.
            if units and (token.unit is None or token.unit in units):
                return True
            return token.clock and token.value in self.clock_numbers
        return self._contains(token.value, word=True)  # name


def unsupported_kinds(tokens: Iterable[Token], sources: Iterable[SourceIndex]) -> list[str]:
    """Kinds (in ``TOKEN_KINDS`` order, each once) of tokens no source supports."""

    pool = list(sources)
    missing = {token.kind for token in tokens if not any(src.supports(token) for src in pool)}
    return [kind for kind in TOKEN_KINDS if kind in missing]


def iter_token_kinds(tokens: Iterable[Token]) -> Iterator[str]:
    for token in tokens:
        yield token.kind


__all__ = [
    "TOKEN_KINDS",
    "SourceIndex",
    "Token",
    "cjk_number",
    "extract_tokens",
    "unsupported_kinds",
]
