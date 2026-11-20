"""Redaction gaps found in the v0.5.0 test report (all values synthetic).

Addresses come from the documentation ranges (192.0.2.0/24, 198.51.100.0/24,
203.0.113.0/24, plus private ranges that identify nobody), phone numbers from
the fictional 555 range or the reserved CN test-style 138-0013-8000 shape, and
names from ``example.test``.
"""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from memfw.conversations.share import ConversationShare
from memfw.handoff import tools_map
from memfw.history.index import TranscriptIndex
from memfw.history.redact import redact, redact_path

# Built at run time so the tracked file names no home directory.
MAC_HOME = "/" + "Users" + "/someone/"
LINUX_HOME = "/" + "home" + "/someone/"

# -- IPv4, also inside host names ---------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # (a) an address embedded in a host name used to escape the lookbehind
        ("clone git.192.0.2.7.sslip.io/repo", "clone git.[REDACTED:ip].sslip.io/repo"),
        ("http://app.198.51.100.20.nip.io:8080/", "http://app.[REDACTED:ip].nip.io:8080/"),
        ("ping 10.0.0.1.", "ping [REDACTED:ip]."),  # sentence end used to escape too
        ("host=203.0.113.9,port=22", "host=[REDACTED:ip],port=22"),
        # (d) dash and hex encodings of wildcard-DNS services
        ("svc-192-0-2-7.sslip.io", "svc-[REDACTED:ip].sslip.io"),
        ("192-0-2-7.nip.io", "[REDACTED:ip].nip.io"),
        ("app-c0000207.nip.io", "app-[REDACTED:ip].nip.io"),
        ("x.c0000207.xip.io", "x.[REDACTED:ip].xip.io"),
        # dash-encoded working directory of a Claude Code project
        ("cwd -srv-git-192-0-2-7-sslip-io", "cwd -srv-git-[REDACTED:ip]-sslip-io"),
    ],
)
def test_ipv4_in_host_names(text: str, expected: str) -> None:
    assert redact(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "v1.2.3.4.5",  # five dotted parts: not an address
        "release 2.10.3",  # three parts
        "1.2.3.4.5.6.7",
        "build 256.1.1.1",  # octet out of range
        "see example-1-2-3.example.test",  # dashed digits outside sslip/nip
        "deadbeef.example.test",  # hex label outside sslip/nip
    ],
)
def test_ipv4_false_positive_guards(text: str) -> None:
    assert redact(text) == text


def test_four_part_version_is_masked_by_design() -> None:
    # A four-part version whose parts are all 0-255 cannot be told apart from
    # an address; it was masked before this change too and stays masked.
    assert redact("pkg 1.2.3.4 installed") == "pkg [REDACTED:ip] installed"


# -- Tailscale ----------------------------------------------------------------


def test_tailnet_label_masked_machine_kept() -> None:
    assert redact("ssh build-01.tail0000.ts.net") == "ssh build-01.[REDACTED:tailnet].ts.net"
    assert redact("https://box.example-net.ts.net/") == "https://box.[REDACTED:tailnet].ts.net/"


def test_tailscale_cgnat_address() -> None:
    assert redact("tailscale ip 100.64.0.7") == "tailscale ip [REDACTED:ip]"


# -- host names: hook, default off ----------------------------------------------


def test_host_names_kept_by_default() -> None:
    text = "deploy on build-01 then build-02"
    assert redact(text) == text


def test_redact_hostnames_hook() -> None:
    text = "deploy on Build-01, not build-010; build-01.tail0000.ts.net"
    out = redact(text, redact_hostnames=True, hostnames=["build-01"])
    assert out == (
        "deploy on [REDACTED:host], not build-010; [REDACTED:host].[REDACTED:tailnet].ts.net"
    )
    assert redact(out, redact_hostnames=True, hostnames=["build-01"]) == out
    # Without a list only the machine label of a *.ts.net name is known.
    assert (
        redact("box.tail0000.ts.net and box", redact_hostnames=True)
        == "[REDACTED:host].[REDACTED:tailnet].ts.net and box"
    )


# -- phone numbers ------------------------------------------------------------


