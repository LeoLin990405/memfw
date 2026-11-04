"""Offline synthetic tests for the privacy-first pilot importer."""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from pilot_import import ImportFormatError, import_pilot

SHANGHAI = ZoneInfo("Asia/Shanghai")


def _snapshot(root: Path) -> dict[str, tuple[str, int, int]]:
    result: dict[str, tuple[str, int, int]] = {}
    for path in root.rglob("*"):
        if path.is_file():
            stat = path.stat()
            result[path.relative_to(root).as_posix()] = (
                hashlib.sha256(path.read_bytes()).hexdigest(),
                stat.st_mtime_ns,
                stat.st_size,
            )
    return result


def _fixture(root: Path) -> None:
    (root / "notes").mkdir(parents=True)
    (root / "notes" / "one.md").write_text(
        "---\nid: note-one\ntitle: One\ncreated_at: 2026-01-01T00:00:00Z\n"
        "updated_at: 2026-01-02T00:00:00Z\n---\nBody [[note-two]] and [[missing]].\n",
        encoding="utf-8",
    )
    (root / "notes" / "two.md").write_text(
        "---\nid: note-two\ntitle: Two\n---\nSecond note.\n", encoding="utf-8"
    )
    (root / "notes" / "three.md").write_text(
        "---\ntitle: Three\n---\nThird note.\n", encoding="utf-8"
    )
    chat = {
        "conversations": [
            {
                "id": "chat-1",
                "title": "Chat one",
                "create_time": 1767225600,
                "update_time": 1767312000,
                "current_node": "node-2",
                "mapping": {
                    "node-1": {
                        "parent": None,
                        "message": {
                            "author": {"role": "user"},
                            "content": {"parts": ["First prompt"]},
                        },
                    },
                    "node-2": {
                        "parent": "node-1",
                        "message": {
                            "author": {"role": "assistant"},
                            "content": {"parts": ["First answer"]},
                        },
                    },
                },
            }
        ]
    }
    (root / "chat.json").write_text(json.dumps(chat), encoding="utf-8")
    (root / "chat.txt").write_text(
        "MEMFW_CHAT_EXPORT_V1\nTitle: Text chat\nCreated: 2026-01-03T00:00:00Z\n"
        "Modified: 2026-01-03T01:00:00Z\n\nuser: Hello\nassistant: Hi\n",
        encoding="utf-8",
    )


def test_imports_five_synthetic_records_and_resolves_links(tmp_path: Path) -> None:
    _fixture(tmp_path)
    before = _snapshot(tmp_path)

    result = import_pilot(tmp_path)

    after = _snapshot(tmp_path)
    assert len(result.records) == 5
    assert result.canonical_bytes > 0
    assert result.earliest == datetime(2026, 1, 1, tzinfo=UTC)
    record = next(item for item in result.records if item.source_id == "note-one")
    known = next(item for item in result.records if item.source_id == "note-two")
    assert record.references == (known.record_id,)
    assert known.record_id == record.references[0]
    assert record.unresolved_references == ("missing",)
    assert record.body.endswith("[[missing]].\n")
    assert record.created_at == datetime(2026, 1, 1, tzinfo=UTC)
    assert record.payload()["memory_id"] == record.record_id
    assert before == after


def test_unknown_json_reports_structure_only_without_values(tmp_path: Path) -> None:
    secret = "private body that must not escape"
    (tmp_path / "unknown.json").write_text(
        json.dumps({"private-title": secret, "messages": [{"body": secret}]}), encoding="utf-8"
    )

    with pytest.raises(ImportFormatError) as caught:
        import_pilot(tmp_path)

    assert caught.value.code == "STRUCTUREONLY"
    assert secret not in str(caught.value)
    assert "private-title" not in str(caught.value)


