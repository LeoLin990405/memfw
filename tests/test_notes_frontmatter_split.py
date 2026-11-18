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
}
#: Read exactly as before the fix (the first candidate is at column 0, or an
#: indented close with nothing privacy-related after it).
UNCHANGED = {
    "plain": "---\ntitle: A\nprivacy: public\n---\n",
    "hr_in_body": "---\nprivacy: public\n---\nbody\n---\nprivacy: local-private\n",
    "indented_close": "---\nprivacy: shared\n  ---\nbody\n",
    "indented_close_hr": "---\nprivacy: shared\n  ---\nbody\n---\nmore\n",
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
