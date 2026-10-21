#!/usr/bin/env python3
"""Day 8 personal-data pilot controls plus a five-record fake-only rehearsal."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import math
import os
import stat
import sys
import tempfile
import time
from collections import Counter
from collections.abc import Sequence
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPO_ROOT / "src"), str(REPO_ROOT / "scripts")]

DATA_ROOT = Path.home() / "Documents" / "memfw-data"
PILOT_ROOT = DATA_ROOT / "pilot" / "fleet-memory"
MODEL_ROOT = DATA_ROOT / "models"
PRIVATE_ROOT = DATA_ROOT / "pilot-vault"
CANDIDATE_QUERIES = "pilot_queries_to_review.md"
USER_QUERIES = "pilot_queries_user.md"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _import_summary(result: Any) -> dict[str, object]:
    """Return aggregate importer statistics without source text or tag values."""
    return {
        "supported_files": result.supported_files,
        "imported_records": result.imported_records,
        "skipped_hidden": result.skipped_hidden,
        "skipped_read_error": result.skipped_read_error,
        "skipped_non_utf8": result.skipped_non_utf8,
        "skipped_empty": result.skipped_empty,
        "skipped_invalid_frontmatter": result.skipped_invalid_frontmatter,
        "mtime_fallback_records": result.mtime_fallback_records,
        "timestamp_metadata_records": result.timestamp_metadata_records,
        "timestamp_date_records": result.timestamp_date_records,
        "timestamp_source_counts": dict(
            sorted(Counter(record.timestamp_source for record in result.records).items())
        ),
        "source_format_counts": dict(
            sorted(Counter(record.source_format for record in result.records).items())
        ),
        "resolved_reference_count": sum(len(record.references) for record in result.records),
        "records_with_tags": sum(bool(record.tags) for record in result.records),
        "tag_assignments": sum(len(record.tags) for record in result.records),
        "tag_count_per_record": dict(
            sorted(Counter(len(record.tags) for record in result.records).items())
        ),
        "canonical_bytes": result.canonical_bytes,
        "unresolved_reference_count": len(result.unresolved_references),
        "diagnostic_count": len(result.diagnostics),
        "earliest_utc": result.earliest.isoformat() if result.earliest else None,
        "latest_utc": result.latest.isoformat() if result.latest else None,
    }


def _rank(values: Sequence[float]) -> list[float]:
    ordered = sorted(enumerate(values), key=lambda item: item[1])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(ordered):
        j = i + 1
        while j < len(ordered) and ordered[j][1] == ordered[i][1]:
            j += 1
        average = (i + 1 + j) / 2
        for index, _ in ordered[i:j]:
            ranks[index] = average
        i = j
    return ranks


def spearman(left: Sequence[float], right: Sequence[float]) -> float | None:
    if len(left) != len(right) or len(left) < 2:
        return None
    x, y = _rank(left), _rank(right)
    mx, my = sum(x) / len(x), sum(y) / len(y)
    dx, dy = [v - mx for v in x], [v - my for v in y]
    denom = math.sqrt(sum(v * v for v in dx) * sum(v * v for v in dy))
    return None if denom == 0 else sum(a * b for a, b in zip(dx, dy, strict=True)) / denom


def _module_available(name: str) -> bool:
    from importlib.util import find_spec

    try:
        return find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def check_inputs() -> dict[str, object]:
    model_ready = False
    compatible_models = []
    if MODEL_ROOT.is_dir():
        for candidate in sorted(path for path in MODEL_ROOT.iterdir() if path.is_dir()):
            try:
                config = json.loads((candidate / "config.json").read_text(encoding="utf-8"))
                quantization = config.get("quantization")
                tokenizer_ready = any(
                    (candidate / name).is_file()
                    for name in ("tokenizer.json", "tokenizer.model", "tokenizer_config.json")
                )
                if (
                    isinstance(quantization, dict)
                    and quantization.get("bits") == 4
                    and tuple(candidate.glob("*.safetensors"))
                    and tokenizer_ready
                ):
                    compatible_models.append(candidate.name)
            except (OSError, ValueError, TypeError):
                continue
    model_ready = bool(compatible_models)
    paths = {"data": PILOT_ROOT, "models": MODEL_ROOT}
    mlx_ready = _module_available("mlx_lm") and _module_available("mlx")
    source_validation: dict[str, object] = {"status": "not_checked"}
    if PILOT_ROOT.is_dir():
        try:
            from pilot_import import import_pilot

            imported = import_pilot(PILOT_ROOT)
            source_validation = {
                "status": "valid",
                **_import_summary(imported),
            }
        except Exception as exc:
            # Importer errors are constrained to safe codes; never serialize source values.
            source_validation = {
                "status": "invalid",
                "error_type": type(exc).__name__,
                "error_code": getattr(exc, "code", None),
            }
    return {
        "status": "ready"
        if (
            PILOT_ROOT.is_dir()
            and source_validation.get("status") == "valid"
            and model_ready
            and mlx_ready
        )
        else "blocked_missing_inputs_or_runtime",
        "paths": {
            key: {"path": str(value), "exists": value.is_dir()} for key, value in paths.items()
        },
        "private_vault": {
            "path": str(PRIVATE_ROOT),
            "exists": PRIVATE_ROOT.is_dir(),
            "created_by_import_command": True,
        },
        "compatible_local_4bit_model_count": len(compatible_models),
        "mlx_available": mlx_ready,
        "source_validation": source_validation,
    }


def _safe_private_root(*, create: bool = False) -> Path:
    configured = DATA_ROOT / "pilot-vault"
    expected = configured.resolve()
    workspace = REPO_ROOT.resolve()
    if expected == workspace or workspace in expected.parents:
        raise SystemExit("Resolved private pilot vault must remain outside the repository")
    if create:
        expected.mkdir(parents=True, exist_ok=True)
    if not expected.is_dir():
        if not create:
            raise SystemExit(f"Private pilot vault is absent; no files written: {expected}")
        expected.mkdir(parents=True, mode=0o700)
    if create:
        os.chmod(expected, 0o700)
        for child in expected.rglob("*"):
            if child.is_symlink():
                continue
            if child.is_dir():
                os.chmod(child, 0o700)
            elif child.is_file():
                os.chmod(child, 0o600)
    return expected


def _secure_private_tree(root: Path) -> None:
    os.chmod(root, 0o700)
    for child in root.rglob("*"):
        if child.is_symlink():
            continue
        if child.is_dir():
            os.chmod(child, 0o700)
        elif child.is_file():
            os.chmod(child, 0o600)


@contextlib.contextmanager
def _private_umask() -> Any:
    previous = os.umask(0o077)
    try:
        yield
    finally:
        os.umask(previous)


def _private_repo(*, create: bool = False) -> Any:
    from memfw.core.repository import Repository

    root = _safe_private_root(create=create)
    repo = Repository(root)
    if create:
        with _private_umask():
            repo.init()
        _secure_private_tree(root)
    return repo


def _private_file(repo: Any, name: str) -> Path:
    root = repo.root.resolve()
    path = repo.root / name
    if path.is_symlink():
        raise SystemExit("Private pilot files cannot be symlinks")
    resolved = path.resolve()
    if resolved.parent != root:
        raise SystemExit("Private pilot file resolves outside the private vault")
    return cast(Path, resolved)


def _write_private_text(path: Path, value: str, *, exclusive: bool = False) -> None:
    flags = os.O_WRONLY | os.O_CREAT | (os.O_EXCL if exclusive else os.O_TRUNC)
    descriptor = os.open(path, flags, 0o600)
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())


def _import_batch(source_root: Path) -> Any:
    from pilot_import import import_pilot

    resolved = source_root.expanduser().resolve()
    permitted = PILOT_ROOT.resolve()
    if resolved != permitted:
        raise SystemExit("Source path must be the configured local pilot directory")
    return import_pilot(resolved)


def _corpus_fingerprint(records: Sequence[Any]) -> str:
    payload = []
    for record in records:
        body = record.body
        payload.append(
            {
                "id": record.record_id,
                "source_id": record.source_id,
                "body_sha256": _sha256(body.encode("utf-8")),
                "created_at": record.created_at.isoformat(),
                "updated_at": record.updated_at.isoformat(),
            }
        )
    return _sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    )


def _add_imported_record(repo: Any, record: Any) -> None:
    payload = record.payload()
    payload.pop("created_at", None)
    payload.pop("updated_at", None)
    repo.clock = lambda value=record.created_at: value
    repo.add(**payload)
    refs = list(dict.fromkeys([record.source_ref, *record.references]))
    repo.clock = lambda value=record.updated_at: value
    repo.update(
        record.record_id, provenance=json.dumps(refs, ensure_ascii=False, separators=(",", ":"))
    )


def _redact_model_reason(proposal: Any) -> Any:
    """Keep only a digest and UTF-8 byte length of model-produced rationale."""
    reason = proposal.reason
    encoded = reason.encode("utf-8")
    safe_reason = f"model_reason_sha256:{_sha256(encoded)};utf8_bytes:{len(encoded)}"
    return proposal.model_copy(update={"reason": safe_reason})


def import_records(source_root: Path = PILOT_ROOT) -> dict[str, object]:
    """Parse and validate the full batch before creating any repository record."""
    result = _import_batch(source_root)
    if not result.records:
        raise SystemExit("The validated source batch has no importable records")
    repo = _private_repo(create=True)
    if repo.list():
        raise SystemExit(
            "Private pilot vault already contains records; refusing a second import batch"
        )
    pending = [record.payload() for record in result.records]
    # The parse-only importer validated every payload before the first write.
    with _private_umask():
        for record, _payload in zip(result.records, pending, strict=True):
            _add_imported_record(repo, record)
    _secure_private_tree(repo.root)
    return {
        "status": "imported",
        "record_count": len(result.records),
        **_import_summary(result),
        "corpus_fingerprint": _corpus_fingerprint(result.records),
    }


def _capture_receipt(repo: Any, receipt: Any) -> None:
    """Append only scalar metadata. Hashes are opaque; content and errors are omitted."""
    metadata = {
        "model": receipt.model_name,
        "quantization": receipt.quantization,
        "prompt_version": receipt.prompt_version,
        "task": receipt.task,
        "status": receipt.status,
        "input_tokens": receipt.input_tokens,
        "output_tokens": receipt.output_tokens,
        "peak_memory_bytes": receipt.peak_memory_bytes,
        "cold_load_seconds": getattr(receipt, "cold_load_seconds", None),
        "cold_peak_memory_bytes": getattr(receipt, "cold_peak_memory_bytes", None),
        "total_seconds": getattr(receipt, "total_seconds", None),
        "input_truncated_bytes": getattr(receipt, "input_truncated_bytes", None),
        "raw_input_hash": getattr(receipt, "raw_input_hash", None),
        "submitted_input_hash": getattr(receipt, "submitted_input_hash", None),
        "output_hash": receipt.output_hash,
    }
    from memfw.core.models import EventKind

    repo.capture_event(
        EventKind.AGENT_PROPOSAL,
        actor="agent:mlx",
        details={
            "memory_id": "audit",
            "operation": "local_model_call",
            "kind": "inference_audit",
            "agent_id": "mlx",
            "signature": receipt.output_hash,
            "target_hash": receipt.input_hash,
            "input_hash": receipt.input_hash,
            "elapsed_seconds": receipt.elapsed_seconds,
            "reason": json.dumps(metadata, sort_keys=True, separators=(",", ":")),
        },
    )


def _load_model_path(model_path: Path) -> Path:
    resolved = model_path.expanduser().resolve()
    root = MODEL_ROOT.resolve()
    if resolved != root and root not in resolved.parents:
        raise SystemExit("Model path must be inside the configured local models directory")
    return resolved


def _local_judge(
    model_path: Path,
    repo: Any,
    *,
    diagnostic_callback: Any = None,
    query_shape_callback: Any = None,
) -> Any:
    from mlx_local_transport import MLXLocalTransport

    from memfw.agents import LocalMLXJudge
    from memfw.agents.mlx_live import LocalInference

    resolved = _load_model_path(model_path)
    if not _module_available("mlx_lm") or not _module_available("mlx"):
        raise SystemExit(
            "Local MLX runtime is unavailable; no query or proposal files were written"
        )
    return LocalMLXJudge(
        inference=cast(LocalInference, MLXLocalTransport(resolved)),
        model_name=resolved.name,
        quantization="4-bit",
        prompt_version="v1",
        telemetry=lambda receipt: _capture_receipt(repo, receipt),
        diagnostic_callback=diagnostic_callback,
        query_shape_callback=query_shape_callback,
    )


def propose_salience(model_path: Path) -> dict[str, object]:
    from memfw.agents.models import canonical_payload_hash

    repo = _private_repo()
    documents = repo.list()
    if not documents:
        raise SystemExit("No imported records found in the private pilot vault")
    if any(
        p.kind == "salience" and p.agent_name == "mlx" for p in repo.list_proposals(status=None)
    ):
        raise SystemExit("MLX salience proposals already exist; refusing an ambiguous second batch")
    judge = _local_judge(model_path, repo)
    proposals = []
    failed_ids = []
    for document in documents:
        try:
            with _private_umask():
                proposal = judge.score_salience([document])[0]
                proposal = _redact_model_reason(proposal)
                repo.record_proposal(proposal)
            proposals.append(proposal)
        except Exception:
            failed_ids.append(document.id)
    _secure_private_tree(repo.root)
    return {
        "status": "pending_human_review" if not failed_ids else "partial_failed",
        "proposal_count": len(proposals),
        "proposal_ids": [proposal.proposal_id for proposal in proposals],
        "failed_record_ids": failed_ids,
        "pending_set_sha256": canonical_payload_hash(
            [{"proposal_id": p.proposal_id, "payload_hash": p.payload_hash} for p in proposals]
        ),
    }


def _query_rows(path: Path) -> list[dict[str, str]]:
    try:
        text = path.read_text(encoding="utf-8")
        marker = "```json\n"
        if text.count(marker) != 1:
            raise ValueError
        raw = text.split(marker, 1)[1].split("\n```", 1)[0]
        value = json.loads(raw)
        if not isinstance(value, list):
            raise ValueError
        output = []
        for item in value:
            if not isinstance(item, dict) or set(item) != {"target_id", "query"}:
                raise ValueError
            target_id, query = item["target_id"], item["query"]
            if target_id == "" and query == "":
                continue
            if (
                not isinstance(target_id, str)
                or not target_id
                or not isinstance(query, str)
                or not query.strip()
            ):
                raise ValueError
            output.append({"target_id": target_id, "query": query.strip()})
        return output
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        raise SystemExit("Private query Markdown has invalid JSON or row fields") from None


class _PilotQueryFailure(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _legacy_query_error(raw: str | None, expected_id: str) -> str | None:
    """Check the strict Day 8 JSON envelope and row binding without recovery parsing."""
    if raw is None:
        return "invalid_schema"
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return "invalid_json"
    rows = value.get("queries") if isinstance(value, dict) else None
    if not isinstance(rows, list) or len(rows) != 1:
        return "invalid_schema"
    row = rows[0]
    if not isinstance(row, dict):
        return "invalid_schema"
    target_id = row.get("target_id")
    query = row.get("query")
    if (
        not isinstance(target_id, str)
        or target_id != expected_id
        or not isinstance(query, str)
        or not query.strip()
    ):
        return "invalid_schema"
    return None


def _write_query_file(path: Path, title: str, queries: Sequence[dict[str, str]]) -> None:
    body = json.dumps(list(queries), ensure_ascii=False, indent=2)
    _write_private_text(path, f"# {title}\n\n```json\n{body}\n```\n")


def _preserve_empty_candidate(repo: Any) -> None:
    candidate = _private_file(repo, CANDIDATE_QUERIES)
    backup = _private_file(repo, "pilot_queries_to_review.day8-empty.md")
    if not candidate.exists():
        return
    try:
        existing_rows = _query_rows(candidate)
    except SystemExit:
        raise SystemExit("Existing candidate query file is invalid; preserving it") from None
    if existing_rows:
        raise SystemExit("Existing candidate query file is nonempty; preserving reviewed work")
    if backup.exists():
        raise SystemExit("Day 8 empty-candidate backup already exists; refusing to overwrite")
    candidate.rename(backup)
    os.chmod(backup, 0o600)


def generate_queries(
    model_path: Path, count: int = 30, *, judge: Any | None = None
) -> dict[str, object]:
    repo = _private_repo()
    documents = repo.list()
    if not documents:
        raise SystemExit("No imported records found in the private pilot vault")
    if count < 1 or count > 30:
        raise SystemExit("Query candidate count must be between 1 and 30")
    candidates_path = _private_file(repo, CANDIDATE_QUERIES)
    user_path = _private_file(repo, USER_QUERIES)
    _preserve_empty_candidate(repo)
    judge = judge or _local_judge(model_path, repo)
    earlier = sorted(documents, key=lambda item: (item.created_at, item.id))[:count]
    generated = []
    failures: list[dict[str, str]] = []
    with _private_umask():
        for document in earlier:
            try:
                rows = judge.suggest_queries([document], count=1)
                if not rows:
                    raise _PilotQueryFailure("empty_output")
                if len(rows) != 1:
                    raise _PilotQueryFailure("invalid_schema")
                row = rows[0]
                if row.get("target_id") != document.id or not row.get("query", "").strip():
                    raise _PilotQueryFailure("invalid_target")
                generated.append(row)
            except Exception as exc:
                known_codes = {
                    "invalid_json",
                    "ambiguous_json",
                    "invalid_schema",
                    "empty_output",
                    "invalid_target",
                    "transport_failure",
                    "invalid_response",
                    "diagnostic_logging_failure",
                }
                supplied_code = getattr(exc, "code", None)
                code = (
                    supplied_code
                    if isinstance(supplied_code, str) and supplied_code in known_codes
                    else "generation_failure"
                )
                failures.append({"target_id": document.id, "reason": code})
        _write_query_file(
            candidates_path, "Model query candidates for private human review", generated
        )
        if not user_path.exists():
            _write_query_file(
                user_path,
                "Add up to 20 user-written retrieval queries; target IDs must exist in this vault",
                [{"target_id": "", "query": ""} for _ in range(20)],
            )
    os.chmod(candidates_path, 0o600)
    if not user_path.exists():
        os.chmod(user_path, 0o600)
    return {
        "status": "awaiting_query_review"
        if len(generated) == len(earlier)
        else "partial_query_generation",
        "candidate_count": len(generated),
        "requested_candidate_count": len(earlier),
        "failures": failures,
        "failed_record_ids": [failure["target_id"] for failure in failures],
        "failure_reasons": [failure["reason"] for failure in failures],
        "failure_count": len(failures),
        "candidate_path": str(candidates_path),
        "user_template_path": str(user_path),
    }


def _preflight_private_write(directory: Path) -> None:
    """Prove the vault can accept a private atomic replacement before loading records/model."""
    descriptor, name = tempfile.mkstemp(prefix=".query-repair-preflight-", dir=directory)
    path = Path(name)
    try:
        os.fchmod(descriptor, 0o600)
        os.close(descriptor)
        descriptor = -1
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        path.unlink(missing_ok=True)


def repair_missing_queries(
    model_path: Path, count: int = 30, *, judge: Any | None = None
) -> dict[str, object]:
    """Fill absent earliest candidate targets while preserving the existing candidate bytes."""
    if count < 1 or count > 30:
        raise SystemExit("Query candidate count must be between 1 and 30")
    private_root = _safe_private_root(create=False)
    _preflight_private_write(private_root)
    repo = _private_repo()
    candidate_path = _private_file(repo, CANDIDATE_QUERIES)
    if not candidate_path.is_file():
        raise SystemExit("Existing candidate query file is required for missing-query repair")
    original = candidate_path.read_bytes()
    existing = _query_rows(candidate_path)
    if candidate_path.read_bytes() != original:
        return {"status": "refused", "reason": "candidate_changed", "candidate_count": 0}
    documents = repo.list()
    known_ids = {document.id for document in documents}
    existing_ids = [row["target_id"] for row in existing]
    if len(set(existing_ids)) != len(existing_ids):
        return {
            "status": "refused",
            "reason": "duplicate_target_id",
            "candidate_count": len(existing),
        }
    if any(target_id not in known_ids for target_id in existing_ids):
        return {
            "status": "refused",
            "reason": "unknown_target_id",
            "candidate_count": len(existing),
        }
    earliest = sorted(documents, key=lambda item: (item.created_at, item.id))[:count]
    expected_ids = {document.id for document in earliest}
    if len(earliest) != count:
        return {
            "status": "incomplete_source",
            "candidate_count": len(existing),
            "requested_count": count,
        }
    if any(target_id not in expected_ids for target_id in existing_ids):
        return {
            "status": "refused",
            "reason": "unexpected_target_id",
            "candidate_count": len(existing),
        }
    existing_set = set(existing_ids)
    missing = [document for document in earliest if document.id not in existing_set]
    if not missing:
        if len(existing) != count:
            return {
                "status": "refused",
                "reason": "unexpected_candidate_count",
                "candidate_count": len(existing),
            }
        return {
            "status": "complete",
            "candidate_count": len(existing),
            "repaired_count": 0,
            "requested_count": len(earliest),
        }
    shape_diagnostics: list[dict[str, object]] = []
    judge = judge or _local_judge(
        model_path,
        repo,
        query_shape_callback=lambda shape: shape_diagnostics.append(dict(shape)),
    )
    additions: list[dict[str, str]] = []
    failures: dict[str, int] = {}
    for document in missing:
        try:
            rows = judge.suggest_queries([document], count=1)
            if len(rows) != 1:
                raise _PilotQueryFailure("invalid_schema")
            row = rows[0]
            if row.get("target_id") != document.id or not row.get("query", "").strip():
                raise _PilotQueryFailure("invalid_target")
            additions.append({"target_id": document.id, "query": row["query"].strip()})
        except Exception as exc:
            code = getattr(exc, "code", None)
            allowed = {
                "invalid_json",
                "ambiguous_json",
                "invalid_schema",
                "empty_output",
                "invalid_target",
                "transport_failure",
                "invalid_response",
                "diagnostic_logging_failure",
            }
            safe_code = code if isinstance(code, str) and code in allowed else "generation_failure"
            failures[safe_code] = failures.get(safe_code, 0) + 1
    if failures or len(additions) != len(missing):
        return {
            "status": "partial_failure",
            "reason_counts": dict(sorted(failures.items())) or {"generation_failure": 1},
            "candidate_count": len(existing),
            "repaired_count": 0,
            "missing_count": len(missing),
            "shape_diagnostics": shape_diagnostics,
        }
    combined = existing + additions
    if len(combined) != count or len({row["target_id"] for row in combined}) != count:
        return {
            "status": "refused",
            "reason": "candidate_set_incomplete",
            "candidate_count": len(existing),
        }
    backup_path = candidate_path.with_name(candidate_path.name + ".repair-backup")
    replacement = candidate_path.with_name(candidate_path.name + ".repair-tmp")
    if backup_path.exists() or backup_path.is_symlink():
        return {"status": "refused", "reason": "backup_exists", "candidate_count": len(existing)}
    if replacement.exists() or replacement.is_symlink():
        return {
            "status": "refused",
            "reason": "temporary_path_exists",
            "candidate_count": len(existing),
        }
    if candidate_path.read_bytes() != original:
        return {
            "status": "refused",
            "reason": "candidate_changed",
            "candidate_count": len(existing),
        }
    backup_fd = os.open(backup_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(backup_fd, "wb") as stream:
            stream.write(original)
    except Exception:
        backup_path.unlink(missing_ok=True)
        raise
    try:
        _write_query_file(replacement, "Model query candidates for private human review", combined)
        os.chmod(replacement, 0o600)
        os.replace(replacement, candidate_path)
    finally:
        replacement.unlink(missing_ok=True)
    return {
        "status": "repaired",
        "candidate_count": len(combined),
        "repaired_count": len(additions),
        "requested_count": len(earliest),
        "shape_diagnostics": shape_diagnostics,
    }


def _approval_payload(repo: Any) -> dict[str, Any]:
    candidate_path = _private_file(repo, CANDIDATE_QUERIES)
    user_path = _private_file(repo, USER_QUERIES)
    if not candidate_path.is_file() or not user_path.is_file():
        raise SystemExit("Both private query Markdown files are required before approval")
    rows = _query_rows(candidate_path) + _query_rows(user_path)
    known = {item.id for item in repo.list()}
    target_ids = sorted({row["target_id"] for row in rows})
    if not rows or any(target_id not in known for target_id in target_ids):
        raise SystemExit("Query files must contain reviewed queries with existing target IDs")
    from pilot_evaluation import corpus_fingerprint

    corpus = corpus_fingerprint(repo.list())
    query_hashes = {
        CANDIDATE_QUERIES: _sha256(candidate_path.read_bytes()),
        USER_QUERIES: _sha256(user_path.read_bytes()),
    }
    binding = {"query_hashes": query_hashes, "corpus_fingerprint": corpus, "target_ids": target_ids}
    binding["approval_sha256"] = _sha256(
        json.dumps(binding, sort_keys=True, separators=(",", ":")).encode()
    )
    return binding


def prepare_query_approval() -> dict[str, object]:
    """Print the local query snapshot binding without recording an approval."""
    repo = _private_repo()
    binding = _approval_payload(repo)
    candidate_path = _private_file(repo, CANDIDATE_QUERIES)
    user_path = _private_file(repo, USER_QUERIES)
    approval_file = _private_file(repo, "query_approval.json")
    approved = False
    if approval_file.is_file():
        try:
            recorded = json.loads(approval_file.read_text())
            approved = recorded.get("approval_sha256") == binding["approval_sha256"]
        except (OSError, ValueError):
            approved = False
    return {
        "status": "approved" if approved else "ready_for_private_human_review_not_approved",
        "candidate_query_path": str(candidate_path),
        "candidate_query_sha256": _sha256(candidate_path.read_bytes()),
        "user_query_path": str(user_path),
        "user_query_sha256": _sha256(user_path.read_bytes()),
        "approval_sha256": binding["approval_sha256"],
        "approval_input_format": f"APPROVE {binding['approval_sha256']}",
        "corpus_fingerprint": binding["corpus_fingerprint"],
        "target_count": len(binding["target_ids"]),
        "query_count": len(_query_rows(repo.root / CANDIDATE_QUERIES))
        + len(_query_rows(repo.root / USER_QUERIES)),
        "approved": approved,
    }


def _append_private_log(path: Path, value: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "a", encoding="utf-8") as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())


def _day9_private_log(path: Path) -> Path:
    expected = (DATA_ROOT / "logs").expanduser().absolute()
    resolved_expected = expected.resolve()
    resolved = path.expanduser().resolve()
    workspace = REPO_ROOT.resolve()
    if resolved_expected == workspace or workspace in resolved_expected.parents:
        raise SystemExit("Day 9 private log directory cannot resolve inside the repository")
    if resolved != resolved_expected and resolved_expected not in resolved.parents:
        raise SystemExit("Day 9 logs must be inside the explicit private memfw-data/logs directory")
    if resolved == resolved_expected:
        raise SystemExit("A private log filename is required inside memfw-data/logs")
    return resolved


def _day9_runner(model_path: Path, diagnostic_log: Path, steps_log: Path) -> dict[str, Any]:
    """Diagnose three legacy responses, safely replace an empty candidate, then generate."""
    diagnostic_log = _day9_private_log(diagnostic_log)
    steps_log = _day9_private_log(steps_log)
    if diagnostic_log == steps_log:
        raise SystemExit("Diagnostic and step logs must be separate files")
    repo = _private_repo()
    documents = repo.list()
    if not documents:
        raise SystemExit("No imported records found in the private pilot vault")
    diagnostic_docs = sorted(documents, key=lambda item: (item.created_at, item.id))[:3]
    if len(diagnostic_docs) < 3:
        raise SystemExit("Day 9 legacy diagnosis requires at least three private records")
    diagnostic_results: list[dict[str, object]] = []
    current_id = ""
    diagnostic_log_failed = False
    preflight_started = time.monotonic()
    _append_private_log(steps_log, "")
    _append_private_log(diagnostic_log, "")
    _append_private_log(
        steps_log,
        json.dumps(
            {
                "step": "private-log-preflight",
                "rc": 0,
                "elapsed_seconds": round(time.monotonic() - preflight_started, 3),
                "utc": datetime.now(UTC).isoformat(),
            },
            sort_keys=True,
        )
        + "\n",
    )
    _append_private_log(
        diagnostic_log,
        json.dumps(
            {"step": "diagnostic-log-preflight", "rc": 0, "utc": datetime.now(UTC).isoformat()},
            sort_keys=True,
        )
        + "\n",
    )

    def capture(raw: str | None, error: BaseException | None) -> None:
        # Raw model text and exception details remain only in the explicitly private 0600 log.
        nonlocal diagnostic_log_failed
        legacy_error_code = _legacy_query_error(raw, current_id)
        record = {
            "target_id": current_id,
            "raw_output": raw,
            "adapter_exception": None
            if error is None
            else {"type": type(error).__name__, "message": str(error)},
            "legacy_exception": None
            if legacy_error_code is None
            else {"type": "LegacyValidationError", "message": legacy_error_code},
        }
        try:
            _append_private_log(diagnostic_log, json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            diagnostic_log_failed = True
            raise
        diagnostic_results.append(
            {
                "target_id": current_id,
                "raw_output_present": raw is not None,
                "legacy_strict_schema": legacy_error_code is None,
                "error_code": legacy_error_code,
            }
        )

    judge = _local_judge(model_path, repo, diagnostic_callback=capture)
    for document in diagnostic_docs:
        current_id = document.id
        started = time.monotonic()
        rc = 0
        try:
            judge.suggest_queries([document], count=1)
        except Exception:
            rc = 1
        _append_private_log(
            steps_log,
            json.dumps(
                {
                    "step": f"legacy-diagnostic:{document.id}",
                    "rc": rc,
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                    "utc": datetime.now(UTC).isoformat(),
                },
                sort_keys=True,
            )
            + "\n",
        )
        if diagnostic_log_failed:
            raise SystemExit("Private diagnostic log write failed; stopping before generation")

    if hasattr(judge, "diagnostic_callback"):
        judge.diagnostic_callback = None
    backup_started = time.monotonic()
    try:
        _preserve_empty_candidate(repo)
    except SystemExit:
        _append_private_log(
            steps_log,
            json.dumps(
                {
                    "step": "preserve-empty-day8-candidate",
                    "rc": 1,
                    "elapsed_seconds": round(time.monotonic() - backup_started, 3),
                    "utc": datetime.now(UTC).isoformat(),
                },
                sort_keys=True,
            )
            + "\n",
        )
        raise
    _append_private_log(
        steps_log,
        json.dumps(
            {
                "step": "preserve-empty-day8-candidate",
                "rc": 0,
                "elapsed_seconds": round(time.monotonic() - backup_started, 3),
                "utc": datetime.now(UTC).isoformat(),
            },
            sort_keys=True,
        )
        + "\n",
    )
    started = time.monotonic()
    generation = generate_queries(model_path, count=30, judge=judge)
    candidate_count = cast(int, generation["candidate_count"])
    generation_rc = 0 if candidate_count else 1
    _append_private_log(
        steps_log,
        json.dumps(
            {
                "step": "generate-queries",
                "rc": generation_rc,
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "utc": datetime.now(UTC).isoformat(),
                "candidate_count": candidate_count,
                "failure_count": generation["failure_count"],
            },
            sort_keys=True,
        )
        + "\n",
    )
    if candidate_count < 20:
        return {
            "status": "below_generation_acceptance_threshold",
            "diagnostics": diagnostic_results,
            "generation": generation,
            "approval_info": None,
            "stopped_before": "approval-info",
            "approved": False,
        }
    started = time.monotonic()
    approval = prepare_query_approval()
    _append_private_log(
        steps_log,
        json.dumps(
            {
                "step": "approval-info",
                "rc": 0,
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "utc": datetime.now(UTC).isoformat(),
            },
            sort_keys=True,
        )
        + "\n",
    )
    return {
        "status": "ready_for_private_human_review_not_approved",
        "diagnostics": diagnostic_results,
        "generation": generation,
        "approval_info": approval,
        "approved": False,
    }


def approve_queries() -> dict[str, object]:
    from memfw.core.models import EventKind

    repo = _private_repo()
    binding = _approval_payload(repo)
    typed = input(f"Review both private query files. Type APPROVE {binding['approval_sha256']}: ")
    if typed.strip() != f"APPROVE {binding['approval_sha256']}":
        raise SystemExit("Query approval not recorded")
    repo.capture_event(
        EventKind.HUMAN_DECISION,
        actor="human",
        details={
            "memory_id": "query-review",
            "operation": "approve_query_snapshot",
            "decision": "approve",
            "target_hash": str(binding["approval_sha256"]),
            "input_hash": str(binding["corpus_fingerprint"]),
            "reason": json.dumps(binding["query_hashes"], sort_keys=True, separators=(",", ":")),
        },
    )
    target = _private_file(repo, "query_approval.json")
    _write_private_text(target, json.dumps(binding, sort_keys=True, indent=2) + "\n")
    _secure_private_tree(repo.root)
    return {
        "status": "approved",
        "approval_sha256": binding["approval_sha256"],
        "query_count": len(_query_rows(repo.root / CANDIDATE_QUERIES))
        + len(_query_rows(repo.root / USER_QUERIES)),
    }


def approve_salience() -> dict[str, object]:
    """Approve current pending salience proposals after digest-bound human review."""
    from memfw.curation.review import decide_proposal

    repo = _private_repo()
    pending = [
        p
        for p in repo.list_proposals(status="pending")
        if p.kind == "salience" and p.agent_name == "mlx"
    ]
    if not pending:
        raise SystemExit("No pending MLX salience proposals to approve")
    pairs = [{"proposal_id": p.proposal_id, "payload_hash": p.payload_hash} for p in pending]
    pairs.sort(key=lambda item: item["proposal_id"])
    review_hash = _sha256(json.dumps(pairs, sort_keys=True, separators=(",", ":")).encode())
    print(
        json.dumps(
            [
                {
                    "proposal_id": p.proposal_id,
                    "target_id": p.target_ids[0],
                    "score": p.output.get("score"),
                }
                for p in pending
            ],
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    typed = input(f"Review the private proposals locally. Type APPROVE {review_hash}: ")
    if typed.strip() != f"APPROVE {review_hash}":
        raise SystemExit("Model score approval not recorded")
    for item in pending:
        decide_proposal(repo, item.proposal_id, "approve", payload_hash=item.payload_hash)
    from pilot_evaluation import corpus_fingerprint

    binding = {
        "proposal_pairs": pairs,
        "approval_sha256": review_hash,
        "corpus_fingerprint": corpus_fingerprint(repo.list()),
    }
    _write_private_text(
        _private_file(repo, "score_approval.json"),
        json.dumps(binding, sort_keys=True, indent=2) + "\n",
    )
    _secure_private_tree(repo.root)
    return {"status": "approved", "proposal_count": len(pending), "approval_sha256": review_hash}


def _call_metrics(repo: Any) -> dict[str, object]:
    from memfw.core.models import EventKind

    calls = []
    for event in repo.events():
        details = event.details
        if details.get("operation") != "local_model_call" or event.kind != EventKind.AGENT_PROPOSAL:
            continue
        try:
            metadata = json.loads(str(details.get("reason", "{}")))
        except (ValueError, TypeError):
            continue
        calls.append(
            {
                "model": metadata.get("model"),
                "quantization": metadata.get("quantization"),
                "prompt_version": metadata.get("prompt_version"),
                "task": metadata.get("task"),
                "status": metadata.get("status"),
                "input_tokens": metadata.get("input_tokens"),
                "output_tokens": metadata.get("output_tokens"),
                "input_truncated_bytes": metadata.get("input_truncated_bytes"),
                "raw_input_hash": metadata.get("raw_input_hash"),
                "submitted_input_hash": metadata.get("submitted_input_hash"),
                "inference_seconds": details.get("elapsed_seconds"),
                "cold_load_seconds": metadata.get("cold_load_seconds"),
                "total_seconds": metadata.get("total_seconds"),
                "inference_peak_memory_bytes": metadata.get("peak_memory_bytes"),
                "cold_peak_memory_bytes": metadata.get("cold_peak_memory_bytes"),
                "output_hash": metadata.get("output_hash"),
            }
        )
    from types import SimpleNamespace

    from pilot_diagnostics import aggregate_real_receipts

    diagnostics_receipts = [
        SimpleNamespace(
            status=item["status"],
            elapsed_seconds=item["inference_seconds"],
            cold_load_seconds=item["cold_load_seconds"],
            peak_memory_bytes=item["inference_peak_memory_bytes"],
            cold_peak_memory_bytes=item["cold_peak_memory_bytes"],
            input_tokens=item["input_tokens"],
            output_tokens=item["output_tokens"],
            total_seconds=item["total_seconds"],
        )
        for item in calls
    ]
    aggregate = aggregate_real_receipts(diagnostics_receipts, measured_live=True)
    successful = [item for item in calls if item["status"] == "completed"]
    for item in calls:
        item.pop("output_hash", None)
    return {
        "observed_calls": calls,
        "completed_call_count": len(successful),
        "failed_call_count": sum(item["status"] != "completed" for item in calls),
        "estimate_1000_serial_calls": aggregate,
        "diagnostics_aggregate": aggregate,
        "cold_peak_scope": "load peak; inference peak is separately reported",
    }


def evaluate() -> dict[str, object]:
    import math

    from pilot_evaluation import ApprovedQuery, corpus_fingerprint, evaluate_queries

    from memfw.core.models import EventKind
    from memfw.stats.keyness import KeynessScorer

    repo = _private_repo()
    binding_path = _private_file(repo, "query_approval.json")
    score_path = _private_file(repo, "score_approval.json")
    if not binding_path.is_file() or not score_path.is_file():
        raise SystemExit("Human approvals for query files and model scores are required")
    current_binding = _approval_payload(repo)
    approved_binding = json.loads(binding_path.read_text(encoding="utf-8"))
    if any(
        current_binding.get(key) != approved_binding.get(key)
        for key in ("query_hashes", "corpus_fingerprint", "target_ids", "approval_sha256")
    ):
        raise SystemExit("Query files or corpus changed after human approval")
    approved_by_human = any(
        event.kind == EventKind.HUMAN_DECISION
        and event.actor == "human"
        and event.details.get("operation") == "approve_query_snapshot"
        and event.details.get("decision") == "approve"
        and event.details.get("target_hash") == approved_binding.get("approval_sha256")
        and event.details.get("input_hash") == approved_binding.get("corpus_fingerprint")
        for event in repo.events()
    )
    if not approved_by_human:
        raise SystemExit("No matching human query approval event was found")
    queries = _query_rows(repo.root / CANDIDATE_QUERIES) + _query_rows(repo.root / USER_QUERIES)
    docs = repo.list()
    scores: dict[str, float] = {}
    proposal_ids: dict[str, str] = {}
    payload_hashes: dict[str, str] = {}
    approved_payloads: dict[str, dict[str, Any]] = {}
    seen_ids: set[str] = set()
    for proposal in repo.list_proposals(status="approved"):
        if proposal.kind != "salience" or proposal.agent_name != "mlx":
            continue
        if len(proposal.target_ids) != 1:
            raise SystemExit("An approved MLX score proposal has invalid target coverage")
        target_id = proposal.target_ids[0]
        if target_id in seen_ids:
            raise SystemExit("Multiple approved MLX salience proposals target one record")
        seen_ids.add(target_id)
        repo.assert_proposal_current(proposal.proposal_id)
        score = proposal.output.get("score")
        if (
            isinstance(score, bool)
            or not isinstance(score, (int, float))
            or not math.isfinite(score)
            or not 0.0 <= score <= 1.0
        ):
            raise SystemExit("An approved MLX salience proposal has an invalid score")
        scores[target_id] = float(score)
        proposal_ids[target_id] = proposal.proposal_id
        payload_hashes[target_id] = proposal.payload_hash
        approved_payloads[target_id] = cast(dict[str, Any], dict(proposal.output))
    if set(scores) != {item.id for item in docs}:
        raise SystemExit("Every corpus record needs a separately human-approved MLX salience score")
    approved_pairs = [
        {"proposal_id": proposal_ids[identifier], "payload_hash": payload_hashes[identifier]}
        for identifier in sorted(proposal_ids)
    ]
    approved_pairs.sort(key=lambda item: item["proposal_id"])
    score_binding = json.loads(score_path.read_text(encoding="utf-8"))
    score_hash = _sha256(json.dumps(approved_pairs, sort_keys=True, separators=(",", ":")).encode())
    if (
        approved_pairs != score_binding.get("proposal_pairs")
        or score_hash != score_binding.get("approval_sha256")
        or score_binding.get("corpus_fingerprint") != corpus_fingerprint(docs)
    ):
        raise SystemExit("Approved score proposals changed after human approval")
    approved_queries = [
        ApprovedQuery(target_id=row["target_id"], query=row["query"]) for row in queries
    ]
    current_fingerprint = corpus_fingerprint(docs)
    metrics = evaluate_queries(
        docs,
        approved_queries,
        now=datetime.now(UTC),
        approved_mlx_scores=scores,
        approved_mlx_proposal_ids=proposal_ids,
        approved_mlx_corpus_fingerprint=str(score_binding["corpus_fingerprint"]),
        approved_mlx_payload_hashes=payload_hashes,
        approved_mlx_payloads=approved_payloads,
        approved_query_binding_hash=str(approved_binding["approval_sha256"]),
        current_query_binding_hash=str(current_binding["approval_sha256"]),
        approved_query_target_ids=approved_binding["target_ids"],
    )
    keyness = KeynessScorer().score_documents({item.id: item.body for item in docs})
    from pilot_diagnostics import rank_disagreement

    diagnostics = rank_disagreement(
        {identifier: result.score for identifier, result in keyness.items()},
        scores,
        sample_size=50,
        top_n=10,
    )
    call_metrics = _call_metrics(repo)
    return {
        "status": "completed_approved_queries_and_scores",
        "corpus_fingerprint": current_fingerprint,
        "spearman_p1_mlx_vs_keyness": diagnostics["spearman"],
        "top10_id_score_pairs": sorted(
            [{"id": identifier, "score": score} for identifier, score in scores.items()],
            key=lambda item: (-cast(float, item["score"]), str(item["id"])),
        )[:10],
        "keyness_mlx_disagreement_diagnostics": {
            "sample_count": diagnostics["sample_count"],
            "common_id_count": diagnostics["common_id_count"],
            "coverage_fraction": diagnostics["coverage_fraction"],
            "top10": diagnostics["largest_disagreements"],
        },
        "budget_results": [asdict(item) for item in metrics.metrics],
        "generated_queries": len(_query_rows(repo.root / CANDIDATE_QUERIES)),
        "user_queries": len(_query_rows(repo.root / USER_QUERIES)),
        "model_call_metrics": call_metrics,
    }


class _FakeInference:
    """Deterministic fixture transport. It never loads MLX or touches the network."""

    def __init__(self) -> None:
        self.calls = 0

    def infer(
        self,
        *,
        model_name: str,
        quantization: str,
        prompt_version: str,
        prompt: str,
        input_json: str,
        temperature: float = 0.0,
        seed: int = 0,
    ) -> Any:
        from types import SimpleNamespace

        started = time.perf_counter()
        data = json.loads(input_json)
        if "long-term usefulness" in prompt:
            index = int(data["title"].rsplit(" ", 1)[-1])
            response = {
                "score": [0.91, 0.64, 0.83, 0.22, 0.77][index - 1],
                "reason": "CANARY_PRIVATE_REASON_MUST_NOT_PERSIST synthetic fixture score",
            }
        elif "retrieval queries" in prompt:
            memories = data["memories"]
            response = {
                "queries": [
                    {
                        "target_id": item["id"],
                        "query": f"unique-fact-alpha{item['title'].rsplit(' ', 1)[-1]}",
                    }
                    for item in memories
                ]
            }
        else:
            response = {"reason": "FAKE synthetic fixture response", "merge_pairs": []}
        self.calls += 1
        return SimpleNamespace(
            text=json.dumps(response),
            elapsed_seconds=time.perf_counter() - started,
            input_tokens=0,
            output_tokens=0,
            peak_memory_bytes=0,
        )


def dry_run() -> dict[str, object]:
    """Exercise the public runner path end to end with five synthetic records."""
    from unittest.mock import patch

    from memfw.agents import LocalMLXJudge

    with tempfile.TemporaryDirectory(prefix="memfw-day8-fake-") as tmp:
        base = Path(tmp)
        source = base / "source"
        source.mkdir()
        for index in range(1, 6):
            text = (
                "---\n"
                f"title: FAKE fixture memory {index}\n"
                f"created_at: 2026-01-0{index}T00:00:00Z\n"
                f"updated_at: 2026-01-0{index}T00:00:00Z\n"
                "---\n"
                f"Synthetic fixture body with unique-fact-alpha{index}. Shared retrieval details.\n"
            )
            (source / f"fixture-{index}.md").write_text(text, encoding="utf-8")
        fake_data_root = base / "memfw-data"
        fake_data_root.mkdir()
        fake_model = fake_data_root / "models" / "FAKE-model"
        fake_model.mkdir(parents=True)
        inference = _FakeInference()
        fake_receipts: list[Any] = []

        def fake_judge(model_path: Path, repo: Any) -> Any:
            return LocalMLXJudge(
                inference=inference,
                model_name="FAKE-only",
                quantization="FAKE-no-quantization",
                prompt_version="v1",
                telemetry=fake_receipts.append,
            )

        def approve(prompt: str) -> str:
            if "query files" in prompt:
                return "APPROVE " + str(_approval_payload(_private_repo())["approval_sha256"])
            repo = _private_repo()
            pairs = [
                {"proposal_id": item.proposal_id, "payload_hash": item.payload_hash}
                for item in repo.list_proposals(status="pending")
                if item.kind == "salience" and item.agent_name == "mlx"
            ]
            pairs.sort(key=lambda item: item["proposal_id"])
            return "APPROVE " + _sha256(
                json.dumps(pairs, sort_keys=True, separators=(",", ":")).encode()
            )

        with (
            patch(__name__ + ".DATA_ROOT", fake_data_root),
            patch(__name__ + ".PILOT_ROOT", source),
            patch(__name__ + ".MODEL_ROOT", fake_data_root / "models"),
            patch(__name__ + ".PRIVATE_ROOT", fake_data_root / "pilot-vault"),
            patch(__name__ + "._local_judge", side_effect=fake_judge),
            patch("builtins.input", side_effect=approve),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            import_result = import_records(source)
            proposal_result = propose_salience(fake_model)
            query_result = generate_queries(fake_model, count=30)
            query_approval = approve_queries()
            score_approval = approve_salience()
            evaluation = evaluate()
            repo = _private_repo()
            evaluation["model_call_metrics"] = _call_metrics(repo)
            if proposal_result["proposal_count"] != 5 or query_result["candidate_count"] != 5:
                raise RuntimeError("synthetic runner did not complete all expected fake records")
            events_text = repo.events_path.read_text(encoding="utf-8")
            proposal_text = "\n".join(
                path.read_text(encoding="utf-8") for path in repo.proposals_dir.glob("*.md")
            )
            if "CANARY_PRIVATE_REASON_MUST_NOT_PERSIST" in events_text or (
                "CANARY_PRIVATE_REASON_MUST_NOT_PERSIST" in proposal_text
            ):
                raise RuntimeError("model rationale canary appeared in private persistence")
            mode_errors = [
                f"{child.relative_to(repo.root)}:{stat.S_IMODE(child.stat().st_mode):04o}"
                for child in repo.root.rglob("*")
                if not child.is_symlink()
                and stat.S_IMODE(child.stat().st_mode) != (0o700 if child.is_dir() else 0o600)
            ]
            if mode_errors or stat.S_IMODE(repo.root.stat().st_mode) != 0o700:
                raise RuntimeError(
                    f"synthetic private vault permissions are too broad: {mode_errors}"
                )
        return {
            "status": "completed_fake_only",
            "data_class": "five generated synthetic Markdown records; no personal content",
            "runner_stages": [
                import_result["status"],
                proposal_result["status"],
                query_result["status"],
                query_approval["status"],
                score_approval["status"],
                evaluation["status"],
            ],
            "spearman_p1_mlx_vs_keyness": evaluation["spearman_p1_mlx_vs_keyness"],
            "top10_id_score_pairs": evaluation["top10_id_score_pairs"],
            "budget_results": evaluation["budget_results"],
            "keyness_mlx_disagreement_diagnostics": evaluation[
                "keyness_mlx_disagreement_diagnostics"
            ],
            "inference_call_count": inference.calls,
            "telemetry_receipts": len(fake_receipts),
            "fake_transport_timing": "not reported as MLX performance",
            "fake_token_counts": None,
            "fake_memory_bytes": None,
            "real_model_call_count": 0,
            "model_reason_canary_persisted": False,
            "real_determinism": "not measured; requires three independent fixed-seed model runs",
        }


def determinism_probe(model_path: Path, memory_id: str) -> dict[str, object]:
    """Run three independent fixed-seed salience calls and return hashes only."""
    from pilot_diagnostics import repeatability_probe

    repo = _private_repo()
    matches = [item for item in repo.list() if item.id == memory_id]
    if not matches:
        raise SystemExit("Requested memory ID is not present in the private vault")

    from mlx_local_transport import MLXLocalTransport

    from memfw.agents import LocalMLXJudge
    from memfw.agents.mlx_live import LocalInference

    resolved = _load_model_path(model_path)
    if not _module_available("mlx_lm") or not _module_available("mlx"):
        raise SystemExit("Local MLX runtime is unavailable")
    transport = cast(LocalInference, MLXLocalTransport(resolved))

    def factory(telemetry: Any) -> Any:
        def capture(receipt: Any) -> None:
            _capture_receipt(repo, receipt)
            telemetry(receipt)

        return LocalMLXJudge(
            inference=transport,
            model_name=resolved.name,
            quantization="4-bit",
            prompt_version="v1",
            telemetry=capture,
        )

    result = repeatability_probe(factory, matches[0], repeats=3)
    return {
        "status": result["status"],
        "repeat_count": 3,
        "valid_repeat_count": result["valid_repeat_count"],
        "raw_output_agreement_count": result["raw_output_agreement_count"],
        "payload_agreement_count": result["payload_agreement_count"],
        "runs": result["runs"],
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check-inputs", help="Report required local directories and MLX availability")
    sub.add_parser(
        "import", help="Validate the entire source batch, then import into the private vault"
    )
    sub.add_parser("dry-run", help="Run the five-record synthetic end-to-end rehearsal")
    propose = sub.add_parser("propose", help="Create pending local MLX salience proposals")
    propose.add_argument("--model", required=True, type=Path)
    query = sub.add_parser(
        "generate-queries", help="Generate private candidate queries for human review"
    )
    query.add_argument("--model", required=True, type=Path)
    query.add_argument("--count", type=int, default=30)
    repair = sub.add_parser(
        "repair-missing-queries", help="Fill only missing earliest candidate query targets"
    )
    repair.add_argument("--model", required=True, type=Path)
    repair.add_argument("--count", type=int, default=30)
    sub.add_parser(
        "approve-queries", help="Bind query approvals to query files, corpus and target IDs"
    )
    sub.add_parser(
        "prepare-query-approval",
        help="Print the query snapshot digest and paths without approving them",
    )
    sub.add_parser(
        "approval-info",
        help="Read query paths and digest without recording a human approval",
    )
    sub.add_parser("approve-scores", help="Approve pending MLX scores by exact proposal set digest")
    sub.add_parser(
        "evaluate", help="Evaluate only the currently approved private query and score snapshots"
    )
    repeat = sub.add_parser(
        "determinism", help="Compare three fixed-seed local generations by hash"
    )
    repeat.add_argument("--model", required=True, type=Path)
    repeat.add_argument("--memory-id", required=True)
    day9 = sub.add_parser(
        "day9-run",
        help=(
            "Run three private legacy-parser diagnostics, query generation, "
            "and read-only approval-info"
        ),
    )
    day9.add_argument("--model", required=True, type=Path)
    day9.add_argument("--diagnostic-log", required=True, type=Path)
    day9.add_argument("--steps-log", required=True, type=Path)
    args = parser.parse_args(argv)
    if args.command == "check-inputs":
        result = check_inputs()
    elif args.command == "import":
        result = import_records()
    elif args.command == "dry-run":
        result = dry_run()
    elif args.command == "propose":
        result = propose_salience(args.model)
    elif args.command == "generate-queries":
        result = generate_queries(args.model, args.count)
    elif args.command == "repair-missing-queries":
        result = repair_missing_queries(args.model, args.count)
    elif args.command in {"prepare-query-approval", "approval-info"}:
        result = prepare_query_approval()
    elif args.command == "approve-queries":
        result = approve_queries()
    elif args.command == "approve-scores":
        result = approve_salience()
    elif args.command == "determinism":
        result = determinism_probe(args.model, args.memory_id)
    elif args.command == "day9-run":
        result = _day9_runner(args.model, args.diagnostic_log, args.steps_log)
    else:
        result = evaluate()
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    day9_result = cast(dict[str, Any], result)
    if args.command == "generate-queries" and result["candidate_count"] == 0:
        return 1
    if args.command == "day9-run" and day9_result["generation"]["candidate_count"] < 20:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