def test_external_symlink_is_rejected(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.md"
    outside.write_text("private", encoding="utf-8")
    pilot = tmp_path / "pilot"
    pilot.mkdir()
    link = pilot / "escape.md"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks are unavailable")

    with pytest.raises(ImportFormatError, match="PATH_REJECTED"):
        import_pilot(pilot)


def test_invalid_utf8_is_skipped_and_counted(tmp_path: Path) -> None:
    (tmp_path / "bad.md").write_bytes(b"\xffprivate")
    (tmp_path / "good.md").write_text("---\ndate: 2026-01-01\n---\nBody", encoding="utf-8")

    result = import_pilot(tmp_path)
    assert len(result.records) == 1
    assert result.skipped_non_utf8 == 1
    assert "private" not in repr(result)


def test_markdown_without_frontmatter_uses_heading_and_filesystem_times(tmp_path: Path) -> None:
    note = tmp_path / "plain.md"
    note.write_text("# Plain title\n\nAn Obsidian note [[other]].\n", encoding="utf-8")
    result = import_pilot(tmp_path)
    record = result.records[0]
    stat = note.stat()

    assert record.title == "Plain title"
    assert record.body.startswith("# Plain title")
    assert record.created_at == datetime.fromtimestamp(stat.st_mtime, UTC)
    assert record.updated_at == datetime.fromtimestamp(stat.st_mtime, UTC)
    assert result.mtime_fallback_records == 1
    assert record.unresolved_references == ("other",)


def test_yaml_date_and_naive_iso_times_use_shanghai_and_updated_alias(tmp_path: Path) -> None:
    (tmp_path / "dated.md").write_text(
        "---\ncreated_at: 2026-03-04\nupdated: 2026-03-05T01:02:03\n---\nBody.\n",
        encoding="utf-8",
    )
    (tmp_path / "offset.md").write_text(
        "---\ncreated_at: '2026-03-04T01:00:00+02:00'\n"
        "updated_at: '2026-03-04T02:00:00Z'\n---\nBody.\n",
        encoding="utf-8",
    )

    result = import_pilot(tmp_path)
    by_name = {record.source_ref: record for record in result.records}

    assert by_name["markdown:dated.md"].created_at == datetime(2026, 3, 4, tzinfo=SHANGHAI)
    assert by_name["markdown:dated.md"].updated_at == datetime(2026, 3, 5, 1, 2, 3, tzinfo=SHANGHAI)
    assert by_name["markdown:offset.md"].created_at == datetime(2026, 3, 3, 23, tzinfo=UTC)


def test_hidden_paths_and_bad_or_empty_sources_are_skipped(tmp_path: Path) -> None:
    (tmp_path / ".hidden.md").write_text("Body", encoding="utf-8")
    hidden = tmp_path / ".private"
    hidden.mkdir()
    (hidden / "nested.md").write_text("Body", encoding="utf-8")
    (tmp_path / "empty.md").write_text("  \n", encoding="utf-8")
    (tmp_path / "frontmatter-only.md").write_text("---\ntitle: Empty\n---\n", encoding="utf-8")
    (tmp_path / "bad-frontmatter.md").write_text("---\n[broken\n---\nBody", encoding="utf-8")
    (tmp_path / "good.md").write_text("---\ndate: 2026-01-01\n---\nBody", encoding="utf-8")

    result = import_pilot(tmp_path)
    assert [record.source_ref for record in result.records] == ["markdown:good.md"]
    assert result.skipped_empty == 2
    assert result.skipped_invalid_frontmatter == 1


def test_nested_metadata_wins_over_top_level_date_and_invalid_frontmatter_counts(
    tmp_path: Path,
) -> None:
    (tmp_path / "nested.md").write_text(
        "---\ndate: 2020-01-01\nmetadata:\n  created_at: 2026-02-01T10:00:00\n"
        "  modified: 2026-02-03T10:00:00\n---\nBody",
        encoding="utf-8",
    )
    (tmp_path / "unclosed.md").write_text("---\ntitle: No close\nBody", encoding="utf-8")
    result = import_pilot(tmp_path)
    record = result.records[0]
    assert record.created_at == datetime(2026, 2, 1, 10, tzinfo=SHANGHAI)
    assert record.updated_at == datetime(2026, 2, 3, 10, tzinfo=SHANGHAI)
    assert result.timestamp_metadata_records == 1
    assert result.timestamp_date_records == 0
    assert result.skipped_invalid_frontmatter == 1


def test_all_skipped_candidates_return_zero_record_statistics(tmp_path: Path) -> None:
    (tmp_path / "bad.md").write_bytes(b"\xff")
    (tmp_path / "empty.md").write_text(" \n", encoding="utf-8")
    result = import_pilot(tmp_path)
    assert result.records == ()
    assert result.supported_files == 2
    assert result.imported_records == 0
    assert result.skipped_non_utf8 == 1
    assert result.skipped_empty == 1


def test_markdown_relative_links_normalize_and_ignore_external_or_escaping_paths(
    tmp_path: Path,
) -> None:
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes" / "source.md").write_text(
        "---\nid: source\ndate: 2026-01-01\n---\n"
        "[target](../target%20file.md#heading) [bad](../../target%20file.md) "
        "[remote](https://example.test/target%20file.md)\n",
        encoding="utf-8",
    )
    (tmp_path / "target file.md").write_text(
        "---\nid: target\ndate: 2026-01-01\n---\nBody", encoding="utf-8"
    )
    (tmp_path / "other").mkdir()
    (tmp_path / "other" / "target file.md").write_text(
        "---\nid: other-target\ndate: 2026-01-01\n---\nBody", encoding="utf-8"
    )
    result = import_pilot(tmp_path)
    source = next(record for record in result.records if record.source_id == "source")
    target = next(record for record in result.records if record.source_id == "target")
    assert source.references == (target.record_id,)


def test_modified_only_sets_created_and_markdown_links_resolve_and_tags_survive(
    tmp_path: Path,
) -> None:
    (tmp_path / "source.md").write_text(
        "---\nid: source\nmodified: 2026-02-01T12:00:00\ntags: [alpha, beta]\n---\n"
        "[target](sub/target.md)\n",
        encoding="utf-8",
    )
    target = tmp_path / "sub"
    target.mkdir()
    (target / "target.md").write_text(
        "---\nid: target\ndate: 2026-01-01\n---\nBody", encoding="utf-8"
    )

    result = import_pilot(tmp_path)
    source = next(record for record in result.records if record.source_id == "source")
    target_record = next(record for record in result.records if record.source_id == "target")
    assert source.created_at == source.updated_at == datetime(2026, 2, 1, 12, tzinfo=SHANGHAI)
    assert source.references == (target_record.record_id,)
    assert source.payload()["tags"] == ["alpha", "beta"]
    assert result.mtime_fallback_records == 0


def test_single_string_tag_is_preserved_as_one_tag(tmp_path: Path) -> None:
    (tmp_path / "tagged.md").write_text(
        "---\ntags: research-note\ndate: 2026-01-01\n---\nBody", encoding="utf-8"
    )
    record = import_pilot(tmp_path).records[0]
    assert record.tags == ("research-note",)
    assert record.payload()["tags"] == ["research-note"]


def test_invalid_explicit_timestamp_fails_without_echoing_value(tmp_path: Path) -> None:
    private_value = "PRIVATE_TIMESTAMP_CANARY"
    (tmp_path / "invalid.md").write_text(
        f"---\ncreated_at: '{private_value}'\n---\nBody.\n", encoding="utf-8"
    )

    with pytest.raises(ImportFormatError) as caught:
        import_pilot(tmp_path)

    assert caught.value.code == "INVALID_TIMESTAMP"
    assert private_value not in str(caught.value)
