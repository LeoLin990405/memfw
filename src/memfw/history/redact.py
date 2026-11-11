"""Secret scan and redaction for transcript text.

Two pattern sets:

* **secrets** -- credentials. An entry with any secret finding is skipped at
  index time (only a count is kept), and withheld at read time.
* **personal** -- e-mail addresses, IPv4 addresses and home-directory user
  names. These never cause a skip; they are masked in every snippet and window.

Everything returned to a caller goes through :func:`redact`, which masks both
sets. The scan is pattern based: it catches common credential shapes, not every
secret a person can type in prose.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Key names whose value is a credential. ``\b`` keeps ``max_tokens`` or
# ``token_count`` out: an underscore is a word character.
_SECRET_KEY = (
    r"(?:password|passwd|passphrase|pwd|secret|client[_-]?secret|api[_-]?key|apikey|"
    r"access[_-]?key(?:[_-]?id)?|secret[_-]?access[_-]?key|private[_-]?key|auth[_-]?token|"
    r"access[_-]?token|refresh[_-]?token|bearer[_-]?token|session[_-]?token|token)"
)
# Separator: optional closing quote, ``:`` or ``=``, then spaces and at most two
# line breaks, so ``password:\n    value`` is still one pair.
_KV = re.compile(
    r"(?i)(?<![\w-])(?P<key>[\w.-]*" + _SECRET_KEY + r")[\"']?[ \t]*[:=][ \t]*"
    r"(?:\\?\r?\n[ \t]*|\\n[ \t]*){0,2}[\"']?(?P<value>[^\s\"',;}\]]{6,})"
)
_PLACEHOLDER = re.compile(
    r"(?i)^(?:null|none|nil|true|false|redacted|\[redacted[^\]]*\]|x{3,}|\*{3,}|"
    r"<[^>]*>?|\$\{?[\w.]+\}?|%\(?[\w.]+\)?s?|\{\{.*|changeme|example\w*|placeholder\w*|"
    r"required|optional|string|number|integer|boolean|undefined)$"
)

_PEM_BLOCK = re.compile(
    r"-----BEGIN (?P<label>[A-Z0-9 ]*PRIVATE KEY[A-Z0-9 ]*)-----.*?"
    r"(?:-----END (?P=label)-----|\Z)",
    re.DOTALL,
)
_PEM_HEADER = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY[A-Z0-9 ]*-----")
_URL_CREDENTIALS = re.compile(
    r"(?i)(?P<scheme>\b[a-z][a-z0-9+.-]{1,20}://)(?P<user>[^\s/:@\"'<>]+):(?P<password>[^\s/@\"'<>]+)@"
)
_TOKEN_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{22,})")),
    ("api_key", re.compile(r"\bsk-(?:[A-Za-z]+-)?[A-Za-z0-9_-]{20,}")),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
    ("bearer", re.compile(r"(?i)\bbearer[ \t]+[A-Za-z0-9._~+/=-]{20,}")),
)

_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_OCTET = r"(?:25[0-5]|2[0-4]\d|1?\d?\d)"
_IPV4 = re.compile(r"(?<![\d.])(?:" + _OCTET + r"\.){3}" + _OCTET + r"(?![\d.])")
_HOME = re.compile(r"(?P<prefix>/Users/|/home/|[A-Za-z]:\\\\?Users\\\\?)(?P<user>[^/\\\s\"'<>]+)")

SECRET_MARK = "[REDACTED:{kind}]"


@dataclass(frozen=True)
class Finding:
    """One secret finding: kind and character span (never the value)."""

    kind: str
    start: int
    end: int


def scan(text: str) -> list[Finding]:
    """Return secret findings in ``text``. Empty means the text may be indexed."""

    findings: list[Finding] = []
    for match in _PEM_HEADER.finditer(text):
        findings.append(Finding("private_key", match.start(), match.end()))
    for match in _URL_CREDENTIALS.finditer(text):
        findings.append(Finding("url_credentials", match.start(), match.end()))
    for kind, pattern in _TOKEN_PATTERNS:
        for match in pattern.finditer(text):
            findings.append(Finding(kind, match.start(), match.end()))
    for match in _KV.finditer(text):
        if _PLACEHOLDER.match(match.group("value")):
            continue
        findings.append(Finding("key_value", match.start(), match.end()))
    findings.sort(key=lambda item: (item.start, item.end))
    return findings


def has_secret(text: str) -> bool:
    return bool(scan(text))


def _kv_replace(match: re.Match[str]) -> str:
    if _PLACEHOLDER.match(match.group("value")):
        return match.group(0)
    start, end = match.span("value")
    offset = match.start()
    whole = match.group(0)
    return whole[: start - offset] + SECRET_MARK.format(kind="secret") + whole[end - offset :]


def redact(text: str) -> str:
    """Mask secrets and personal identifiers. Safe to call more than once."""

    value = _PEM_BLOCK.sub(SECRET_MARK.format(kind="private_key"), text)
    value = _URL_CREDENTIALS.sub(
        lambda match: match.group("scheme") + SECRET_MARK.format(kind="credentials") + "@", value
    )
    for kind, pattern in _TOKEN_PATTERNS:
        value = pattern.sub(SECRET_MARK.format(kind=kind), value)
    value = _KV.sub(_kv_replace, value)
    value = _EMAIL.sub("[REDACTED:email]", value)
    value = _IPV4.sub("[REDACTED:ip]", value)
    value = _HOME.sub(lambda match: match.group("prefix") + "[REDACTED:user]", value)
    return value
