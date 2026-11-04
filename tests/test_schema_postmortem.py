from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import cast

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from schema_postmortem import TARGET_ID, inspect_file


def write_records(path: Path, rows: list[object]) -> None:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def first_match(path: Path) -> dict[str, object]:
    report = inspect_file(path)
    matches = cast(list[object], report["matches"])
    return cast(dict[str, object], matches[0])


def mapping(value: object) -> dict[str, object]:
    return cast(dict[str, object], value)


def items(value: object) -> list[object]:
    return cast(list[object], value)


def test_inspects_only_target_and_never_emits_source_strings(tmp_path: Path) -> None:
    target_raw = json.dumps(
        {
            "queries": [
                {
                    "target_id": TARGET_ID,
                    "query": "CANARY_QUERY_SECRET",
                    "PRIVATE_KEY_NAME": "CANARY_UNKNOWN_VALUE",
                }
            ]
        }
    )
    path = tmp_path / "diagnostics.jsonl"
    write_records(
        path,
        [
            {"target_id": "unrelated-secret-id", "raw_output": "CANARY_NONMATCH_RAW"},
            {"target_id": TARGET_ID, "raw_output": target_raw},
        ],
    )

    report = inspect_file(path)
    encoded = json.dumps(report)

    assert report["matched_count"] == 1
    assert report["matches"][0]["unknown_field_count"] == 1  # type: ignore[index]
    assert report["matches"][0]["raw_output_redacted"].startswith("<redacted len=")  # type: ignore[index]
    assert any(
        item.get("redacted_value") == f"<redacted len={len(TARGET_ID)}>"
        for item in report["matches"][0]["structure"]  # type: ignore[index]
    )
    assert "CANARY" not in encoded
    assert "PRIVATE_KEY_NAME" not in encoded
    assert TARGET_ID not in encoded


def test_cli_output_keeps_matched_source_strings_redacted(tmp_path: Path) -> None:
    raw = json.dumps({"queries": [{"target_id": TARGET_ID, "query": "CANARY_CLI_PRIVATE_VALUE"}]})
    path = tmp_path / "cli-canary.jsonl"
    write_records(path, [{"target_id": TARGET_ID, "raw_output": raw}])

    result = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve().parents[1] / "scripts" / "schema_postmortem.py"),
            "--input",
            str(path),
        ],
        capture_output=True,
        check=False,
        text=True,
    )
    assert result.returncode == 0
    assert "CANARY_CLI_PRIVATE_VALUE" not in result.stdout
    assert TARGET_ID not in result.stdout
    assert "<redacted len=" in result.stdout
    assert result.stderr == ""


def test_fenced_valid_json_shows_legacy_current_difference(tmp_path: Path) -> None:
    payload = json.dumps({"queries": [{"target_id": TARGET_ID, "query": "safe fixture"}]})
    raw = f"```json\n{payload}\n```"
    path = tmp_path / "fenced.jsonl"
    write_records(path, [{"target_id": TARGET_ID, "raw_output": raw}])

    match = first_match(path)
    assert match["json_parseable"] is False
    assert mapping(match["legacy"])["valid"] is False
    assert mapping(match["current"])["valid"] is True


def test_wrong_target_is_visible_as_structural_mismatch_and_current_behavior(
    tmp_path: Path,
) -> None:
    raw = json.dumps({"queries": [{"target_id": "PRIVATE_WRONG_ID", "query": "safe fixture"}]})
    path = tmp_path / "wrong-target.jsonl"
    write_records(path, [{"target_id": TARGET_ID, "raw_output": raw}])

    match = first_match(path)
    assert mapping(match["legacy"])["valid"] is False
    # Current single-target validation accepts any string target_id.
    # Structural diagnostics still report a mismatch.
    assert mapping(match["current"])["valid"] is True
    assert any(
        mapping(item).get("error") == "target_mismatch" for item in items(match["structure"])
    )
    assert "PRIVATE_WRONG_ID" not in json.dumps(match)


def test_empty_nonstring_and_malformed_outputs_are_sanitized(tmp_path: Path) -> None:
    path = tmp_path / "invalid.jsonl"
    write_records(
        path,
        [
            {"target_id": TARGET_ID, "raw_output": ""},
            {"target_id": TARGET_ID, "raw_output": {"PRIVATE": "CANARY"}},
            {"target_id": TARGET_ID, "raw_output": "CANARY_MALFORMED_TEXT"},
            {"target_id": TARGET_ID, "raw_output": json.dumps({"queries": []})},
            {
                "target_id": TARGET_ID,
                "raw_output": json.dumps({"queries": [{"target_id": TARGET_ID, "query": ""}]}),
            },
        ],
    )

    report = inspect_file(path)
    encoded = json.dumps(report)
    assert report["matched_count"] == 5
    assert "CANARY" not in encoded
    assert "PRIVATE" not in encoded
    matches = cast(list[object], report["matches"])
    nonstring_match = cast(dict[str, object], matches[1])
    nonstring_error = cast(dict[str, object], nonstring_match["raw_output"])
    assert nonstring_error["error"] == "raw_output_not_string"


def test_overproduction_is_reported_without_row_values(tmp_path: Path) -> None:
    raw = json.dumps(
        {"queries": [{"target_id": TARGET_ID, "query": f"safe-{index}"} for index in range(31)]}
    )
    path = tmp_path / "overproduced.jsonl"
    write_records(path, [{"target_id": TARGET_ID, "raw_output": raw}])

    match = first_match(path)
    assert mapping(match["current"])["valid"] is True
    assert mapping(match["legacy"])["valid"] is False
    assert mapping(match["current"])["request_count"] == 1
    assert mapping(items(match["structure"])[0])["array_length"] == 31


def test_fenced_structure_uses_current_normalized_json(tmp_path: Path) -> None:
    payload = json.dumps({"queries": [{"target_id": TARGET_ID, "query": ""}]})
    raw = f"```json\n{payload}\n```"
    path = tmp_path / "fenced-invalid.jsonl"
    write_records(path, [{"target_id": TARGET_ID, "raw_output": raw}])

    match = first_match(path)
    assert match["json_parseable"] is False
    assert any(mapping(item).get("error") == "blank_string" for item in items(match["structure"]))


def test_non_utf8_input_returns_sanitized_status(tmp_path: Path) -> None:
    path = tmp_path / "invalid-utf8.jsonl"
    path.write_bytes(b'{"target_id":"' + TARGET_ID.encode() + b'","raw_output":"\xff"}\n')

    report = inspect_file(path)
    assert report == {"status": "input_read_error", "matched_count": 0, "matches": []}


def test_no_match_and_multiple_matches_have_explicit_safe_status(tmp_path: Path) -> None:
    path = tmp_path / "counts.jsonl"
    write_records(path, [{"target_id": "other", "raw_output": "secret"}])
    empty_match = inspect_file(path)
    assert empty_match["status"] == "no_match"
    assert empty_match["scanned_record_count"] == 1

    result = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve().parents[1] / "scripts" / "schema_postmortem.py"),
            "--input",
            str(path),
        ],
        capture_output=True,
        check=False,
        text=True,
    )
    assert result.returncode == 1
    assert '"status": "no_match"' in result.stdout
    assert result.stderr == ""

    write_records(path, [{"target_id": TARGET_ID, "raw_output": "{}"}] * 2)
    report = inspect_file(path)
    assert report["status"] == "multiple_matches"
    assert report["matched_count"] == 2
