from __future__ import annotations

import importlib.util
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TextIO, cast

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "approval_metadata_status.py"
SPEC = importlib.util.spec_from_file_location("approval_metadata_status", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

SHA = "25ea0d719e5f4e68baa1c87c5ddfb43e1e3e760b79943f34984d751b65fc1d23"
FINGERPRINT = "a" * 64
SECRET = "CANARY_PRIVATE_QUERY_AND_REASON_MUST_NOT_LEAK"


def _json_line(value: object) -> str:
    return json.dumps(value, separators=(",", ":"))


def test_projection_reads_only_sidecar_and_events_and_never_emits_private_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    allowed = {vault / "query_approval.json", vault / "events.jsonl"}
    (vault / "query_approval.json").write_text(
        json.dumps(
            {
                "approval_sha256": SHA,
                "corpus_fingerprint": FINGERPRINT,
                "target_ids": [SECRET, "second-id"],
                "query_hashes": {"candidate": SECRET},
                "reason": SECRET,
            }
        ),
        encoding="utf-8",
    )
    (vault / "events.jsonl").write_text(
        "\n".join(
            [
                _json_line(
                    {
                        "kind": "human_decision",
                        "actor": "human",
                        "timestamp": "2026-10-21T05:10:29.554476Z",
                        "details": {
                            "operation": "approve_query_snapshot",
                            "decision": "approve",
                            "target_hash": SHA,
                            "input_hash": FINGERPRINT,
                            "reason": SECRET,
                        },
                    }
                ),
                _json_line(
                    {
                        "kind": "human_decision",
                        "actor": "human",
                        "timestamp": SECRET,
                        "details": {"operation": "other", "content": SECRET},
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )
    (vault / "pilot_queries_to_review.md").write_text(SECRET, encoding="utf-8")
    (vault / "memories").mkdir()
    (vault / "memories" / "private.md").write_text(SECRET, encoding="utf-8")

    original_open = cast(
        Callable[[Path, str, int, str | None, str | None, str | None], TextIO], Path.open
    )
    opened: list[Path] = []

    def guarded_open(
        path: Path,
        mode: str = "r",
        buffering: int = -1,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> TextIO:
        resolved = path.resolve()
        opened.append(resolved)
        if resolved not in allowed:
            raise AssertionError("unexpected file read")
        return original_open(path, mode, buffering, encoding, errors, newline)

    monkeypatch.setattr(Path, "open", guarded_open)
    result = MODULE.inspect(vault, SHA)
    assert set(opened) == {path.resolve() for path in allowed}
    assert result == {
        "code": "OK",
        "approval_sidecar_matches_sha": True,
        "matching_approval_event_count": 1,
        "target_count": 2,
        "event_dates": ["2026-10-21"],
        "current_query_snapshot_revalidated": False,
    }
    assert SECRET not in json.dumps(result)

    assert MODULE.main(["--vault", str(vault), "--approval-sha256", SHA]) == 0
    output = capsys.readouterr().out
    assert SECRET not in output
    assert "reason" not in output
    assert "content" not in output
    assert SHA not in output


@pytest.mark.parametrize(
    ("sidecar_sha", "expected_count", "expected_sidecar_match"),
    [(SHA, 1, True), ("b" * 64, 1, False)],
)
def test_counts_only_matching_hash_and_fingerprint(
    tmp_path: Path, sidecar_sha: str, expected_count: int, expected_sidecar_match: bool
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "query_approval.json").write_text(
        json.dumps(
            {"approval_sha256": sidecar_sha, "corpus_fingerprint": FINGERPRINT, "target_ids": []}
        ),
        encoding="utf-8",
    )
    base = {
        "kind": "human_decision",
        "actor": "human",
        "timestamp": "2026-10-21T00:49:34Z",
        "details": {
            "operation": "approve_query_snapshot",
            "decision": "approve",
            "target_hash": SHA,
            "input_hash": FINGERPRINT,
        },
    }
    wrong_fingerprint = json.loads(_json_line(base))
    wrong_fingerprint["details"]["input_hash"] = "c" * 64
    (vault / "events.jsonl").write_text(
        _json_line(base) + "\n" + _json_line(wrong_fingerprint), encoding="utf-8"
    )
    result = MODULE.inspect(vault, SHA)
    assert result["matching_approval_event_count"] == expected_count
    assert result["approval_sidecar_matches_sha"] is expected_sidecar_match
    assert result["current_query_snapshot_revalidated"] is False


def test_errors_use_fixed_codes_without_exception_text(tmp_path: Path) -> None:
    result = MODULE.inspect(tmp_path, SHA)
    assert result == {"code": "APPROVAL_SIDECAR_MISSING"}
    assert MODULE.inspect(tmp_path, "secret-invalid-value") == {"code": "INVALID_SHA256"}
