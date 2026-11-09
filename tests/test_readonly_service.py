"""Handler-level and function-level tests for ``memfw-http/2`` (contract v2).

The four v1 tests (current/tenant filtering, superseded exclusion, related
items, staging never touching the vault) are kept in intent and updated to the
v2 semantics by ``test_tenant_filter_returns_a_full_limit``,
``test_include_history_and_superseded_documents``/``test_as_of_keeps_a_document_superseded_after_the_instant``,
``test_related_edges_mode_and_no_write``/``test_related_falls_back_to_fts`` and
``test_stage_writes_the_canonical_candidate``/``test_stage_never_touches_the_vault``;
the staged path is now the relative ``_candidates/remember/conductor-<id>.md``.

The suite finishes with the canonical sample set under
``tests/fixtures/contract_v2/``: every pair is canonical JSON (``json.dumps``
with ``ensure_ascii=False, indent=2, sort_keys=True`` plus a trailing newline)
and is replayed against a deterministic seed vault.

Regenerate the samples after an intentional contract change::

    MEMFW_UPDATE_CONTRACT_FIXTURES=1 python -m pytest
        tests/test_readonly_service.py -k contract_fixtures
"""

from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import socket
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import yaml

import scripts.memfw_readonly_service as service
from memfw.capture.review import list_staged_candidates
from memfw.core.models import MemoryDocument
from memfw.core.repository import Repository
from memfw.index import MemoryIndex
from memfw.related import RelatedIndex
from memfw.storage.markdown import MarkdownStorageMixin
from scripts.memfw_readonly_service import (
    CONTRACT,
    Handler,
    ServiceError,
    related_vault,
    search_vault,
    stage_candidate,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "contract_v2"
READ_TOKEN = "read-token"
WRITE_TOKEN = "write-token"
BASE = datetime(2026, 11, 6, tzinfo=UTC)


# The suite-wide socket guard in conftest.py would refuse the loopback HTTP client.
@pytest.fixture(autouse=True)
def disable_network() -> None:
    """These tests talk to a loopback server on purpose."""


def _memory(
    memory_id: str,
    title: str,
    body: str,
    *,
    tenant: str = "",
    source: str = "manual",
    tags: tuple[str, ...] = (),
    related: tuple[str, ...] = (),
    valid_from: datetime | None = None,
    valid_to: datetime | None = None,
    superseded_by: str | None = None,
) -> MemoryDocument:
    return MemoryDocument(
        id=memory_id,
        title=title,
        body=body,
        tenant=tenant,
        source=source,
        tags=list(tags),
        related=list(related),
        valid_from=valid_from,
        valid_to=valid_to,
        superseded_by=superseded_by,
        created_at=BASE,
        updated_at=BASE,
    )


SEED_DOCUMENTS = [
    _memory(
        "mem-alpha",
        "Alpha corpus note",
        "alpha corpus keyword",
        tenant="tenant-a",
        tags=("shared", "alpha"),
        related=("mem-beta",),
    ),
    _memory(
        "mem-beta",
        "Beta corpus note",
        "alpha corpus keyword companion",
        tenant="tenant-a",
        source="notes",
        tags=("shared",),
    ),
    _memory(
        "mem-gamma",
        "Gamma corpus note",
        "gamma corpus keyword other",
        tenant="tenant-b",
    ),
    _memory(
        "mem-old",
        "Alpha early corpus note",
        "alpha corpus keyword archive",
        tenant="tenant-a",
        valid_from=datetime(2026, 11, 6, 0, 10, tzinfo=UTC),
        valid_to=datetime(2026, 11, 6, 0, 20, tzinfo=UTC),
        superseded_by="mem-alpha",
    ),
]


def seed_vault(root: Path, documents: list[MemoryDocument] | None = None) -> Path:
    """Write a deterministic vault: fixed timestamps, rendered canonical Markdown."""
    repository = Repository(root)
    repository.init()
    (root / "memories").mkdir(exist_ok=True)
    for document in documents if documents is not None else SEED_DOCUMENTS:
        path = root / "memories" / f"{document.id}.md"
        path.write_bytes(MarkdownStorageMixin.render(document).encode("utf-8"))
    MemoryIndex(repository.index_path).rebuild(repository)
    return root


def build_related(root: Path) -> Path:
    path = root / "related.sqlite3"
    RelatedIndex(path).rebuild_memory(Repository(root))
    return path


@pytest.fixture
def httpd(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MEMFW_READONLY_TOKEN", READ_TOKEN)
    monkeypatch.setenv("MEMFW_WRITE_TOKEN", WRITE_TOKEN)
    monkeypatch.delenv("MEMFW_RELATED_DB", raising=False)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _http(
    base_url: str,
    method: str,
    path: str,
    raw_body: bytes | None = None,
    token: str | None = None,
) -> tuple[int, dict]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers[service.TOKEN_HEADER] = token
    request = urllib.request.Request(base_url + path, data=raw_body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))


def _post(base_url: str, path: str, payload: dict, token: str | None = READ_TOKEN):
    return _http(base_url, "POST", path, _canonical(payload), token)


def _canonical(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _use_vault(monkeypatch: pytest.MonkeyPatch, root: Path, *, related: bool = False) -> None:
    monkeypatch.setenv("MEMFW_READONLY_ROOT", str(root))
    if related:
        monkeypatch.setenv("MEMFW_RELATED_DB", str(root / "related.sqlite3"))
    else:
        monkeypatch.delenv("MEMFW_RELATED_DB", raising=False)


# ---------------------------------------------------------------------------
# HTTP contract surface
# ---------------------------------------------------------------------------


def test_health_needs_no_token_and_exposes_no_sensitive_fields(
    httpd: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = seed_vault(tmp_path / "vault")
    build_related(root)
    _use_vault(monkeypatch, root, related=True)
    status, payload = _http(httpd, "GET", "/health")
    assert status == 200
    assert payload == {
        "ok": True,
        "service": "memfw",
        "contract": CONTRACT,
        "index_present": True,
        "related_index_present": True,
    }
    text = json.dumps(payload)
    assert str(root) not in text
    assert READ_TOKEN not in text and WRITE_TOKEN not in text


def test_health_without_related_projection(
    httpd: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = seed_vault(tmp_path / "vault")
    _use_vault(monkeypatch, root)
    status, payload = _http(httpd, "GET", "/health")
    assert status == 200
    assert payload["related_index_present"] is False
    assert payload["index_present"] is True


def test_missing_or_wrong_token_is_401(
    httpd: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_vault(monkeypatch, seed_vault(tmp_path / "vault"))
    for token in (None, "wrong"):
        status, payload = _post(httpd, "/search", {"query": "corpus"}, token)
        assert status == 401
        _assert_error(payload, "unauthorized")


def test_read_token_cannot_stage(
    httpd: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_vault(monkeypatch, seed_vault(tmp_path / "vault"))
    status, payload = _post(
        httpd,
        "/stage",
        {"proposal_id": "p1", "title": "T", "content": "body"},
        READ_TOKEN,
    )
    assert status == 403
    _assert_error(payload, "forbidden")


def test_stage_is_403_when_no_write_token_is_configured(
    httpd: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_vault(monkeypatch, seed_vault(tmp_path / "vault"))
    monkeypatch.delenv("MEMFW_WRITE_TOKEN", raising=False)
    monkeypatch.delenv("MEMFW_WRITE_TOKEN_FILE", raising=False)
    status, payload = _post(
        httpd, "/stage", {"proposal_id": "p1", "title": "T", "content": "body"}, WRITE_TOKEN
    )
    assert status == 403
    _assert_error(payload, "forbidden")


def test_tokens_can_come_from_files(
    httpd: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_vault(monkeypatch, seed_vault(tmp_path / "vault"))
    read_file = tmp_path / "read.token"
    read_file.write_text(f"  {READ_TOKEN}\n", encoding="utf-8")
    read_file.chmod(0o600)
    write_file = tmp_path / "write.token"
    write_file.write_text(f"{WRITE_TOKEN}\n", encoding="utf-8")
    write_file.chmod(0o600)
    monkeypatch.delenv("MEMFW_READONLY_TOKEN", raising=False)
    monkeypatch.delenv("MEMFW_WRITE_TOKEN", raising=False)
    monkeypatch.setenv("MEMFW_READONLY_TOKEN_FILE", str(read_file))
    monkeypatch.setenv("MEMFW_WRITE_TOKEN_FILE", str(write_file))
    status, payload = _post(httpd, "/search", {"query": "corpus"}, READ_TOKEN)
    assert status == 200 and payload["contract"] == CONTRACT
    status, payload = _post(
        httpd,
        "/stage",
        {"proposal_id": "p-file", "title": "T", "content": "body"},
        WRITE_TOKEN,
    )
    assert status == 200 and payload["staged"] is True


def test_unknown_path_and_wrong_method_are_404(
    httpd: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_vault(monkeypatch, seed_vault(tmp_path / "vault"))
    for method, path, body in (
        ("GET", "/nope", None),
        ("POST", "/nope", b"{}"),
        ("GET", "/search", None),
        ("DELETE", "/search", b"{}"),
        ("PUT", "/stage", b"{}"),
    ):
        status, payload = _http(httpd, method, path, body, READ_TOKEN)
        assert status == 404, (method, path)
        _assert_error(payload, "not_found")


def test_request_validation_returns_400(
    httpd: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_vault(monkeypatch, seed_vault(tmp_path / "vault"))
    cases = [
        (b"{not json", "bad_request"),
        (b"[1, 2]", "bad_request"),
        (_canonical({"query": "corpus", "limit": "ten"}), "bad_request"),
        (_canonical({"query": "corpus", "limit": True}), "bad_request"),
        (_canonical({"query": "corpus", "as_of": "yesterday"}), "bad_request"),
        (_canonical({"query": "corpus", "include_history": "yes"}), "bad_request"),
        (_canonical({"query": ""}), "bad_request"),
        (_canonical({"query": "x" * 513}), "bad_request"),
    ]
    for raw, code in cases:
        status, payload = _http(httpd, "POST", "/search", raw, READ_TOKEN)
        assert status == 400, raw[:40]
        _assert_error(payload, code)


def test_missing_content_length_returns_400(
    httpd: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_vault(monkeypatch, seed_vault(tmp_path / "vault"))
    parts = urlsplit(httpd)
    connection = HTTPConnection(parts.hostname, parts.port, timeout=10)
    connection.putrequest("POST", "/search")
    connection.putheader(service.TOKEN_HEADER, READ_TOKEN)
    connection.endheaders(b'{"query": "corpus"}')
    response = connection.getresponse()
    payload = json.loads(response.read().decode("utf-8"))
    connection.close()
    assert response.status == 400
    _assert_error(payload, "bad_request")


def test_body_size_limits_return_413(
    httpd: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_vault(monkeypatch, seed_vault(tmp_path / "vault"))
    # /search body > 8 KiB
    status, payload = _http(
        httpd,
        "POST",
        "/search",
        _canonical({"query": "q" * 9000}),
        READ_TOKEN,
    )
    assert status == 413
    _assert_error(payload, "too_large")
    # /stage body > 128 KiB
    status, payload = _http(httpd, "POST", "/stage", b"x" * (129 * 1024), WRITE_TOKEN)
    assert status == 413
    _assert_error(payload, "too_large")
    # /stage content > 64 KiB (body still under the 128 KiB request limit)
    status, payload = _post(
        httpd,
        "/stage",
        {"proposal_id": "large", "title": "Large", "content": "y" * (65_537)},
        WRITE_TOKEN,
    )
    assert status == 413
    _assert_error(payload, "too_large")


def test_related_unknown_memory_is_404(
    httpd: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_vault(monkeypatch, seed_vault(tmp_path / "vault"))
    status, payload = _post(httpd, "/related", {"memory_id": "mem-missing"})
    assert status == 404
    _assert_error(payload, "not_found")
    status, payload = _post(httpd, "/related", {"memory_id": "../memories/mem-alpha"})
    assert status == 400
    _assert_error(payload, "bad_request")


def test_internal_error_message_is_fixed(
    httpd: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_vault(monkeypatch, seed_vault(tmp_path / "vault"))

    def explode(*_args: object, **_kwargs: object) -> dict:
        raise RuntimeError(f"boom {tmp_path}/secret")

    monkeypatch.setattr(service, "search_vault", explode)
    status, payload = _post(httpd, "/search", {"query": "corpus"})
    assert status == 500
    assert payload == {
        "contract": CONTRACT,
        "error": {"code": "internal", "message": "internal error"},
    }
    text = json.dumps(payload)
    assert "RuntimeError" not in text
    assert str(tmp_path) not in text


# ---------------------------------------------------------------------------
# Search semantics
# ---------------------------------------------------------------------------


def test_bound_token_tenant_rejects_a_switch_and_defaults_when_omitted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, httpd: str
) -> None:
    root = seed_vault(
        tmp_path / "vault",
        [
            _memory("mem-alpha", "Tenant alpha", "corpus keyword", tenant="tenant-a"),
            _memory("mem-beta", "Tenant beta", "corpus keyword", tenant="tenant-b"),
        ],
    )
    _use_vault(monkeypatch, root)
    monkeypatch.setenv("MEMFW_TENANT", "tenant-a")

    status, same = _post(httpd, "/search", {"query": "corpus keyword", "tenant": "tenant-a"})
    assert status == 200
    assert [item["id"] for item in same["results"]] == ["mem-alpha"]

    status, other = _post(httpd, "/search", {"query": "corpus keyword", "tenant": "tenant-b"})
    assert status == 403
    assert other["error"]["code"] == "forbidden"

    status, omitted = _post(httpd, "/search", {"query": "corpus keyword"})
    assert status == 200
    assert [item["id"] for item in omitted["results"]] == ["mem-alpha"]

    status, switched = _post(httpd, "/related", {"memory_id": "mem-beta", "tenant": "tenant-b"})
    assert status == 403


def test_tenant_filter_returns_a_full_limit(tmp_path: Path) -> None:
    documents = []
    for index in range(6):
        documents.append(
            _memory(
                f"other-{index}",
                "corpus keyword corpus keyword",
                "corpus keyword corpus keyword",
                tenant="tenant-b",
            )
        )
    for index in range(4):
        documents.append(
            _memory(
                f"target-{index}",
                f"Corpus note {index}",
                "corpus keyword",
                tenant="tenant-a",
                source="notes" if index < 3 else "manual",
            )
        )
    root = seed_vault(tmp_path / "vault", documents)
    result = search_vault(root, "corpus keyword", limit=3, tenant="tenant-a")
    assert len(result["results"]) == 3
    assert {item["tenant"] for item in result["results"]} == {"tenant-a"}


def test_source_filter_runs_before_the_limit(tmp_path: Path) -> None:
    documents = [
        _memory(f"manual-{index}", "corpus keyword", "corpus keyword") for index in range(4)
    ] + [
        _memory(f"notes-{index}", "corpus keyword", "corpus keyword", source="notes")
        for index in range(3)
    ]
    root = seed_vault(tmp_path / "vault", documents)
    result = search_vault(root, "corpus keyword", limit=2, source="notes")
    assert len(result["results"]) == 2
    assert {item["source"] for item in result["results"]} == {"notes"}


def test_score_is_normalised_and_monotonic(tmp_path: Path) -> None:
    root = seed_vault(tmp_path / "vault")
    result = search_vault(root, "corpus keyword", limit=10, tenant="tenant-a")
    assert len(result["results"]) >= 2
    scores = [item["score"] for item in result["results"]]
    raw_scores = [item["raw_score"] for item in result["results"]]
    assert all(0.0 <= score <= 1.0 for score in scores)
    assert scores == sorted(scores, reverse=True)
    assert raw_scores == sorted(raw_scores)
    short = search_vault(root, "al", limit=5, tenant="tenant-a")
    assert short["results"]
    short_scores = [item["score"] for item in short["results"]]
    assert short_scores == sorted(short_scores, reverse=True)
    assert all(item["raw_score"] is None for item in short["results"])
    assert all(0.0 <= score <= 1.0 for score in short_scores)
    assert len(set(short_scores)) == len(short_scores)
    upper = search_vault(root, "AL", limit=5, tenant="tenant-a")
    assert [item["id"] for item in upper["results"]] == [item["id"] for item in short["results"]]
    single = search_vault(root, "a", limit=5, tenant="tenant-a")
    assert single["results"]
    assert all(item["raw_score"] is None for item in single["results"])


def test_as_of_keeps_a_document_superseded_after_the_instant(tmp_path: Path) -> None:
    documents = [
        _memory(
            "mem-a",
            "Alpha current",
            "alpha phrase",
            valid_from=datetime(2026, 11, 6, 0, 10, tzinfo=UTC),
            valid_to=datetime(2026, 11, 6, 0, 20, tzinfo=UTC),
            superseded_by="mem-b",
        ),
        _memory(
            "mem-b",
            "Beta replacement",
            "alpha phrase",
            valid_from=datetime(2026, 11, 6, 0, 20, tzinfo=UTC),
        ),
        _memory(
            "mem-c",
            "Gamma expired",
            "alpha phrase",
            valid_from=datetime(2026, 11, 6, 0, 0, tzinfo=UTC),
            valid_to=datetime(2026, 11, 6, 0, 5, tzinfo=UTC),
        ),
        _memory(
            "mem-d",
            "Delta future",
            "alpha phrase",
            valid_from=datetime(2026, 11, 6, 0, 25, tzinfo=UTC),
        ),
    ]
    root = seed_vault(tmp_path / "vault", documents)
    current = search_vault(root, "alpha phrase", limit=10)
    assert {item["id"] for item in current["results"]} == {"mem-b", "mem-d"}
    at_instant = search_vault(root, "alpha phrase", limit=10, as_of="2026-11-06T00:15:00+00:00")
    assert {item["id"] for item in at_instant["results"]} == {"mem-a"}
    history = search_vault(root, "alpha phrase", limit=10, include_history=True)
    assert {item["id"] for item in history["results"]} == {
        "mem-a",
        "mem-b",
        "mem-c",
        "mem-d",
    }


def test_result_payload_carries_temporal_and_hash_fields(tmp_path: Path) -> None:
    root = seed_vault(tmp_path / "vault")
    result = search_vault(root, "corpus keyword", limit=10, tenant="tenant-a")
    by_id = {item["id"]: item for item in result["results"]}
    alpha = by_id["mem-alpha"]
    assert alpha["related"] == ["mem-beta"]
    assert alpha["valid_from"] is None
    expected = (
        "sha256:" + hashlib.sha256((root / "memories" / "mem-alpha.md").read_bytes()).hexdigest()
    )
    assert alpha["content_hash"] == expected
    assert alpha["summary"] == "alpha corpus keyword"
    assert alpha["title"] == "Alpha corpus note"


def test_include_history_and_superseded_documents(tmp_path: Path) -> None:
    root = seed_vault(tmp_path / "vault")
    current = search_vault(root, "corpus keyword", limit=10, tenant="tenant-a")
    assert "mem-old" not in {item["id"] for item in current["results"]}
    history = search_vault(
        root, "corpus keyword", limit=10, include_history=True, tenant="tenant-a"
    )
    assert "mem-old" in {item["id"] for item in history["results"]}


# ---------------------------------------------------------------------------
# Related
# ---------------------------------------------------------------------------


def test_related_edges_mode_and_no_write(tmp_path: Path) -> None:
    root = seed_vault(tmp_path / "vault")
    related_db = build_related(root)
    before = related_db.read_bytes()
    result = related_vault(root, "mem-alpha", limit=5, related_db=related_db)
    assert result["mode"] == "edges"
    assert result["degraded"] is False
    assert result["contract"] == CONTRACT
    ids = [item["id"] for item in result["results"]]
    assert "mem-alpha" not in ids
    assert "mem-beta" in ids
    beta = next(item for item in result["results"] if item["id"] == "mem-beta")
    assert beta["reasons"]
    assert "lexical" in beta["reasons"]
    assert related_db.read_bytes() == before


def test_related_falls_back_to_fts(tmp_path: Path) -> None:
    root = seed_vault(tmp_path / "vault")
    result = related_vault(root, "mem-alpha", limit=5, tenant="tenant-a")
    assert result["mode"] == "fts"
    assert result["degraded"] is True
    assert [item["reasons"] for item in result["results"]] == [["lexical"]]
    assert all(item["id"] != "mem-alpha" for item in result["results"])
    assert result["results"]


def test_related_rejects_invalid_ids_and_unknown_memory(tmp_path: Path) -> None:
    root = seed_vault(tmp_path / "vault")
    with pytest.raises(ServiceError) as invalid:
        related_vault(root, "../memories/mem-alpha")
    assert invalid.value.status == 400
    with pytest.raises(ServiceError) as missing:
        related_vault(root, "mem-missing")
    assert missing.value.status == 404


# ---------------------------------------------------------------------------
# Staging
# ---------------------------------------------------------------------------


def test_stage_writes_the_canonical_candidate(tmp_path: Path) -> None:
    root = seed_vault(tmp_path / "vault")
    memories_before = sorted((root / "memories").glob("*.md"))
    result = stage_candidate(
        root,
        {
            "proposal_id": "p-1",
            "title": "Proposal memory",
            "content": "candidate body",
            "tags": ["x", "y"],
            "related": ["mem-alpha"],
            "tenant": "tenant-a",
        },
    )
    assert result["contract"] == CONTRACT
    assert result["authority"] is False
    assert result["staged"] is True
    assert result["duplicate"] is False
    assert result["candidate_id"] == "conductor-p-1"
    assert result["path"] == "_candidates/remember/conductor-p-1.md"
    path = root / "_candidates" / "remember" / "conductor-p-1.md"
    assert path.is_file()
    parsed = yaml.safe_load(path.read_text(encoding="utf-8").split("---\n")[1])
    required = {
        "proposal_id",
        "source",
        "tenant",
        "tags",
        "related",
        "title",
        "created_at",
        "content_hash",
        "authority",
    }
    assert required <= set(parsed)
    assert parsed["authority"] is False
    assert parsed["source"] == "conductor"
    assert parsed["content_hash"] == ("sha256:" + hashlib.sha256(b"candidate body").hexdigest())
    assert parsed["metadata"]["proposal_id"] == "p-1"
    staged = list_staged_candidates(root)
    assert [item["title"] for item in staged] == ["Proposal memory"]
    assert staged[0]["body"] == "candidate body"
    assert staged[0]["tags"] == ["x", "y"]
    assert sorted((root / "memories").glob("*.md")) == memories_before


def test_stage_duplicate_does_not_rewrite(tmp_path: Path) -> None:
    root = seed_vault(tmp_path / "vault")
    payload = {"proposal_id": "p-2", "title": "T", "content": "same body"}
    first = stage_candidate(root, payload)
    path = root / first["path"]
    before_mtime = path.stat().st_mtime_ns
    before_bytes = path.read_bytes()
    second = stage_candidate(root, payload)
    assert second["duplicate"] is True
    assert second["candidate_id"] == first["candidate_id"]
    assert second["path"] == first["path"]
    assert path.stat().st_mtime_ns == before_mtime
    assert path.read_bytes() == before_bytes


def test_stage_conflict_is_409(tmp_path: Path) -> None:
    root = seed_vault(tmp_path / "vault")
    stage_candidate(root, {"proposal_id": "p-3", "title": "T", "content": "one"})
    with pytest.raises(ServiceError) as conflict:
        stage_candidate(root, {"proposal_id": "p-3", "title": "T", "content": "two"})
    assert conflict.value.status == 409
    assert conflict.value.code == "conflict"


def test_stage_validation(tmp_path: Path) -> None:
    root = seed_vault(tmp_path / "vault")
    for payload in (
        {"title": "T", "content": "body"},
        {"proposal_id": "bad id", "title": "T", "content": "body"},
        {"proposal_id": "p", "content": "body"},
        {"proposal_id": "p", "title": "x" * 201, "content": "body"},
        {"proposal_id": "p", "title": "T", "content": ""},
        {"proposal_id": "p", "title": "T", "content": "   "},
        {"proposal_id": "p", "title": "T", "content": "\n\t"},
        {"proposal_id": "p", "title": "T", "content": "body", "tags": "x"},
        {"proposal_id": "p", "title": "T", "content": "body", "related": [1]},
    ):
        with pytest.raises(ServiceError) as invalid:
            stage_candidate(root, payload)
        assert invalid.value.status == 400, payload
    with pytest.raises(ServiceError) as too_large:
        stage_candidate(root, {"proposal_id": "p", "title": "T", "content": "x" * 65_537})
    assert too_large.value.status == 413


def test_stage_never_touches_the_vault(tmp_path: Path) -> None:
    root = seed_vault(tmp_path / "vault")
    stage_candidate(root, {"proposal_id": "p-4", "title": "T", "content": "body"})
    assert not (tmp_path / "_candidates").exists()
    assert not (root / "_candidates").joinpath("memfw").exists()


def test_search_rejects_a_non_fts_mode(
    httpd: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_vault(monkeypatch, seed_vault(tmp_path / "vault"))
    status, payload = _post(
        httpd, "/search", {"query": "corpus keyword", "mode": "hybrid"}, READ_TOKEN
    )
    assert status == 400
    _assert_error(payload, "bad_request")
    assert payload["error"]["message"] == "mode is not available"
    status, payload = _post(
        httpd,
        "/search",
        {"query": "corpus keyword", "mode": "fts", "tenant": "tenant-a"},
        READ_TOKEN,
    )
    assert status == 200
    assert payload["mode"] == "fts"
    assert payload["results"]


def test_envelope_fields_and_summary_truncation(tmp_path: Path) -> None:
    documents = [_memory("mem-long", "Long note", "corpus " + "detail " * 200)]
    root = seed_vault(tmp_path / "vault", documents)
    result = search_vault(root, "corpus", limit=5)
    assert result["mode"] == "fts"
    assert result["degraded"] is False
    assert result["errors"] == []
    item = result["results"][0]
    assert len(item["summary"]) == 500
    assert item["summary"].startswith("corpus detail")


def test_limit_is_clamped_and_not_an_error(tmp_path: Path) -> None:
    root = seed_vault(tmp_path / "vault")
    low = search_vault(root, "corpus keyword", limit=0, tenant="tenant-a")
    assert len(low["results"]) == 1
    high = search_vault(root, "corpus keyword", limit=999, tenant="tenant-a")
    assert 1 <= len(high["results"]) <= 50
    over = related_vault(root, "mem-alpha", limit=0)
    assert isinstance(over["results"], list)


def test_related_body_limit_returns_413(
    httpd: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_vault(monkeypatch, seed_vault(tmp_path / "vault"))
    raw = _canonical({"memory_id": "mem-alpha", "padding": "p" * 9000})
    status, payload = _http(httpd, "POST", "/related", raw, READ_TOKEN)
    assert status == 413
    _assert_error(payload, "too_large")


def test_write_token_can_read(httpd: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _use_vault(monkeypatch, seed_vault(tmp_path / "vault"))
    status, payload = _post(httpd, "/search", {"query": "corpus"}, WRITE_TOKEN)
    assert status == 200
    assert payload["contract"] == CONTRACT
    status, payload = _post(httpd, "/related", {"memory_id": "mem-alpha"}, WRITE_TOKEN)
    assert status == 200
    assert payload["mode"] == "fts"


def test_environment_credential_wins_over_file(
    httpd: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_vault(monkeypatch, seed_vault(tmp_path / "vault"))
    credential_file = tmp_path / "file.credential"
    credential_file.write_text("file-credential\n", encoding="utf-8")
    monkeypatch.setenv("MEMFW_READONLY_TOKEN", "environment-credential")
    monkeypatch.setenv("MEMFW_READONLY_TOKEN_FILE", str(credential_file))
    status, _payload = _post(httpd, "/search", {"query": "corpus"}, "environment-credential")
    assert status == 200
    status, payload = _post(httpd, "/search", {"query": "corpus"}, "file-credential")
    assert status == 401
    _assert_error(payload, "unauthorized")


def test_main_binds_loopback_and_requires_a_credential(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, object] = {}

    class FakeServer:
        def __init__(self, address: tuple[str, int], handler: object) -> None:
            captured["address"] = address
            captured["handler"] = handler

        def serve_forever(self) -> None:
            raise KeyboardInterrupt

        def server_close(self) -> None:
            pass

    monkeypatch.setattr(service, "ThreadingHTTPServer", FakeServer)
    for name in (
        "MEMFW_READONLY_TOKEN_FILE",
        "MEMFW_WRITE_TOKEN",
        "MEMFW_WRITE_TOKEN_FILE",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MEMFW_READONLY_TOKEN", READ_TOKEN)
    assert service.main(["--root", str(tmp_path / "vault")]) == 0
    assert captured["address"] == ("127.0.0.1", 18080)
    monkeypatch.delenv("MEMFW_READONLY_TOKEN", raising=False)
    monkeypatch.delenv("MEMFW_READONLY_TOKEN_FILE", raising=False)
    with pytest.raises(SystemExit):
        service.main(["--root", str(tmp_path / "vault")])


def test_unit_file_binds_loopback_and_names_the_tailnet_override() -> None:
    unit = Path(__file__).resolve().parents[1] / "scripts" / "memfw-readonly.service"
    text = unit.read_text(encoding="utf-8")
    assert "--host 127.0.0.1" in text
    assert "--related-db" in text
    assert "EnvironmentFile=" in text
    for forbidden in ("0.0.0.0", "nohup"):
        assert forbidden not in text


def _candidate_body(path: Path) -> str:
    _head, body = path.read_text(encoding="utf-8").split("---\n", 2)[1:]
    return body.strip("\n")


def _stage_in_child(root: str, content: str, results: multiprocessing.Queue) -> None:
    try:
        outcome = stage_candidate(
            Path(root),
            {"proposal_id": "process-race", "title": "Race", "content": content},
        )
        results.put(("ok", content, outcome["duplicate"]))
    except ServiceError as error:
        results.put(("error", content, error.status))


def test_stage_is_atomic_under_thread_contention(tmp_path: Path) -> None:
    root = seed_vault(tmp_path / "vault")
    barrier = threading.Barrier(8)
    outcomes: list[tuple] = []
    lock = threading.Lock()

    def worker(content: str) -> None:
        barrier.wait()
        try:
            result = stage_candidate(
                root, {"proposal_id": "thread-race", "title": "Race", "content": content}
            )
            with lock:
                outcomes.append(("ok", content, result["duplicate"]))
        except ServiceError as error:
            with lock:
                outcomes.append(("error", content, error.status))

    threads = [
        threading.Thread(target=worker, args=(content,))
        for content in ("alpha", "alpha", "alpha", "alpha", "beta", "beta", "beta", "beta")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    winners = [item for item in outcomes if item[0] == "ok" and item[2] is False]
    assert len(winners) == 1, outcomes
    winner_content = winners[0][1]
    path = root / "_candidates" / "remember" / "conductor-thread-race.md"
    assert _candidate_body(path) == winner_content
    for item in outcomes:
        if item[0] == "error":
            assert item[2] == 409
        elif item[2] is False:
            continue
        else:
            assert item[1] == winner_content, outcomes


def test_stage_is_atomic_across_processes(tmp_path: Path) -> None:
    root = seed_vault(tmp_path / "vault")
    context = multiprocessing.get_context("spawn")
    results = context.Queue()
    processes = [
        context.Process(target=_stage_in_child, args=(str(root), content, results))
        for content in ("alpha", "beta")
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=30)
    collected = [results.get(timeout=5) for _ in processes]
    published = [item for item in collected if item[0] == "ok" and item[2] is False]
    assert len(published) == 1, collected
    winner_content = published[0][1]
    path = root / "_candidates" / "remember" / "conductor-process-race.md"
    assert _candidate_body(path) == winner_content
    for item in collected:
        if item[0] == "error":
            assert item[2] == 409
        elif item[2] is True:
            assert item[1] == winner_content


def test_stage_refuses_symlinked_candidate_directories(tmp_path: Path) -> None:
    root = seed_vault(tmp_path / "vault")
    (root / "_candidates").symlink_to(root / "memories")
    with pytest.raises(ServiceError) as first:
        stage_candidate(root, {"proposal_id": "bypass", "title": "T", "content": "x"})
    assert first.value.status == 500
    assert not (root / "memories" / "conductor-bypass.md").exists()

    root_link = seed_vault(tmp_path / "vault3")
    (root_link / "_candidates").mkdir()
    (root_link / "_candidates" / "remember").symlink_to(root_link / "memories")
    with pytest.raises(ServiceError) as third:
        stage_candidate(root_link, {"proposal_id": "bypass3", "title": "T", "content": "x"})
    assert third.value.status == 500
    assert not list((root_link / "memories").glob("conductor-*.md"))

    outside = tmp_path / "outside"
    outside.mkdir()
    root2 = seed_vault(tmp_path / "vault2")
    (root2 / "_candidates").mkdir()
    (root2 / "_candidates" / "remember").symlink_to(outside)
    with pytest.raises(ServiceError) as second:
        stage_candidate(root2, {"proposal_id": "bypass2", "title": "T", "content": "x"})
    assert second.value.status == 500
    assert not list(outside.glob("*.md"))


def test_stage_serialises_multiline_values_safely(tmp_path: Path) -> None:
    root = seed_vault(tmp_path / "vault")
    payload = {
        "proposal_id": "multiline",
        "title": "A---\nB",
        "content": "multiline body",
        "tenant": "t\n---\nu",
        "tags": ["x---\ny", "z"],
        "related": ["r\n---\ns"],
    }
    first = stage_candidate(root, payload)
    assert first["duplicate"] is False
    path = root / "_candidates" / "remember" / "conductor-multiline.md"
    text = path.read_text(encoding="utf-8")
    header_text = text.split("---\n", 2)[1]
    assert "---\n" not in header_text + "\n"
    header = yaml.safe_load(header_text)
    assert header["title"] == "A---\nB"
    assert header["tenant"] == "t\n---\nu"
    assert header["tags"] == ["x---\ny", "z"]
    assert header["content_hash"] == ("sha256:" + hashlib.sha256(b"multiline body").hexdigest())
    staged = list_staged_candidates(root)
    assert [item["title"] for item in staged] == ["A---\nB"]
    assert staged[0]["tags"] == ["x---\ny", "z"]
    retry = stage_candidate(root, payload)
    assert retry["duplicate"] is True


def test_stage_source_with_control_characters_is_400(tmp_path: Path) -> None:
    root = seed_vault(tmp_path / "vault")
    for source in (
        "a\n---\nb",
        "a\x00b",
        "a\tb",
        "a\u0085b",
        "a\u2028b",
        "a\u2029b",
    ):
        payload = {"proposal_id": "src", "title": "T", "content": "x", "source": source}
        with pytest.raises(ServiceError) as rejected:
            stage_candidate(root, payload)
        assert rejected.value.status == 400, source
    assert not list((root / "_candidates").rglob("*.md"))


def test_related_reasons_stay_in_the_contract_vocabulary(tmp_path: Path) -> None:
    root = seed_vault(tmp_path / "vault")
    related_db = build_related(root)
    result = related_vault(root, "mem-alpha", limit=10, related_db=related_db)
    reasons = {reason for item in result["results"] for reason in item["reasons"]}
    assert reasons <= service.ALLOWED_REASONS
    for method, expected in (
        ("explicit", "link"),
        ("related", "related_field"),
        ("tag", "shared_tag"),
        ("course", "shared_course"),
        ("entity", "shared_entity"),
        ("lexical", "lexical"),
        ("semantic", "semantic"),
        ("section", "section"),
    ):
        assert service._reason_name(method) == expected
    assert service._reason_name("something-new") == "lexical"
    assert service._reason_name("mixed") == "lexical"
    fallback = related_vault(root, "mem-alpha", limit=5, tenant="tenant-a")
    fallback_reasons = {reason for item in fallback["results"] for reason in item["reasons"]}
    assert fallback_reasons == {"lexical"}


def test_stage_is_atomic_under_same_content_thread_contention(tmp_path: Path) -> None:
    root = seed_vault(tmp_path / "vault")
    barrier = threading.Barrier(6)
    outcomes: list[tuple] = []
    lock = threading.Lock()

    def worker() -> None:
        barrier.wait()
        try:
            result = stage_candidate(
                root, {"proposal_id": "thread-same", "title": "Race", "content": "same"}
            )
            with lock:
                outcomes.append(("ok", result["duplicate"]))
        except ServiceError as error:
            with lock:
                outcomes.append(("error", error.status))

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    published = [item for item in outcomes if item == ("ok", False)]
    duplicates = [item for item in outcomes if item == ("ok", True)]
    assert len(published) == 1, outcomes
    assert len(duplicates) == 5, outcomes
    path = root / "_candidates" / "remember" / "conductor-thread-same.md"
    assert _candidate_body(path) == "same"


def test_stage_is_atomic_across_processes_for_the_same_content(tmp_path: Path) -> None:
    root = seed_vault(tmp_path / "vault")
    context = multiprocessing.get_context("spawn")
    results = context.Queue()
    processes = [
        context.Process(target=_stage_in_child, args=(str(root), "same", results)) for _ in range(3)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=30)
    collected = [results.get(timeout=5) for _ in processes]
    assert len([item for item in collected if item[0] == "ok" and item[2] is False]) == 1
    assert len([item for item in collected if item[0] == "ok" and item[2] is True]) == 2
    assert not [item for item in collected if item[0] == "error"]


def test_candidate_directory_walk_refuses_a_symlink_after_the_precheck(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fd walk, not the pre-check, is what stops a swapped-in symlink."""
    root = seed_vault(tmp_path / "vault")
    (root / "_candidates").symlink_to(root / "memories")
    monkeypatch.setattr(service, "_assert_safe_candidate_directory", lambda *_args, **_kwargs: None)
    with pytest.raises(ServiceError) as refused:
        stage_candidate(root, {"proposal_id": "race", "title": "T", "content": "x"})
    assert refused.value.status == 500
    assert not list((root / "memories").glob("conductor-*.md"))


def test_stage_proposal_id_boundaries(tmp_path: Path) -> None:
    root = seed_vault(tmp_path / "vault")
    longest = "a" * 128
    result = stage_candidate(root, {"proposal_id": longest, "title": "T", "content": "x"})
    assert result["candidate_id"] == f"conductor-{longest}"
    assert (root / "_candidates" / "remember" / f"conductor-{longest}.md").is_file()
    for bad in ("a" * 129, "", "abc\n", "abc def", "abc/def", "abc.def", "abc:déf"):
        with pytest.raises(ServiceError) as rejected:
            stage_candidate(root, {"proposal_id": bad, "title": "T", "content": "x"})
        assert rejected.value.status == 400, bad
    published = [
        path.name
        for path in (root / "_candidates" / "remember").iterdir()
        if not path.name.startswith(".")
    ]
    assert published == [f"conductor-{longest}.md"]


def test_non_ascii_credential_is_401_not_500(
    httpd: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_vault(monkeypatch, seed_vault(tmp_path / "vault"))
    status, payload = _post(httpd, "/search", {"query": "corpus"}, "tökén")
    assert status == 401
    _assert_error(payload, "unauthorized")


def test_stage_control_characters_are_400_over_http(
    httpd: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = seed_vault(tmp_path / "vault")
    _use_vault(monkeypatch, root)
    payloads = [
        {"proposal_id": "p1", "title": "A---\nB", "content": "x", "source": "s\nu"},
        {"proposal_id": "p2", "title": "T", "content": "x", "source": "s\x00"},
        {"proposal_id": "p3", "title": "T", "content": "x", "source": "s\tt"},
    ]
    for payload in payloads:
        status, body = _post(httpd, "/stage", payload, WRITE_TOKEN)
        assert status == 400, payload
        _assert_error(body, "bad_request")
    assert not list((root / "_candidates").rglob("*.md") if (root / "_candidates").exists() else [])


def _clear_credential_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "MEMFW_READONLY_TOKEN",
        "MEMFW_READONLY_TOKEN_FILE",
        "MEMFW_WRITE_TOKEN",
        "MEMFW_WRITE_TOKEN_FILE",
    ):
        monkeypatch.delenv(name, raising=False)


def _socket_exchange(port: int, payload: bytes) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=3) as sock:
        sock.settimeout(3)
        try:
            sock.sendall(payload)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        chunks: list[bytes] = []
        while True:
            try:
                piece = sock.recv(65536)
            except TimeoutError:
                break
            if not piece:
                break
            chunks.append(piece)
        return b"".join(chunks)


def _status_and_body(raw: bytes) -> tuple[int, bytes]:
    head, separator, rest = raw.partition(b"\r\n\r\n")
    assert separator, raw[:120]
    status_line = head.split(b"\r\n", 1)[0]
    assert status_line.startswith(b"HTTP/"), status_line
    return int(status_line.split()[1]), rest


class _QuietHandler(BaseHTTPRequestHandler):
    def log_message(self, message_format: str, *args: object) -> None:
        del message_format, args


def test_surrogate_or_nul_query_is_400(tmp_path: Path) -> None:
    root = seed_vault(tmp_path / "vault")
    for query in ("\ud800", "a\x00b", "corpus\udfff"):
        with pytest.raises(ServiceError) as rejected:
            search_vault(root, query)
        assert rejected.value.status == 400, query


def test_surrogate_or_nul_query_is_400_over_http(
    httpd: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_vault(monkeypatch, seed_vault(tmp_path / "vault"))
    for raw in (b'{"query": "\\uD800"}', b'{"query": "a\\u0000b"}'):
        status, payload = _http(httpd, "POST", "/search", raw, READ_TOKEN)
        assert status == 400, raw
        _assert_error(payload, "bad_request")
    status, payload = _post(httpd, "/search", {"query": "corpus", "tenant": "tenant-a"})
    assert status == 200
    assert payload["results"]


def test_head_patch_and_options_are_404(
    httpd: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_vault(monkeypatch, seed_vault(tmp_path / "vault"))
    port = int(urlsplit(httpd).port or 0)
    raw = _socket_exchange(
        port, b"HEAD /health HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n"
    )
    status, body = _status_and_body(raw)
    assert status == 404
    assert body == b""
    for method, path in (("PATCH", "/search"), ("OPTIONS", "/related")):
        status, payload = _http(httpd, method, path, b"{}", READ_TOKEN)
        assert status == 404, (method, path)
        _assert_error(payload, "not_found")


def test_query_strings_and_trailing_slashes_are_404(
    httpd: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_vault(monkeypatch, seed_vault(tmp_path / "vault"))
    cases = (
        ("GET", "/health?x=1", None),
        ("GET", "/health/", None),
        ("POST", "/search/", b"{}"),
        ("POST", "/search?q=1", b"{}"),
        ("POST", "/related/", b"{}"),
        ("POST", "/stage/", b"{}"),
    )
    for method, path, body in cases:
        status, payload = _http(httpd, method, path, body, READ_TOKEN)
        assert status == 404, (method, path)
        _assert_error(payload, "not_found")


def test_include_history_overrides_as_of(
    httpd: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = seed_vault(tmp_path / "vault")
    _use_vault(monkeypatch, root)
    as_of = "2026-11-06T00:00:00+00:00"
    status, before = _post(
        httpd,
        "/search",
        {"query": "corpus keyword", "limit": 10, "as_of": as_of, "tenant": "tenant-a"},
    )
    assert status == 200
    assert "mem-old" not in {item["id"] for item in before["results"]}
    status, history = _post(
        httpd,
        "/search",
        {
            "query": "corpus keyword",
            "limit": 10,
            "as_of": as_of,
            "include_history": True,
            "tenant": "tenant-a",
        },
    )
    assert status == 200
    old = next(item for item in history["results"] if item["id"] == "mem-old")
    assert old["valid_to"]
    assert old["superseded_by"] == "mem-alpha"


def test_related_forwards_tenant_and_drops_stale_edges(
    httpd: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = seed_vault(tmp_path / "vault")
    database = root / "related.sqlite3"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE related_items (
            source TEXT NOT NULL, id TEXT NOT NULL, title TEXT NOT NULL,
            path TEXT NOT NULL, hash TEXT NOT NULL, text TEXT NOT NULL DEFAULT '',
            tenant TEXT NOT NULL DEFAULT '', scope TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (source, id)
        );
        CREATE TABLE related_edges (
            src_source TEXT NOT NULL, src_id TEXT NOT NULL,
            dst_source TEXT NOT NULL, dst_id TEXT NOT NULL,
            method TEXT NOT NULL, score REAL NOT NULL, evidence TEXT NOT NULL,
            PRIMARY KEY (src_source, src_id, dst_source, dst_id, method)
        );
        """
    )
    for identifier, title, tenant in (
        ("mem-alpha", "Alpha", "tenant-a"),
        ("mem-beta", "Beta", "tenant-a"),
        ("mem-gamma", "Gamma", "tenant-b"),
        ("mem-old", "Old", "tenant-a"),
    ):
        connection.execute(
            "INSERT INTO related_items"
            "(source,id,title,path,hash,text,tenant,scope) VALUES (?,?,?,?,?,?,?,?)",
            ("memory", identifier, title, f"memories/{identifier}.md", "", "", tenant, ""),
        )
    for destination, score in (("mem-beta", 1.5), ("mem-gamma", 4.0), ("mem-old", 9.0)):
        connection.execute(
            "INSERT INTO related_edges"
            "(src_source,src_id,dst_source,dst_id,method,score,evidence)"
            " VALUES (?,?,?,?,?,?,?)",
            ("memory", "mem-alpha", "memory", destination, "lexical", score, "{}"),
        )
    connection.commit()
    connection.close()
    _use_vault(monkeypatch, root, related=True)
    status, payload = _post(
        httpd, "/related", {"memory_id": "mem-alpha", "tenant": "tenant-a", "limit": 10}
    )
    assert status == 200, payload
    assert payload["mode"] == "edges"
    assert [item["id"] for item in payload["results"]] == ["mem-beta"]
    assert payload["results"][0]["score"] == 0.6
    assert "mem-old" not in {item["id"] for item in payload["results"]}
    status, missing = _post(httpd, "/related", {"memory_id": "mem-alpha", "tenant": "tenant-b"})
    assert status == 404
    _assert_error(missing, "not_found")


def _pressure_documents() -> list[MemoryDocument]:
    documents = [
        _memory(
            "z-subject",
            "Z subject",
            "shared corpus token",
            tenant="tenant-a",
            tags=("shared",),
        ),
        _memory(
            "z-target",
            "Secret Title",
            "shared corpus token",
            tenant="tenant-a",
            tags=("shared",),
        ),
    ]
    documents.extend(
        _memory(
            f"b-foreign-{index:02d}",
            f"Foreign {index:02d}",
            "shared corpus token",
            tenant="tenant-b",
            tags=("shared",),
        )
        for index in range(21)
    )
    return documents


def test_related_http_drops_target_after_live_tenant_move(
    httpd: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = seed_vault(tmp_path / "vault", _pressure_documents()[:2])
    build_related(root)
    target = root / "memories" / "z-target.md"
    text = target.read_text(encoding="utf-8")
    assert "tenant: tenant-a" in text
    target.write_text(text.replace("tenant: tenant-a", "tenant: tenant-b", 1), encoding="utf-8")
    _use_vault(monkeypatch, root, related=True)
    status, payload = _post(
        httpd, "/related", {"memory_id": "z-subject", "tenant": "tenant-a", "limit": 10}
    )
    assert status == 200, payload
    assert payload["mode"] == "edges"
    assert payload["degraded"] is False
    blob = json.dumps(payload)
    assert "z-target" not in blob
    assert "Secret Title" not in blob


def test_related_http_keeps_same_tenant_under_foreign_pressure(tmp_path: Path) -> None:
    root = seed_vault(tmp_path / "vault", _pressure_documents())
    related_db = build_related(root)
    result = related_vault(root, "z-subject", limit=10, tenant="tenant-a", related_db=related_db)
    assert result["mode"] == "edges"
    assert result["degraded"] is False
    ids = [item["id"] for item in result["results"]]
    assert ids == ["z-target"]
    assert "Secret Title" in {item["title"] for item in result["results"]}


def test_protocol_errors_map_414_431_and_505(
    httpd: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_vault(monkeypatch, seed_vault(tmp_path / "vault"))
    port = int(urlsplit(httpd).port or 0)
    exchanges = (
        b"GET /" + (b"a" * 70000) + b" HTTP/1.1\r\nHost: localhost\r\n\r\n",
        b"GET /health HTTP/1.1\r\nX-Big: " + (b"b" * 70000) + b"\r\n\r\n",
        b"GET /health HTTP/2.0\r\nHost: localhost\r\n\r\n",
    )
    for payload in exchanges:
        raw = _socket_exchange(port, payload)
        status, body = _status_and_body(raw)
        assert status in (400, 413), raw[:80]
        parsed = json.loads(body.decode("utf-8"))
        assert parsed["error"]["code"] in {"bad_request", "too_large"}
        assert raw.startswith(b"HTTP/")


def test_handler_timeout_cuts_off_a_slow_client(monkeypatch: pytest.MonkeyPatch) -> None:
    assert Handler.timeout == 5

    class FastTimeout(Handler):
        timeout = 0.25

    _clear_credential_env(monkeypatch)
    monkeypatch.setenv("MEMFW_READONLY_TOKEN", READ_TOKEN)
    monkeypatch.setenv("MEMFW_WRITE_TOKEN", WRITE_TOKEN)
    server = ThreadingHTTPServer(("127.0.0.1", 0), FastTimeout)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = int(server.server_address[1])
        started = time.monotonic()
        with socket.create_connection(("127.0.0.1", port), timeout=2) as sock:
            sock.settimeout(2)
            try:
                sock.recv(16)
            except TimeoutError:
                pass
        assert time.monotonic() - started < 1.5
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_equal_tokens_refuse_startup_and_requests(
    httpd: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_credential_env(monkeypatch)
    monkeypatch.setenv("MEMFW_READONLY_TOKEN", "same-token")
    monkeypatch.setenv("MEMFW_WRITE_TOKEN", "same-token")

    def explode(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("server must not start")

    monkeypatch.setattr(service, "ThreadingHTTPServer", explode)
    with pytest.raises(SystemExit) as refused:
        service.main(["--root", str(tmp_path / "vault"), "--port", "0"])
    assert refused.value.code not in (0, None)
    monkeypatch.setattr(service, "ThreadingHTTPServer", ThreadingHTTPServer)
    _use_vault(monkeypatch, seed_vault(tmp_path / "vault"))
    status, payload = _post(httpd, "/search", {"query": "corpus"}, "same-token")
    assert status == 403
    _assert_error(payload, "forbidden")


def test_all_interfaces_host_is_refused(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _clear_credential_env(monkeypatch)
    monkeypatch.setenv("MEMFW_READONLY_TOKEN", READ_TOKEN)

    def explode(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("server must not start")

    monkeypatch.setattr(service, "ThreadingHTTPServer", explode)
    with pytest.raises(SystemExit) as refused:
        service.main(["--root", str(tmp_path), "--host", "0.0.0.0", "--port", "0"])
    assert refused.value.code not in (0, None)


def test_root_falls_back_to_the_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, object] = {}

    class FakeServer:
        def __init__(self, address: tuple[str, int], handler: object) -> None:
            captured["address"] = address

        def serve_forever(self) -> None:
            raise KeyboardInterrupt

        def server_close(self) -> None:
            pass

    monkeypatch.setattr(service, "ThreadingHTTPServer", FakeServer)
    _clear_credential_env(monkeypatch)
    monkeypatch.setenv("MEMFW_READONLY_TOKEN", READ_TOKEN)
    root = tmp_path / "from-env"
    monkeypatch.setenv(service.ROOT_ENV, str(root))
    assert service.main(["--port", "0"]) == 0
    assert captured["address"] == ("127.0.0.1", 0)
    assert os.environ[service.ROOT_ENV] == str(root.resolve())


def test_credential_file_must_be_mode_0600(
    httpd: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_credential_env(monkeypatch)
    secret = tmp_path / "read.token"
    secret.write_text("secret\n", encoding="utf-8")
    secret.chmod(0o644)
    monkeypatch.setenv("MEMFW_READONLY_TOKEN_FILE", str(secret))
    monkeypatch.setenv("MEMFW_WRITE_TOKEN", WRITE_TOKEN)
    with pytest.raises(ServiceError) as loose:
        service.read_token()
    assert loose.value.status == 500
    secret.chmod(0o600)
    assert service.read_token() == "secret"
    missing = tmp_path / "missing-token"
    monkeypatch.setenv("MEMFW_READONLY_TOKEN_FILE", str(missing))
    with pytest.raises(ServiceError) as absent:
        service.read_token()
    assert absent.value.status == 500
    directory = tmp_path / "token-dir"
    directory.mkdir()
    directory.chmod(0o600)
    monkeypatch.setenv("MEMFW_READONLY_TOKEN_FILE", str(directory))
    with pytest.raises(ServiceError):
        service.read_token()
    secret.chmod(0o644)
    monkeypatch.setenv("MEMFW_READONLY_TOKEN_FILE", str(secret))
    _use_vault(monkeypatch, seed_vault(tmp_path / "vault"))
    status, payload = _post(httpd, "/search", {"query": "corpus"}, "secret")
    assert status == 500
    _assert_error(payload, "internal")


def test_stage_disk_full_removes_the_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = seed_vault(tmp_path / "vault")
    kept = stage_candidate(root, {"proposal_id": "pad", "title": "T", "content": " x "})
    assert kept["staged"] is True

    def no_space(_descriptor: int) -> None:
        raise OSError(28, "No space left on device")

    real_fsync = os.fsync
    monkeypatch.setattr(os, "fsync", no_space)
    with pytest.raises(OSError):
        stage_candidate(root, {"proposal_id": "full", "title": "T", "content": "body"})
    directory = root / "_candidates" / "remember"
    assert directory.is_dir()
    published = sorted(path.name for path in directory.iterdir() if not path.name.startswith("."))
    assert published == ["conductor-pad.md"]
    monkeypatch.setattr(os, "fsync", real_fsync)


def test_health_contract_check_uses_a_local_server(
    httpd: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_vault(monkeypatch, seed_vault(tmp_path / "vault"), related=True)
    checked = service.check_readonly_contract(httpd + "/health", timeout=2)
    assert urlsplit(httpd).hostname == "127.0.0.1"
    assert checked["ok"] is True
    assert checked["contract"] == CONTRACT
    assert service.main(["--check-url", httpd + "/health", "--port", "0"]) == 0

    class PreV2(_QuietHandler):
        def do_GET(self) -> None:  # noqa: N802
            body = b'{"ok": true, "service": "memfw-readonly"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    class Redirect(_QuietHandler):
        def do_GET(self) -> None:  # noqa: N802
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:9/secret")
            self.send_header("Content-Length", "0")
            self.end_headers()

    def serve(handler: type[BaseHTTPRequestHandler]) -> tuple[ThreadingHTTPServer, int]:
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        server.daemon_threads = True
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        return server, int(server.server_address[1])

    pre_v2, pre_port = serve(PreV2)
    try:
        stale = service.check_readonly_contract(f"http://127.0.0.1:{pre_port}/health", timeout=2)
    finally:
        pre_v2.shutdown()
        pre_v2.server_close()
    assert stale["reachable"] is True
    assert stale["ok"] is False
    assert stale["contract"] is None

    redirect, redirect_port = serve(Redirect)
    try:
        followed = service.check_readonly_contract(
            f"http://127.0.0.1:{redirect_port}/health", timeout=2
        )
    finally:
        redirect.shutdown()
        redirect.server_close()
    assert followed["reachable"] is True
    assert followed["status"] == 302
    assert followed["ok"] is False
    assert service.check_readonly_contract("https://127.0.0.1/health")["reachable"] is False


# ---------------------------------------------------------------------------
# Canonical sample set (contract section 9)
# ---------------------------------------------------------------------------

STAGE_OK = {
    "proposal_id": "sample-ok",
    "title": "Staged sample",
    "content": "Staged sample body.",
    "tags": ["sample"],
    "related": ["mem-alpha"],
    "tenant": "tenant-a",
    "source": "conductor",
}

SCENARIOS: dict[str, dict] = {
    "health.ok": {
        "status": 200,
        "method": "GET",
        "path": "/health",
        "body": {},
        "token": None,
        "related": True,
    },
    "search.ok": {
        "status": 200,
        "method": "POST",
        "path": "/search",
        "body": {"query": "corpus keyword", "limit": 3, "tenant": "tenant-a"},
        "token": READ_TOKEN,
        "related": True,
    },
    "search.as_of_superseded_after_t": {
        "status": 200,
        "method": "POST",
        "path": "/search",
        "body": {
            "query": "corpus keyword",
            "limit": 5,
            "as_of": "2026-11-06T00:15:00+00:00",
            "tenant": "tenant-a",
        },
        "token": READ_TOKEN,
        "related": True,
    },
    "search.unauthorized": {
        "status": 401,
        "method": "POST",
        "path": "/search",
        "body": {"query": "corpus keyword"},
        "token": None,
        "related": True,
    },
    "search.too_large": {
        "status": 413,
        "method": "POST",
        "path": "/search",
        "body": {"query": "q" * 9000},
        "token": READ_TOKEN,
        "related": True,
    },
    "related.edges_ok": {
        "status": 200,
        "method": "POST",
        "path": "/related",
        "body": {"memory_id": "mem-alpha", "limit": 3},
        "token": READ_TOKEN,
        "related": True,
    },
    "related.fts_degraded": {
        "status": 200,
        "method": "POST",
        "path": "/related",
        "body": {"memory_id": "mem-alpha", "limit": 3, "tenant": "tenant-a"},
        "token": READ_TOKEN,
        "related": False,
    },
    "related.not_found": {
        "status": 404,
        "method": "POST",
        "path": "/related",
        "body": {"memory_id": "mem-missing"},
        "token": READ_TOKEN,
        "related": True,
    },
    "stage.ok": {
        "status": 200,
        "method": "POST",
        "path": "/stage",
        "body": STAGE_OK,
        "token": WRITE_TOKEN,
        "related": True,
    },
    "stage.duplicate": {
        "status": 200,
        "method": "POST",
        "path": "/stage",
        "body": STAGE_OK,
        "token": WRITE_TOKEN,
        "related": True,
        "before": ["stage.ok"],
    },
    "stage.conflict": {
        "status": 409,
        "method": "POST",
        "path": "/stage",
        "body": {
            "proposal_id": "sample-ok",
            "title": "Staged sample",
            "content": "Different body.",
            "tags": ["sample"],
            "tenant": "tenant-a",
            "source": "conductor",
        },
        "token": WRITE_TOKEN,
        "related": True,
        "before": ["stage.ok"],
    },
    "stage.forbidden_read_token": {
        "status": 403,
        "method": "POST",
        "path": "/stage",
        "body": {
            "proposal_id": "sample-forbidden",
            "title": "Forbidden sample",
            "content": "Forbidden sample body.",
        },
        "token": READ_TOKEN,
        "related": True,
    },
    "stage.too_large": {
        "status": 413,
        "method": "POST",
        "path": "/stage",
        "body": {
            "proposal_id": "sample-too-large",
            "title": "Too large sample",
            "content": "x" * 65_537,
        },
        "token": WRITE_TOKEN,
        "related": True,
    },
}
ENDPOINT_OF = {
    "health.ok": "/health",
    "search.ok": "/search",
    "search.as_of_superseded_after_t": "/search",
    "search.unauthorized": "/search",
    "search.too_large": "/search",
    "related.edges_ok": "/related",
    "related.fts_degraded": "/related",
    "related.not_found": "/related",
    "stage.ok": "/stage",
    "stage.duplicate": "/stage",
    "stage.conflict": "/stage",
    "stage.forbidden_read_token": "/stage",
    "stage.too_large": "/stage",
}


def _scenario_response(
    base_url: str, name: str, root: Path, monkeypatch: pytest.MonkeyPatch
) -> dict:
    scenario = SCENARIOS[name]
    for previous in scenario.get("before", []):
        _scenario_response(base_url, previous, root, monkeypatch)
    if scenario["related"] and not (root / "related.sqlite3").exists():
        build_related(root)
    _use_vault(monkeypatch, root, related=scenario["related"])
    raw = _canonical(scenario["body"])
    if scenario["method"] == "GET":
        status, payload = _http(base_url, "GET", scenario["path"], None, scenario["token"])
    else:
        status, payload = _http(base_url, "POST", scenario["path"], raw, scenario["token"])
    return {"status": status, "payload": payload}


def _assert_error(payload: dict, code: str) -> None:
    assert payload["contract"] == CONTRACT
    assert set(payload) == {"contract", "error"}
    assert set(payload["error"]) == {"code", "message"}
    assert payload["error"]["code"] == code
    assert isinstance(payload["error"]["message"], str) and payload["error"]["message"]


def _assert_close(actual: object, expected: object, path: str = "") -> None:
    if isinstance(expected, bool):
        assert isinstance(actual, bool) and actual is expected, path
    elif isinstance(expected, float):
        assert isinstance(actual, int | float), path
        assert abs(float(actual) - expected) <= max(1e-9, abs(expected) * 1e-6), path
    elif isinstance(expected, dict):
        assert isinstance(actual, dict), path
        assert set(actual) == set(expected), path
        for key, value in expected.items():
            _assert_close(actual[key], value, f"{path}/{key}")
    elif isinstance(expected, list):
        assert isinstance(actual, list), path
        assert len(actual) == len(expected), path
        for index, value in enumerate(expected):
            _assert_close(actual[index], value, f"{path}/{index}")
    else:
        assert actual == expected, path


def _fixture_names() -> list[str]:
    return sorted(path.name[: -len(".request.json")] for path in FIXTURES.glob("*.request.json"))


def test_contract_fixtures(httpd: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    if os.environ.get("MEMFW_UPDATE_CONTRACT_FIXTURES") == "1":
        _write_fixtures(httpd, tmp_path, monkeypatch)
    names = _fixture_names()
    assert names == sorted(SCENARIOS), names
    for name in names:
        request_path = FIXTURES / f"{name}.request.json"
        response_path = FIXTURES / f"{name}.response.json"
        request_body = json.loads(request_path.read_text(encoding="utf-8"))
        assert request_path.read_bytes() == _canonical(request_body), name
        expected = json.loads(response_path.read_text(encoding="utf-8"))
        assert response_path.read_bytes() == _canonical(expected), name
        assert expected["contract"] == CONTRACT, name
        for path in (request_path, response_path):
            text = path.read_text(encoding="utf-8")
            for forbidden in (
                "/Users/",
                "/home/",
                "/vol1/",
                READ_TOKEN,
                WRITE_TOKEN,
            ):
                assert forbidden not in text, (name, path.name, forbidden)
        assert request_body == SCENARIOS[name]["body"], name
        root = seed_vault(tmp_path / "replay" / f"vault-{name}")
        result = _scenario_response(httpd, name, root, monkeypatch)
        assert result["status"] == SCENARIOS[name]["status"], name
        _assert_close(result["payload"], expected, name)
    covered = {ENDPOINT_OF[name] for name in names}
    assert covered == {"/health", "/search", "/related", "/stage"}
    assert any("as_of" in SCENARIOS[name]["body"] for name in names)
    as_of_response = json.loads(
        (FIXTURES / "search.as_of_superseded_after_t.response.json").read_text(encoding="utf-8")
    )
    assert "mem-old" in {item["id"] for item in as_of_response["results"]}


def _write_fixtures(httpd: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    for name in sorted(SCENARIOS):
        request_path = FIXTURES / f"{name}.request.json"
        response_path = FIXTURES / f"{name}.response.json"
        request_path.write_bytes(_canonical(SCENARIOS[name]["body"]))
        root = seed_vault(tmp_path / "generate" / f"vault-{name}")
        result = _scenario_response(httpd, name, root, monkeypatch)
        response_path.write_bytes(_canonical(result["payload"]))
