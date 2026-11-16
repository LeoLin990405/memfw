"""Readonly service hardening: bind guard, stage tenant scope, stage conflicts,
and concept errors without absolute vault paths."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

import scripts.memfw_readonly_service as service
from memfw.ontology.model import OntologyError, load_concepts
from scripts.memfw_readonly_service import Handler, ServiceError, search_vault, stage_candidate
from tests.test_readonly_service import seed_vault

READ_TOKEN = "read-token"
WRITE_TOKEN = "write-token"


# The suite-wide guard refuses getaddrinfo and the loopback client, both used here on purpose.
@pytest.fixture(autouse=True)
def disable_network() -> None:
    """Loopback only: the server binds 127.0.0.1 and nothing binds a wildcard."""


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[tuple[str, Path]]:
    root = seed_vault(tmp_path / "vault")
    monkeypatch.setenv("MEMFW_READONLY_TOKEN", READ_TOKEN)
    monkeypatch.setenv("MEMFW_WRITE_TOKEN", WRITE_TOKEN)
    monkeypatch.setenv("MEMFW_READONLY_ROOT", str(root))
    monkeypatch.delenv("MEMFW_RELATED_DB", raising=False)
    monkeypatch.delenv("MEMFW_TENANT", raising=False)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}", root
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _post(base: str, path: str, payload: dict, token: str = WRITE_TOKEN) -> tuple[int, dict]:
    request = urllib.request.Request(
        base + path,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={service.TOKEN_HEADER: token, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


# ---------------------------------------------------------------------------
# S5: wildcard bind guard
# ---------------------------------------------------------------------------


class _FakeServer:
    bound: list[tuple[str, int]] = []

    def __init__(self, address: tuple[str, int], _handler: object) -> None:
        _FakeServer.bound.append(address)

    def serve_forever(self) -> None:
        raise KeyboardInterrupt

    def server_close(self) -> None:
        pass


@pytest.fixture
def fake_server(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, int]]:
    _FakeServer.bound = []
    monkeypatch.setattr(service, "ThreadingHTTPServer", _FakeServer)
    for name in ("MEMFW_READONLY_TOKEN_FILE", "MEMFW_WRITE_TOKEN", "MEMFW_WRITE_TOKEN_FILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MEMFW_READONLY_TOKEN", READ_TOKEN)
    return _FakeServer.bound


@pytest.mark.parametrize("host", ["0.0.0.0", "", "0", "0.0", "000.0.0.0", " 0.0.0.0", "::"])
def test_wildcard_hosts_are_refused(fake_server: list, tmp_path: Path, host: str) -> None:
    with pytest.raises(SystemExit) as refused:
        service.main(["--root", str(tmp_path), "--host", host, "--port", "0"])
    assert refused.value.code not in (0, None)
    assert fake_server == []


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_loopback_hosts_still_bind(fake_server: list, tmp_path: Path, host: str) -> None:
    assert service.main(["--root", str(tmp_path), "--host", host, "--port", "0"]) == 0
    assert fake_server == [(host, 0)]


# ---------------------------------------------------------------------------
# S6: /stage honours the bound tenant
# ---------------------------------------------------------------------------


def _candidate_tenant(root: Path, proposal_id: str) -> str:
    path = root / "_candidates" / "remember" / f"conductor-{proposal_id}.md"
    return str(service._metadata(path).get("tenant") or "")


def test_stage_refuses_a_foreign_tenant_when_bound(
    server: tuple[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    base, root = server
    monkeypatch.setenv("MEMFW_TENANT", "tenant-a")
    status, body = _post(
        base,
        "/stage",
        {"proposal_id": "p-foreign", "title": "t", "content": "c", "tenant": "tenant-b"},
    )
    assert status == 403
    assert body["error"]["code"] == "forbidden"
    assert not (root / "_candidates" / "remember" / "conductor-p-foreign.md").exists()

    status, _ = _post(base, "/stage", {"proposal_id": "p-omitted", "title": "t", "content": "c"})
    assert status == 200
    assert _candidate_tenant(root, "p-omitted") == "tenant-a"

    status, _ = _post(
        base,
        "/stage",
        {"proposal_id": "p-same", "title": "t", "content": "c", "tenant": "tenant-a"},
    )
    assert status == 200
    assert _candidate_tenant(root, "p-same") == "tenant-a"


def test_stage_without_a_binding_keeps_the_payload_tenant(server: tuple[str, Path]) -> None:
    base, root = server
    status, _ = _post(
        base,
        "/stage",
        {"proposal_id": "p-free", "title": "t", "content": "c", "tenant": "tenant-b"},
    )
    assert status == 200
    assert _candidate_tenant(root, "p-free") == "tenant-b"


# ---------------------------------------------------------------------------
# S8: same proposal_id with different metadata is a conflict
# ---------------------------------------------------------------------------

BASE_PROPOSAL = {
    "proposal_id": "p-meta",
    "title": "Title",
    "content": "same body",
    "tags": ["a"],
    "related": ["mem-alpha"],
    "tenant": "tenant-a",
}


@pytest.mark.parametrize(
    "change",
    [
        {"title": "Other title"},
        {"tags": ["b"]},
        {"related": []},
        {"tenant": "tenant-b"},
        {"source": "elsewhere"},
    ],
)
def test_stage_same_id_different_metadata_is_409(tmp_path: Path, change: dict) -> None:
    root = seed_vault(tmp_path / "vault")
    first = stage_candidate(root, BASE_PROPOSAL)
    path = root / first["path"]
    before = path.read_bytes()
    with pytest.raises(ServiceError) as conflict:
        stage_candidate(root, {**BASE_PROPOSAL, **change})
    assert conflict.value.status == 409
    assert conflict.value.code == "conflict"
    assert path.read_bytes() == before


def test_stage_identical_resubmission_stays_duplicate(tmp_path: Path) -> None:
    root = seed_vault(tmp_path / "vault")
    assert stage_candidate(root, BASE_PROPOSAL)["duplicate"] is False
    assert stage_candidate(root, dict(BASE_PROPOSAL))["duplicate"] is True
    multiline = {**BASE_PROPOSAL, "proposal_id": "p-ml", "title": "a\nb", "tags": ["x\ny"]}
    assert stage_candidate(root, multiline)["duplicate"] is False
    assert stage_candidate(root, multiline)["duplicate"] is True


# ---------------------------------------------------------------------------
# S4: concept errors carry no absolute vault path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (b"---\ntitle: x\n---\nbody\n", "concepts/thing/x.md has no oid"),
        (b"\xff\xfe not utf-8", "concepts/thing/x.md cannot be read"),
        (b"---\nfoo: [\n---\nbody\n", None),
    ],
)
def test_concept_errors_are_vault_relative(
    tmp_path: Path, raw: bytes, expected: str | None
) -> None:
    root = seed_vault(tmp_path / "vault")
    (root / "concepts" / "thing").mkdir(parents=True)
    (root / "concepts" / "thing" / "x.md").write_bytes(raw)
    with pytest.raises(OntologyError) as error:
        load_concepts(root)
    message = str(error.value)
    assert str(tmp_path) not in message
    assert str(root.resolve()) not in message
    if expected is not None:
        assert message == expected


def test_search_alias_error_is_fixed_and_detail_goes_to_stderr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = seed_vault(tmp_path / "vault")
    (root / "concepts" / "thing").mkdir(parents=True)
    (root / "concepts" / "thing" / "x.md").write_text(
        "---\ntitle: x\n---\nbody\n", encoding="utf-8"
    )
    payload = search_vault(root, "alpha corpus", tenant="tenant-a")
    assert payload["results"]
    errors = " ".join(payload["errors"])
    assert errors == "alias expansion unavailable: a concept file is invalid"
    assert str(tmp_path) not in json.dumps(payload)
    assert "concepts/thing/x.md" in capsys.readouterr().err