@pytest.mark.parametrize(
    "number",
    [
        "13800138000",
        "138 0013 8000",
        "138-0013-8000",
        "+8613800138000",
        "+86 138 0013 8000",
        "+86-138-0013-8000",
        "0086 13800138000",
        "086-13800138000",
        "8613800138000",
        "(+86) 138-0013-8000",
        "+1-555-010-0199",
        "+1 555 010 0199",
        "+15550100199",
        "+44 20 7946 0958",
        "+49 (0)30 1234567",
    ],
)
def test_phone_numbers(number: str) -> None:
    assert redact(f"call {number} now") == "call [REDACTED:phone] now"
    assert redact(f"tel:{number},") == "tel:[REDACTED:phone],"


def test_phone_with_trailing_number_is_trimmed_not_skipped() -> None:
    # 18 digits in all: the trailing group is dropped until 8-15 digits remain.
    assert redact("+44 20 7946 0958 12345") == "[REDACTED:phone] 12345"


@pytest.mark.parametrize(
    "text",
    [
        "2026-11-20",
        "2026-11-20T08:30:00+08:00",
        "20261120",
        "order 202611201380013800",  # longer order id
        "id 1380013800",  # ten digits
        "ts 1763600000000",  # millisecond timestamp
        "commit a13800138000b9",  # hex hash with an 11-digit run
        "+100.00 (1234.56)",  # money, dots are not separators
        "@@ -12,7 +12,8 @@",  # diff hunk header
        "+123456789012345678901",  # too many digits for E.164
        "score +12 / +3",
        "x+13800138000",  # glued to a word
        "555-0100",  # bare national number without "+": not matched
    ],
)
def test_phone_false_positive_guards(text: str) -> None:
    assert redact(text) == text


def test_cn_mobile_shaped_id_is_masked_by_design() -> None:
    # An 11-digit id with the exact shape of a mobile number is masked: the
    # safe side when the two cannot be told apart.
    assert redact("ticket 13800138000") == "ticket [REDACTED:phone]"


# -- idempotence and older behaviour ------------------------------------------

SAMPLE = (
    "user a.person@example.test at " + MAC_HOME + "x on git.192.0.2.7.sslip.io, "
    "svc-192-0-2-7.sslip.io, box.tail0000.ts.net, +86 138 0013 8000, "
    "+1-555-010-0199, ping 10.0.0.1. token=abcd1234efgh"
)


def test_idempotent() -> None:
    once = redact(SAMPLE)
    assert redact(once) == once
    for leaked in ("192.0.2.7", "192-0-2-7", "tail0000", "0013", "555", "10.0.0.1"):
        assert leaked not in once


def test_redact_path_unchanged() -> None:
    # redact_path still masks only home-directory user names.
    text = MAC_HOME + "git.192.0.2.7.sslip.io +86 138 0013 8000"
    assert redact_path(text) == "/Users/[REDACTED:user]/git.192.0.2.7.sslip.io +86 138 0013 8000"


# -- call sites that used redact_path -----------------------------------------


def _row(**values: object) -> sqlite3.Row:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    names = ", ".join(f"? AS {name}" for name in values)
    row = connection.execute(f"SELECT {names}", tuple(values.values())).fetchone()
    assert isinstance(row, sqlite3.Row)
    return row


def test_history_entry_redacts_path_and_snippet() -> None:
    row = _row(
        source="claude",
        offset=0,
        line=1,
        session_id="s",
        parent_session_id="",
        window_id="w",
        parent_window_id="",
        entry_id="e",
        parent_entry_id="",
        role="user",
        kind="text",
        agent="claude",
        ts="",
        path=LINUX_HOME + ".claude/projects/-home-someone-git-192-0-2-7-sslip-io/a.jsonl",
        # a snippet indexed under the older rules
        snippet="clone git.192.0.2.7.sslip.io, call +86 138 0013 8000",
    )
    entry = TranscriptIndex._entry(row, "fid")
    assert entry["path"] == (
        "/home/[REDACTED:user]/.claude/projects/-home-[REDACTED:user]-git-[REDACTED:ip]-sslip-io/a.jsonl"
    )
    assert entry["snippet"] == "clone git.[REDACTED:ip].sslip.io, call [REDACTED:phone]"


