#!/usr/bin/env python3
"""Read query approval metadata without opening query or memory content."""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Never

DEFAULT_VAULT = Path.home() / "Documents" / "memfw-data" / "pilot-vault"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$")


def _event_date(value: Any) -> str | None:
    """Return only a validated calendar date; never echo arbitrary event text."""
    if not isinstance(value, str) or not DATE_RE.fullmatch(value):
        return None
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError:
        return None
    return parsed.date().isoformat()


def _read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as stream:
        return json.load(stream)


def _direct_file(vault: Path, name: str) -> Path:
    root = vault.resolve()
    path = vault / name
    if path.is_symlink() or path.resolve().parent != root:
        raise OSError("unsafe metadata path")
    return path


def inspect(vault: Path, expected_sha: str) -> dict[str, object]:
    """Inspect only the approval sidecar and event log's approval metadata."""
    if not SHA256_RE.fullmatch(expected_sha):
        return {"code": "INVALID_SHA256"}

    try:
        approval_path = _direct_file(vault, "query_approval.json")
        events_path = _direct_file(vault, "events.jsonl")
    except OSError:
        return {"code": "UNSAFE_METADATA_PATH"}
    try:
        record = _read_json(approval_path)
    except FileNotFoundError:
        return {"code": "APPROVAL_SIDECAR_MISSING"}
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {"code": "APPROVAL_SIDECAR_INVALID"}
    if not isinstance(record, dict):
        return {"code": "APPROVAL_SIDECAR_INVALID"}

    sidecar_matches = record.get("approval_sha256") == expected_sha
    fingerprint = record.get("corpus_fingerprint")
    target_ids = record.get("target_ids")
    target_count = len(target_ids) if isinstance(target_ids, list) else 0
    event_count = 0
    event_dates: set[str] = set()
    try:
        with events_path.open("r", encoding="utf-8") as stream:
            for line in stream:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict) or event.get("kind") != "human_decision":
                    continue
                details = event.get("details")
                if not isinstance(details, dict):
                    continue
                if (
                    details.get("operation") != "approve_query_snapshot"
                    or event.get("actor") != "human"
                    or details.get("decision") != "approve"
                ):
                    continue
                if details.get("target_hash") != expected_sha:
                    continue
                if not isinstance(fingerprint, str) or details.get("input_hash") != fingerprint:
                    continue
                event_count += 1
                date = _event_date(event.get("timestamp"))
                if date is not None:
                    event_dates.add(date)
    except FileNotFoundError:
        return {"code": "EVENT_LOG_MISSING"}
    except (OSError, UnicodeError):
        return {"code": "EVENT_LOG_INVALID"}

    return {
        "code": "OK",
        "approval_sidecar_matches_sha": sidecar_matches,
        "matching_approval_event_count": event_count,
        "target_count": target_count,
        "event_dates": sorted(event_dates),
        "current_query_snapshot_revalidated": False,
    }


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:  # noqa: ARG002 - do not echo raw arguments
        raise ValueError("INVALID_ARGUMENTS")


def main(argv: list[str] | None = None) -> int:
    parser = _Parser(add_help=False)
    parser.add_argument("--vault", type=Path, default=DEFAULT_VAULT)
    parser.add_argument("--approval-sha256", required=True)
    try:
        args = parser.parse_args(argv)
        result = inspect(args.vault, args.approval_sha256)
    except ValueError:
        result = {"code": "INVALID_ARGUMENTS"}
    except Exception:
        # Do not expose exception text or filesystem paths.
        result = {"code": "INTERNAL_ERROR"}
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("code") == "OK" else 1


if __name__ == "__main__":
    sys.exit(main())
