"""Scrub free text before it can appear in a skill candidate.

Redaction (:func:`memfw.history.redact`) already masks secrets and personal
data it recognises. Skill drafts are stricter: they are meant to be read and
shared, so anything that names a place or a credential goes, even when it
does not look like a known secret format:

* URLs, absolute and relative paths (``src/pkg/plan.py``), bare domains
  (``example.com/team/repo``);
* host-like tokens: words that mix letters, digits and ``-``/``_``
  (``db-host-01``, ``node7``);
* the value of any ``NAME=value`` assignment, and the word after a secret
  cue (``password``, ``token``, ``secret``, ``key:``).

Agent text in backticks is scrubbed like any other text; only command text
the code generates itself (from the fixed signature vocabulary) is added
afterwards and skips this step.
"""

from __future__ import annotations

import re

_VALUE = r"[^\s`'\"<>]+"
"""A token up to whitespace, a quote or a backtick (so quoting survives)."""
_URL = re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://" + _VALUE)
_DOMAIN = re.compile(
    r"(?i)(?<![\w@.-])(?:[a-z0-9-]+\.)+(?:com|org|net|io|dev|ai|app|cn|co|uk|de|jp|edu|gov|"
    r"info|me|sh|cloud|local|internal|lan|xyz|tech|site)\b(?::\d+)?(?:/(?:" + _VALUE + r")?)?"
)
_ABSOLUTE = re.compile(r"(?:^|(?<=[\s=:'\"(`,]))(?:~|\.{1,2})?/[^\s'\"`;,)]*|[A-Za-z]:\\\S*")
_RELATIVE = re.compile(r"(?<![\w<])[\w.-]+(?:/[\w.-]+)+/?")
_HOSTLIKE = re.compile(r"(?i)\b(?=[a-z0-9_-]*[a-z])(?=[a-z0-9_-]*\d)[a-z0-9]+(?:[-_][a-z0-9]+)*\b")
_ASSIGN = re.compile(r"(?<![\w<])([A-Za-z_][A-Za-z0-9_]*)=(?!<secret>)[^\s`'\";,)]+")
"""``NAME=value``: the value is never kept, whatever the name."""
_CUE = re.compile(
    r"(?i)(?<!<)\b(password|passwd|passphrase|pwd|token|secret|credential|credentials|api[_ -]?key|"
    r"access[_ -]?key|private[_ -]?key)\b(?!=)(\s*(?::|is|was)?\s*)(" + _VALUE + ")"
)
_KEY_CUE = re.compile(r"(?i)(?<!<)\b(key)(\s*[:=]\s*)(" + _VALUE + ")")
PLACE = "<place>"
SECRET = "<secret>"


def scrub(text: str) -> str:
    """``text`` with places and credentials replaced by ``<place>`` / ``<secret>``.

    Everything is scrubbed, text in backticks too: agents quote paths and
    hosts in backticks in their own prose. Generated command text must be
    added after scrubbing (see :func:`.draft.step_headline`), never passed
    through here.
    """

    value = str(text or "")
    value = _ASSIGN.sub(lambda match: f"{match.group(1)}={SECRET}", value)
    value = _CUE.sub(lambda match: f"{match.group(1)}{match.group(2) or ' '}{SECRET}", value)
    value = _KEY_CUE.sub(lambda match: f"{match.group(1)}{match.group(2)}{SECRET}", value)
    value = _URL.sub(PLACE, value)
    value = _DOMAIN.sub(PLACE, value)
    value = _ABSOLUTE.sub(PLACE, value)
    value = _RELATIVE.sub(PLACE, value)
    value = _HOSTLIKE.sub(PLACE, value)
    return " ".join(value.split())
