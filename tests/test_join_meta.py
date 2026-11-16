"""Unit tests for the id <-> path join, privacy classes and hit parsing (§3.8)."""

from __future__ import annotations

import hashlib

import pytest

from memfw.projection import (
    DEFAULT_CLEARANCE,
    DEFAULT_TENANTS,
    classify_privacy,
    hit_to_id,
    join_meta,
    parse_clearance,
    parse_meta_index,
    parse_tenants,
    stable_id,
)

REL = "example_gen8_cron.md"
REL_ID = "f" + hashlib.sha256(REL.encode("utf-8")).hexdigest()[:12]


def _meta(*paths: str) -> object:
    return parse_meta_index(
        {
            "generated_at": "2026-11-10T00:05:00Z",
            "items": [{"path": path, "lifecycle": "active", "privacy": None} for path in paths],
        }
    )


# ------------------------------------------------------------------------ stable_id


def test_stable_id_is_f_plus_first_12_hex_of_sha256_of_utf8_rel() -> None:
    assert stable_id(REL) == REL_ID
    assert len(stable_id(REL)) == 13
    rel = "迁移/笔记.md"
    assert stable_id(rel) == "f" + hashlib.sha256(rel.encode("utf-8")).hexdigest()[:12]


def test_stable_id_is_byte_sensitive() -> None:
    assert stable_id("a_b.md") != stable_id("a-b.md")


# ------------------------------------------------------------------------- join_meta


def test_join_ok_returns_item() -> None:
    result = join_meta(REL_ID, [f"fleet_path:{REL}", "cron"], _meta(REL))
    assert result.status == "ok"
    assert result.fleet_path == REL
    assert result.item is not None and result.item["path"] == REL


def test_join_without_fleet_path_tag_is_unjoined() -> None:
    result = join_meta(REL_ID, ["cron"], _meta(REL))
    assert result.status == "unjoined"
    assert result.fleet_path is None
    assert result.item is None


def test_join_with_multiple_fleet_path_tags_is_unjoined() -> None:
    result = join_meta(REL_ID, [f"fleet_path:{REL}", "fleet_path:other.md"], _meta(REL))
    assert result.status == "unjoined"
    assert result.item is None
    assert "multiple" in result.reason


def test_join_tag_present_but_path_absent_from_meta_is_unjoined() -> None:
    result = join_meta(REL_ID, [f"fleet_path:{REL}"], _meta("something_else.md"))
    assert result.status == "unjoined"
    assert result.fleet_path == REL
    assert result.item is None


def test_join_duplicate_meta_path_is_unjoined() -> None:
    meta = _meta(REL, REL)
    result = join_meta(REL_ID, [f"fleet_path:{REL}"], meta)
    assert result.status == "unjoined"
    assert result.item is None
    assert "duplicate" in result.reason


def test_join_imported_id_mismatch() -> None:
    result = join_meta("f000000000000", [f"fleet_path:{REL}"], _meta(REL))
    assert result.status == "id_mismatch"
    assert result.fleet_path == REL
    assert result.item is None


def test_join_id_mismatch_is_checked_before_meta_availability() -> None:
    assert join_meta("f000000000000", [f"fleet_path:{REL}"], None).status == "id_mismatch"


@pytest.mark.parametrize("memory_id", ["native-note-0001", "zeval-0001", "F0123456789AB"])
def test_join_non_f_ids_are_not_id_checked(memory_id: str) -> None:
    result = join_meta(memory_id, [f"fleet_path:{REL}"], _meta(REL))
    assert result.status == "ok"


@pytest.mark.parametrize("memory_id", ["native-note-0001", "zeval-0001"])
def test_join_non_f_ids_still_need_tag_and_item(memory_id: str) -> None:
    assert join_meta(memory_id, [], _meta(REL)).status == "unjoined"
    assert join_meta(memory_id, [f"fleet_path:{REL}"], _meta("other.md")).status == "unjoined"