def test_tools_map_clean_uses_full_redaction() -> None:
    assert tools_map._clean("/srv/backup-192.0.2.7/notes.txt") == (
        "/srv/backup-[REDACTED:ip]/notes.txt"
    )


def test_share_files_uses_full_redaction() -> None:
    turn = SimpleNamespace(
        tool_calls=[{"name": "Edit", "summary": "file_path=/srv/backup-192.0.2.7/notes.txt"}]
    )
    files = ConversationShare._files([turn])  # type: ignore[list-item]
    assert files == ["/srv/backup-[REDACTED:ip]/notes.txt"]


# -- review round 2 -------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # a trailing token used to make the whole number escape
        ("+1 555 010 0123 5pm", "[REDACTED:phone] 5pm"),
        ("+1 555 010 0123 1763596800", "[REDACTED:phone] 1763596800"),
        ("+44 20 7946 0958 3rd", "[REDACTED:phone] 3rd"),
        ("+15550100123 1763596800", "[REDACTED:phone] 1763596800"),
        ("+1 (555) 010-0123 13800138000", "[REDACTED:phone] [REDACTED:phone]"),
        ("+44 2079460958", "[REDACTED:phone]"),
        # Unicode hyphens / dashes and dots as separators
        ("138‑0013‑8000", "[REDACTED:phone]"),
        ("+1–555–010–0123", "[REDACTED:phone]"),
        ("138.0013.8000", "[REDACTED:phone]"),
        ("+1.555.010.0123", "[REDACTED:phone]"),
        # space-grouped CN numbers: prefix or a phone word required
        ("phone: 138 0013 8000", "phone: [REDACTED:phone]"),
        ("手机 138 0013 8000", "手机 [REDACTED:phone]"),
        ("电话：138 0013 8000", "电话：[REDACTED:phone]"),
        ("0086 138 0013 8000", "[REDACTED:phone]"),
    ],
)
def test_review_phone_cases(text: str, expected: str) -> None:
    assert redact(text) == expected
    assert redact(expected) == expected


@pytest.mark.parametrize(
    "text",
    [
        "GMT +8 2026-11-20 10:00",
        "+20261120",
        "+5 10 15 20 25 30",
        "+2 1024 4096",
        "commits 143 1024 2048",
        "sizes 138 0013 8000",  # space-grouped, no phone word, no prefix
        "+86 2026-11-20",
        "v1.138.0013.8000",
        "+1.555 010-0123",  # mixed dots and other separators
    ],
)
def test_review_phone_false_positives(text: str) -> None:
    assert redact(text) == text


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("C0000207.NIP.IO", "[REDACTED:ip].NIP.IO"),
        ("APP-192-0-2-7.SSLIP.IO", "APP-[REDACTED:ip].SSLIP.IO"),
        ("-srv-box-tail0000-ts-net", "-srv-box-[REDACTED:tailnet]-ts-net"),
        # custom names in dashed form are not masked (ambiguous with directory words)
        ("-srv-box-cat-dog-ts-net", "-srv-box-cat-dog-ts-net"),
    ],
)
def test_review_host_encodings(text: str, expected: str) -> None:
    assert redact(text) == expected


def test_loopback_and_any_unchanged() -> None:
    # Same as before this change: every dotted IPv4, loopback included.
    assert redact("bind 127.0.0.1 not 0.0.0.0") == "bind [REDACTED:ip] not [REDACTED:ip]"


def test_hostname_hook_skips_reserved_words_and_masks() -> None:
    text = "phone [REDACTED:phone] ip host box"
    names = ["phone", "ip", "REDACTED", "host", "box"]
    out = redact(text, redact_hostnames=True, hostnames=names)
    assert out == "phone [REDACTED:phone] ip host [REDACTED:host]"
    assert redact(out, redact_hostnames=True, hostnames=names) == out


def test_distill_drops_secret_before_redacting() -> None:
    from memfw.dream.distill import _clean

    assert _clean("set password=hunter2hunter2 now") is None
    assert _clean("call +1 555 010 0199") == "call [REDACTED:phone]"


