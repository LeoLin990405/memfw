"""Fold a label before alias comparison.

NFKC, then case folding, then a Traditional-to-Simplified pass, then one
trailing ``@host`` suffix, then hyphen, underscore, and whitespace become a
single space. Characters missing from the conversion table stay as written.
"""

from __future__ import annotations

import re
import unicodedata

_SEPARATORS = re.compile(r"[\s_\-]+")
_HOST = re.compile(r"@[^\s@]+$")

# One-to-one Traditional → Simplified. This is not a complete orthography.
_T2S = str.maketrans(
    {
        "揮": "挥",
        "憶": "忆",
        "國": "国",
        "這": "这",
        "個": "个",
        "們": "们",
        "時": "时",
        "間": "间",
        "發": "发",
        "對": "对",
        "與": "与",
        "從": "从",
        "會": "会",
        "過": "过",
        "還": "还",
        "開": "开",
        "關": "关",
        "門": "门",
        "長": "长",
        "東": "东",
        "車": "车",
        "馬": "马",
        "魚": "鱼",
        "鳥": "鸟",
        "龍": "龙",
        "學": "学",
        "習": "习",
        "書": "书",
        "說": "说",
        "語": "语",
        "話": "话",
        "電": "电",
        "腦": "脑",
        "網": "网",
        "絡": "络",
        "機": "机",
        "後": "后",
        "裏": "里",
        "裡": "里",
        "爲": "为",
        "為": "为",
        "於": "于",
        "麼": "么",
        "體": "体",
        "係": "系",
        "統": "统",
        "專": "专",
        "記": "记",
        "項": "项",
        "並": "并",
        "臺": "台",
        "廣": "广",
        "產": "产",
        "業": "业",
        "點": "点",
        "線": "线",
        "頭": "头",
        "題": "题",
        "實": "实",
        "現": "现",
        "應": "应",
        "該": "该",
        "經": "经",
        "進": "进",
        "運": "运",
        "選": "选",
        "連": "连",
        "達": "达",
        "邊": "边",
    }
)


def normalise(label: str) -> str:
    """Return the comparison key for one alias label."""
    text = unicodedata.normalize("NFKC", str(label)).casefold()
    text = text.translate(_T2S)
    text = _HOST.sub("", text)
    return _SEPARATORS.sub(" ", text).strip()
