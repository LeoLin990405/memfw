"""Scrub free text before it can appear in a skill candidate.

Redaction (:func:`memfw.history.redact`) already masks secrets and personal
data it recognises. Skill drafts are stricter: they are meant to be read and
shared, so anything that names a place or a credential goes, even when it
does not look like a known secret format:

* URLs, absolute and relative paths (``src/pkg/plan.py``), bare domains
  (``example.com/team/repo``);
* host-like tokens: words that mix letters, digits and ``-``/``_``
  (``db-host-01``, ``node7``);
* the value of an assignment (``NAME=value``; a plain small number or
  boolean under a harmless name such as ``retries=3`` is kept), and the
  value after a secret cue (``password``, ``token``, ``secret``, ``key:``,
  ``pass:``): after ``:``/``=``, after a filler word (``is``, ``to``, ``,``),
  when it looks like a credential, or any non-common word after a password
  cue. Plain English after a cue (``token count``, ``password reset``)
  stays.

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
_UNC = re.compile(r"\\\\[^\s\\`'\"]+(?:\\[^\s\\`'\"]*)*")
r"""``\\host\share\dir``."""
_RELATIVE = re.compile(
    r"(?<![\w<])[\w.-]+(?:[/\\][\w.-]+)+[/\\]?"  # dir/file, dir\file
    r"|(?<![\w<])[\w.-]*\w[/\\](?=$|[\s`'\",;:)\w])"  # dir/ or dir\ on its own
)
_HOSTLIKE = re.compile(r"(?i)\b(?=[a-z0-9_-]*[a-z])(?=[a-z0-9_-]*\d)[a-z0-9]+(?:[-_][a-z0-9]+)*\b")
_QUOTED = r"'[^'\n]*'?|\"[^\"\n]*\"?"
_SUBST = r"\$\([^)\n]*\)?|`[^`\n]*`"
"""Command substitution as a value: ``$(cat file)``, ```whoami```."""
_ASSIGN = re.compile(
    r"(?<![\w<$])((?i:\$env:)?[A-Za-z_][A-Za-z0-9_]*)(\s*)(?<![=!<>])=(?!=)(\s*)"
    r"(" + _SUBST + "|" + _QUOTED + r"|(?!<secret>)[^\s`'\";,)]+)"
)
"""``NAME=value`` (also ``NAME = value``, quoted or substituted values and
PowerShell ``$env:NAME=``). The value is masked, except a plain small number
or boolean under a name that does not look secret (``retries=3``)."""
_SECRET_NAME = re.compile(r"(?i)pass|pwd|secret|token|key|auth|cred|private|session|cookie|cert")
_PLAIN_VALUE = re.compile(r"(?i)\d{1,6}|true|false|yes|no|on|off|none|null")
_FILLER = r"(?>(?:\s*(?:[:=,\-\u2013\u2014]|\b(?:is|was|to|as|of|be|set\s+to)\b))*\s*)"
"""Words that sit between a cue and its value: ``token to x``, ``Password - x``,
``the token, x``. Atomic, so the value is the first token after them."""
_SECRET_VALUE = r"(?!<secret>)(?:" + _QUOTED + r"|(?=[^\s`'\"<>]*\w)" + _VALUE + ")"
_CUE = re.compile(
    r"(?i)(?<!<)\b(password|passwd|passphrase|pwd|token|secret|credential|credentials|api[_ -]?key|"
    r"access[_ -]?key|private[_ -]?key)\b(?!=)(" + _FILLER + r")(" + _SECRET_VALUE + ")"
)
_KEY_CUE = re.compile(r"(?i)(?<!<)\b(key|pass)(\s*[:=]\s*)(" + _SECRET_VALUE + ")")
_STRONG_CUES = frozenset({"password", "passwd", "passphrase", "pwd"})
"""Cues after which even a plain word is a password (``password hunter``)."""
COMMON_WORDS = frozenset(
    "count counts reset resets flow flows before after rotation rotate rotated manager policy "
    "policies field fields file files length expiry expiration store storage change changes "
    "update updates prompt prompts hash hashing check checks validation input inputs form page "
    "screen login logout auth the a an and or for in on with from of to is was be it this that "
    "used usage limit limits budget refresh scope scopes type types value values name names env "
    "variable variables rule rules requirement requirements strength protected protection "
    "required missing invalid expired leak leaks safe handling support will should must can "
    "scanner scanning parser parsing format generation generator handler endpoint request "
    "requests response header headers cache caching expires lifetime size budget bucket test "
    "tests".split()
)
"""Words that follow a cue as plain English (``token count``, ``password reset``)."""
_CREDENTIAL = re.compile(r"[\d\W_]|(?=.*[a-z])(?=.*[A-Z]).{8,}")


def _looks_secret(word: str) -> bool:
    """A digit or symbol, or 8+ characters of mixed case: shaped like a credential."""

    return bool(_CREDENTIAL.search(word))


def _cue(match: re.Match[str]) -> str:
    cue, filler, value = match.group(1), match.group(2), match.group(3)
    bare = value.strip("'\"")
    separated = bool(re.search(r"[:=]", filler))
    worded = bool(filler.strip()) and not separated
    if value[:1] in "'\"" or separated or _looks_secret(bare):
        masked = True
    elif bare.lower() in COMMON_WORDS:
        masked = False
    else:
        masked = worded or cue.lower() in _STRONG_CUES
    if not masked:
        return match.group(0)
    # Keep sentence punctuation after a bare value (``the token, x, to``).
    tail = re.search(r"[,.;:]+$", value) if value[:1] not in "'\"([" else None
    return f"{cue}{filler or ' '}{SECRET}{tail.group(0) if tail else ''}"


def _assign(match: re.Match[str]) -> str:
    name, value = match.group(1), match.group(4)
    if not _SECRET_NAME.search(name) and _PLAIN_VALUE.fullmatch(value):
        return match.group(0)
    return f"{name}={SECRET}"


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
    value = _UNC.sub(PLACE, value)
    value = _ASSIGN.sub(_assign, value)
    value = _CUE.sub(_cue, value)
    value = _KEY_CUE.sub(lambda match: f"{match.group(1)}{match.group(2)}{SECRET}", value)
    value = _URL.sub(PLACE, value)
    value = _DOMAIN.sub(PLACE, value)
    value = _ABSOLUTE.sub(PLACE, value)
    value = _RELATIVE.sub(PLACE, value)
    value = _HOSTLIKE.sub(PLACE, value)
    # ``host:/path``, ``box:backup/``: the host goes with its path.
    value = re.sub(r"(?<![\w<])[\w.-]+:" + re.escape(PLACE), PLACE, value)
    return " ".join(value.split())
