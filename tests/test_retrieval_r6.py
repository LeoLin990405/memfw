"""R6: memfw-http/3 beside v2. An absent contract header stays v2."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

import scripts.memfw_readonly_service as service
from tests.test_readonly_service import READ_TOKEN, WRITE_TOKEN, seed_vault


# The suite-wide socket guard would refuse this loopback client.
@pytest.fixture(autouse=True)
def disable_network() -> None:
    """These tests talk to the in-process readonly service on loopback."""


@pytest.fixture
def httpd(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MEMFW_READONLY_TOKEN", READ_TOKEN)
    monkeypatch.setenv("MEMFW_WRITE_TOKEN", WRITE_TOKEN)
    monkeypatch.delenv("MEMFW_RELATED_DB", raising=False)
    server = ThreadingHTTPServer(("127.0.0.1", 0), service.Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _exchange(
    base_url: str,
    path: str,
    payload: dict,
    token: str | None,
    contract: str | None = None,
) -> tuple[int, dict]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers[service.TOKEN_HEADER] = token
    if contract is not None:
        headers[service.CONTRACT_HEADER] = contract
    request = urllib.request.Request(
        base_url + path,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers=headers,
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))


def _vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = seed_vault(tmp_path / "vault")
    monkeypatch.setenv("MEMFW_READONLY_ROOT", str(root))
    return root


def test_absent_header_stays_on_v2_without_recorded_at(
    httpd: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _vault(tmp_path, monkeypatch)
    status, payload = _exchange(
        httpd, "/search", {"query": "corpus keyword", "tenant": "tenant-a"}, READ_TOKEN
    )
    assert status == 200
    assert payload["contract"] == "memfw-http/2"
    assert payload["mode"] == "fts"
    assert payload["results"]
    assert all("recorded_at" not in item for item in payload["results"])


def test_v2_refuses_multihop_and_context(
    httpd: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _vault(tmp_path, monkeypatch)
    status, payload = _exchange(
        httpd,
        "/search",
        {"query": "corpus keyword", "mode": "multihop", "tenant": "tenant-a"},
        READ_TOKEN,
        "memfw-http/2",
    )
    assert status == 400
    assert payload["contract"] == "memfw-http/2"
    assert payload["error"]["message"] == "mode is not available"
    status, payload = _exchange(
        httpd, "/context", {"query": "corpus keyword", "tenant": "tenant-a"}, READ_TOKEN
    )
    assert status == 400
    assert payload["error"]["message"] == "contract does not include /context"


def test_v3_fts_adds_recorded_at_and_context(
    httpd: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _vault(tmp_path, monkeypatch)
    body = {"query": "corpus keyword", "tenant": "tenant-a", "limit": 5}
    status, payload = _exchange(httpd, "/search", body, READ_TOKEN, "memfw-http/3")
    assert status == 200, payload
    assert payload["contract"] == "memfw-http/3"
    assert payload["mode"] == "fts"
    ids = {item["id"] for item in payload["results"]}
    assert "mem-alpha" in ids
    for item in payload["results"]:
        assert isinstance(item["recorded_at"], str) and item["recorded_at"]
        assert item["why"]
        assert item["provenance_type"]
    status, context = _exchange(httpd, "/context", body, READ_TOKEN, "memfw-http/3")
    assert status == 200, context
    assert context["contract"] == "memfw-http/3"
    assert "mem-alpha: Alpha corpus note" in context["context"].splitlines()


def test_v3_multihop_does_not_require_a_hosted_endpoint(
    httpd: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _vault(tmp_path, monkeypatch)
    status, payload = _exchange(
        httpd,
        "/search",
        {"query": "corpus", "mode": "multihop", "tenant": "tenant-a"},
        READ_TOKEN,
        "memfw-http/3",
    )
    assert status == 200, payload
    assert payload["contract"] == "memfw-http/3"
    assert payload["mode"] in {"multihop", "fts"}
    assert payload["results"]
    assert all("recorded_at" in item for item in payload["results"])
    status, rejected = _exchange(
        httpd,
        "/search",
        {"query": "corpus", "mode": "multihop", "endpoint": "http://127.0.0.1:9/judge"},
        READ_TOKEN,
        "memfw-http/3",
    )
    assert status == 400
    assert rejected["error"]["message"] == "endpoint fields are not accepted"


def test_unknown_contract_header_is_400(
    httpd: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _vault(tmp_path, monkeypatch)
    status, payload = _exchange(httpd, "/search", {"query": "corpus"}, READ_TOKEN, "memfw-http/9")
    assert status == 400
    assert payload["contract"] == "memfw-http/2"
    assert payload["error"]["message"] == "contract is not available"


def test_forget_keeps_its_contract(
    httpd: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _vault(tmp_path, monkeypatch)
    monkeypatch.setenv("MEMFW_TENANT", "tenant-a")
    before = (root / "memories" / "mem-alpha.md").read_bytes()
    status, payload = _exchange(
        httpd,
        "/forget",
        {"proposal_id": "del-v3", "memory_id": "mem-alpha"},
        WRITE_TOKEN,
        "memfw-http/3",
    )
    assert status == 200, payload
    assert payload["contract"] == "memfw-http/3"
    assert payload["deleted"] is False
    assert (root / "memories" / "mem-alpha.md").read_bytes() == before