_TOKENS = [
    "+",
    "1",
    "7",
    "44",
    "86",
    "138",
    "0013",
    "8000",
    "555",
    "010",
    "0123",
    "20",
    "7946",
    "1763596800",
    "13800138000",
    "2026",
    "-11-20",
    " ",
    " ",
    "-",
    ".",
    "(",
    ")",
    "–",
    "5pm",
    "3rd",
    "tel ",
    "phone: ",
    "手机",
    "[REDACTED:phone]",
    "[REDACTED:ip]",
    "x",
    "192.0.2.7",
    ".sslip.io",
    "-sslip-io",
    "c0000207",
    ".nip.io",
    "box",
    ".tail0000",
    ".ts.net",
    "-ts-net",
    "tail0000",
    "-",
    "0086",
    "00",
    "9",
]


def test_idempotence_fuzz() -> None:
    import random

    rng = random.Random(20261120)
    failures = []
    for _ in range(20000):
        text = "".join(rng.choice(_TOKENS) for _ in range(rng.randint(1, 14)))
        once = redact(text)
        if redact(once) != once:
            failures.append(text)
    assert failures == []


# -- review round 3 -------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # letters glued to the tail ended the match and the number escaped
        ("+1-555-010-0123x12", "[REDACTED:phone]"),
        ("+1 555 010 0123 ext. 34", "[REDACTED:phone]"),
        ("+1 555 010 0123 #56", "[REDACTED:phone]"),
        ("+1 555 010 0123foo", "[REDACTED:phone]foo"),
        ("+8613800138000x", "[REDACTED:phone]x"),
        ("+44 20 7946 0958abc", "[REDACTED:phone]abc"),
        # cue words before a space-grouped CN mobile
        ("手机号：138 0013 8000", "手机号：[REDACTED:phone]"),
        ("Tel. 138 0013 8000", "Tel. [REDACTED:phone]"),
        ("Mob: 138 0013 8000", "Mob: [REDACTED:phone]"),
        ("my phone number is 138 0013 8000", "my phone number is [REDACTED:phone]"),
        ("号码 138 0013 8000", "号码 [REDACTED:phone]"),
        # runs of spaces and slashes as separators, leading-zero octets
        ("+1 555  010 0123", "[REDACTED:phone]"),
        ("+1/555/010/0123", "[REDACTED:phone]"),
        ("138/0013/8000", "[REDACTED:phone]"),
        ("192.0.2.007", "[REDACTED:ip]"),
        ("-srv-box-tail0000-ts-net-foo", "-srv-box-[REDACTED:tailnet]-ts-net-foo"),
    ],
)
def test_round3_masked(text: str, expected: str) -> None:
    assert redact(text) == expected
    assert redact(expected) == expected


@pytest.mark.parametrize(
    "text",
    [
        "+1 555 010 0123 5pm",  # a time after the number stays
        "commits 143 1024 2048",
        "sizes 138 0013 8000",
        "2026/11/20",
        "+1/2",
        "+1.555/010-0123",  # mixed dot / slash / hyphen separators
        "a13800138000b9",
        "x+13800138000",
        "-srv-a-work-ts-net/x",  # no over-mask of directory words
        "-srv-x-ts-net-foo",
    ],
)
def test_round3_unmasked_parts(text: str) -> None:
    out = redact(text)
    if text == "+1 555 010 0123 5pm":
        assert out == "[REDACTED:phone] 5pm"
    else:
        assert out == text


def test_fixed_point_converges_quickly() -> None:
    import importlib

    module = importlib.import_module("memfw.history.redact")
    finders = (module._secret_spans, module._personal_spans, module._path_spans)
    realistic = [
        SAMPLE,
        "clone git.192.0.2.7.sslip.io then ssh box.tail0000.ts.net; tel 138 0013 8000",
        "93rd86.ts.net192.0.2.7",  # a mask changes a neighbour's boundary
        "1)13800138000+7 13880007",
        "no personal data here",
    ]
    before = module.PASS_LIMIT_HITS
    for text in realistic:
        _out, passes = module._fixpoint(text, finders)
        assert passes <= 3, text
    assert module.PASS_LIMIT_HITS == before
