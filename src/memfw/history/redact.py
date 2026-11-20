"""Secret scan and redaction for transcript text.

Two pattern sets:

* **secrets** -- credentials. An entry with any secret finding is skipped at
  index time (only a count is kept), and withheld at read time.
* **personal** -- e-mail addresses, IPv4 addresses (also inside a host name
  such as ``git.192.0.2.7.sslip.io``), the IP part of ``sslip.io`` /
  ``nip.io`` / ``xip.io`` names in dash or hex form, the tailnet label of
  Tailscale ``*.ts.net`` names, phone numbers (Chinese mobiles with or
  without a ``+86`` / ``0086`` / ``086`` prefix, and ``+``-prefixed
  international numbers), home-directory user names and long high-entropy
  blobs. These never cause a skip; they are masked in every snippet, field and
  window.

Host names (``build-01``, the machine label of ``build-01.tail0000.ts.net``)
are kept by default: agents need them to tell machines apart. ``redact`` takes
``redact_hostnames=True`` plus an explicit ``hostnames`` list as an opt-in hook;
nothing turns it on yet.

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

import bisect
import logging
import re
import sys
import unicodedata
from collections.abc import Callable, Iterable
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
    # secret_id (Vault AppRole) is a login credential, unlike other "_id" keys.
    r"(?:client[_-]?)?secret[_-]?id|(?:pg|mysql_)?pass(?:word|wd|phrase)|pwd|secret(?:[_-]?(?:key|access[_-]?key|token))?|"
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
# Key: up to 128 characters glued in front (spring.datasource...password,
# SPRING_..._PASSWORD), the key word, then up to three short suffixes after a
# separator or digits (SECRET_KEY_BASE, DB_PASSWORD_PROD, password1, api_key_v2).
# Separator: ":", "=" or "=>", optionally a YAML block indicator ("|", ">").
# Suffixes that name something about a credential rather than the credential
# itself (token_type, password_hash, api_key_header, secret_name, ...).
_BENIGN_SUFFIX = (
    r"(?!(?:type|name|hash|header|id|file|path|url|len|length|count|ref|min|max|"
    r"policy|prompt|reset|expiry|expires|ttl)(?![A-Za-z0-9]))"
)
_KV = re.compile(
    r"(?i)(?<![A-Za-z0-9_.-])(?P<key>[A-Za-z0-9_.-]{0,128}?(?:"
    + _KEY
    + r")(?:[_-]"
    + _BENIGN_SUFFIX
    + r"[A-Za-z0-9]{1,16}|[0-9]{1,4}){0,3})\\?[\"']?[ \t]{0,8}(?:=>|[:=])[ \t]{0,8}"
    r"(?:[|>][-+]?[ \t]{0,8})?"
    r"(?:(?:\\r)?(?:\\n|\r?\n)[ \t]{0,16}){0,3}\\?" + _VALUE
)
# A command-line flag followed by its value: --password VALUE, -token VALUE.
_FLAG = re.compile(
    r"(?i)(?<![\w-])--?(?:password|passwd|pass|token|secret|client[_-]?secret|api[_-]?key|"
    r"access[_-]?token|auth[_-]?token)[ \t]{1,8}(?P<value>(?![-\[])[^\s\"']{4,256})"
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
    r"(?P<user>[^\s/:@\"'<>]{0,256}):(?P<password>[^\s/@\"'<>]{1,256})@"
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
# Leading zeros are accepted (``192.0.2.007``): some tools print them.
_OCTET = r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d|0\d{1,2})"
# Not part of a longer dotted number (``1.2.3.4.5``): no digit, or digit and
# dot, on either side. A dot next to a letter is fine, so an address inside a
# host name (``git.192.0.2.7.sslip.io``) or at the end of a sentence matches.
# A four-part version number whose parts are all 0-255 (``1.2.3.4``) is masked
# too: it cannot be told apart from an address, and masking is the safe side.
_IPV4 = re.compile(r"(?<!\d)(?<!\d\.)(?:" + _OCTET + r"\.){3}" + _OCTET + r"(?!\d)(?!\.\d)")
# Wildcard-DNS services that encode an address in the name: dash form
# (``app-192-0-2-7.sslip.io``) and, for nip.io / sslip.io, hex form
# (``app-c0000207.nip.io``). The dotted form is caught by _IPV4.
# The dots may be dashes: Claude Code encodes a working directory such as
# ``git.192.0.2.7.sslip.io`` as ``git-192-0-2-7-sslip-io``.
_IP_DNS = r"(?=[.-](?i:sslip|nip|xip)[.-](?i:io)(?![A-Za-z0-9]))"
_IPV4_DASHED = re.compile(r"(?<!\d)(?<!\d-)(?:" + _OCTET + r"-){3}" + _OCTET + _IP_DNS)
_IPV4_HEX = re.compile(r"(?<![A-Za-z0-9])[0-9A-Fa-f]{8}" + _IP_DNS)
# Tailscale MagicDNS: ``<machine>.<tailnet>.ts.net``. The tailnet label names
# the owner's network and is masked; the machine label is a host name.
_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_TAILNET = re.compile(
    r"(?<![A-Za-z0-9-])(?P<tailnet>" + _LABEL + r")(?=\.ts\.net(?![A-Za-z0-9-]))", re.IGNORECASE
)
# The same name in a dash-encoded Claude Code project directory
# (``-srv-box-tail0000-ts-net``): dots became dashes, so label boundaries are
# lost. Only Tailscale's default ``tail<hex>`` tailnet name is masked there;
# a custom two-word name cannot be told apart from ordinary directory words
# (``-home-a-work-ts-net``) and is left alone (see docs/history.md).
_TAILNET_DASHED = re.compile(
    r"(?i)(?<=[-.])(?P<tailnet>tail[0-9a-f]{4,16})(?=-ts-net(?![A-Za-z0-9]))"
)
_TAILNET_MACHINE = re.compile(
    r"(?<![A-Za-z0-9-])(?P<host>"
    + _LABEL
    + r")(?=\.(?:"
    + _LABEL
    + r"|\[REDACTED:tailnet\])\.ts\.net(?![A-Za-z0-9-]))",
    re.IGNORECASE,
)
# Phone separators: space, dot, hyphen and the Unicode hyphens/dashes
# U+2010-U+2013 (NFKC folds U+2011 to U+2010 but keeps U+2013).
_DASHES = "‐-–"
# Chinese mobile numbers: 1[3-9] and nine more digits, either in one run or
# grouped 3-4-4 with one consistent separator, optionally after +86, 0086,
# 086 or 86 (also in parentheses). The number must not touch a letter or
# digit (nor be part of a dotted number), so an 11-digit run inside a hash, a
# longer order id or a millisecond timestamp is left alone. Space-grouped
# numbers without a prefix also need a phone word right before them
# ("phone:", "tel", "手机", ...), because "commits 143 1024 2048" has the same
# shape. An 11-digit id that looks exactly like a mobile number is masked.
# A separator is one dot, slash, hyphen or dash, or a run of up to three
# spaces; the same one must be used throughout. Letters glued to the end
# ("13800138000x") do not stop the match: masking the digits is the safe side.
_SEP = r"(?: {1,3}|[./" + _DASHES + r"-])"
_CN_MOBILE = re.compile(
    r"(?<![A-Za-z0-9+])(?<!\d\.)"
    r"(?P<prefix>(?:\+|00|0)?86" + _SEP + r"?|\((?:\+|00)?86\)" + _SEP + r"?)?"
    r"1[3-9]\d(?:(?P<sep>" + _SEP + r")\d{4}(?P=sep)\d{4}|\d{8})"
    r"(?!\d)(?!\.\d)"
)
_PHONE_WORD = re.compile(
    r"(?i)(?<![A-Za-z:])(?:telephone|phone|tel|mobile|mob|cell|call|contact|whatsapp|wechat|"
    r"sms|手机|电话|联系方式|联系|号码|微信)(?:号码|号)?\.?"
    r"(?:[ \t]{0,2}(?:no\.?|number|#))?(?:[ \t]{1,3}(?:is|was))?[ \t]{0,3}[:：]?[ \t]{0,3}$"
)
# International numbers need a leading "+" and a valid ITU country code, then
# digit groups split by one space, dot (only dots, then) or hyphen; one group
# may be in parentheses. 8-15 digits in all (E.164). Each group must end at a
# boundary, so a following token ("5pm", a timestamp) ends the number instead
# of hiding it; groups are possessive and start with a separator or "(", so
# the scan stays linear. Bare national numbers without "+" are not matched.
_INTL_PHONE = re.compile(
    r"(?<![A-Za-z0-9+])\+[1-9]\d{0,14}+(?!\d)"
    r"(?:(?<![A-Za-z])" + _SEP + r"\d{1,12}+(?!\d)"
    r"|(?<![A-Za-z])" + _SEP + r"?\(\d{1,5}\)" + _SEP + r"?\d{1,12}+(?!\d)){0,6}"
)
_PHONE_GROUP = re.compile(_SEP + r"?\(\d+\)" + _SEP + r"?\d+|" + _SEP + r"\d+")
# An extension right after the number ("x12", " ext. 34", "#56") is masked too.
_PHONE_EXT = re.compile(r"(?i)(?:[ \t]{0,2}(?:x|ext\.?|#)[ \t]{0,2}\d{1,6}(?!\d))")
# A short last group glued to one of these is a time or an ordinal ("5pm",
# "3rd"), not part of the number.
_UNIT_TAIL = re.compile(r"(?i)(?:am|pm|st|nd|rd|th|h|min|s)(?![A-Za-z])")
# Country codes (ITU-T E.164 zones): 1 and 7; the two-digit codes; every
# other code has three digits and starts with one of the listed pairs.
_CC_2 = frozenset(
    "20 27 30 31 32 33 34 36 39 40 41 43 44 45 46 47 48 49 51 52 53 54 55 56 57 58 "
    "60 61 62 63 64 65 66 81 82 84 86 90 91 92 93 94 95 98".split()
)
_CC_3_PREFIX = frozenset(
    "21 22 23 24 25 26 29 35 37 38 42 50 59 67 68 69 80 85 87 88 96 97 99".split()
)
_DATE_SHAPE = re.compile(r"(?:19|20)\d{2}[./" + _DASHES + r"-](?:0?[1-9]|1[0-2])(?![0-9])")
_COMPACT_DATE = re.compile(r"(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])")
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
_MAX_PASSES = 8
PASS_LIMIT_HITS = 0
"""How often :func:`redact` stopped at :data:`_MAX_PASSES` (expected: never)."""
_LOG = logging.getLogger(__name__)


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
    for match in _FLAG.finditer(value):
        if _flag_value(match.group("value")):
            findings.append(Finding("cli_flag", match.start(), match.end()))
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


_FILENAME = re.compile(r"[\w.~-]*\.[A-Za-z][A-Za-z0-9]{0,5}")


def _flag_value(value: str) -> bool:
    """Whether the word after ``--password`` / ``--token`` looks like a secret.

    Not when it is a placeholder, a path (starting with ``/``, ``./``, ``../``
    or ``~``) or file name, or a short plain word
    ("--token option"): it must contain a digit or symbol, or be 12+ letters.
    """

    if (
        _PLACEHOLDER.match(value)
        or value.startswith(("/", "./", "../", "~"))
        or _FILENAME.fullmatch(value)
    ):
        return False
    return not value.isalpha() or len(value) >= 12


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
    for match in _FLAG.finditer(value):
        if _flag_value(match.group("value")):
            spans.append((match.start("value"), match.end("value"), "secret"))
    return spans


def _country_code(code: str) -> bool:
    if len(code) == 1:
        return code in {"1", "7"}
    if len(code) == 2:
        return code in _CC_2
    return len(code) == 3 and code[:2] in _CC_3_PREFIX


def _intl_valid(code: str, groups: list[str]) -> bool:
    digits = len(code) + sum(char.isdigit() for item in groups for char in item)
    if not 8 <= digits <= 15:
        return False
    if not groups:
        # "+15550100123": no separators, so the code is a prefix; a
        # date-shaped run ("+20261120", a diff line) is not a number.
        return (
            digits >= 10
            and any(_country_code(code[:size]) for size in (1, 2, 3))
            and not _COMPACT_DATE.match(code)
        )
    separators = {
        re.match(r" +|[^\d(]", item).group(0)  # type: ignore[union-attr]
        for item in groups
        if not item[0].isdigit() and item[0] != "("
    }
    if len(separators) > 1 and separators & {".", "/"}:
        return False
    if _DATE_SHAPE.search(code + "".join(groups)):
        return False
    if len(code) > 3 or not _country_code(code):
        return False
    return len(groups) >= 2 or sum(char.isdigit() for char in groups[0]) >= 7


def _phone_spans(value: str) -> list[tuple[int, int, str]]:
    spans: list[tuple[int, int, str]] = []
    for match in _CN_MOBILE.finditer(value):
        if (
            match.group("prefix") is None
            and match.group("sep") == " "
            and not _PHONE_WORD.search(value[max(0, match.start() - 40) : match.start()])
        ):
            continue
        spans.append((match.start(), match.end(), "phone"))
    for match in _INTL_PHONE.finditer(value):
        text = match.group(0)
        code = re.match(r"\+(\d+)", text).group(1)  # type: ignore[union-attr]
        groups = [item.group(0) for item in _PHONE_GROUP.finditer(text, 1 + len(code))]
        glued = _UNIT_TAIL.match(value, match.end())
        if glued and groups and sum(char.isdigit() for char in groups[-1]) <= 2:
            groups.pop()  # "+1 555 010 0123 5pm": the "5pm" is a time
        # Drop trailing groups ("+44 20 7946 0958 12345") until it is valid.
        # Letters glued to the last group ("0123foo") end the number there.
        while True:
            if _intl_valid(code, groups):
                end = match.start() + 1 + len(code) + sum(map(len, groups))
                if end == match.end():
                    extension = _PHONE_EXT.match(value, end)
                    if extension:
                        end = extension.end()
                spans.append((match.start(), end, "phone"))
                break
            if not groups:
                break
            groups.pop()
    return spans


def _personal_spans(value: str) -> list[tuple[int, int, str]]:
    spans = [(match.start(), match.end(), "email") for match in _EMAIL.finditer(value)]
    for pattern in (_IPV4, _IPV4_DASHED, _IPV4_HEX):
        spans.extend((match.start(), match.end(), "ip") for match in pattern.finditer(value))
    for pattern in (_TAILNET, _TAILNET_DASHED):
        spans.extend(
            (match.start("tailnet"), match.end("tailnet"), "tailnet")
            for match in pattern.finditer(value)
        )
    spans.extend(_phone_spans(value))
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
    # URL userinfo also looks like an e-mail address, or like a key/value pair
    # when the user name ends in a key word. The credentials mask covers the
    # userinfo, and those overlapping matches are dropped, so host, port and
    # path stay visible.
    credentials = [(start, end) for start, end, _rank, kind in spans if kind == "credentials"]
    if credentials:
        starts = [start for start, _end in credentials]

        def inside_url(start: int, end: int) -> bool:
            index = bisect.bisect_right(starts, end)
            return any(
                credentials[i][0] < end and start < credentials[i][1]
                for i in range(max(0, index - 2), index)
            )

        spans = [
            span
            for span in spans
            if span[3] not in {"email", "secret"} or not inside_url(span[0], span[1])
        ]
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


# Words that also appear in masks or field names: never treated as host names,
# so a name list cannot eat a mask and break idempotence.
_RESERVED_HOSTNAMES = frozenset(
    {
        "redacted",
        "secret",
        "credentials",
        "private_key",
        "email",
        "ip",
        "phone",
        "tailnet",
        "host",
        "user",
        "blob",
        "withheld",
    }
)
_MASK = re.compile(r"\[REDACTED[^\]\s]{0,40}\]")


def _hostname_finder(hostnames: Iterable[str]) -> Callable[[str], list[tuple[int, int, str]]]:
    names = sorted(
        {
            name.strip()
            for name in hostnames
            if name and name.strip() and name.strip().lower() not in _RESERVED_HOSTNAMES
        },
        key=len,
    )
    named = (
        re.compile(
            r"(?i)(?<![A-Za-z0-9_-])(?:"
            + "|".join(re.escape(name) for name in reversed(names))
            + r")(?![A-Za-z0-9_-])"
        )
        if names
        else None
    )

    def find(value: str) -> list[tuple[int, int, str]]:
        masks = [(match.start(), match.end()) for match in _MASK.finditer(value)]

        def in_mask(start: int, end: int) -> bool:
            return any(left < end and start < right for left, right in masks)

        spans = [
            (match.start("host"), match.end("host"), "host")
            for match in _TAILNET_MACHINE.finditer(value)
        ]
        if named is not None:
            spans.extend((match.start(), match.end(), "host") for match in named.finditer(value))
        return [span for span in spans if not in_mask(span[0], span[1])]

    return find


def redact(text: str, *, redact_hostnames: bool = False, hostnames: Iterable[str] = ()) -> str:
    """Mask secrets and personal identifiers in the original text. Idempotent.

    Host names are kept unless ``redact_hostnames`` is true (an opt-in hook,
    default off, not wired to any setting yet): then the machine label of a
    ``*.ts.net`` name and every whole-word occurrence of a name in
    ``hostnames`` (case-insensitive) become ``[REDACTED:host]``. A bare host
    name cannot be recognised by its shape, so the list is required for those.
    """

    finders: tuple[Callable[[str], list[tuple[int, int, str]]], ...] = (
        _secret_spans,
        _personal_spans,
        _path_spans,
    )
    if redact_hostnames:
        finders += (_hostname_finder(hostnames),)
    # A mask can change what a neighbour's boundary check sees (in
    # ".ts.net192.0.2.7" the tailnet label only ends at a boundary once the
    # address is masked), so masking repeats until the text is stable. Text
    # without findings takes one pass; each further pass only masks more.
    return _fixpoint(text, finders)[0]


def _fixpoint(
    text: str, finders: tuple[Callable[[str], list[tuple[int, int, str]]], ...]
) -> tuple[str, int]:
    """``(masked text, passes)``; logs (no text) when the pass limit is hit."""

    global PASS_LIMIT_HITS
    previous, current, passes = text, _apply(text, finders), 1
    while current != previous:
        if passes >= _MAX_PASSES:
            PASS_LIMIT_HITS += 1
            _LOG.warning("redact: no fixed point after %d passes (%d chars)", passes, len(text))
            break
        previous, current = current, _apply(current, finders)
        passes += 1
    return current, passes


def redact_path(text: str) -> str:
    """Mask user names in home-directory paths, including dash-encoded ones."""

    return _apply(text, (_path_spans,))
