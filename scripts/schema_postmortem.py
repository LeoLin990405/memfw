#!/usr/bin/env python3
"""Report payload-free schema diagnostics for one selected Day 9 JSONL record."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import cast

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import pilot  # noqa: E402

from memfw.agents import JsonObject  # noqa: E402
from memfw.agents.mlx_live import LocalMLXJudge, LocalMLXQueryError  # noqa: E402

TARGET_ID = "import-47f01ead0e0adb498caea85ea5b12071"
DEFAULT_INPUT = Path.home() / "Documents" / "memfw-data" / "logs" / "day9-query-diagnostics.jsonl"
MAX_REPORTED_ROWS = 100


def _type_name(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (int, float)):
        return "number"
    return "other"


def _issue(
    path: str, expected: str, actual: object, array_length: int | None = None
) -> dict[str, object]:
    item: dict[str, object] = {
        "path": path,
        "expected_type": expected,
        "actual_type": _type_name(actual),
    }
    if array_length is not None:
        item["array_length"] = array_length
    return item


def _structural_issues(value: object, expected_id: str) -> list[dict[str, object]]:
    issues: list[dict[str, object]] = []
    if not isinstance(value, dict):
        return [_issue("$", "object", value)]
    rows = value.get("queries")
    if not isinstance(rows, list):
        return [_issue("$.queries", "array", rows)]
    issues.append(_issue("$.queries", "array", rows, len(rows)))
    if not rows:
        issues.append({"path": "$.queries", "error": "empty_array", "array_length": 0})
        return issues
    if len(rows) != 1:
        issues.append(
            {
                "path": "$.queries",
                "error": "unexpected_array_length",
                "expected_length": 1,
                "array_length": len(rows),
            }
        )
    if len(rows) > MAX_REPORTED_ROWS:
        issues.append(
            {"path": "$.queries", "error": "row_limit_exceeded", "array_length": len(rows)}
        )
        return issues
    for index, row in enumerate(rows):
        row_path = f"$.queries[{index}]"
        if not isinstance(row, dict):
            issues.append(_issue(row_path, "object", row))
            continue
        target_id = row.get("target_id")
        query = row.get("query")
        if isinstance(target_id, str):
            issues.append(
                {
                    "path": f"{row_path}.target_id",
                    "redacted_value": f"<redacted len={len(target_id)}>",
                }
            )
        if not isinstance(target_id, str):
            issues.append(_issue(f"{row_path}.target_id", "string", target_id))
        elif target_id != expected_id:
            issues.append(
                {
                    "path": f"{row_path}.target_id",
                    "error": "target_mismatch",
                    "actual_type": "string",
                }
            )
        if not isinstance(query, str):
            issues.append(_issue(f"{row_path}.query", "string", query))
        else:
            issues.append(
                {
                    "path": f"{row_path}.query",
                    "redacted_value": f"<redacted len={len(query)}>",
                }
            )
            if not query.strip():
                issues.append(
                    {
                        "path": f"{row_path}.query",
                        "error": "blank_string",
                        "actual_type": "string",
                    }
                )
        # Unknown keys are deliberately counted elsewhere and never reflected in paths.
    return issues


def _current_validation(raw: str, expected_id: str) -> tuple[dict[str, object], object | None]:
    result: dict[str, object] = {
        "json_parseable": False,
        "valid": False,
        "error": None,
        "request_count": 1,
    }
    try:
        parsed = LocalMLXJudge._parse_response(raw, "query")
        result["json_parseable"] = True
        if not isinstance(parsed, dict):
            raise LocalMLXQueryError("invalid_schema")
        try:
            LocalMLXJudge._validate_queries(cast(JsonObject, parsed), {expected_id}, 1)
        except LocalMLXQueryError as error:
            result["error"] = error.code
        else:
            result["valid"] = True
        return result, parsed
    except LocalMLXQueryError as error:
        result["error"] = error.code
    except Exception:
        result["error"] = "validation_error"
    return result, None


def _legacy_validation(raw: str, expected_id: str) -> dict[str, object]:
    try:
        error_code = pilot._legacy_query_error(raw, expected_id)
    except Exception:
        return {"json_parseable": False, "valid": False, "error": "validation_error"}
    return {
        "json_parseable": error_code != "invalid_json",
        "valid": error_code is None,
        "error": error_code,
    }


def _inspect_raw(raw_value: object, expected_id: str) -> dict[str, object]:
    if not isinstance(raw_value, str):
        return {
            "raw_output": {
                "json_parseable": False,
                "valid": False,
                "error": "raw_output_not_string",
            },
            "structure": [_issue("$.raw_output", "string", raw_value)],
        }
    parsed: object = None
    current, normalized = _current_validation(raw_value, expected_id)
    try:
        parsed = json.loads(raw_value)
        parseable = True
        issues = _structural_issues(parsed, expected_id)
        unknown_fields = 0
        if isinstance(parsed, dict) and isinstance(parsed.get("queries"), list):
            for row in parsed["queries"]:
                if isinstance(row, dict):
                    unknown_fields += sum(key not in {"target_id", "query"} for key in row)
    except (json.JSONDecodeError, TypeError, RecursionError, ValueError):
        parseable = False
        issues = [{"path": "$", "error": "invalid_json"}]
        unknown_fields = 0
    if not parseable and normalized is not None:
        issues = _structural_issues(normalized, expected_id)
    query_count = (
        len(parsed["queries"])
        if isinstance(parsed, dict) and isinstance(parsed.get("queries"), list)
        else None
    )
    return {
        "json_parseable": parseable,
        "raw_output_redacted": f"<redacted len={len(raw_value)}>",
        "legacy": _legacy_validation(raw_value, expected_id),
        "current": current,
        "structure": issues,
        "unknown_field_count": unknown_fields,
        "query_array_length": query_count,
    }


def inspect_file(path: Path, expected_id: str = TARGET_ID) -> dict[str, object]:
    matches: list[dict[str, object]] = []
    malformed_container_lines = 0
    scanned_record_count = 0
    try:
        stream = path.open("r", encoding="utf-8")
    except OSError:
        return {"status": "input_unavailable", "matched_count": 0, "matches": []}
    try:
        with stream:
            for line in stream:
                try:
                    record = json.loads(line)
                except (json.JSONDecodeError, TypeError, RecursionError, ValueError):
                    malformed_container_lines += 1
                    continue
                scanned_record_count += 1
                if not isinstance(record, dict) or record.get("target_id") != expected_id:
                    continue
                raw_value = record.get("raw_output")
                matches.append(_inspect_raw(raw_value, expected_id))
    except (OSError, UnicodeDecodeError):
        return {"status": "input_read_error", "matched_count": 0, "matches": []}
    status = "no_match" if not matches else "matched"
    if len(matches) > 1:
        status = "multiple_matches"
    return {
        "status": status,
        "matched_count": len(matches),
        "scanned_record_count": scanned_record_count,
        "malformed_container_line_count": malformed_container_lines,
        "matches": matches,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    args = parser.parse_args()
    try:
        report = inspect_file(args.input)
        print(json.dumps(report, ensure_ascii=True, sort_keys=True))
    except Exception:
        # Never expose exception text because it can embed source data.
        print('{"status":"diagnostic_error","matched_count":0,"matches":[]}')
        return 2
    return 1 if report["status"] != "matched" else 0


if __name__ == "__main__":
    raise SystemExit(main())