def test_join_path_match_is_exact_without_underscore_dash_normalisation() -> None:
    rel = "a_b.md"
    result = join_meta(stable_id(rel), [f"fleet_path:{rel}"], _meta("a-b.md"))
    assert result.status == "unjoined"
    rel_upper = "Example_Gen8_Cron.md"
    assert join_meta(stable_id(rel_upper), [f"fleet_path:{rel_upper}"], _meta(REL)).status == (
        "unjoined"
    )


def test_join_meta_none_is_unjoined() -> None:
    result = join_meta(REL_ID, [f"fleet_path:{REL}"], None)
    assert result.status == "unjoined"
    assert result.item is None


# -------------------------------------------------------------------------- privacy


@pytest.mark.parametrize(
    "raw", [None, "", "none", "public", "fleet", "shared", "NONE", " Public ", "  "]
)
def test_classify_privacy_none_values(raw: object) -> None:
    assert classify_privacy(raw, tagged_private=False) == ("none", False)


def test_classify_privacy_local_private() -> None:
    assert classify_privacy("local-private", tagged_private=True) == ("local-private", False)


@pytest.mark.parametrize("raw", ["team-secret", "private", "secret", 5, ["local-private"]])
def test_classify_privacy_other_values_are_unknown(raw: object) -> None:
    klass, _ = classify_privacy(raw, tagged_private=False)
    assert klass == "unknown"


def test_classify_privacy_tag_private_meta_none_takes_stricter_and_flags_mismatch() -> None:
    assert classify_privacy(None, tagged_private=True) == ("local-private", True)


def test_classify_privacy_meta_private_tag_missing_takes_stricter_and_flags_mismatch() -> None:
    assert classify_privacy("local-private", tagged_private=False) == ("local-private", True)


def test_classify_privacy_unknown_beats_private_tag() -> None:
    klass, _ = classify_privacy("team-secret", tagged_private=True)
    assert klass == "unknown"


def test_classify_privacy_unknown_without_tag_flags_mismatch() -> None:
    assert classify_privacy("team-secret", tagged_private=False) == ("unknown", True)


# --------------------------------------------------------------------------- hit_to_id


@pytest.mark.parametrize(
    ("hit", "expected"),
    [
        (REL_ID, REL_ID),
        (f"  {REL_ID}  ", REL_ID),
        ("zeval-0001", "zeval-0001"),
        (f"memories/{REL_ID}.md", REL_ID),
        (f"fleet-memory/memories/{REL_ID}.md", REL_ID),
        (f"qmd://fleet-memory/memories/{REL_ID}.md", REL_ID),
        (f"files/memories/{REL_ID}.md", REL_ID),
        (f"{REL_ID}\tPause cron before disk migration", REL_ID),
    ],
)
def test_hit_to_id_accepts_id_forms(hit: str, expected: str) -> None:
    assert hit_to_id(hit) == expected


@pytest.mark.parametrize(
    "hit",
    [
        "files/example_gen8_cron.md",
        "fleet-memory-legacy/files/example_gen8_cron.md",
        "example_gen8_cron.md",
        "",
        "   ",
        "\t",
        "../../etc/passwd",
        "memories/../x",
        "memories/bad id.md",
        "hello world",
        "qmd://",
    ],
)
def test_hit_to_id_rejects_non_ids(hit: str) -> None:
    assert hit_to_id(hit) is None


# ------------------------------------------------------------------ clearance/tenants


@pytest.mark.parametrize("raw", [None, "", "   ", "none;local-private", "None", ",", "a b"])
def test_parse_clearance_defaults_to_none(raw: str | None) -> None:
    assert parse_clearance(raw) == frozenset({"none"})
    assert parse_clearance(raw) == DEFAULT_CLEARANCE


def test_parse_clearance_explicit_list() -> None:
    assert parse_clearance("none,local-private") == frozenset({"none", "local-private"})
    assert parse_clearance(" none , local-private ") == frozenset({"none", "local-private"})


def test_parse_tenants_default() -> None:
    assert parse_tenants(None) == frozenset({"", "fleet"})
    assert parse_tenants(None) == DEFAULT_TENANTS


def test_parse_tenants_explicit_list_with_empty_item() -> None:
    assert parse_tenants("fleet,other-team") == frozenset({"fleet", "other-team"})
    assert parse_tenants(",fleet") == frozenset({"", "fleet"})
