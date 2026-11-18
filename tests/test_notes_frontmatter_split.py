"""Frontmatter split: a ``---`` line inside a YAML block scalar never ends the block.

Before the fix, ``memfw.notes.index`` closed the frontmatter at the first line
that was ``---`` once stripped, so an indented ``---`` inside a ``|``/``>``
block scalar ended it and a later ``privacy: local-private`` became body: the
note read as public. The kernel and the two adapters that mirror its split
(PageIndex, QMD) now share one rule (``frontmatter_close``); these tests pin
the rule, the kernel's privacy reading and the adapters' parity with it.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from memfw.notes import NotesIndex
from memfw.notes.index import NoteParseError, frontmatter_close, split_frontmatter
from memfw.ports.privacy import metadata_level
from memfw.recall.builtin import NotesSegmentResolver

ROOT = Path(__file__).resolve().parents[1]
for name in ("memfw-adapter-pageindex", "memfw-adapter-qmd"):
    src = ROOT / "packages" / "adapters" / name / "src"
    if str(src) not in sys.path:  # workspace members; CI does not install them
        sys.path.insert(0, str(src))

from memfw_adapter_pageindex import tree as pageindex_tree  # noqa: E402
from memfw_adapter_qmd import provider as qmd_provider  # noqa: E402

#: The leak: a ``---`` line inside a block scalar, a private key after it.
LEAKS = {
    "literal": "---\ntitle: A\nnotes: |\n  one\n  ---\n  two\nprivacy: local-private\n---\n",
    "folded": "---\ntitle: A\nnotes: >\n  one\n  ---\n  two\nsensitivity: private\n---\n",
    "keep": "---\nnotes: |+\n  ---\n\ntags: [privacy:local-private]\n---\n",
    "strip_indent": "---\nnotes: |2-\n   ---\nprivacy: local-private\n---\n",
    "nested": "---\nmeta:\n  log: |\n    ---\nprivacy: local-private\n---\n",
    "plain_multiline": "---\ntitle: a\n  ---\n  b\nprivacy: local-private\n---\n",
    "two_inner": "---\nn: |\n  ---\n  x\n  ---\nprivacy: local-private\n---\n",
    "no_hard_close": "---\nn: |\n  ---\nprivacy: local-private\n",
    "broken_after": "---\nn: |\n  ---\nb: [unclosed\nprivacy: local-private\n---\n",
    "dup_override": "---\nprivacy: local-private\nn: |\n  ---\nprivacy: public\n---\n",
    # An indented close whose longer reading does not parse: ambiguous now.
    "indented_close_hr": "---\nprivacy: shared\n  ---\nbody\n---\nmore\n",
    # Reviewer cases (#104): quoted/merge keys after a failing later reading,
    # and more candidates than are tried.
    "quoted_key_failing": '---\nn: |\n  ---\n"privacy": local-private\ndate: 2024-13-45\n---\nb\n',
    "bare_key_failing": "---\nn: |\n  ---\nprivacy: local-private\ndate: 2024-13-45\n---\nb\n",
    "merge_key_failing": "---\nn: |\n  ---\n<<: {privacy: local-private}\nx: [\n---\nb\n",
    "65_candidates_quoted": "---\nn: |\n" + "  ---\n" * 66 + '"privacy": local-private\n---\nb\n',
    "70_candidates": "---\nprivacy: local-private\nn: |\n"
    + "  ---\n" * 70
    + "privacy: public\n---\n",
    "explicit_key_after": "---\nn: |\n  ---\n? privacy\n: public\n---\n",
}
#: Read exactly as before the fix (the first candidate is at column 0, or an
#: indented close with nothing privacy-related after it).
UNCHANGED = {
    "plain": "---\ntitle: A\nprivacy: public\n---\n",
    "hr_in_body": "---\nprivacy: public\n---\nbody\n---\nprivacy: local-private\n",
    "indented_close": "---\nprivacy: shared\n  ---\nbody\n",
    "block_scalar_no_dashes": "---\nnotes: |\n  a\n  b\nprivacy: shared\n---\n",
    "column0_inside_block": "---\nnotes: |\n  a\n---\nprivacy: local-private\n",
    "crlf": "---\r\nprivacy: public\r\n---\r\nbody\r\n",
    "empty": "---\n---\nbody\n",
}
#: The YAML reading is now used: a block scalar holding ``---`` with a public note.
CORRECTED = {
    "literal_public": "---\nprivacy: public\nnotes: |\n  one\n  ---\n  two\n---\n",
    "folded_shared": "---\nprivacy: shared\nnotes: >\n  ---\n---\n",
}
BODY = "# Heading\nsection text\n"


def _kernel_level(text: str, legacy: str = "public") -> str:
    try:
        frontmatter, _body, _offset = split_frontmatter(text)
    except NoteParseError:
        return "unknown"
    return metadata_level(frontmatter, legacy)


def _old_split(text: str) -> tuple[dict[str, object], str, int]:
    """The split before the fix, for the "unchanged" cases."""

    import yaml

    if not text.startswith("---"):
        return {}, text, 0
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return {}, text, 0
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        return {}, text, 0
    return dict(yaml.safe_load("".join(lines[1:end])) or {}), "".join(lines[end + 1 :]), end + 1


@pytest.mark.parametrize("name", sorted(LEAKS))
def test_a_dash_line_in_a_block_scalar_never_makes_a_note_public(name: str) -> None:
    text = LEAKS[name] + BODY
    assert _kernel_level(text) in ("local-private", "unknown"), name


@pytest.mark.parametrize("name", sorted(UNCHANGED))
def test_ordinary_notes_split_exactly_as_before(name: str) -> None:
    text = UNCHANGED[name] + BODY
    assert split_frontmatter(text) == _old_split(text), name


@pytest.mark.parametrize("name", sorted(CORRECTED))
def test_the_block_scalar_reading_is_used_when_it_is_unambiguous(name: str) -> None:
    text = CORRECTED[name] + BODY
    frontmatter, body, _offset = split_frontmatter(text)
    assert "---" in str(frontmatter["notes"])
    assert body == BODY
    assert metadata_level(frontmatter, "local-private") in ("public", "shared")


def test_the_literal_leak_reads_the_whole_block() -> None:
    frontmatter, body, offset = split_frontmatter(LEAKS["literal"] + BODY)
    assert frontmatter == {
        "title": "A",
        "notes": "one\n---\ntwo\n",
        "privacy": "local-private",
    }
    assert body == BODY and offset == 8


ALL = {**LEAKS, **UNCHANGED, **CORRECTED}


@pytest.mark.parametrize("name", sorted(ALL))
def test_the_adapters_close_the_block_where_the_kernel_does(name: str) -> None:
    lines = (ALL[name] + BODY).splitlines(keepends=True)
    kernel = frontmatter_close(lines)
    assert pageindex_tree.frontmatter_close(lines) == kernel, name
    assert qmd_provider.frontmatter_close(lines) == kernel, name


@pytest.mark.parametrize("legacy", ["public", "shared"])
@pytest.mark.parametrize("name", sorted(ALL))
def test_the_adapters_are_never_less_private_than_the_kernel(name: str, legacy: str) -> None:
    text = ALL[name] + BODY
    rank = {"public": 0, "shared": 1, "local-private": 2, "unknown": 3}
    kernel = _kernel_level(text, legacy)
    qmd_level, qmd_start = qmd_provider.note_level(text, legacy)
    assert rank[qmd_level] >= rank[kernel], name
    pageindex_level = pageindex_tree.note_level(text, legacy)
    assert rank[pageindex_level] >= min(rank[kernel], rank["local-private"]), name
    if name in LEAKS:
        assert qmd_level in ("local-private", "unknown"), name
        assert pageindex_level == "local-private", name
    try:
        _frontmatter, _body, offset = split_frontmatter(text)
    except NoteParseError:
        return
    if offset:
        assert qmd_start == offset, name
        _block, start, _malformed = pageindex_tree._frontmatter_block(text)
        assert start == offset, name


def test_notes_search_and_kh_resolver_withhold_the_leak(tmp_path: Path) -> None:
    root = tmp_path / "notes"
    root.mkdir()
    (root / "leak.md").write_text(LEAKS["literal"] + BODY, encoding="utf-8")
    (root / "open.md").write_text(CORRECTED["literal_public"] + BODY, encoding="utf-8")
    db = tmp_path / "notes.sqlite3"
    NotesIndex(db).index(root)
    resolver = NotesSegmentResolver(root, db, legacy_privacy_default="public")
    found = resolver.resolve(["kh:leak.md#Heading", "kh:open.md#Heading"])
    assert set(found) == {"kh:open.md#Heading"}
    cleared = NotesSegmentResolver(
        root,
        db,
        clearance=frozenset({"public", "shared", "local-private"}),
        legacy_privacy_default="public",
    )
    private = cleared.resolve(["kh:leak.md#Heading"])["kh:leak.md#Heading"]
    assert private.privacy == "local-private"


# --- #104 review: the reviewer's differential harness, as tests --------------------------

#: The reviewer's corpus (kernel vs PageIndex vs QMD vs the split before the fix).
REVIEW_CASES = {
    "plain": "---\nprivacy: public\n---\nbody\n",
    "crlf": "---\r\nprivacy: local-private\r\n---\r\nbody\r\n",
    "bom": "﻿---\nprivacy: local-private\n---\nbody\n",
    "block_indented": "---\nprivacy: public\nnote: |\n  a\n  ---\nprivacy: local-private\n---\nb\n",
    "block_strip_keep": "---\nnote: |-\n  ---\nx: >+\n  ---\n\nprivacy: public\n---\n",
    "block_col0_dash": "---\nnote: |\n  a\n---\nprivacy: public\n---\n",
    "docend": "---\nprivacy: local-private\n...\nprivacy: public\n---\n",
    "tab_indent": "---\nnote: |\n\t---\nprivacy: public\n---\n",
    "flow": '---\n{privacy: public, a: "\n  ---\n  "}\n---\n',
    "dup_keys": "---\nprivacy: local-private\nn: |\n  ---\nprivacy: public\n---\n",
    "anchor_alias": "---\na: &a public\nn: |\n  ---\nprivacy: *a\n---\n",
    "merge": "---\nb: &b {privacy: public}\nn: |\n  ---\n<<: *b\n---\n",
    "merge_override": "---\nprivacy: local-private\nb: &b {privacy: public}\n<<: *b\n---\n",
    "x85_sep": "---\nprivacy: public\x85---\nprivacy: local-private\n---\n",
    "x0c": "---\nprivacy: public\x0c---\n",
    "x1c": "---\nprivacy: public\n  ---\x1cprivacy: local-private\n---\n",
    "nbsp_dash": "---\nn: |\n  a\n ---\nprivacy: public\n---\n",
    "quoted_multiline": '---\nt: "a\n  ---\n  b"\nprivacy: public\n---\n',
    "quoted_ml_priv_before": (
        "---\nprivacy: local-private\nt: 'a\n  ---\n  b'\nprivacy: public\n---\n"
    ),
    "Case_key": "---\nn: |\n  ---\nPrivacy: public\n---\n",
    "nested": "---\nn: |\n  ---\nmeta:\n  privacy: public\n---\n",
    "trailing_ws": "---\nprivacy: public\n---   \nb\n",
    "dash_comment": "---\ntitle: x\n--- # c\nprivacy: public\n---\n",
    "after_first_col0": "---\ntitle: x\n---\nprivacy: local-private\n",
    "quoted_key_after": '---\nn: |\n  ---\n"privacy": public\n---\n',
    "explicit_key_after": "---\nn: |\n  ---\n? privacy\n: public\n---\n",
    "unknown_forced_down": "---\nprivacy: secret\nnote: |\n  ---\ntags: [x]\n---\n",
    "short_fails": '---\nt: "a\n  ---\nb"\n---\n',
    "short_fails_unknown": '---\nprivacy: Secret\nt: "a\n  ---\nb"\n---\n',
    "tag_key_kernel": "---\nprivacy: public\ntag: [privacy:local-private]\n---\n",
    "scalar_tags": "---\nprivacy: public\ntags: privacy:local-private\n---\n",
    "bad_date": "---\ndate: 2024-13-45\n---\n",
    "bad_date_indented": "---\nn: |\n  ---\ndate: 2024-13-45\n---\n",
    **{f"leak_{name}": text for name, text in LEAKS.items()},
}
REVIEW_RANK = {"public": 0, "shared": 1, "local-private": 2, "unknown": 3, "hidden": 3}


def _old_level(text: str, legacy: str) -> str:
    try:
        frontmatter, _body, _offset = _old_split(text)
    except Exception:  # noqa: BLE001 - the old reader withheld (or crashed on) it
        return "hidden"
    return metadata_level(frontmatter, legacy)


def _new_level(text: str, legacy: str) -> str:
    try:
        frontmatter, _body, _offset = split_frontmatter(text)
    except NoteParseError:
        return "hidden"
    return metadata_level(frontmatter, legacy)


@pytest.mark.parametrize("legacy", ["public", "local-private"])
@pytest.mark.parametrize("name", sorted(REVIEW_CASES))
def test_review_corpus_parity_and_never_looser(name: str, legacy: str) -> None:
    text = REVIEW_CASES[name]
    lines = text.splitlines(keepends=True)
    closes = [
        frontmatter_close(lines),
        pageindex_tree.frontmatter_close(lines),
        qmd_provider.frontmatter_close(lines),
    ]
    assert closes[0] == closes[1] == closes[2], name
    kernel = _new_level(text, legacy)  # never raises anything but NoteParseError
    assert REVIEW_RANK[kernel] >= REVIEW_RANK[_old_level(text, legacy)], name
    qmd = qmd_provider.note_level(text, legacy)[0]
    pageindex = pageindex_tree.note_level(text, legacy)
    assert REVIEW_RANK[qmd] >= REVIEW_RANK[kernel], name  # an adapter is never looser
    assert REVIEW_RANK[pageindex] >= min(REVIEW_RANK[kernel], 2), name


def test_an_ambiguous_close_keeps_an_unknown_level() -> None:
    """Forcing ``local-private`` must not relax a note that already read ``unknown``."""

    for name in ("unknown_forced_down", "short_fails_unknown"):
        text = REVIEW_CASES[name]
        assert _new_level(text, "public") in ("unknown", "hidden"), name
        assert qmd_provider.note_level(text, "public")[0] == "unknown", name


def test_ordinary_notes_split_as_before_fuzz() -> None:
    """The reviewer's identity fuzz: a column-0 close splits exactly as before."""

    import random

    rng = random.Random(7)
    atoms = ["privacy: public", "title: x", "tags: [a, b]", "n: |", "  text", "...", "k: 'a b'"]
    atoms += ["- item", ""]
    for _ in range(3000):
        nl = rng.choice(["\n", "\r\n"])
        pre = [rng.choice(atoms) for _ in range(rng.randint(0, 6))]
        text = "---" + nl + nl.join(pre) + (nl if pre else "")
        text += rng.choice(["---", "--- ", "---\t"]) + nl
        text += nl.join(rng.choice([*atoms, "  ---"]) for _ in range(4)) + nl
        try:
            old: Any = _old_split(text)
        except Exception:  # noqa: BLE001
            old = "error"
        try:
            frontmatter, body, offset = split_frontmatter(text)
            new: Any = (frontmatter, body, offset)
        except NoteParseError:
            new = "error"
        assert new == old, repr(text)


def test_frontmatter_close_is_bounded() -> None:
    """64 far-apart candidates in a huge block: answered fast, and fail closed."""

    import time

    chunk = "".join("  " + "a" * 70 + "\n" for _ in range(300))
    big = "---\nn: |\n" + ("  ---\n" + chunk) * 64 + "tail: x\n"
    started = time.monotonic()
    end, ambiguous = frontmatter_close(big.splitlines(keepends=True))
    assert time.monotonic() - started < 5.0
    assert ambiguous and end is not None
    huge = "---\nn: |\n" + ("  " + "a" * 60 + "\n  ---\n") * 20000 + "privacy: public\n"
    started = time.monotonic()
    assert _new_level(huge, "public") in ("local-private", "unknown")
    assert time.monotonic() - started < 5.0
