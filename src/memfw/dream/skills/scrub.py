"""Scrub free text before it can appear in a skill candidate.

Redaction (:func:`memfw.history.redact`) already masks secrets and personal
data it recognises. Skill drafts are stricter: they are meant to be read and
shared, so anything that names a place or a credential goes, even when it
does not look like a known secret format:

* URLs, absolute and relative paths (``src/pkg/plan.py``), bare domains
  (``example.com/team/repo``);
* host-like tokens: words that mix letters, digits and ``-``/``_``
  (``db-host-01``, ``node7``);
* the word after a secret cue (``password``, ``token``, ``secret``, ``key:``).
"""

from __future__ import annotations

import re

_URL = re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://\S+")
_DOMAIN = re.compile(
    r"(?i)(?<![\w@.-])(?:[a-z0-9-]+\.)+(?:com|org|net|io|dev|ai|app|cn|co|uk|de|jp|edu|gov|"
    r"info|me|sh|cloud|local|internal|lan|xyz|tech|site)\b(?::\d+)?(?:/\S*)?"
)
_ABSOLUTE = re.compile(r"(?:^|(?<=[\s=:'\"(`,]))(?:~|\.{1,2})?/[^\s'\"`;,)]*|[A-Za-z]:\\\S*")
_RELATIVE = re.compile(r"(?<![\w<])[\w.-]+(?:/[\w.-]+)+/?")
_HOSTLIKE = re.compile(r"(?i)\b(?=[a-z0-9_-]*[a-z])(?=[a-z0-9_-]*\d)[a-z0-9]+(?:[-_][a-z0-9]+)*\b")
_CUE = re.compile(
    r"(?i)\b(password|passwd|passphrase|pwd|token|secret|credential|credentials|api[_ -]?key|"
    r"access[_ -]?key|private[_ -]?key)\b(\s*(?:[:=]|is|was)?\s*)(\S+)"
)
_KEY_CUE = re.compile(r"(?i)\b(key)(\s*[:=]\s*)(\S+)")
PLACE = "<place>"
SECRET = "<secret>"


def _scrub_plain(value: str) -> str:
    value = _CUE.sub(lambda match: f"{match.group(1)}{match.group(2) or ' '}{SECRET}", value)
    value = _KEY_CUE.sub(lambda match: f"{match.group(1)}{match.group(2)}{SECRET}", value)
    value = _URL.sub(PLACE, value)
    value = _DOMAIN.sub(PLACE, value)
    value = _ABSOLUTE.sub(PLACE, value)
    value = _RELATIVE.sub(PLACE, value)
    return _HOSTLIKE.sub(PLACE, value)


def scrub(text: str) -> str:
    """``text`` with places and credentials replaced by ``<place>`` / ``<secret>``.

    Spans in backticks are generated command text (signature words and flag
    names from a fixed vocabulary) and are left as they are.
    """

    parts = re.split(r"(`[^`\n]*`)", str(text or ""))
    out = [
        part
        if part.startswith("`") and part.endswith("`") and len(part) > 1
        else _scrub_plain(part)
        for part in parts
    ]
    return " ".join("".join(out).split())
