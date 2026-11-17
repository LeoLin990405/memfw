"""The LightRAG Deriver adapter (``packages/adapters/memfw-adapter-lightrag``).

No test here opens a socket: every request goes to an in-process opener that
answers from ``tests/fixtures/lightrag_contract/exchanges.json`` (response
bodies in the shape of the pinned lightrag-server's route models). The API
key is a made-up test string, checked for in every output.
"""

from __future__ import annotations

import ast
import io
import json
import pickle
import re
import sys
import urllib.error
import urllib.request
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from memfw.ports import AdapterRegistry, AdaptersConfig, DeriveRequest
from memfw.ports.staging import stage_candidates
from memfw.ports.testing import check_deriver, tree_digest

ROOT = Path(__file__).resolve().parents[1]
ADAPTER = ROOT / "packages" / "adapters" / "memfw-adapter-lightrag"
ADAPTER_SRC = ADAPTER / "src"
if str(ADAPTER_SRC) not in sys.path:  # the adapter is a workspace member, not installed here
    sys.path.insert(0, str(ADAPTER_SRC))

from memfw_adapter_lightrag import (  # noqa: E402
    DEFAULT_KEY_ENV,
    EXTERNAL_VERSION,
    SPEC,
    LightRagDeriver,
    LightRagError,
    Settings,
    candidates,
    factory,
)
from memfw_adapter_lightrag import deriver as module  # noqa: E402
from memfw_adapter_lightrag.deriver import (  # noqa: E402
    check_loopback_url,
    graph_rows,
    parse_sources,
    source_name,
)

from memfw.ports.reference._vault import read_memory  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "lightrag_contract" / "exchanges.json"
RECORDED: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
TEST_KEY = "lr-" + "unit-test-key-0000-not-real"
PRIVATE_MARKER = "walrus-private-marker-17"
ENV = {DEFAULT_KEY_ENV: TEST_KEY}


def _memory(memory_id: str, body: str, *, privacy: str | None, version: int = 3) -> str:
    lines = ["---", f"id: {memory_id}", f"title: {memory_id} title", f"version: {version}"]
    if privacy is not None:
        lines.append(f"privacy: {privacy}")
    return "\n".join([*lines, "tags: []", "---", body, ""])


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    (root / "memories").mkdir(parents=True)
    (root / "vault.yaml").write_text("schema_version: 1\n", encoding="utf-8")
    files = {
        "m-backup": _memory(
            "m-backup", "The nightly backup job copies the vault.", privacy="public"
        ),
        "m-release": _memory("m-release", "Release 0.4.0 ships the registry.", privacy="shared"),
        "m-private": _memory("m-private", PRIVATE_MARKER, privacy="local-private", version=1),
        "m-legacy": _memory("m-legacy", PRIVATE_MARKER, privacy=None),
    }
    for memory_id, text in files.items():
        (root / "memories" / f"{memory_id}.md").write_text(text, encoding="utf-8")
    NAMES.clear()
    for memory_id in ("m-backup", "m-release"):
        NAMES[f"{memory_id}.v3.aaaaaaaaaaaa.md"] = name_of(root, memory_id)
    return root


IDS = ("m-backup", "m-release", "m-private", "m-legacy", "m-none")
#: The fixture names the current m-backup and m-release with the placeholder hash
#: ``aaaaaaaaaaaa``; the ``vault`` fixture records the real names here, and the fake
#: server answers with them.
NAMES: dict[str, str] = {}


def name_of(vault: Path, memory_id: str) -> str:
    memory = read_memory(vault, memory_id)
    assert memory is not None
    return source_name(memory, Settings().max_chars)


