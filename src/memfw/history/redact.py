"""Secret scan and redaction for transcript text.

Two pattern sets:

* **secrets** -- credentials. An entry with any secret finding is skipped at
  index time (only a count is kept), and withheld at read time.
* **personal** -- e-mail addresses, IPv4 addresses, home-directory user names
  and long high-entropy blobs. These never cause a skip; they are masked in
  every snippet, field and window.

Matching runs on a normalised view of the text: NFKC (so a fullwidth colon
or a no-break space becomes ASCII) with every Unicode format character
(category Cf, e.g. zero-width, bidi and invisible operators) plus U+034F and
U+180E removed. Masks found there are mapped back and applied to the
*original* text, so what callers see is the original with masks, not the
normalised form.

Every pattern is anchored on a literal or a token start and uses bounded
quantifiers, so scanning is linear in the input. Callers still cap the input:
:data:`MAX_SCAN_CHARS`. The scan is pattern based: it catches common credential
shapes, not every secret a person can type in prose (see docs/history.md).
"""

from __future__ import annotations

import re
import sys
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

MAX_SCAN_CHARS = 1024 * 1024
"""Longest text the scanner accepts; callers skip longer entries outright."""

# Removed from the scan copy: format characters (Cf), combining marks (Mn, Me),
# the variation selector U+FE0F and the keycap mark U+20E3, U+034F and U+180E.
_INVISIBLE = frozenset(
    [cp for cp in range(sys.maxunicode + 1) if unicodedata.category(chr(cp)) in {"Cf", "Mn", "Me"}]
    + [0x034F, 0x180E, 0xFE0F, 0x20E3]
)

