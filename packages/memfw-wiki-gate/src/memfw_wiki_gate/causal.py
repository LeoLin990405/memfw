"""G5: what a sentence's wording asserts, from a fixed zh + en lexicon.

A wiki claim carries a causal label (``none``, ``association``,
``hypothesis``; never ``verified``) that the format already caps at its cited
sources. The wording must not say more than the label: a page may not turn
"may be related" into "causes". :func:`asserted_causal` reads the wording:

* a **causal marker** (导致, 造成, 触发, 因为, 由于, 所以, causes, leads to,
  because, triggered, due to, therefore, thus, hence, ", so ", …) with a
  **hedge** (可能, 也许, 推测, may, might, possibly, suggests, …) asserts
  ``hypothesis``;
* a causal marker without a hedge, or with a **certainty marker** (已证实,
  证明了, 毫无疑问, proven, definitely, …), asserts ``verified``: it states the
  cause as settled, which no wiki claim may do;
* an **association marker** (相关性, 正相关, 存在关联, 伴随, correlated,
  co-occurs, …) without a causal marker asserts ``association``. Plain
  "related" words (相关的配置, associated with an account, linked to a page)
  are left out on purpose: they mostly mean "about", not a statistical link;
* otherwise ``none``.

:func:`asserts_certainty` flags certainty markers anywhere: a claim is at
most ``inference``, so wording that presents something as proven is above its
epistemic label even without a causal marker.

Deliberately conservative (design 2.2 G5: causal wording in ``summary`` /
``facts`` is moved to ``relations`` or dropped): negation is not parsed, so
"并未导致" / "did not cause" still counts as causal wording, and a timeline
sentence such as "因为磁盘满了被取代" is dropped. A negated *modal* is the
exception: "could not", "may not", "cannot", "不可能" and "不太可能" are not
hedges (they do not soften a cause into a hypothesis), and "unlikely" is not
in the lexicon.

Matching runs on copies of the text only (the claim text itself is never
changed): NFKD, case folded, every ``Cf``, ``Mn`` and ``Me`` character
removed (zero-width joiners, combining accents, variation selectors). The
Chinese copy also drops all whitespace and maps the traditional forms of the
markers' characters to simplified ones (導致, 引發, 觸發, 因為); Chinese markers
are substrings of it. English markers are whole words of the other copy;
``", so "`` counts as a connective, and a capitalised "May" before a day or
year, or after "in", is the month, not a hedge. The gate itself drops a claim
with any ``Cf`` character or with a word mixing Latin and Cyrillic/Greek
letters before this lexicon is read. The lexicon is data (``LEXICON``) so a
reviewer can read all of it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from .normalize import matching_copy

LEXICON: Final[dict[str, dict[str, tuple[str, ...]]]] = {
    "causal": {
        "zh": (
            "导致",
            "造成",
            "触发",
            "引起",
            "引发",
            "致使",
            "使得",
            "因为",
            "由于",
            "所以",
            "因此",
            "因而",
            "从而",
            "以致",
            "以至于",
            "归因于",
            "源于",
            "所致",
            "起因",
            "根源",
            "原因是",
            "的原因",
            "结果是",
            "之所以",
        ),
        "en": (
            "cause",
            "causes",
            "caused",
            "causing",
            "because",
            "trigger",
            "triggers",
            "triggered",
            "triggering",
            "thus",
            "due to",
            "owing to",
            "lead to",
            "leads to",
            "led to",
            "leading to",
            "result in",
            "results in",
            "resulted in",
            "resulting in",
            "as a result",
            "therefore",
            "hence",
            "consequently",
            "thereby",
            "attributed to",
            "stem from",
            "stems from",
            "stemmed from",
            "stemming from",
            "responsible for",
            "as a consequence",
            "root cause",
        ),
    },
    "hedge": {
        "zh": (
            "可能",
            "也许",
            "或许",
            "大概",
            "推测",
            "猜测",
            "疑似",
            "似乎",
            "假设",
            "怀疑",
            "未必",
            "不一定",
            "尚不确定",
            "待验证",
            "有待验证",
            "或可",
        ),
        "en": (
            "may",
            "might",
            "could",
            "possibly",
            "perhaps",
            "likely",
            "probably",
            "suspected",
            "hypothesis",
            "hypothesised",
            "hypothesized",
            "appears to",
            "appear to",
            "seems to",
            "seem to",
            "suggests",
            "suggest",
            "plausibly",
            "potentially",
        ),
    },
    "association": {
        "zh": (
            "相关性",
            "正相关",
            "负相关",
            "呈相关",
            "有关联",
            "存在关联",
            "相互关联",
            "伴随",
            "同时出现",
        ),
        "en": (
            "correlated",
            "correlates",
            "correlation",
            "association between",
            "co-occur",
            "co-occurs",
            "co-occurred",
            "coincides",
            "coincided",
        ),
    },
    "certainty": {
        "zh": (
            "已证实",
            "被证实",
            "证实了",
            "证明了",
            "已证明",
            "确证",
            "毫无疑问",
            "无疑",
            "必然",
            "一定是",
            "肯定是",
        ),
        "en": (
            "proven",
            "proves",
            "proved",
            "definitely",
            "certainly",
            "undoubtedly",
            "conclusively",
            "without doubt",
            "without a doubt",
            "beyond doubt",
        ),
    },
}

CAUSAL_ORDER: Final[tuple[str, ...]] = ("none", "association", "hypothesis", "verified")
_RANK: Final[dict[str, int]] = {value: rank for rank, value in enumerate(CAUSAL_ORDER)}


def _compile(words: tuple[str, ...]) -> re.Pattern[str]:
    alternatives = "|".join(
        re.escape(word).replace(r"\ ", r"\s+") for word in sorted(words, key=len, reverse=True)
    )
    return re.compile(r"(?<![a-z0-9])(?:" + alternatives + r")(?![a-z0-9])")


_EN: Final[dict[str, re.Pattern[str]]] = {
    group: _compile(entries["en"]) for group, entries in LEXICON.items()
}


#: Traditional forms of the characters the Chinese markers use (导致 = 導致,
#: 引发 = 引發, 触发 = 觸發, 因为 = 因為, 由于 = 由於, 证实 = 證實, …). Matching only.
_TRAD: Final = str.maketrans(
    "導發觸為於證實確關聯隨現許設懷驗結從歸無測時負問",
    "导发触为于证实确关联随现许设怀验结从归无测时负问",
)
#: ", so " as a connective (not "so far", "so that" is still a purpose clause
#: and counts: conservative).
_SO_RE: Final = re.compile(r"[,;]\s*so(?![a-z0-9])")
#: A negated modal is not a hedge: "could not", "may not", "cannot".
_NEGATED_EN_RE: Final = re.compile(
    r"(?<![a-z0-9])(?:(?:may|might|could|can)\s+not|cannot|couldn['’]t|mightn['’]t|mayn['’]t)"
    r"(?![a-z0-9])"
)
_NEGATED_ZH: Final = ("不太可能", "不可能")
#: Capitalised "May" before a day or year, or after "in", is the month.
_MONTH_MAY_RE: Final = re.compile(r"\bMay(?=\.?\s+\d)|(?<![A-Za-z])(?i:in)\s+May\b")


@dataclass(frozen=True, slots=True)
class _Copies:
    zh: str
    en: str


def _copies(text: str) -> _Copies:
    """Matching copies: Cf/Mn/Me removed (``导\u200d致``, ``导\u0301致``, ``导\ufe0f致``
    read as ``导致``); the Chinese one without whitespace and in simplified form."""

    text = _MONTH_MAY_RE.sub(" ", text)
    zh = matching_copy(text, keep_spaces=False).translate(_TRAD)
    en = matching_copy(text, keep_spaces=True)
    return _Copies(zh=zh, en=en)


def _has(group: str, copies: _Copies) -> bool:
    zh, en = copies.zh, copies.en
    if group == "hedge":
        for negated in _NEGATED_ZH:
            zh = zh.replace(negated, "")
        en = _NEGATED_EN_RE.sub(" ", en)
    if any(marker in zh for marker in LEXICON[group]["zh"]):
        return True
    if group == "causal" and _SO_RE.search(en):
        return True
    return _EN[group].search(en) is not None


def asserted_causal(text: str) -> str:
    """The causal grade the wording of ``text`` asserts (one of ``CAUSAL_ORDER``)."""

    copies = _copies(text)
    if _has("causal", copies):
        if _has("certainty", copies) or not _has("hedge", copies):
            return "verified"
        return "hypothesis"
    if _has("association", copies):
        return "association"
    return "none"


def asserts_certainty(text: str) -> bool:
    """True when ``text`` presents something as proven or certain."""

    return _has("certainty", _copies(text))


def causal_rank(value: str) -> int:
    """Rank of a grade in ``CAUSAL_ORDER`` (an unknown grade ranks highest)."""

    return _RANK.get(value, len(CAUSAL_ORDER))


__all__ = [
    "CAUSAL_ORDER",
    "LEXICON",
    "asserted_causal",
    "asserts_certainty",
    "causal_rank",
]