class FakeServer:
    """An opener answering like the pinned server; records every request."""

    def __init__(self, routes: dict[str, Any] | None = None) -> None:
        self.routes = routes or {
            "GET /health": RECORDED["health"],
            "POST /documents/text": RECORDED["insert"],
            "POST /query/data": RECORDED["query_data"],
            "POST /query": RECORDED["query_answer"],
        }
        self.requests: list[dict[str, Any]] = []

    def __call__(self, request: urllib.request.Request, timeout: float) -> Any:
        path = request.full_url.split("9621", 1)[1]
        body = json.loads(request.data) if request.data else None
        self.requests.append(
            {
                "method": request.get_method(),
                "url": request.full_url,
                "path": path,
                "headers": dict(request.header_items()),
                "body": body,
                "timeout": timeout,
            }
        )
        answer = self.routes[f"{request.get_method()} {path}"]
        if isinstance(answer, int):
            raise urllib.error.HTTPError(request.full_url, answer, "error", {}, io.BytesIO(b"{}"))  # type: ignore[arg-type]
        text = json.dumps(answer)
        for placeholder, name in NAMES.items():
            text = text.replace(placeholder, name)
        return io.BytesIO(text.encode("utf-8"))


def make(server: FakeServer | None = None, **settings: Any) -> tuple[LightRagDeriver, FakeServer]:
    server = server or FakeServer()
    deriver = LightRagDeriver(Settings.from_mapping(settings), environ=ENV, opener=server)
    return deriver, server


def query(kind: str, ids: Sequence[str] = IDS, **options: Any) -> DeriveRequest:
    return DeriveRequest(kind, tuple(ids), {"query": "how does the backup work", **options})


# -- the contract --------------------------------------------------------------------------


def test_fixture_matches_the_pinned_release() -> None:
    assert RECORDED["external_version"] == EXTERNAL_VERSION == SPEC.version == "1.5.7"
    assert SPEC.kind == "deriver" and SPEC.license == "MIT" and SPEC.requires == ()


def test_graph_view_passes_the_contract_and_keeps_only_requested_current_rows(
    vault: Path,
) -> None:
    deriver, server = make()
    view = check_deriver(deriver, vault=vault, request=query("graph"))
    assert view.cited_ids() == ("m-backup", "m-release")
    assert view.epistemic_cap == "inference" and view.causal_cap == "association"
    payload = json.loads(view.content)
    assert payload["format"] == "memfw-lightrag-v1" and payload["kind"] == "graph"
    names = [row.get("name") for row in payload["rows"] if row["type"] == "entity"]
    # Mixed with a private document, a stale version, a foreign file, no source: dropped.
    assert names == ["Adapter Registry", "Backup Job", "NAS"]
    relations = [row for row in payload["rows"] if row["type"] == "relation"]
    assert [(r["source"], r["target"]) for r in relations] == [("Backup Job", "NAS")]
    registry = next(row for row in payload["rows"] if row.get("name") == "Adapter Registry")
    assert registry["sources"] == ["m-backup", "m-release"]
    sent = server.requests[0]
    assert sent["path"] == "/query/data" and sent["method"] == "POST"
    assert sent["body"] == {"query": "how does the backup work", "mode": "mix", "top_k": 20}


def test_graph_drops_everything_when_a_cited_memory_is_not_requested(vault: Path) -> None:
    deriver, _ = make()
    view = deriver.derive(vault, query("graph", ("m-backup",)))
    rows = json.loads(view.content)["rows"]
    assert view.cited_ids() == ("m-backup",)
    assert all(row["sources"] == ["m-backup"] for row in rows)
    assert "Adapter Registry" not in json.dumps(rows)  # it also names m-release


def test_a_newer_vault_version_makes_old_rows_stale(vault: Path) -> None:
    path = vault / "memories" / "m-backup.md"
    path.write_text(path.read_text().replace("version: 3", "version: 4"), encoding="utf-8")
    deriver, _ = make()
    # Every fixture row names m-backup at version 3, so nothing is left.
    with pytest.raises(ValueError, match="no graph rows"):
        deriver.derive(vault, query("graph"))
    server = FakeServer()
    server.routes["POST /query"] = RECORDED["query_answer"]
    with pytest.raises(ValueError, match="outside the request"):
        make(server)[0].derive(vault, query("answer"))