# Key words whose value is a credential. The lookbehind only rejects a letter
# or digit, so STRIPE_SECRET_KEY and x-api-key match, but the key must end at a
# separator, so max_tokens or token_count do not.
_KEY = (
    r"(?:pg|mysql_)?pass(?:word|wd|phrase)|pwd|secret(?:[_-]?(?:key|access[_-]?key|token))?|"
    r"client[_-]?secret|api[_-]?key|apikey|x[_-]api[_-]key|access[_-]?key(?:[_-]?id)?|"
    r"private[_-]?key|(?:auth|access|refresh|bearer|session|id)[_-]?token|token|"
    # A bare "pass" is too common a word; only common credential owners before it.
    r"(?:db|admin|user|root|my|app|smtp|mail|ftp|ssh|redis|mysql|pg|postgres|mongo|ldap|"
    r"proxy|wifi|vnc|[_-])[_-]?pass"
)
_VALUE = (
    r"(?!\\?[\"']?\[REDACTED)"
    r"(?:\"(?P<dq>(?:[^\"\r\n\\]|\\(?!\")){4,256})\\?\"|'(?P<sq>[^'\r\n\\]{4,256})'"
    r"|(?P<bare>[^\s\"',;}\]\[\\]{4,256}))"
)
_KV = re.compile(
    r"(?i)(?<![A-Za-z0-9_.-])(?P<key>[A-Za-z0-9_.-]{0,32}?(?:"
    + _KEY
    + r"))\\?[\"']?[ \t]{0,8}[:=][ \t]{0,8}"
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
    ("api_key", re.compile(r"(?<![A-Za-z])sk-[A-Za-z0-9_-]{20,512}")),
    ("stripe_key", re.compile(r"(?<![A-Za-z])[rsp]k_(?:live|test)_[A-Za-z0-9]{10,255}")),
    ("huggingface_token", re.compile(r"(?<![A-Za-z])hf_[A-Za-z0-9]{30,255}")),
    ("npm_token", re.compile(r"(?<![A-Za-z])npm_[A-Za-z0-9]{36,255}")),
    ("slack_token", re.compile(r"xox[abprs]-[A-Za-z0-9-]{10,255}")),
    ("google_api_key", re.compile(r"AIza[0-9A-Za-z_-]{35}")),
    ("google_oauth", re.compile(r"(?<![A-Za-z])ya29\.[A-Za-z0-9_-]{10,512}")),
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
    r"(?P<prefix>/Users/|/home/|[A-Za-z]:\\{1,2}Users\\{1,2})(?P<user>(?!\[REDACTED)[^/\\\s\"'<>]{1,128})"
)
# Claude Code stores projects under directories that encode the absolute path
# with dashes, e.g. ``-Users-name-project``.
_DASHED_HOME = re.compile(
    r"(?<![A-Za-z0-9])(?P<prefix>-(?:Users|home)-)(?P<user>(?!\[REDACTED)[^-/\\\s]{1,128})"
)
_BLOB = re.compile(r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{40,}={0,2}(?![A-Za-z0-9+/=])")

SECRET_MARK = "[REDACTED:{kind}]"


@dataclass(frozen=True)
class Finding:
    """One secret finding: kind and character span (never the value)."""

    kind: str
    start: int
    end: int


def _normalized(text: str) -> tuple[str, list[int] | None]:
    """Normalised view plus a map from each view index to its original index.

    The map is ``None`` when the view is the text itself (pure ASCII).
    """

    if text.isascii():
        return text, None
    chars: list[str] = []
    origin: list[int] = []
    for index, char in enumerate(text):
        if ord(char) in _INVISIBLE:
            continue
        folded = unicodedata.normalize("NFKC", char) if ord(char) > 127 else char
        for item in folded:
            if ord(item) in _INVISIBLE:
                continue
            chars.append(item)
            origin.append(index)
    return "".join(chars), origin


def normalize(text: str) -> str:
    """NFKC-normalise and drop format characters (the view the scanner matches on)."""

    return _normalized(text)[0]


def _kv_value(match: re.Match[str]) -> tuple[str, int, int]:
    for group in ("dq", "sq", "bare"):
        value = match.group(group)
        if value is not None:
            start, end = match.span(group)
            return value, start, end
    raise AssertionError("unreachable")  # pragma: no cover


def scan(text: str) -> list[Finding]:
    """Return secret findings (spans in the normalised view). Empty means indexable."""

    return _findings(normalize(text))


def _findings(value: str) -> list[Finding]:
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
    return any(item.end > len(prefix) for item in _findings(prefix + normalize(text)))


def _blob_mixed(blob: str) -> bool:
    return (
        any(char.islower() for char in blob)
        and any(char.isupper() for char in blob)
        and any(char.isdigit() for char in blob)
    )


def _secret_spans(value: str) -> list[tuple[int, int, str]]:
    spans: list[tuple[int, int, str]] = []
    for match in _PEM_BLOCK.finditer(value):
        spans.append((match.start(), match.end(), "private_key"))
    for match in _URL_CREDENTIALS.finditer(value):
        spans.append((match.start("user"), match.end("password"), "credentials"))
    for kind, pattern in _TOKEN_PATTERNS:
        spans.extend((match.start(), match.end(), kind) for match in pattern.finditer(value))
    for match in _KV.finditer(value):
        secret, start, end = _kv_value(match)
        if not _PLACEHOLDER.match(secret.strip()):
            spans.append((start, end, "secret"))
    return spans


def _personal_spans(value: str) -> list[tuple[int, int, str]]:
    spans = [(match.start(), match.end(), "email") for match in _EMAIL.finditer(value)]
    spans.extend((match.start(), match.end(), "ip") for match in _IPV4.finditer(value))
    spans.extend(
        (match.start(), match.end(), "blob")
        for match in _BLOB.finditer(value)
        if _blob_mixed(match.group(0))
    )
    return spans


def _path_spans(value: str) -> list[tuple[int, int, str]]:
    spans: list[tuple[int, int, str]] = []
    for pattern in (_HOME, _DASHED_HOME):
        spans.extend(
            (match.start("user"), match.end("user"), "user") for match in pattern.finditer(value)
        )
    return spans


def _apply(text: str, finders: tuple[Callable[[str], list[tuple[int, int, str]]], ...]) -> str:
    """Find spans on the normalised view, then mask them in the original text."""

    view, origin = _normalized(text)
    # Overlapping spans are masked as their union, so no part of, say, an
    # e-mail address with a token inside it stays visible. The kind shown is
    # the one from the earliest finder (secrets before personal data).
    spans = sorted(
        (start, end, rank, kind)
        for rank, finder in enumerate(finders)
        for start, end, kind in finder(view)
        if end > start
    )
    if not spans:
        return text
    merged: list[list[Any]] = []
    for start, end, rank, kind in spans:
        if merged and start < merged[-1][1]:
            last = merged[-1]
            last[1] = max(last[1], end)
            if rank < last[2]:
                last[2], last[3] = rank, kind
        else:
            merged.append([start, end, rank, kind])
    out: list[str] = []
    cursor = 0
    for start, end, _rank, kind in merged:
        if origin is None:
            original_start, original_end = start, end
        else:
            original_start, original_end = origin[start], origin[end - 1] + 1
        if original_start < cursor:
            original_start = cursor
        out.append(text[cursor:original_start])
        out.append(SECRET_MARK.format(kind=kind))
        cursor = max(cursor, original_end)
    out.append(text[cursor:])
    return "".join(out)


def redact(text: str) -> str:
    """Mask secrets and personal identifiers in the original text. Idempotent."""

    return _apply(text, (_secret_spans, _personal_spans, _path_spans))


def redact_path(text: str) -> str:
    """Mask user names in home-directory paths, including dash-encoded ones."""

    return _apply(text, (_path_spans,))
