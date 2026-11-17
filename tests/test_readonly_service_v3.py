"""``memfw-http/3`` beside ``memfw-http/2`` (roadmap R5 ``/forget``, R6 contract v3).

The service selects the contract per request from the ``MemFW-Contract``
header. These tests run the real HTTP handler:

* the canonical v3 sample set under ``tests/fixtures/contract_v3/`` (replayed
  like the v2 set; regenerate with ``MEMFW_UPDATE_CONTRACT_FIXTURES=1``);
* a dual run of every v2 sample: no header and an explicit ``memfw-http/2``
  header give the same bytes, and the v3 answer of each successful sample is
  the v2 answer plus only the documented v3 additions;
* contract selection on every method and error path;
* ``/forget``: a deletion-request candidate only, write token only, tenant and
  clearance enforced server-side, idempotent on ``request_id``;
* ``/context``: the ``memory_context`` v2 pack, read-only, tenant and
  clearance filtered (the sensitivity key included);
* the v3 evidence fields never name a withheld memory.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

import pytest

import scripts.memfw_readonly_service as service
from memfw.capture.review import list_staged_candidates
from memfw.core.models import MemoryDocument
from tests.test_readonly_service import (
    BASE,
    READ_TOKEN,
    SCENARIOS,
    SEED_DOCUMENTS,
    WRITE_TOKEN,
    _assert_close,
    _canonical,
    _memory,
    _socket_exchange,
    _status_and_body,
    _use_vault,
    build_related,
    httpd,
    seed_vault,
)

__all__ = ["httpd"]  # the fixture, re-exported for pytest

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "contract_v3"
V2_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "contract_v2"
V2 = "memfw-http/2"
V3 = "memfw-http/3"
H3 = {service.CONTRACT_HEADER: V3}


@pytest.fixture(autouse=True)
def disable_network() -> None:
    """These tests talk to a loopback server on purpose."""


def _request(
    base_url: str,
    method: str,
    path: str,
    body: dict | bytes | None = None,
    *,
    token: str | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, bytes, dict[str, str]]:
    raw = _canonical(body) if isinstance(body, dict) else body
    sent = {"Content-Type": "application/json", **(headers or {})}
    if token:
        sent[service.TOKEN_HEADER] = token
    request = urllib.request.Request(base_url + path, data=raw, method=method, headers=sent)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read(), dict(response.headers)
    except urllib.error.HTTPError as error:
        return error.code, error.read(), dict(error.headers)


def _json(
    base_url: str,
    method: str,
    path: str,
    body: dict | bytes | None = None,
    **kwargs: object,
) -> tuple[int, dict]:
    status, raw, _headers = _request(base_url, method, path, body, **kwargs)  # type: ignore[arg-type]
    return status, json.loads(raw.decode("utf-8"))


def _v3(base_url: str, path: str, body: dict, token: str | None = READ_TOKEN) -> tuple[int, dict]:
    return _json(base_url, "POST", path, body, token=token, headers=H3)


def _assert_error(payload: dict, code: str, contract: str) -> None:
    assert payload["contract"] == contract
    assert set(payload) == {"contract", "error"}
    assert payload["error"]["code"] == code


def _tree(root: Path) -> dict[str, str]:
    """sha256 of every file under ``root`` (relative path -> digest)."""

    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


# ---------------------------------------------------------------------------
# Canonical v3 sample set
# ---------------------------------------------------------------------------

FORGET_OK = {
    "request_id": "forget-sample",
    "memory_id": "mem-beta",
    "reason": "Sample deletion request.",
    "tenant": "tenant-a",
    "source": "conductor",
}
#: ``forget-<sha256("tenant-a")[:16]>-<request_id>``: request ids are per tenant.
FORGET_OK_CANDIDATE = "forget-80a707af7dc77ee1-forget-sample"


def _procedural(
    memory_id: str, title: str, body: str, tenant: str, **extra: object
) -> MemoryDocument:
    return MemoryDocument(
        id=memory_id,
        title=title,
        body=body,
        tenant=tenant,
        type="procedural",
        created_at=BASE,
        updated_at=BASE,
        **extra,  # type: ignore[arg-type]
    )


#: ``core`` carries anchors and procedural memories; the sample adds one per tenant.
CONTEXT_DOCUMENTS = [
    *SEED_DOCUMENTS,
    _procedural("proc-a", "Procedure A", "Run the alpha procedure.", "tenant-a"),
    _procedural("proc-b", "Procedure B", "Run the beta procedure.", "tenant-b"),
]

#: A clearance admitting every privacy level (``/context`` needs one configured).
ALL_LEVELS = "public,shared,local-private"

V3_SCENARIOS: dict[str, dict] = {
    "health.ok": {
        "status": 200,
        "method": "GET",
        "path": "/health",
        "body": {},
        "token": None,
        # With a clearance configured, so /context is listed.
        "clearance": ALL_LEVELS,
    },
    "search.ok": {
        "status": 200,
        "method": "POST",
        "path": "/search",
        "body": {"query": "corpus keyword", "limit": 3, "tenant": "tenant-a"},
        "token": READ_TOKEN,
    },
    "search.mode_unavailable": {
        "status": 400,
        "method": "POST",
        "path": "/search",
        "body": {"query": "corpus keyword", "mode": "multihop", "tenant": "tenant-a"},
        "token": READ_TOKEN,
    },
    "related.edges_ok": {
        "status": 200,
        "method": "POST",
        "path": "/related",
        "body": {"memory_id": "mem-alpha", "limit": 3, "tenant": "tenant-a"},
        "token": READ_TOKEN,
    },
    "related.fts_degraded": {
        "status": 200,
        "method": "POST",
        "path": "/related",
        "body": {"memory_id": "mem-alpha", "limit": 3, "tenant": "tenant-a"},
        "token": READ_TOKEN,
        "related": False,
    },
    "context.ok": {
        "status": 200,
        "method": "POST",
        "path": "/context",
        "body": {"tenant": "tenant-a", "limit_tokens": 2000},
        "token": READ_TOKEN,
        "documents": CONTEXT_DOCUMENTS,
        # /context is served only with a clearance; this one admits every level.
        "clearance": ALL_LEVELS,
    },
    "stage.ok": {
        "status": 200,
        "method": "POST",
        "path": "/stage",
        "body": SCENARIOS["stage.ok"]["body"],
        "token": WRITE_TOKEN,
    },
    "forget.ok": {
        "status": 200,
        "method": "POST",
        "path": "/forget",
        "body": FORGET_OK,
        "token": WRITE_TOKEN,
    },
    "forget.duplicate": {
        "status": 200,
        "method": "POST",
        "path": "/forget",
        "body": FORGET_OK,
        "token": WRITE_TOKEN,
        "before": ["forget.ok"],
    },
    "forget.conflict": {
        "status": 409,
        "method": "POST",
        "path": "/forget",
        "body": {**FORGET_OK, "memory_id": "mem-alpha"},
        "token": WRITE_TOKEN,
        "before": ["forget.ok"],
    },
    "forget.not_found": {
        "status": 404,
        "method": "POST",
        "path": "/forget",
        "body": {"request_id": "forget-hidden", "memory_id": "mem-gamma", "tenant": "tenant-a"},
        "token": WRITE_TOKEN,
    },
    "forget.forbidden_read_token": {
        "status": 403,
        "method": "POST",
        "path": "/forget",
        "body": FORGET_OK,
        "token": READ_TOKEN,
    },
    "contract.unknown": {
        "status": 400,
        "method": "POST",
        "path": "/search",
        "body": {"query": "corpus keyword"},
        "token": READ_TOKEN,
        "contract": "memfw-http/9",
    },
}


def _v3_scenario(base_url: str, name: str, root: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    scenario = V3_SCENARIOS[name]
    for previous in scenario.get("before", []):
        _v3_scenario(base_url, previous, root, monkeypatch)
    related = scenario.get("related", True)
    if related and not (root / "related.sqlite3").exists():
        build_related(root)
    _use_vault(monkeypatch, root, related=related)
    if "clearance" in scenario:
        monkeypatch.setenv(service.CLEARANCE_ENV, scenario["clearance"])
    else:
        monkeypatch.delenv(service.CLEARANCE_ENV, raising=False)
    headers = {service.CONTRACT_HEADER: scenario.get("contract", V3)}
    body = None if scenario["method"] == "GET" else scenario["body"]
    status, payload = _json(
        base_url,
        scenario["method"],
        scenario["path"],
        body,
        token=scenario["token"],
        headers=headers,
    )
    if name == "context.ok":
        # The only field that depends on the clock.
        payload["generated_at"] = "<generated_at>"
    return {"status": status, "payload": payload}


def _v3_names() -> list[str]:
    return sorted(path.name[: -len(".request.json")] for path in FIXTURES.glob("*.request.json"))


def test_contract_v3_fixtures(httpd: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    if os.environ.get("MEMFW_UPDATE_CONTRACT_FIXTURES") == "1":
        FIXTURES.mkdir(parents=True, exist_ok=True)
        for name in sorted(V3_SCENARIOS):
            root = seed_vault(
                tmp_path / "generate" / f"vault-{name}", V3_SCENARIOS[name].get("documents")
            )
            result = _v3_scenario(httpd, name, root, monkeypatch)
            (FIXTURES / f"{name}.request.json").write_bytes(_canonical(V3_SCENARIOS[name]["body"]))
            (FIXTURES / f"{name}.response.json").write_bytes(_canonical(result["payload"]))
    names = _v3_names()
    assert names == sorted(V3_SCENARIOS), names
    for name in names:
        request_path = FIXTURES / f"{name}.request.json"
        response_path = FIXTURES / f"{name}.response.json"
        request_body = json.loads(request_path.read_text(encoding="utf-8"))
        assert request_path.read_bytes() == _canonical(request_body), name
        assert request_body == V3_SCENARIOS[name]["body"], name
        expected = json.loads(response_path.read_text(encoding="utf-8"))
        assert response_path.read_bytes() == _canonical(expected), name
        tag = V2 if name == "contract.unknown" else V3
        assert expected["contract"] == tag, name
        for path in (request_path, response_path):
            text = path.read_text(encoding="utf-8")
            for forbidden in ("/Users/", "/home/", READ_TOKEN, WRITE_TOKEN):
                assert forbidden not in text, (name, forbidden)
        root = seed_vault(
            tmp_path / "replay" / f"vault-{name}", V3_SCENARIOS[name].get("documents")
        )
        result = _v3_scenario(httpd, name, root, monkeypatch)
        assert result["status"] == V3_SCENARIOS[name]["status"], name
        _assert_close(result["payload"], expected, name)


# ---------------------------------------------------------------------------
# Dual run: v2 samples under both contracts
# ---------------------------------------------------------------------------

#: v3 additions per v2 endpoint, on each result of a successful answer.
V3_RESULT_ADDITIONS = {"/search": {"recorded_at"}, "/related": {"why", "provenance_type"}}


def _v2_run(
    base_url: str,
    name: str,
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    headers: dict[str, str],
) -> tuple[int, bytes]:
    scenario = SCENARIOS[name]
    for previous in scenario.get("before", []):
        _v2_run(base_url, previous, root, monkeypatch, headers)
    if scenario["related"] and not (root / "related.sqlite3").exists():
        build_related(root)
    _use_vault(monkeypatch, root, related=scenario["related"])
    body = None if scenario["method"] == "GET" else scenario["body"]
    status, raw, _ = _request(
        base_url,
        scenario["method"],
        scenario["path"],
        body,
        token=scenario["token"],
        headers=headers,
    )
    return status, raw


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_v2_samples_dual_run(
    httpd: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    expected = json.loads((V2_FIXTURES / f"{name}.response.json").read_text(encoding="utf-8"))
    scenario = SCENARIOS[name]
    bare = _v2_run(httpd, name, seed_vault(tmp_path / "bare"), monkeypatch, {})
    explicit = _v2_run(
        httpd, name, seed_vault(tmp_path / "explicit"), monkeypatch, {service.CONTRACT_HEADER: V2}
    )
    assert bare[0] == explicit[0] == scenario["status"]
    assert bare[1] == explicit[1], name  # byte-identical
    _assert_close(json.loads(bare[1]), expected, name)

    status, raw = _v2_run(httpd, name, seed_vault(tmp_path / "v3"), monkeypatch, H3)
    assert status == scenario["status"], name
    v3 = json.loads(raw)
    assert v3["contract"] == V3
    v2 = json.loads(bare[1])
    if name == "health.ok":
        assert {key: v3[key] for key in v2 if key != "contract"} == {
            key: value for key, value in v2.items() if key != "contract"
        }
        assert set(v3) - set(v2) == {"contracts", "modes", "endpoints"}
        return
    additions = V3_RESULT_ADDITIONS.get(scenario["path"], set())
    if status == 200 and "results" in v2:
        assert set(v3) == set(v2)
        assert len(v3["results"]) == len(v2["results"])
        for left, right in zip(v2["results"], v3["results"], strict=True):
            assert set(right) - set(left) == additions
            assert {key: right[key] for key in left} == left
        assert {k: v for k, v in v3.items() if k not in ("contract", "results")} == {
            k: v for k, v in v2.items() if k not in ("contract", "results")
        }
    else:
        assert {**v3, "contract": V2} == v2, name


# ---------------------------------------------------------------------------
# Contract selection
# ---------------------------------------------------------------------------


def test_health_advertises_both_contracts(
    httpd: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_vault(monkeypatch, seed_vault(tmp_path / "vault"))
    expected_header = "memfw-http/2, memfw-http/3"
    status, raw, headers = _request(httpd, "GET", "/health")
    assert status == 200
    assert headers[service.CONTRACTS_HEADER] == expected_header
    v2 = json.loads(raw)
    assert v2 == {
        "ok": True,
        "service": "memfw",
        "contract": V2,
        "index_present": True,
        "related_index_present": False,
    }
    status, raw, headers = _request(httpd, "GET", "/health", headers=H3)
    assert status == 200
    assert headers[service.CONTRACTS_HEADER] == expected_header
    v3 = json.loads(raw)
    assert v3["contract"] == V3
    assert v3["contracts"] == [V2, V3]
    assert v3["modes"] == ["fts"]
    # No clearance configured: /context is not served, so not advertised.
    assert v3["endpoints"] == ["/health", "/search", "/related", "/stage", "/forget"]
    monkeypatch.setenv(service.CLEARANCE_ENV, "public,shared")
    _status, v3 = _json(httpd, "GET", "/health", headers=H3)
    assert v3["endpoints"] == ["/health", "/search", "/related", "/stage", "/context", "/forget"]
    monkeypatch.setattr(service, "context_available", lambda: False)
    _status, v3 = _json(httpd, "GET", "/health", headers=H3)
    assert "/context" not in v3["endpoints"]


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/health"),
        ("GET", "/nope"),
        ("POST", "/search"),
        ("POST", "/forget"),
        ("POST", "/context"),
        ("POST", "/nope"),
        ("PUT", "/search"),
        ("DELETE", "/stage"),
        ("PATCH", "/related"),
        ("OPTIONS", "/forget"),
    ],
)
def test_unknown_contract_is_400_everywhere(
    httpd: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method: str, path: str
) -> None:
    root = seed_vault(tmp_path / "vault")
    _use_vault(monkeypatch, root)
    before = _tree(root)
    for value in ("memfw-http/9", "memfw-http/3x", "MEMFW-HTTP/3", "3"):
        status, payload = _json(
            httpd,
            method,
            path,
            b"{}",
            token=WRITE_TOKEN,
            headers={service.CONTRACT_HEADER: value},
        )
        assert status == 400, (method, path, value)
        _assert_error(payload, "bad_request", V2)
        assert payload["error"]["message"] == "contract is not available"
    assert _tree(root) == before


def test_repeated_contract_header_is_400(
    httpd: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_vault(monkeypatch, seed_vault(tmp_path / "vault"))
    port = int(urlsplit(httpd).port or 0)
    raw = _socket_exchange(
        port,
        b"GET /health HTTP/1.1\r\nHost: localhost\r\nMemFW-Contract: memfw-http/3\r\n"
        b"MemFW-Contract: memfw-http/2\r\nConnection: close\r\n\r\n",
    )
    status, body = _status_and_body(raw)
    assert status == 400
    assert json.loads(body)["error"]["message"] == "contract is not available"


def test_head_under_v3_and_unknown_contract(
    httpd: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_vault(monkeypatch, seed_vault(tmp_path / "vault"))
    port = int(urlsplit(httpd).port or 0)
    for header, expected in ((b"memfw-http/3", 404), (b"memfw-http/9", 400)):
        raw = _socket_exchange(
            port,
            b"HEAD /health HTTP/1.1\r\nHost: localhost\r\nMemFW-Contract: "
            + header
            + b"\r\nConnection: close\r\n\r\n",
        )
        status, body = _status_and_body(raw)
        assert (status, body) == (expected, b"")


def test_v3_errors_carry_the_v3_tag(
    httpd: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_vault(monkeypatch, seed_vault(tmp_path / "vault"))
    cases = [
        ("POST", "/search", {"query": "corpus keyword"}, None, 401, "unauthorized"),
        ("POST", "/search", {"query": "q" * 9000}, READ_TOKEN, 413, "too_large"),
        ("POST", "/search", {"query": ""}, READ_TOKEN, 400, "bad_request"),
        ("POST", "/related", {"memory_id": "mem-missing"}, READ_TOKEN, 404, "not_found"),
        ("POST", "/nope", {}, READ_TOKEN, 404, "not_found"),
        ("GET", "/search", None, READ_TOKEN, 404, "not_found"),
        ("PUT", "/search", {}, READ_TOKEN, 404, "not_found"),
        ("OPTIONS", "/context", {}, READ_TOKEN, 404, "not_found"),
        ("POST", "/stage", {"proposal_id": "x"}, READ_TOKEN, 403, "forbidden"),
    ]
    for method, path, body, token, status, code in cases:
        got, payload = _json(httpd, method, path, body, token=token, headers=H3)
        assert got == status, (method, path)
        _assert_error(payload, code, V3)


def test_v2_still_has_no_forget_and_no_context(
    httpd: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = seed_vault(tmp_path / "vault")
    _use_vault(monkeypatch, root)
    before = _tree(root)
    for path, body in (("/forget", FORGET_OK), ("/context", {"tenant": "tenant-a"})):
        for headers in ({}, {service.CONTRACT_HEADER: V2}):
            status, raw, _ = _request(httpd, "POST", path, body, token=WRITE_TOKEN, headers=headers)
            assert status == 404, (path, headers)
            assert json.loads(raw) == {
                "contract": V2,
                "error": {"code": "not_found", "message": "not found"},
            }
    assert _tree(root) == before
    assert not (root / "_candidates").exists()


def test_v3_search_mode_vocabulary(
    httpd: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_vault(monkeypatch, seed_vault(tmp_path / "vault"))
    body = {"query": "corpus keyword", "tenant": "tenant-a"}
    status, payload = _v3(httpd, "/search", {**body, "mode": "fts"})
    assert status == 200 and payload["mode"] == "fts" and payload["results"]
    status, default = _v3(httpd, "/search", body)
    assert status == 200 and default == payload
    for mode in ("multihop", "hybrid", "two_stage", 1, None, ["fts"]):
        status, payload = _v3(httpd, "/search", {**body, "mode": mode})
        assert status == 400, mode
        assert payload["error"]["message"] == "mode is not available"


def test_v3_search_recorded_at(httpd: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = seed_vault(tmp_path / "vault")
    path = root / "memories" / "mem-alpha.md"
    text = path.read_text(encoding="utf-8")
    path.write_text(
        text.replace("---\n", "---\nrecorded_at: '2026-11-06T01:00:00+00:00'\n", 1),
        encoding="utf-8",
    )
    _use_vault(monkeypatch, root)
    status, payload = _v3(httpd, "/search", {"query": "corpus keyword", "tenant": "tenant-a"})
    assert status == 200
    stamps = {item["id"]: item["recorded_at"] for item in payload["results"]}
    assert stamps["mem-alpha"] == "2026-11-06T01:00:00+00:00"
    # No recorded_at on disk: created_at, as MCP memory_search reports it.
    assert stamps["mem-beta"] == BASE.isoformat()
    for item in payload["results"]:
        assert item["why"] in ("lexical", "semantic", "expansion", "related") or item[
            "why"
        ].startswith("alias(")
        assert item["provenance_type"] in ("user_said", "agent_inferred", "imported", "derived")


# ---------------------------------------------------------------------------
# /forget
# ---------------------------------------------------------------------------


def test_forget_writes_only_a_request_candidate(
    httpd: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = seed_vault(tmp_path / "vault")
    _use_vault(monkeypatch, root)
    before = _tree(root)
    status, payload = _v3(httpd, "/forget", FORGET_OK, WRITE_TOKEN)
    assert status == 200
    assert payload == {
        "contract": V3,
        "requested": True,
        "duplicate": False,
        "request_id": "forget-sample",
        "memory_id": "mem-beta",
        "candidate_id": FORGET_OK_CANDIDATE,
        "path": f"_candidates/forget/{FORGET_OK_CANDIDATE}.md",
        "deleted": False,
        "authority": False,
    }
    after = _tree(root)
    added = set(after) - set(before)
    assert added == {f"_candidates/forget/{FORGET_OK_CANDIDATE}.md"}
    assert {key: after[key] for key in before} == before  # memory, events, index untouched
    text = (root / payload["path"]).read_text(encoding="utf-8")
    assert 'type: "forget-request"' in text
    assert 'memory_id: "mem-beta"' in text
    memory_bytes = (root / "memories" / "mem-beta.md").read_bytes()
    assert ("sha256:" + hashlib.sha256(memory_bytes).hexdigest()) in text
    # No body text of the memory is copied into the request.
    assert "alpha corpus keyword companion" not in text
    # ``memfw candidates`` / ``candidate-apply`` never see it.
    assert list_staged_candidates(root) == []
    # The memory is still searchable: nothing was deleted.
    status, search = _v3(httpd, "/search", {"query": "companion", "tenant": "tenant-a"})
    assert [item["id"] for item in search["results"]] == ["mem-beta"]


def test_forget_is_idempotent_and_conflicts_are_409(
    httpd: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = seed_vault(tmp_path / "vault")
    _use_vault(monkeypatch, root)
    status, first = _v3(httpd, "/forget", FORGET_OK, WRITE_TOKEN)
    assert status == 200 and first["duplicate"] is False
    written = (root / first["path"]).read_bytes()
    status, again = _v3(httpd, "/forget", FORGET_OK, WRITE_TOKEN)
    assert status == 200 and again == {**first, "duplicate": True}
    assert (root / first["path"]).read_bytes() == written
    for change in (
        {"memory_id": "mem-alpha"},
        {"reason": "another reason"},
        {"source": "other"},
    ):
        status, payload = _v3(httpd, "/forget", {**FORGET_OK, **change}, WRITE_TOKEN)
        assert status == 409, change
        _assert_error(payload, "conflict", V3)
        assert payload["error"]["message"] == "request exists with different content"
    assert (root / first["path"]).read_bytes() == written
    # A replay still answers duplicate after the memory itself is gone.
    (root / "memories" / "mem-beta.md").unlink()
    status, payload = _v3(httpd, "/forget", FORGET_OK, WRITE_TOKEN)
    assert status == 200 and payload["duplicate"] is True


NOT_FOUND_V3 = {"contract": V3, "error": {"code": "not_found", "message": "memory not found"}}


def test_forget_request_ids_are_per_tenant_no_cross_tenant_oracle(
    httpd: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another tenant replaying a recorded ``request_id`` learns nothing (404, never 409)."""

    root = seed_vault(tmp_path / "vault")
    _use_vault(monkeypatch, root)
    status, first = _v3(httpd, "/forget", FORGET_OK, WRITE_TOKEN)
    assert status == 200 and first["duplicate"] is False
    written = _tree(root)
    # Same request_id, same (tenant-a) memory, from another tenant or untenanted.
    for tenant in ("", "tenant-b"):
        for change in ({}, {"reason": "another reason"}, {"memory_id": "mem-alpha"}):
            body = {**FORGET_OK, **change, "tenant": tenant}
            status, payload = _v3(httpd, "/forget", body, WRITE_TOKEN)
            assert (status, payload) == (404, NOT_FOUND_V3), (tenant, change)
    assert _tree(root) == written
    # The same request_id is free for another tenant's own memory: no pre-claim.
    body = {**FORGET_OK, "memory_id": "mem-gamma", "tenant": "tenant-b"}
    status, other = _v3(httpd, "/forget", body, WRITE_TOKEN)
    assert status == 200 and other["duplicate"] is False
    assert other["candidate_id"] != first["candidate_id"]
    assert other["candidate_id"] == service.forget_candidate_id("tenant-b", "forget-sample")
    # After the memory is gone, another tenant still gets 404, not duplicate.
    (root / "memories" / "mem-beta.md").unlink()
    status, payload = _v3(httpd, "/forget", {**FORGET_OK, "tenant": ""}, WRITE_TOKEN)
    assert (status, payload) == (404, NOT_FOUND_V3)
    status, payload = _v3(httpd, "/forget", FORGET_OK, WRITE_TOKEN)
    assert status == 200 and payload["duplicate"] is True
    # ``memfw forget requests`` still lists both tenant-scoped files.
    from memfw.curation.residue import list_forget_requests

    listed = {(row["tenant"], row["memory_id"]) for row in list_forget_requests(root)}
    assert listed == {("tenant-a", "mem-beta"), ("tenant-b", "mem-gamma")}