def test_answer_view_cites_its_references(vault: Path) -> None:
    deriver, server = make()
    view = check_deriver(deriver, vault=vault, request=query("answer", mode="hybrid"))
    payload = json.loads(view.content)
    assert payload["answer"].startswith("The nightly backup job")
    assert payload["sources"] == ["m-backup", "m-release"]
    assert payload["untrusted_model_output"] is True
    assert server.requests[0]["body"] == {
        "query": "how does the backup work",
        "mode": "hybrid",
        "top_k": 20,
        "include_references": True,
        "stream": False,
    }


@pytest.mark.parametrize(
    ("route", "message"),
    [
        ("query_answer_foreign", "outside the request"),
        ("query_answer_no_context", "no context"),
    ],
)
def test_answer_outside_the_request_or_without_context_is_refused(
    vault: Path, route: str, message: str
) -> None:
    server = FakeServer()
    server.routes["POST /query"] = RECORDED[route]
    deriver, _ = make(server)
    with pytest.raises(ValueError, match=message):
        deriver.derive(vault, query("answer"))


def test_answer_with_a_private_reference_is_refused(vault: Path) -> None:
    server = FakeServer()
    reply = json.loads(json.dumps(RECORDED["query_answer"]))
    reply["references"].append({"reference_id": "3", "file_path": "m-private.v1.bbbbbbbbbbbb.md"})
    server.routes["POST /query"] = reply
    with pytest.raises(ValueError, match="outside the request"):
        make(server)[0].derive(vault, query("answer"))


@pytest.mark.parametrize(
    "options",
    [{}, {"query": ""}, {"query": 3}, {"query": "x" * 1001}, {"query": "q", "mode": "bypass"}],
)
def test_bad_queries_are_refused_before_anything_is_sent(
    vault: Path, options: dict[str, Any]
) -> None:
    deriver, server = make()
    with pytest.raises(ValueError):
        deriver.derive(vault, DeriveRequest("graph", IDS, options))
    assert server.requests == []


def test_only_private_memories_sends_nothing(vault: Path) -> None:
    deriver, server = make()
    with pytest.raises(ValueError, match="may leave the vault"):
        deriver.derive(vault, query("graph", ("m-private", "m-legacy")))
    assert server.requests == []


def test_unknown_kind_is_refused(vault: Path) -> None:
    deriver, server = make()
    with pytest.raises(ValueError, match="kind"):
        deriver.derive(vault, query("entities"))
    assert server.requests == []


# -- sync --------------------------------------------------------------------------------------


def test_sync_sends_only_shareable_memories_named_by_id_and_version(vault: Path) -> None:
    deriver, server = make()
    result = deriver.sync(vault, IDS)
    assert result.inserted == ("m-backup", "m-release")
    assert result.skipped == ("m-private", "m-legacy", "m-none")
    bodies = [request["body"] for request in server.requests]
    assert [body["file_source"] for body in bodies] == [
        name_of(vault, "m-backup"),
        name_of(vault, "m-release"),
    ]
    assert re.fullmatch(r"m-backup\.v3\.[0-9a-f]{12}\.md", bodies[0]["file_source"])
    assert bodies[0]["text"] == "# m-backup title\n\nThe nightly backup job copies the vault."
    assert PRIVATE_MARKER not in json.dumps(server.requests)
    assert {request["path"] for request in server.requests} == {"/documents/text"}


def test_sync_counts_a_409_as_already_present(vault: Path) -> None:
    server = FakeServer()
    server.routes["POST /documents/text"] = 409
    result = make(server)[0].sync(vault, ("m-backup",))
    assert (result.inserted, result.present) == ((), ("m-backup",))


# -- network rules and the key ------------------------------------------------------------------


def test_key_is_sent_as_header_and_never_shown(vault: Path, caplog: Any) -> None:
    deriver, server = make()
    deriver.derive(vault, query("graph"))
    assert server.requests[0]["headers"]["X-api-key"] == TEST_KEY
    shown = repr(deriver) + repr(deriver.client) + str(deriver.client._key)
    assert TEST_KEY not in shown and "***" in shown
    with pytest.raises(TypeError):
        pickle.dumps(deriver.client._key)
    server.routes["POST /query/data"] = 401
    with pytest.raises(LightRagError) as raised:
        deriver.derive(vault, query("graph"))
    assert raised.value.code == "http_401" and TEST_KEY not in str(raised.value)
    assert TEST_KEY not in caplog.text


