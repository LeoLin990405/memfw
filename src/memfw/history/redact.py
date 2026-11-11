"""Secret scan and redaction for transcript text.

Two pattern sets:

* **secrets** -- credentials. An entry with any secret finding is skipped at
  index time (only a count is kept), and withheld at read time.
* **personal** -- e-mail addresses, IPv4 addresses, home-directory user names
  and long high-entropy blobs. These never cause a skip; they are masked in
  every snippet, field and window.

Text is normalised first (NFKC, so a fullwidth colon or a no-break space
becomes ASCII, and zero-width characters are removed), so the output of
:func:`redact` is the normalised text.

Every pattern is anchored on a literal or a token start and uses bounded
quantifiers, so scanning is linear in the input. Callers still cap the input:
:data:`MAX_SCAN_CHARS`. The scan is pattern based: it catches common credential
shapes, not every secret a person can type in prose (see docs/history.md).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

MAX_SCAN_CHARS = 1024 * 1024
"""Longest text the scanner accepts; callers skip longer entries outright."""

_ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff\u00ad"), None)

# Key words whose value is a credential. The lookbehind only rejects a letter
# or digit, so STRIPE_SECRET_KEY and x-api-key match, but the key must end at a
# separator, so max_tokens or token_count do not.
_KEY = (
    r"(?:pg|mysql_)?pass(?:word|wd|phrase)|pwd|secret(?:[_-]?(?:key|access[_-]?key|token))?|"
    r"client[_-]?secret|api[_-]?key|apikey|x[_-]api[_-]key|access[_-]?key(?:[_-]?id)?|"
    r"private[_-]?key|(?:auth|access|refresh|bearer|session|id)[_-]?token|token"
)
_VALUE = (
    r"(?!\\?[\"']?\[REDACTED)"
    r"(?:\"(?P<dq>(?:[^\"\r\n\\]|\\(?!\")){4,256})\\?\"|'(?P<sq>[^'\r\n\\]{4,256})'"
    r"|(?P<bare>[^\s\"',;}\]\[\\]{4,256}))"
)
_KV = re.compile(
    r"(?i)(?<![A-Za-z0-9])(?P<key>" + _KEY + r")\\?[\"']?[ \t]{0,8}[:=][ \t]{0,8}"
    r"(?:(?:\\r)?(?:\\n|\r?\n)[ \t]{0,16}){0,3}\\?" + _VALUE
)
_PLACEHOLDER = re.compile(
    r"(?i)^(?:null|none|nil|true|false|redacted|\[REDACTED[^\]]*\]|x{3,}|\*{3,}|<[^>]*>?|\$\{?[\w.]+\}?|"
    r"%\(?[\w.]+\)?s?|\{\{.*|changeme|example\w*|placeholder\w*|required|optional|string|"
    r"number|integer|boolean|undefined|\d{1,7})$"
)

_PEM_BLOCK = re.compile(
    r"-----BEGIN (?P<label>[A-Z0-9 ]{0,40}PRIVATE KEY[A-Z0-9 ]{0,40})-----.*?"
    r"(?:-----END (?P=label)-----|\Z)",
    re.DOTALL,
)
_PEM_HEADER = re.compile(r"-----BEGIN [A-Z0-9 ]{0,40}PRIVATE KEY[A-Z0-9 ]{0,40}-----")
_URL_CREDENTIALS = re.compile(
    r"(?i)(?<![a-z0-9+.-])(?P<scheme>[a-z][a-z0-9+.-]{1,20}://)"
    r"(?P<user>[^\s/:@\"'<>]{1,256}):(?P<password>[^\s/@\"'<>]{1,256})@"
)
_TOKEN_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("aws_access_key", re.compile(r"(?:AKIA|ASIA)[0-9A-Z]{16}(?![0-9A-Z])")),
    (
        "github_token",
        re.compile(r"(?:gh[pousr]_[A-Za-z0-9]{30,255}|github_pat_[A-Za-z0-9_]{22,255})"),
    ),
    ("gitlab_token", re.compile(r"glpat-[A-Za-z0-9_-]{20,255}")),
    ("api_key", re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{20,512}")),
    ("stripe_key", re.compile(r"(?<![A-Za-z0-9])[rsp]k_(?:live|test)_[A-Za-z0-9]{10,255}")),
    ("huggingface_token", re.compile(r"(?<![A-Za-z0-9])hf_[A-Za-z0-9]{30,255}")),
    ("npm_token", re.compile(r"(?<![A-Za-z0-9])npm_[A-Za-z0-9]{36,255}")),
    ("slack_token", re.compile(r"xox[abprs]-[A-Za-z0-9-]{10,255}")),
    ("google_api_key", re.compile(r"AIza[0-9A-Za-z_-]{35}")),
    ("google_oauth", re.compile(r"(?<![A-Za-z0-9])ya29\.[A-Za-z0-9_-]{10,512}")),
    (
        "jwt",
        re.compile(r"eyJ[A-Za-z0-9_-]{8,4096}\.eyJ[A-Za-z0-9_-]{8,4096}\.[A-Za-z0-9_-]{8,4096}"),
    ),
    (
        "authorization",
        re.compile(
            r"(?i)(?<![a-z0-9])(?:authorization|proxy-authorization)\\?[\"']?[ \t]{0,8}[:=]"
            r"[ \t]{0,8}\\?[\"']?(?:basic|bearer|token|digest)[ \t]{1,8}[A-Za-z0-9._~+/=-]{6,4096}"
        ),
    ),
    ("bearer", re.compile(r"(?i)(?<![a-z0-9])bearer[ \t]{1,8}[A-Za-z0-9._~+/=-]{20,4096}")),
    (
        "cli_user_password",
        re.compile(r"(?<!\S)(?:-u|--user)(?:[ \t]{1,8}|=)[^\s:\"']{1,64}:[^\s\"']{3,256}"),
    ),
    (
        "cli_password",
        re.compile(r"(?<![\w-])mysql(?:dump|admin)?[ \t][^\n]{0,120}?(?<=\s)-p[^\s-]\S{2,255}"),
    ),
)

_EMAIL = re.compile(
    r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63}){0,8}"
    r"\.[A-Za-z]{2,24}(?![A-Za-z])"
)
_OCTET = r"(?:25[0-5]|2[0-4]\d|1?\d?\d)"
_IPV4 = re.compile(r"(?<![\d.])(?:" + _OCTET + r"\.){3}" + _OCTET + r"(?![\d.])")
_HOME = re.compile(
    r"(?P<prefix>/Users/|/home/|[A-Za-z]:\\{1,2}Users\\{1,2})(?P<user>[^/\\\s\"'<>]{1,128})"
)
# Claude Code stores projects under directories that encode the absolute path
# with dashes, e.g. ``-Users-name-project``.
_DASHED_HOME = re.compile(r"(?<![A-Za-z0-9])(?P<prefix>-(?:Users|home)-)(?P<user>[^-/\\\s]{1,128})")
_BLOB = re.compile(r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{40,4096}={0,2}(?![A-Za-z0-9+/=])")

SECRET_MARK = "[REDACTED:{kind}]"
_MARKS = {kind: SECRET_MARK.format(kind=kind) for kind, _pattern in _TOKEN_PATTERNS}


@dataclass(frozen=True)
class Finding:
    """One secret finding: kind and character span (never the value)."""

    kind: str
    start: int
    end: int


def normalize(text: str) -> str:
    """NFKC-normalise and drop zero-width characters."""

    if text.isascii():
        return text
    return unicodedata.normalize("NFKC", text).translate(_ZERO_WIDTH)


def _kv_value(match: re.Match[str]) -> tuple[str, int, int]:
    for group in ("dq", "sq", "bare"):
        value = match.group(group)
        if value is not None:
            start, end = match.span(group)
            return value, start, end
    raise AssertionError("unreachable")  # pragma: no cover


def scan(text: str) -> list[Finding]:
    """Return secret findings in ``text``. Empty means the text may be indexed."""

    value = normalize(text)
    findings: list[Finding] = []
    for match in _PEM_HEADER.finditer(value):
        findings.append(Finding("private_key", match.start(), match.end()))
    for match in _URL_CREDENTIALS.finditer(value):
        findings.append(Finding("url_credentials", match.start(), match.end()))
    for kind, pattern in _TOKEN_PATTERNS:
        for match in pattern.finditer(value):
            findings.append(Finding(kind, match.start(), match.end()))
    for match in _KV.finditer(value):
        secret, _start, _end = _kv_value(match)
        if _PLACEHOLDER.match(secret.strip()):
            continue
        findings.append(Finding("key_value", match.start(), match.end()))
    findings.sort(key=lambda item: (item.start, item.end))
    return findings


def has_secret(text: str) -> bool:
    return bool(scan(text))


def secret_continues(previous: str, text: str, *, tail: int = 200) -> bool:
    """True when ``previous``'s tail and ``text`` together form a secret in ``text``.

    Catches a key at the end of one entry with its value at the start of the next.
    """

    prefix = normalize(previous)[-tail:] + "\n"
    return any(item.end > len(prefix) for item in scan(prefix + text))


def _kv_replace(match: re.Match[str]) -> str:
    secret, start, end = _kv_value(match)
    if _PLACEHOLDER.match(secret.strip()):
        return match.group(0)
    offset = match.start()
    whole = match.group(0)
    return whole[: start - offset] + SECRET_MARK.format(kind="secret") + whole[end - offset :]


def redact(text: str) -> str:
    """Mask secrets and personal identifiers. Idempotent."""

    value = normalize(text)
    value = _PEM_BLOCK.sub(SECRET_MARK.format(kind="private_key"), value)
    value = _URL_CREDENTIALS.sub(
        lambda match: match.group("scheme") + SECRET_MARK.format(kind="credentials") + "@", value
    )
    for kind, pattern in _TOKEN_PATTERNS:
        value = pattern.sub(_MARKS[kind], value)
    value = _KV.sub(_kv_replace, value)
    value = _EMAIL.sub("[REDACTED:email]", value)
    value = _IPV4.sub("[REDACTED:ip]", value)
    value = redact_path(value)
    value = _BLOB.sub(_blob_replace, value)
    return value


def redact_path(text: str) -> str:
    """Mask user names in home-directory paths, including dash-encoded ones."""

    value = _HOME.sub(lambda match: match.group("prefix") + "[REDACTED:user]", text)
    return _DASHED_HOME.sub(lambda match: match.group("prefix") + "[REDACTED:user]", value)


def _blob_replace(match: re.Match[str]) -> str:
    blob = match.group(0)
    mixed = (
        any(char.islower() for char in blob)
        and any(char.isupper() for char in blob)
        and any(char.isdigit() for char in blob)
    )
    return "[REDACTED:blob]" if mixed else blob