def test_forget_lower_clearance_replay_is_404(
    httpd: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A same-tenant process whose clearance withholds the memory gets 404, not duplicate."""

    documents = [
        *SEED_DOCUMENTS,
        _memory("mem-private", "Private note", "private corpus keyword", tenant="tenant-a"),
        MemoryDocument(
            id="mem-shared",
            title="Shared note",
            body="shared corpus keyword",
            tenant="tenant-a",
            privacy="shared",
            created_at=BASE,
            updated_at=BASE,
        ),
    ]
    root = seed_vault(tmp_path / "vault", documents)
    _use_vault(monkeypatch, root)
    body = {"request_id": "private-1", "memory_id": "mem-private", "tenant": "tenant-a"}
    monkeypatch.setenv(service.CLEARANCE_ENV, "public,shared,local-private")
    status, first = _v3(httpd, "/forget", body, WRITE_TOKEN)
    assert status == 200 and first["duplicate"] is False
    written = _tree(root)
    monkeypatch.setenv(service.CLEARANCE_ENV, "public,shared")
    for change in ({}, {"reason": "another reason"}):
        status, payload = _v3(httpd, "/forget", {**body, **change}, WRITE_TOKEN)
        assert (status, payload) == (404, NOT_FOUND_V3), change
    # The request id is the tenant's own: a visible memory under it is still
    # 409, an answer that names no memory.
    status, payload = _v3(httpd, "/forget", {**body, "memory_id": "mem-shared"}, WRITE_TOKEN)
    assert status == 409
    assert _tree(root) == written
    monkeypatch.setenv(service.CLEARANCE_ENV, "public,shared,local-private")
    status, payload = _v3(httpd, "/forget", body, WRITE_TOKEN)
    assert status == 200 and payload["duplicate"] is True


def test_forget_hidden_targets_are_indistinguishable(
    httpd: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    documents = [
        *SEED_DOCUMENTS,
        _memory("mem-private", "Private note", "private corpus keyword", tenant="tenant-a"),
        MemoryDocument(
            id="mem-shared",
            title="Shared note",
            body="shared corpus keyword",
            tenant="tenant-a",
            privacy="shared",
            created_at=BASE,
            updated_at=BASE,
        ),
    ]
    root = seed_vault(tmp_path / "vault", documents)
    _use_vault(monkeypatch, root)
    monkeypatch.setenv(service.CLEARANCE_ENV, "public,shared")
    answers = []
    for index, memory_id in enumerate(("mem-missing", "mem-gamma", "mem-private")):
        status, payload = _v3(
            httpd,
            "/forget",
            {"request_id": f"hidden-{index}", "memory_id": memory_id, "tenant": "tenant-a"},
            WRITE_TOKEN,
        )
        answers.append((status, payload))
    assert {json.dumps(answer, sort_keys=True) for answer in answers} == {
        json.dumps(
            (404, {"contract": V3, "error": {"code": "not_found", "message": "memory not found"}}),
            sort_keys=True,
        )
    }
    assert not (root / "_candidates" / "forget").exists() or not any(
        (root / "_candidates" / "forget").iterdir()
    )
    status, payload = _v3(
        httpd,
        "/forget",
        {"request_id": "visible", "memory_id": "mem-shared", "tenant": "tenant-a"},
        WRITE_TOKEN,
    )
    assert status == 200 and payload["requested"] is True


def test_forget_reused_request_id_is_no_existence_oracle(
    httpd: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A recorded ``request_id`` reused with a probe memory id: missing, another
    tenant's and clearance-withheld all answer byte-identical 404s (never 409)."""

    documents = [
        *SEED_DOCUMENTS,
        _memory("mem-private", "Private note", "private corpus keyword", tenant="tenant-a"),
        MemoryDocument(
            id="mem-shared",
            title="Shared note",
            body="shared corpus keyword",
            tenant="tenant-a",
            privacy="shared",
            created_at=BASE,
            updated_at=BASE,
        ),
    ]
    root = seed_vault(tmp_path / "vault", documents)
    _use_vault(monkeypatch, root)

    def probe(request_id: str, memory_id: str) -> tuple[int, bytes]:
        body = {"request_id": request_id, "memory_id": memory_id, "tenant": "tenant-a"}
        status, raw, _headers = _request(
            httpd, "POST", "/forget", body, token=WRITE_TOKEN, headers=H3
        )
        return status, raw

    # A never-recorded id on a missing memory: the baseline 404.
    monkeypatch.setenv(service.CLEARANCE_ENV, "public,shared")
    baseline = probe("never-recorded", "mem-missing")
    assert baseline[0] == 404
    assert json.loads(baseline[1]) == NOT_FOUND_V3

    # Same process: record on a visible memory, then reuse the id.
    status, first = _v3(
        httpd,
        "/forget",
        {"request_id": "probe-1", "memory_id": "mem-shared", "tenant": "tenant-a"},
        WRITE_TOKEN,
    )
    assert status == 200 and first["duplicate"] is False
    written = _tree(root)
    for memory_id in ("mem-missing", "mem-gamma", "mem-private"):
        assert probe("probe-1", memory_id) == baseline, memory_id
    assert _tree(root) == written

    # A lower-clearance process reusing an id recorded under a higher one:
    # the recorded memory itself is now withheld too.
    monkeypatch.setenv(service.CLEARANCE_ENV, "public,shared,local-private")
    status, second = _v3(
        httpd,
        "/forget",
        {"request_id": "probe-2", "memory_id": "mem-private", "tenant": "tenant-a"},
        WRITE_TOKEN,
    )
    assert status == 200 and second["duplicate"] is False
    written = _tree(root)
    monkeypatch.setenv(service.CLEARANCE_ENV, "public,shared")
    for memory_id in ("mem-missing", "mem-gamma", "mem-private", "mem-beta"):
        assert probe("probe-2", memory_id) == baseline, memory_id
    assert _tree(root) == written
    # A visible memory under the tenant's own id is still the 409 that names nothing.
    status, _raw = probe("probe-2", "mem-shared")
    assert status == 409
    # An exact replay after the recorded memory is gone is still duplicate.
    (root / "memories" / "mem-shared.md").unlink()
    status, payload = _v3(
        httpd,
        "/forget",
        {"request_id": "probe-1", "memory_id": "mem-shared", "tenant": "tenant-a"},
        WRITE_TOKEN,
    )
    assert status == 200 and payload["duplicate"] is True


def _clearance_documents() -> list[MemoryDocument]:
    return [
        *SEED_DOCUMENTS,
        _memory("mem-private", "Private note", "private corpus keyword", tenant="tenant-a"),
        MemoryDocument(
            id="mem-shared",
            title="Shared note",
            body="shared corpus keyword",
            tenant="tenant-a",
            privacy="shared",
            created_at=BASE,
            updated_at=BASE,
        ),
    ]


def _forget_probe(base_url: str, body: dict) -> tuple[int, bytes]:
    status, raw, _headers = _request(
        base_url, "POST", "/forget", body, token=WRITE_TOKEN, headers=H3
    )
    return status, raw


def test_forget_deleted_target_replay_needs_the_recorded_clearance(
    httpd: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review #78 round 3: a high-clearance process records R on private M, M
    is deleted; a low-clearance exact replay of R answers the same bytes as a
    never-recorded missing id, a same-clearance replay is still duplicate."""

    root = seed_vault(tmp_path / "vault", _clearance_documents())
    _use_vault(monkeypatch, root)
    body = {"request_id": "private-r", "memory_id": "mem-private", "tenant": "tenant-a"}
    monkeypatch.setenv(service.CLEARANCE_ENV, "public,shared,local-private")
    status, first = _v3(httpd, "/forget", body, WRITE_TOKEN)
    assert status == 200 and first["duplicate"] is False
    recorded = root / first["path"]
    assert 'target_privacy: "local-private"' in recorded.read_text(encoding="utf-8")
    (root / "memories" / "mem-private.md").unlink()
    written = _tree(root)

    monkeypatch.setenv(service.CLEARANCE_ENV, "public,shared")
    baseline = _forget_probe(
        httpd, {"request_id": "never-recorded", "memory_id": "mem-missing", "tenant": "tenant-a"}
    )
    assert baseline[0] == 404 and json.loads(baseline[1]) == NOT_FOUND_V3
    assert _forget_probe(httpd, body) == baseline
    assert _tree(root) == written

    monkeypatch.setenv(service.CLEARANCE_ENV, "public,shared,local-private")
    status, again = _v3(httpd, "/forget", body, WRITE_TOKEN)
    assert status == 200 and again == {**first, "duplicate": True}
    # No clearance configured admits every level, as before.
    monkeypatch.delenv(service.CLEARANCE_ENV)
    status, again = _v3(httpd, "/forget", body, WRITE_TOKEN)
    assert status == 200 and again["duplicate"] is True
    assert _tree(root) == written


def test_forget_records_the_sensitivity_level(
    httpd: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``target_privacy`` is the effective level: ``sensitivity: private`` raises it."""

    root = seed_vault(tmp_path / "vault", _clearance_documents())
    path = root / "memories" / "mem-shared.md"
    path.write_text(
        path.read_text(encoding="utf-8").replace("---\n", "---\nsensitivity: private\n", 1),
        encoding="utf-8",
    )
    _use_vault(monkeypatch, root)
    body = {"request_id": "sensitive-r", "memory_id": "mem-shared", "tenant": "tenant-a"}
    status, first = _v3(httpd, "/forget", body, WRITE_TOKEN)
    assert status == 200
    assert 'target_privacy: "local-private"' in (root / first["path"]).read_text(encoding="utf-8")
    path.unlink()
    monkeypatch.setenv(service.CLEARANCE_ENV, "public,shared")
    baseline = _forget_probe(
        httpd, {"request_id": "never-recorded", "memory_id": "mem-missing", "tenant": "tenant-a"}
    )
    assert baseline[0] == 404
    assert _forget_probe(httpd, body) == baseline


def test_forget_legacy_request_without_level_fails_closed(
    httpd: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A request staged before ``target_privacy`` counts as ``unknown``."""

    root = seed_vault(tmp_path / "vault", _clearance_documents())
    _use_vault(monkeypatch, root)
    body = {"request_id": "legacy-r", "memory_id": "mem-shared", "tenant": "tenant-a"}
    monkeypatch.setenv(service.CLEARANCE_ENV, "public,shared")
    status, first = _v3(httpd, "/forget", body, WRITE_TOKEN)
    assert status == 200 and first["duplicate"] is False
    recorded = root / first["path"]
    text = recorded.read_text(encoding="utf-8")
    assert 'target_privacy: "shared"\n' in text
    recorded.write_text(text.replace('target_privacy: "shared"\n', ""), encoding="utf-8")
    (root / "memories" / "mem-shared.md").unlink()
    baseline = _forget_probe(
        httpd, {"request_id": "never-recorded", "memory_id": "mem-missing", "tenant": "tenant-a"}
    )
    assert baseline[0] == 404
    # Low clearance, and even one admitting every level: only no clearance
    # admits ``unknown``.
    for clearance in ("public,shared", "public,shared,local-private"):
        monkeypatch.setenv(service.CLEARANCE_ENV, clearance)
        assert _forget_probe(httpd, body) == baseline, clearance
    monkeypatch.delenv(service.CLEARANCE_ENV)
    status, again = _v3(httpd, "/forget", body, WRITE_TOKEN)
    assert status == 200 and again["duplicate"] is True
    # The listing shows the missing field as ``None``.
    from memfw.curation.residue import list_forget_requests

    assert [row["target_privacy"] for row in list_forget_requests(root)] == [None]


def test_forget_tenant_is_bound_server_side(
    httpd: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = seed_vault(tmp_path / "vault")
    _use_vault(monkeypatch, root)
    monkeypatch.setenv(service.TENANT_ENV, "tenant-a")
    status, payload = _v3(httpd, "/forget", {**FORGET_OK, "tenant": "tenant-b"}, WRITE_TOKEN)
    assert status == 403
    _assert_error(payload, "forbidden", V3)
    # Omitted tenant: the bound one. Another tenant's memory stays hidden.
    body = {"request_id": "bound-1", "memory_id": "mem-gamma"}
    status, payload = _v3(httpd, "/forget", body, WRITE_TOKEN)
    assert status == 404
    body = {"request_id": "bound-2", "memory_id": "mem-beta"}
    status, payload = _v3(httpd, "/forget", body, WRITE_TOKEN)
    assert status == 200
    text = (root / payload["path"]).read_text(encoding="utf-8")
    assert 'tenant: "tenant-a"' in text
    status, payload = _v3(httpd, "/forget", {**body, "tenant": 7}, WRITE_TOKEN)
    assert status == 400


def test_forget_authentication(httpd: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = seed_vault(tmp_path / "vault")
    _use_vault(monkeypatch, root)
    status, payload = _v3(httpd, "/forget", FORGET_OK, None)
    assert status == 401
    status, payload = _v3(httpd, "/forget", FORGET_OK, "wrong-token")
    assert status == 401
    status, payload = _v3(httpd, "/forget", FORGET_OK, READ_TOKEN)
    assert status == 403
    assert payload["error"]["message"] == "write credential required"
    monkeypatch.delenv("MEMFW_WRITE_TOKEN")
    status, payload = _v3(httpd, "/forget", FORGET_OK, READ_TOKEN)
    assert status == 403
    assert payload["error"]["message"] == "write access is not configured"
    assert not (root / "_candidates").exists()


@pytest.mark.parametrize(
    "change",
    [
        {"request_id": ""},
        {"request_id": "a/b"},
        {"request_id": "x" * 129},
        {"request_id": 5},
        {"memory_id": "../vault"},
        {"memory_id": "mem.beta"},
        {"memory_id": ""},
        {"memory_id": None},
        {"reason": "r" * 501},
        {"reason": 3},
        {"source": "two\nlines"},
        {"judge_endpoint": "http://127.0.0.1:1"},
    ],
)
def test_forget_validation_is_400(
    httpd: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: dict
) -> None:
    root = seed_vault(tmp_path / "vault")
    _use_vault(monkeypatch, root)
    status, payload = _v3(httpd, "/forget", {**FORGET_OK, **change}, WRITE_TOKEN)
    assert status == 400, change
    _assert_error(payload, "bad_request", V3)
    assert not (root / "_candidates").exists()


def test_forget_pending_requests_are_capped(
    httpd: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = seed_vault(tmp_path / "vault")
    _use_vault(monkeypatch, root)
    monkeypatch.setattr(service, "MAX_PENDING_FORGET_REQUESTS", 2)
    for index in range(2):
        body = {**FORGET_OK, "request_id": f"cap-{index}"}
        status, payload = _v3(httpd, "/forget", body, WRITE_TOKEN)
        assert status == 200 and payload["duplicate"] is False
    before = _tree(root)
    status, payload = _v3(httpd, "/forget", {**FORGET_OK, "request_id": "cap-2"}, WRITE_TOKEN)
    assert status == 429
    assert payload == {
        "contract": V3,
        "error": {"code": "too_many_requests", "message": "too many pending forget requests"},
    }
    assert _tree(root) == before
    # A replay of a recorded request is still answered; a hidden target is still 404.
    status, payload = _v3(httpd, "/forget", {**FORGET_OK, "request_id": "cap-0"}, WRITE_TOKEN)
    assert status == 200 and payload["duplicate"] is True
    body = {"request_id": "cap-3", "memory_id": "mem-gamma", "tenant": "tenant-a"}
    status, payload = _v3(httpd, "/forget", body, WRITE_TOKEN)
    assert (status, payload) == (404, NOT_FOUND_V3)
    # The cap is per tenant: another tenant's queue is its own.
    body = {"request_id": "cap-0", "memory_id": "mem-gamma", "tenant": "tenant-b"}
    status, payload = _v3(httpd, "/forget", body, WRITE_TOKEN)
    assert status == 200 and payload["duplicate"] is False
    # ... and does not count toward tenant-a's.
    status, payload = _v3(httpd, "/forget", {**FORGET_OK, "request_id": "cap-2"}, WRITE_TOKEN)
    assert status == 429
    # Once a human clears one, a new request fits again.
    prefix = service.forget_candidate_id("tenant-a", "")
    next((root / "_candidates" / "forget").glob(f"{prefix}*.md")).unlink()
    status, payload = _v3(httpd, "/forget", {**FORGET_OK, "request_id": "cap-2"}, WRITE_TOKEN)
    assert status == 200 and payload["duplicate"] is False


def test_forget_refuses_a_symlinked_directory(tmp_path: Path) -> None:
    root = seed_vault(tmp_path / "vault")
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "_candidates").mkdir()
    (root / "_candidates" / "forget").symlink_to(outside, target_is_directory=True)
    with pytest.raises(service.ServiceError) as raised:
        service.forget_request(root, FORGET_OK, tenant="tenant-a")
    assert raised.value.status == 500
    assert list(outside.iterdir()) == []


def test_forget_converges_under_contention(tmp_path: Path) -> None:
    root = seed_vault(tmp_path / "vault")
    results: list[object] = []
    lock = threading.Lock()

    def worker(memory_id: str) -> None:
        try:
            outcome: object = service.forget_request(
                root, {**FORGET_OK, "memory_id": memory_id}, tenant="tenant-a"
            )
        except service.ServiceError as error:
            outcome = error.status
        with lock:
            results.append(outcome)

    threads = [
        threading.Thread(target=worker, args=("mem-beta" if index % 2 else "mem-alpha",))
        for index in range(8)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    files = list((root / "_candidates" / "forget").glob("*.md"))
    assert len(files) == 1
    winners = [item for item in results if isinstance(item, dict) and not item["duplicate"]]
    assert len(winners) == 1
    assert not list((root / "_candidates" / "forget").glob(".*.tmp"))


# ---------------------------------------------------------------------------
# /context
# ---------------------------------------------------------------------------


def _privacy_vault(root: Path) -> Path:
    documents = [
        MemoryDocument(
            id="ctx-public",
            title="Public context note",
            body="public context body",
            tenant="tenant-a",
            type="procedural",
            privacy="public",
            related=["ctx-private"],
            created_at=BASE,
            updated_at=BASE,
        ),
        MemoryDocument(
            id="ctx-private",
            title="Private context note",
            body="private context body secret-marker",
            tenant="tenant-a",
            type="procedural",
            privacy="local-private",
            created_at=BASE,
            updated_at=BASE,
        ),
        MemoryDocument(
            id="ctx-sensitive",
            title="Sensitive context note",
            body="sensitive context body sensitive-marker",
            tenant="tenant-a",
            type="procedural",
            privacy="public",
            created_at=BASE,
            updated_at=BASE,
        ),
        MemoryDocument(
            id="ctx-other",
            title="Other tenant context note",
            body="other context body other-marker",
            tenant="tenant-b",
            type="procedural",
            privacy="public",
            created_at=BASE,
            updated_at=BASE,
        ),
    ]
    seed_vault(root, documents)
    path = root / "memories" / "ctx-sensitive.md"
    path.write_text(
        path.read_text(encoding="utf-8").replace("---\n", "---\nsensitivity: private\n", 1),
        encoding="utf-8",
    )
    from memfw.core.repository import Repository
    from memfw.index import MemoryIndex

    MemoryIndex(root / "index.sqlite3").rebuild(Repository(root))
    return root


def test_context_is_the_v2_pack_filtered_and_read_only(
    httpd: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _privacy_vault(tmp_path / "vault")
    _use_vault(monkeypatch, root)
    monkeypatch.setenv(service.CLEARANCE_ENV, "public,shared")
    before = _tree(root)
    status, payload = _v3(httpd, "/context", {"tenant": "tenant-a"})
    assert status == 200
    assert payload["contract"] == V3 and payload["v"] == 2
    assert payload["policy"] == "strict"
    ids = [entry["id"] for slot in payload["slots"] for entry in slot["entries"]]
    assert ids == ["ctx-public"]
    raw = json.dumps(payload)
    for hidden in ("ctx-private", "ctx-sensitive", "ctx-other", "marker"):
        assert hidden not in raw, hidden
    assert _tree(root) == before
    # Unchanged pack: header only.
    status, same = _v3(
        httpd, "/context", {"tenant": "tenant-a", "if_version": payload["pack_version"]}
    )
    assert status == 200 and same["unchanged"] is True and "slots" not in same
    # A clearance admitting every level: the tenant still bounds it.
    monkeypatch.setenv(service.CLEARANCE_ENV, ALL_LEVELS)
    status, payload = _v3(httpd, "/context", {"tenant": "tenant-a", "policy": "compat_fts"})
    ids = {entry["id"] for slot in payload["slots"] for entry in slot["entries"]}
    assert ids == {"ctx-public", "ctx-private", "ctx-sensitive"}
    assert _tree(root) == before


def test_context_needs_a_configured_clearance(
    httpd: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unset ``MEMFW_READONLY_CLEARANCE``: /context is 404 like an unknown path, unadvertised."""

    root = _privacy_vault(tmp_path / "vault")
    _use_vault(monkeypatch, root)
    for unset in (None, "", "  "):
        if unset is None:
            monkeypatch.delenv(service.CLEARANCE_ENV, raising=False)
        else:
            monkeypatch.setenv(service.CLEARANCE_ENV, unset)
        for token in (READ_TOKEN, WRITE_TOKEN, None):
            status, payload = _v3(httpd, "/context", {"tenant": "tenant-a"}, token)
            assert status == 404, (unset, token)
            assert payload == {
                "contract": V3,
                "error": {"code": "not_found", "message": "not found"},
            }
        _status, health = _json(httpd, "GET", "/health", headers=H3)
        assert "/context" not in health["endpoints"]
    # /search and /related keep the pre-MM0 behaviour without a clearance.
    monkeypatch.delenv(service.CLEARANCE_ENV, raising=False)
    status, _payload = _v3(httpd, "/search", {"query": "context body", "tenant": "tenant-a"})
    assert status == 200
    monkeypatch.setenv(service.CLEARANCE_ENV, "public")
    status, payload = _v3(httpd, "/context", {"tenant": "tenant-a"})
    assert status == 200 and payload["v"] == 2


def test_context_tenant_binding(
    httpd: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _privacy_vault(tmp_path / "vault")
    _use_vault(monkeypatch, root)
    monkeypatch.setenv(service.CLEARANCE_ENV, ALL_LEVELS)
    monkeypatch.setenv(service.TENANT_ENV, "tenant-a")
    status, payload = _v3(httpd, "/context", {"tenant": "tenant-b"})
    assert status == 403
    status, payload = _v3(httpd, "/context", {})
    ids = {entry["id"] for slot in payload["slots"] for entry in slot["entries"]}
    assert "ctx-other" not in ids and ids


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ({"slots": ["handoff"]}, "slot is not available"),
        ({"slots": ["core", "topics"]}, "slot is not available"),
        ({"policy": "loose"}, "policy must be compat_fts or strict"),
        ({"policy": 1}, "policy must be compat_fts or strict"),
        ({"limit_tokens": True}, "limit_tokens must be an integer"),
        ({"limit_tokens": "2000"}, "limit_tokens must be an integer"),
        ({"v": 1}, "context version is not available"),
        ({"v": True}, "context version is not available"),
        ({"if_version": "abc"}, "if_version must be a pack_version"),
        ({"embedding_url": "http://127.0.0.1:1"}, "endpoint fields are not accepted"),
    ],
)
def test_context_validation(
    httpd: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: dict, message: str
) -> None:
    _use_vault(monkeypatch, seed_vault(tmp_path / "vault"))
    monkeypatch.setenv(service.CLEARANCE_ENV, ALL_LEVELS)
    status, payload = _v3(httpd, "/context", {"tenant": "tenant-a", **body})
    assert status == 400, body
    assert payload["error"]["message"] == message


def test_context_without_memfw_is_404(
    httpd: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_vault(monkeypatch, seed_vault(tmp_path / "vault"))
    monkeypatch.setenv(service.CLEARANCE_ENV, ALL_LEVELS)
    monkeypatch.setattr(service, "_context_module", lambda: None)
    status, payload = _v3(httpd, "/context", {"tenant": "tenant-a"})
    assert status == 404
    assert payload["error"]["message"] == "context is not available"


def test_context_limit_is_clamped(
    httpd: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_vault(monkeypatch, seed_vault(tmp_path / "vault"))
    monkeypatch.setenv(service.CLEARANCE_ENV, ALL_LEVELS)
    for asked, served in ((0, 1), (-5, 1), (10**9, service.MAX_CONTEXT_TOKENS)):
        status, payload = _v3(httpd, "/context", {"tenant": "tenant-a", "limit_tokens": asked})
        assert status == 200 and payload["limit_tokens"] == served


# ---------------------------------------------------------------------------
# Evidence fields never name a withheld memory
# ---------------------------------------------------------------------------


def test_v3_evidence_fields_leak_no_withheld_id(
    httpd: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _privacy_vault(tmp_path / "vault")
    assert build_related(root).is_file()
    _use_vault(monkeypatch, root, related=True)
    monkeypatch.setenv(service.CLEARANCE_ENV, "public,shared")
    for path, body in (
        ("/search", {"query": "context body", "tenant": "tenant-a", "limit": 50}),
        ("/related", {"memory_id": "ctx-public", "tenant": "tenant-a"}),
        ("/context", {"tenant": "tenant-a"}),
    ):
        status, payload = _v3(httpd, path, body)
        assert status == 200, path
        raw = json.dumps(payload)
        for hidden in ("ctx-private", "ctx-sensitive", "ctx-other", "marker"):
            assert hidden not in raw, (path, hidden)
    status, payload = _v3(httpd, "/related", {"memory_id": "ctx-private", "tenant": "tenant-a"})
    assert status == 404
    status, payload = _v3(httpd, "/related", {"memory_id": "ctx-sensitive", "tenant": "tenant-a"})
    assert status == 404


def test_v3_related_carries_why_and_provenance_type(
    httpd: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = seed_vault(tmp_path / "vault")
    build_related(root)
    body = {"memory_id": "mem-alpha", "tenant": "tenant-a"}
    _use_vault(monkeypatch, root, related=True)
    status, edges = _v3(httpd, "/related", body)
    assert status == 200 and edges["mode"] == "edges" and edges["results"]
    by_id = {item["id"]: item for item in edges["results"]}
    assert by_id["mem-beta"]["why"] == "related"
    assert by_id["mem-beta"]["provenance_type"] == "imported"  # source: notes
    _use_vault(monkeypatch, root, related=False)
    status, lexical = _v3(httpd, "/related", body)
    assert status == 200 and lexical["mode"] == "fts" and lexical["results"]
    assert {item["why"] for item in lexical["results"]} == {"lexical"}
    # The v2 answers keep their keys.
    _use_vault(monkeypatch, root, related=True)
    status, v2 = _json(httpd, "POST", "/related", body, token=READ_TOKEN)
    assert all(set(item) == {"id", "title", "score", "reasons"} for item in v2["results"])


# ---------------------------------------------------------------------------
# Golden raw bytes: v2 answers equal those of the service before v3
# ---------------------------------------------------------------------------

RAW_V2_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "contract_v2_raw"
RAW_CASES: dict[str, dict] = json.loads((RAW_V2_FIXTURES / "cases.json").read_text("utf-8"))
#: The one header v3 adds to a v2 answer, on ``/health`` only.
V3_HEALTH_HEADER = f"{service.CONTRACTS_HEADER}: memfw-http/2, memfw-http/3".encode()


def _raw_request(name: str, *, explicit_v2: bool) -> bytes:
    raw = (RAW_V2_FIXTURES / f"{name}.request.http").read_bytes()
    raw = raw.replace(b"{read_token}", READ_TOKEN.encode()).replace(
        b"{write_token}", WRITE_TOKEN.encode()
    )
    if RAW_CASES[name]["headers_only"]:
        # Answered 413 from Content-Length alone; an unread body would turn
        # the server's close into a reset.
        raw = raw.partition(b"\r\n\r\n")[0] + b"\r\n\r\n"
    if explicit_v2:
        raw = raw.replace(b"\r\n", f"\r\n{service.CONTRACT_HEADER}: {V2}\r\n".encode(), 1)
    return raw


def _raw_exchange(port: int, raw: bytes) -> bytes:
    import socket

    with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
        sock.sendall(raw)
        chunks = []
        while piece := sock.recv(65536):
            chunks.append(piece)
    head, separator, body = b"".join(chunks).partition(b"\r\n\r\n")
    lines = [line for line in head.split(b"\r\n") if not line.lower().startswith(b"date:")]
    return b"\r\n".join(lines) + separator + body


#: ``"score"`` / ``"raw_score"``: FTS floats, whose last bits differ between
#: SQLite builds and platforms.
_SCORE_FIELD = re.compile(rb'"(score|raw_score)": (-?[0-9][0-9.eE+-]*)')


def _portable_response(response: bytes, name: str) -> tuple[bytes, bytes, list[float]]:
    """Head and body with the two platform-dependent parts made comparable.

    * the reason phrase is dropped (``413`` is ``Request Entity Too Large``
      before Python 3.13 and ``Content Too Large`` from 3.13 on); the
      protocol and the status code stay;
    * every ``score`` / ``raw_score`` number is replaced by a placeholder and
      returned, so the caller checks it is close (relative 1e-9) to its
      golden counterpart; ``Content-Length`` is re-stated for the normalized
      body after checking it matched the real one.

    Every other byte, headers included, is compared exactly.
    """

    head, _, body = response.partition(b"\r\n\r\n")
    lines = head.split(b"\r\n")
    protocol, code, *_reason = lines[0].split(b" ", 2)
    lines[0] = protocol + b" " + code
    scores = [float(match.group(2)) for match in _SCORE_FIELD.finditer(body)]
    normalized = _SCORE_FIELD.sub(rb'"\1": "<score>"', body)
    for index, line in enumerate(lines):
        if line.lower().startswith(b"content-length:"):
            assert int(line.split(b":", 1)[1]) == len(body), name
            lines[index] = b"Content-Length: " + str(len(normalized)).encode()
    return b"\r\n".join(lines), normalized, scores


@pytest.mark.parametrize("name", sorted(RAW_CASES))
@pytest.mark.parametrize("explicit_v2", [False, True], ids=["bare", "explicit-v2"])
def test_v2_raw_bytes_equal_the_pre_v3_service(
    httpd: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, explicit_v2: bool
) -> None:
    """Status line, headers (``Date`` aside) and body bytes equal the pre-v3 goldens.

    ``tests/fixtures/contract_v2_raw/`` was recorded from the service as it
    was before ``memfw-http/3`` (see its README). The only allowed difference
    is the documented ``MemFW-Contracts`` header on ``/health``.
    """

    for variable in (service.TENANT_ENV, service.CLEARANCE_ENV):
        monkeypatch.delenv(variable, raising=False)
    case = RAW_CASES[name]
    root = seed_vault(tmp_path / "vault")
    build_related(root)
    _use_vault(monkeypatch, root, related=case["related"])
    port = int(urlsplit(httpd).port or 0)
    for previous in case["before"]:
        _raw_exchange(port, _raw_request(previous, explicit_v2=explicit_v2))
    got = _raw_exchange(port, _raw_request(name, explicit_v2=explicit_v2))
    expected = (RAW_V2_FIXTURES / f"{name}.response.http").read_bytes()
    got_head, got_body, got_scores = _portable_response(got, name)
    want_head, want_body, want_scores = _portable_response(expected, name)
    assert len(got_scores) == len(want_scores), name
    for left, right in zip(got_scores, want_scores, strict=True):
        assert math.isclose(left, right, rel_tol=1e-9, abs_tol=1e-15), (name, left, right)
    assert got_body == want_body, name
    got_lines = got_head.split(b"\r\n")
    request_line = (RAW_V2_FIXTURES / f"{name}.request.http").read_bytes().split(b"\r\n", 1)[0]
    if request_line == b"GET /health HTTP/1.1":
        assert V3_HEALTH_HEADER in got_lines, name
        got_lines.remove(V3_HEALTH_HEADER)
    assert got_lines == want_head.split(b"\r\n"), name


def test_v2_raw_fixtures_are_complete_and_clean() -> None:
    names = {path.name.split(".request.http")[0] for path in RAW_V2_FIXTURES.glob("*.request.http")}
    assert names == set(RAW_CASES)
    assert set(SCENARIOS) <= names  # every v2 sample, plus error and method cases
    for path in RAW_V2_FIXTURES.iterdir():
        data = path.read_bytes()
        for forbidden in (
            b"/Users/",
            b"/home/",
            b"/private/",
            READ_TOKEN.encode(),
            WRITE_TOKEN.encode(),
        ):
            assert forbidden not in data, (path.name, forbidden)