def test_without_the_key_the_adapter_is_unavailable(vault: Path) -> None:
    deriver = LightRagDeriver(Settings(), environ={}, opener=FakeServer())
    assert deriver.available().reason == "missing_key"
    with pytest.raises(LightRagError, match="missing_key"):
        deriver.derive(vault, query("graph"))
    keyless = LightRagDeriver(Settings(require_api_key=False), environ={}, opener=FakeServer())
    assert keyless.available().available
    keyless.derive(vault, query("graph"))
    assert "X-api-key" not in keyless.client._opener.requests[0]["headers"]  # type: ignore[attr-defined]


def test_the_key_env_name_comes_from_settings(vault: Path) -> None:
    server = FakeServer()
    deriver = LightRagDeriver(
        Settings(api_key_env="MY_LIGHTRAG_KEY"), environ={"MY_LIGHTRAG_KEY": "k2"}, opener=server
    )
    deriver.derive(vault, query("graph"))
    assert server.requests[0]["headers"]["X-api-key"] == "k2"


@pytest.mark.parametrize(
    "url",
    [
        "http://0.0.0.0:9621",
        "http://localhost:9621",
        "http://localhost.:9621",
        "http://192.0.2.10:9621",
        "https://lightrag.example.com",
        "http://user:pw@127.0.0.1:9621",
        "http://127.0.0.1:9621/?x=1",
        "ftp://127.0.0.1:9621",
    ],
)
def test_non_loopback_urls_are_refused(url: str) -> None:
    with pytest.raises(ValueError, match="bad_base_url"):
        check_loopback_url(url)
    assert factory({"base_url": url}).available().reason == "bad_base_url"


def test_loopback_urls_are_accepted() -> None:
    for url in ("http://127.0.0.1:9621/", "http://127.0.0.2:9621", "https://[::1]:9621"):
        assert check_loopback_url(url) == url.rstrip("/")


@pytest.mark.parametrize(
    ("settings", "code"),
    [
        ({"api_key": TEST_KEY}, "key_in_config"),
        ({"surprise": 1}, "bad_option"),
        ({"api_key_env": "lower-case"}, "bad_option"),
        ({"mode": "bypass"}, "bad_option"),
        ({"top_k": 0}, "bad_option"),
    ],
)
def test_bad_settings_are_unavailable_with_a_code(settings: dict[str, Any], code: str) -> None:
    assert factory(settings).available().reason == code


def test_opener_ignores_proxies_and_refuses_redirects(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[Any] = []
    real = urllib.request.build_opener

    def spy(*handlers: Any) -> Any:
        captured.extend(handlers)
        return real(*handlers)

    monkeypatch.setattr(module.urllib.request, "build_opener", spy)
    module.loopback_opener()
    proxies = [h for h in captured if isinstance(h, urllib.request.ProxyHandler)]
    assert len(proxies) == 1 and proxies[0].proxies == {}
    redirect = next(h for h in captured if isinstance(h, urllib.request.HTTPRedirectHandler))
    assert redirect.redirect_request() is None


@pytest.mark.parametrize(
    ("failure", "code"),
    [
        (urllib.error.URLError("down"), "unreachable"),
        (TimeoutError(), "unreachable"),
        (b"not json", "bad_response"),
        (b"x" * ((4 << 20) + 2), "response_too_large"),
    ],
)
def test_transport_failures_are_short_codes(vault: Path, failure: Any, code: str) -> None:
    def opener(request: Any, timeout: float) -> Any:
        if isinstance(failure, BaseException):
            raise failure
        return io.BytesIO(failure)

    deriver = LightRagDeriver(Settings(), environ=ENV, opener=opener)
    with pytest.raises(LightRagError) as raised:
        deriver.derive(vault, query("graph"))
    assert raised.value.code == code


def test_health_reports_status_fields_only() -> None:
    deriver, server = make()
    assert deriver.health() == {"status": "healthy", "core_version": "1.5.7", "api_version": "0261"}
    assert server.requests[0]["method"] == "GET"


def test_registry_loads_it_only_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(DEFAULT_KEY_ENV, TEST_KEY)
    registry = AdapterRegistry()
    registry.register(SPEC)
    assert registry.load("lightrag", AdaptersConfig()).reason == "disabled"
    status = registry.load("lightrag", AdaptersConfig.from_mapping({"enabled": ["lightrag"]}))
    assert status.available and status.adapter is not None
    monkeypatch.delenv(DEFAULT_KEY_ENV)
    status = registry.load("lightrag", AdaptersConfig.from_mapping({"enabled": ["lightrag"]}))
    assert (status.available, status.reason) == (False, "missing_key")


# -- the output is data ------------------------------------------------------------------------


def test_source_parsing_is_strict() -> None:
    a, b = "m-a.v3.0123456789ab.md", "m_b.v10.0123456789ab.md"
    assert parse_sources(f"{a}<SEP>{b}") == [("m-a", a), ("m_b", b)]
    for bad in (
        "",
        None,
        3,
        "m-a.md",
        "m-a.v3.md",
        "m-a.v3.0123456789AB.md",
        "../m-a.v3.md",
        "dir/m-a.v3.md",
        "m-a.v3.md<SEP>",
        "a.b.v1.md",
    ):
        assert parse_sources(bad) is None, bad


def test_graph_rows_survive_hostile_shapes() -> None:
    current = {"m-a": "m-a.v1.0123456789ab.md"}
    data = {
        "entities": [
            "not a row",
            {"entity_name": "", "file_path": "m-a.v1.0123456789ab.md"},
            {
                "entity_name": "X" * 500,
                "entity_type": 7,
                "description": ["x"],
                "file_path": "m-a.v1.0123456789ab.md",
            },
            {"entity_name": "Y", "file_path": "m-a.v2.0123456789ab.md"},
        ],
        "relationships": {"not": "a list"},
    }
    rows = graph_rows(data, current, max_rows=10)
    assert len(rows) == 1
    assert rows[0]["name"] == "X" * 120 and rows[0]["entity_type"] == "7"
    assert rows[0]["description"] == ""
    assert graph_rows({}, current, max_rows=10) == []


def test_max_rows_caps_the_view(vault: Path) -> None:
    deriver, _ = make(max_rows=1)
    view = deriver.derive(vault, query("graph"))
    assert len(json.loads(view.content)["rows"]) == 1


# -- candidates through memfw's staging ------------------------------------------------------


def test_candidates_stage_only_under_candidates_and_stay_capped(vault: Path) -> None:
    deriver, _ = make()
    for kind in ("graph", "answer"):
        view = deriver.derive(vault, query(kind))
        items = candidates(view)
        assert items and {item.metadata["causal"] for item in items} == {"none"}
        assert {item.metadata["untrusted_model_output"] for item in items} == {True}
        before = tree_digest(vault, exclude=("_candidates",))
        first = stage_candidates(vault, "lightrag", items, proposal_id="p-1", source="lightrag")
        again = stage_candidates(vault, "lightrag", items, proposal_id="p-1", source="lightrag")
        assert len(first.staged) == len(items) and again.staged == ()
        assert tree_digest(vault, exclude=("_candidates",)) == before


# -- layering ---------------------------------------------------------------------------------


def test_adapter_imports_only_stdlib_and_the_ports() -> None:
    allowed = set(sys.stdlib_module_names) | {"memfw_adapter_lightrag", "memfw_ports"}
    for path in sorted((ADAPTER_SRC / "memfw_adapter_lightrag").glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            elif isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            else:
                continue
            for name in names:
                assert name.split(".")[0] in allowed or name.startswith("memfw.ports"), name
    for path in (ROOT / "src" / "memfw").rglob("*.py"):
        assert "memfw_adapter_lightrag" not in path.read_text(encoding="utf-8"), path
