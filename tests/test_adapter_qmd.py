"""R6 PR-2a: the QMD recall adapter (``packages/adapters/memfw-adapter-qmd``).

No test here reaches the network or a real QMD: every request goes to the
adapter's own loopback fake (``memfw_adapter_qmd.fake.FakeQmd``) replaying
``tests/fixtures/qmd_contract/qmd-2.5.3.json``, or to a closed loopback port.
The memory side runs against a real projection manifest exported from the
synthetic vault of ``tests/projection_fixtures.py``.
"""

from __future__ import annotations

import http.server
import json
import socket
import sys
import threading
import time
import tomllib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from projection_fixtures import DOCS, build_vault, stable_id

from memfw.notes.index import NoteParseError, _split_frontmatter
from memfw.ports import (
    AdapterRegistry,
    AdaptersConfig,
    Candidate,
    ProviderUnavailable,
    RecallBudget,
    RecallProvider,
)
from memfw.ports.privacy import metadata_level
from memfw.ports.testing import check_recall_provider
from memfw.projection import export_projection
from memfw.recall import Coordinator, FilterPolicy, ProviderConfig
from memfw.recall.builtin import FtsProvider, default_resolvers
from memfw.runtime.index import ensure_search_index

ROOT = Path(__file__).resolve().parents[1]
ADAPTER = ROOT / "packages" / "adapters" / "memfw-adapter-qmd"
ADAPTER_SRC = ADAPTER / "src"
if str(ADAPTER_SRC) not in sys.path:  # a workspace member; CI does not install it
    sys.path.insert(0, str(ADAPTER_SRC))

import memfw_adapter_qmd  # noqa: E402
from memfw_adapter_qmd import (  # noqa: E402
    MEMORY_SPEC,
    NOTES_SPEC,
    QMD_VERSION,
    MemorySettings,
    NotesSettings,
    QmdMemoryProvider,
    QmdNotesProvider,
    QmdResponseError,
    handelize,
    note_ref,
    parse_note_ref,
)
from memfw_adapter_qmd.fake import FakeQmd  # noqa: E402
from memfw_adapter_qmd.mcp import McpHttpClient, check_endpoint  # noqa: E402
from memfw_adapter_qmd.provider import note_level  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "qmd_contract"
RECORDING = json.loads((FIXTURES / "qmd-2.5.3.json").read_text(encoding="utf-8"))
NOTES = FIXTURES / "notes"
NOW = datetime(2026, 11, 10, 1, 0, tzinfo=UTC)
ID = {name: spec["id"] for name, spec in DOCS.items()}
MARKER = "SNIPPET-MARKER-DO-NOT-RETURN"


_CONNECT = socket.socket.connect
_CONNECT_EX = socket.socket.connect_ex
_CREATE = socket.create_connection
_GETADDRINFO = socket.getaddrinfo
_LOOPBACK = {"127.0.0.1", "localhost"}


@pytest.fixture(autouse=True)
def loopback_only(disable_network: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """Narrow the suite's no-network guard to the loopback fake."""

    def host_of(address: Any) -> str:
        return str(address[0]) if isinstance(address, tuple) and address else str(address)

    def guard(real: Any, address_index: int) -> Any:
        def call(*args: Any, **kwargs: Any) -> Any:
            if host_of(args[address_index]) not in _LOOPBACK:
                raise AssertionError("Tests must not open network sockets")
            return real(*args, **kwargs)

        return call

    monkeypatch.setattr(socket.socket, "connect", guard(_CONNECT, 1))
    monkeypatch.setattr(socket.socket, "connect_ex", guard(_CONNECT_EX, 1))
    monkeypatch.setattr(socket, "create_connection", guard(_CREATE, 0))
    monkeypatch.setattr(
        socket, "getaddrinfo", lambda host, *a, **k: guard(_GETADDRINFO, 0)(host, *a, **k)
    )


def _exchange(name: str) -> dict[str, Any]:
    return next(item for item in RECORDING["exchanges"] if item["name"] == name)


MEMORY = _exchange("memory")
NOTES_X = _exchange("notes")


@pytest.fixture(scope="module")
def projected(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    """``(vault_root, generation_dir)`` of a real ``projection export``."""

    tmp = tmp_path_factory.mktemp("qmd")
    # Without the hand-patched stub (it has no archive, so the FTS index refuses it).
    root, meta = build_vault(tmp, {name: spec for name, spec in DOCS.items() if name != "stub"})
    result = export_projection(root, tmp / "projection", lifecycle_source=meta, now=NOW)
    return root, Path(result["tree"])


@pytest.fixture
def fake() -> Any:
    with FakeQmd.from_recording(RECORDING) as server:
        yield server


def _memory(manifest: Path, url: str, **extra: Any) -> QmdMemoryProvider:
    settings = {"endpoint": url, "manifest": str(manifest), **extra}
    provider = MEMORY_SPEC.factory(settings)
    assert isinstance(provider, QmdMemoryProvider)
    return provider


def _notes(url: str, root: Path = NOTES, **extra: Any) -> QmdNotesProvider:
    settings = {"endpoint": url, "notes_root": str(root), "legacy_privacy": "shared", **extra}
    provider = NOTES_SPEC.factory(settings)
    assert isinstance(provider, QmdNotesProvider)
    return provider


def _search(provider: Any, query: str, *, limit: int = 10, tenant: str = "", **kw: Any) -> list:
    budget = kw.pop("budget", RecallBudget(timeout_seconds=5.0))
    return list(provider.search(query, limit=limit, budget=budget, tenant=tenant, as_of=None))


def _closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


# --------------------------------------------------------------------------- contract


def test_fixture_is_pinned_to_the_adapter_qmd_version() -> None:
    assert RECORDING["external"] == "qmd"
    assert RECORDING["external_version"] == QMD_VERSION == MEMORY_SPEC.version
    assert MEMORY_SPEC.license == NOTES_SPEC.license == "MIT"
    assert MEMORY_SPEC.kind == NOTES_SPEC.kind == "recall"
    init = RECORDING["initialize"]["response"]["body"]["result"]
    assert init["serverInfo"] == {"name": "qmd", "version": QMD_VERSION}


def test_handelize_matches_the_original_function() -> None:
    """``handelize_cases`` are outputs of QMD 2.5.3's own ``handelize`` (store.js)."""

    cases = RECORDING["handelize_cases"]
    assert len(cases) >= 25
    for path, expected in cases.items():
        assert handelize(path) == expected, path


def test_memory_requests_match_the_recorded_contract(
    projected: tuple[Path, Path], fake: FakeQmd
) -> None:
    _root, tree = projected
    provider = _memory(tree, fake.url)
    found = _search(provider, MEMORY["query"])
    assert [c.ref for c in found] == MEMORY["expected_refs"]
    methods = [r["body"].get("method") for r in fake.requests]
    assert methods == ["initialize", "notifications/initialized", "tools/call"]
    init = fake.requests[0]["body"]["params"]
    recorded = RECORDING["initialize"]["request"]["params"]
    assert init.keys() == recorded.keys()
    assert init["protocolVersion"] == recorded["protocolVersion"]
    assert init["clientInfo"] == {"name": "memfw-adapter-qmd", "version": "0.1.1"}
    (call,) = fake.tool_calls()
    assert call["params"] == MEMORY["request"]["params"]
    # The session header and protocol version follow the MCP Streamable HTTP rules.
    assert fake.requests[0]["session"] is None and fake.requests[0]["protocol"] is None
    assert fake.requests[2]["session"] in fake.sessions
    assert fake.requests[2]["protocol"] == "2025-06-18"
    assert "text/event-stream" in fake.requests[2]["accept"]


def test_notes_requests_match_the_recorded_contract(fake: FakeQmd) -> None:
    provider = _notes(fake.url)
    found = _search(provider, NOTES_X["query"])
    assert [c.ref for c in found] == NOTES_X["expected_refs"]
    (call,) = fake.tool_calls()
    assert call["params"] == NOTES_X["request"]["params"]


def test_candidates_carry_references_scores_and_a_source_label_only(
    projected: tuple[Path, Path], fake: FakeQmd
) -> None:
    _root, tree = projected
    for provider, exchange in ((_memory(tree, fake.url), MEMORY), (_notes(fake.url), NOTES_X)):
        found = _search(provider, exchange["query"])
        assert found
        rows = exchange["response"]["body"]["result"]["structuredContent"]["results"]
        scores = {row["score"] for row in rows}
        for rank, candidate in enumerate(found, start=1):
            assert type(candidate) is Candidate
            assert candidate.provider == provider.name and candidate.rank == rank
            assert candidate.score in scores
            assert dict(candidate.extra) == {"kind": provider.kind}
            assert MARKER not in repr(candidate) and "redacted" not in repr(candidate)
        kinds = {c.ref_kind for c in found}
        assert kinds == ({"memory_id"} if provider.name == "qmd" else {"path"})


def test_both_providers_pass_the_recall_contract_kit(
    projected: tuple[Path, Path], fake: FakeQmd
) -> None:
    root, tree = projected
    memory = _memory(tree, fake.url)
    assert isinstance(memory, RecallProvider)
    out = check_recall_provider(memory, queries=[MEMORY["query"]], limit=10, vault=root)
    assert [c.ref for c in out[MEMORY["query"]]] == MEMORY["expected_refs"]
    notes = _notes(fake.url)
    assert isinstance(notes, RecallProvider)
    check_recall_provider(notes, queries=[NOTES_X["query"], "no such query"], limit=3)
    assert memory.projection_version == tree.name


# --------------------------------------------------------------------------- memory: fail closed


def _recorded_files(exchange: dict[str, Any]) -> list[str]:
    rows = exchange["response"]["body"]["result"]["structuredContent"]["results"]
    return [row["file"] for row in rows]


def test_memory_mapping_drops_everything_not_visible(
    projected: tuple[Path, Path], fake: FakeQmd
) -> None:
    _root, tree = projected
    meta = _exchange("memory:meta")
    provider = _memory(tree, fake.url)
    refs = [c.ref for q in (MEMORY["query"], meta["query"]) for c in _search(provider, q)]
    # The live answers do carry these documents; the manifest makes the adapter drop them.
    recorded = " ".join(_recorded_files(MEMORY) + _recorded_files(meta))
    for name in ("private", "privacy_mismatch", "privacy_unknown", "tenant_other"):
        assert ID[name] in recorded, name
        assert ID[name] not in refs, name  # privacy / tenant from the manifest
    other = [c.ref for c in _search(provider, MEMORY["query"], tenant="other-team")]
    assert other == [ID["tenant_other"]]


def test_memory_rows_outside_the_manifest_are_dropped(
    projected: tuple[Path, Path], fake: FakeQmd
) -> None:
    """Hand-made rows (not a recording): shapes a live answer could carry."""

    _root, tree = projected
    active = f"memories/{ID['active']}.md"
    fake.results = lambda arguments: [
        {"file": f"memfw-memory/memories/{ID['retired']}.md", "score": 0.9, "line": 1},
        {"file": f"memfw-memory/memories/{ID['orphan']}.md", "score": 0.9, "line": 1},
        {"file": "memfw-memory/memories/fdeadbeef0000.md", "score": 0.9, "line": 1},
        {"file": f"other-collection/{active}", "score": 0.9, "line": 1},
        {"file": f"memfw-memory/../{active}", "score": 0.9, "line": 1},
        {"file": f"memfw-memory/{active}", "score": True, "line": 1},  # bool is not a score
        {"file": f"memfw-memory/{active}", "score": 0.5, "line": 1},
        {"file": f"qmd://memfw-memory/{active}", "score": 0.4, "line": 1},  # duplicate
    ]
    found = _search(_memory(tree, fake.url), "q")
    assert [(c.ref, c.score) for c in found] == [(ID["active"], 0.5)]


def test_manifest_privacy_beats_the_vault_tags_in_a_coordinator(
    projected: tuple[Path, Path], fake: FakeQmd
) -> None:
    """The vault resolver reads privacy from tags only; the manifest knows more."""

    root, tree = projected
    provider = _memory(tree, fake.url)
    coordinator = Coordinator([provider], resolvers=default_resolvers(root), clock=lambda: NOW)
    result = coordinator.search(MEMORY["query"], limit=10, policy=FilterPolicy())
    assert ID["privacy_mismatch"] not in result.ids
    assert ID["active"] in result.ids
    assert result.labels.providers == ("qmd",)
    assert result.labels.projection_versions == {"qmd": tree.name}


LIFECYCLE_DROPS = (
    "expired_meta",  # meta-index valid_until in the past
    "expiry_unparsable",  # meta-index valid_until "next tuesday"
    "superseded_meta",  # meta-index lifecycle: superseded
    "superseded_vault",  # vault superseded_by
    "expired_vault",  # vault valid_to
)


@pytest.mark.parametrize("policy", ["strict", "compat_fts"])
def test_expired_and_superseded_never_leak_through_a_coordinator(
    projected: tuple[Path, Path], fake: FakeQmd, policy: str
) -> None:
    """Meta-index-only lifecycle fields are invisible to the vault resolver: the adapter drops."""

    root, tree = projected
    provider = _memory(tree, fake.url)
    returned = [c.ref for c in _search(provider, MEMORY["query"])]
    coordinator = Coordinator([provider], resolvers=default_resolvers(root), clock=lambda: NOW)
    chosen = FilterPolicy() if policy == "strict" else FilterPolicy.compat_fts(tenant="")
    result = coordinator.search(MEMORY["query"], limit=10, policy=chosen)  # as recorded
    recorded = " ".join(_recorded_files(MEMORY))
    for name in LIFECYCLE_DROPS:
        assert ID[name] in recorded, name  # the live answer carries it
        assert ID[name] not in returned, name
        assert ID[name] not in result.ids, name
    assert ID["active"] in result.ids


def test_valid_until_is_judged_at_the_adapter_clock(fake: FakeQmd, tmp_path: Path) -> None:
    def doc(memory_id: str, **meta: Any) -> dict[str, Any]:
        base = {"privacy_class": "none", "tenant": "", "lifecycle": "active"}
        return {"id": memory_id, "path": f"memories/{memory_id}.md", "meta": {**base, **meta}}

    docs = [
        doc("m-future", valid_until="2026-12-01"),
        doc("m-past", valid_until="2026-11-01T00:00:00Z"),
        doc("m-edge", valid_until="2026-11-10T01:00:00+00:00"),  # == now: expired
        doc("m-bad", valid_until=17),
        doc("m-blank", valid_until="", superseded_by="", valid_to=None, lifecycle=" Active "),
        doc("m-archived", lifecycle="archived"),
    ]
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {"schema_version": 1, "tree": "t", "documents": docs, "lifecycle_source_status": "ok"}
        ),
        encoding="utf-8",
    )
    fake.results = lambda arguments: [
        {"file": f"memfw-memory/memories/{d['id']}.md", "score": 0.5, "line": 1} for d in docs
    ]
    provider = _memory(manifest, fake.url)
    provider._clock = lambda: NOW  # type: ignore[attr-defined]
    assert [c.ref for c in _search(provider, "q")] == ["m-future", "m-blank"]


def test_manifest_problems_make_the_provider_unavailable(
    projected: tuple[Path, Path], tmp_path: Path
) -> None:
    _root, tree = projected
    url = "http://127.0.0.1:9/mcp"
    assert _memory(tmp_path / "absent", url).available().reason == "no_manifest"
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert _memory(bad, url).available().reason == "bad_manifest"
    bad.write_text(json.dumps({"schema_version": 2, "documents": []}), encoding="utf-8")
    assert _memory(bad, url).available().reason == "bad_manifest"
    manifest = json.loads((tree / "manifest.json").read_text(encoding="utf-8"))
    stale = dict(manifest, lifecycle_source_status="stale")
    bad.write_text(json.dumps(stale), encoding="utf-8")
    assert _memory(bad, url).available().reason == "manifest_lifecycle_not_ok"
    assert _memory(bad, url, require_lifecycle_ok=False).available().available
    young = _memory(tree, url, max_manifest_age_seconds=3600)
    young._clock = lambda: NOW + timedelta(minutes=30)  # type: ignore[attr-defined]
    assert young.available().available
    young._clock = lambda: NOW + timedelta(hours=2)  # type: ignore[attr-defined]
    assert young.available().reason == "manifest_stale"
    with pytest.raises(ProviderUnavailable):
        _search(_memory(tmp_path / "absent", url), "q")


def test_manifest_is_reread_when_the_generation_changes(
    projected: tuple[Path, Path], fake: FakeQmd, tmp_path: Path
) -> None:
    _root, tree = projected
    current = tmp_path / "current"
    current.symlink_to(tree)
    provider = _memory(current, fake.url)
    assert ID["active"] in [c.ref for c in _search(provider, MEMORY["query"])]
    manifest = json.loads((tree / "manifest.json").read_text(encoding="utf-8"))
    for document in manifest["documents"]:
        if document["id"] == ID["active"]:
            document["meta"]["privacy_class"] = "local-private"
    other = tmp_path / "tree-next"
    other.mkdir()
    (other / "manifest.json").write_text(json.dumps(dict(manifest, tree="tree-next")), "utf-8")
    current.unlink()
    current.symlink_to(other)
    assert ID["active"] not in [c.ref for c in _search(provider, MEMORY["query"])]
    assert provider.projection_version == "tree-next"


def test_a_repeated_manifest_document_is_not_trusted(fake: FakeQmd, tmp_path: Path) -> None:
    """The same id twice (one entry looser than the other): dropped, never the looser one."""

    entry = {"id": "m-a", "path": "memories/m-a.md"}
    docs = [
        {**entry, "meta": {"privacy_class": "local-private", "tenant": ""}},
        {**entry, "meta": {"privacy_class": "none", "tenant": ""}},
        {"id": "m-b", "path": "memories/m-b.md", "meta": {"privacy_class": "none", "tenant": ""}},
    ]
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {"schema_version": 1, "tree": "t", "documents": docs, "lifecycle_source_status": "ok"}
        ),
        encoding="utf-8",
    )
    fake.results = lambda arguments: [
        {"file": "memfw-memory/memories/m-a.md", "score": 0.9, "line": 1},
        {"file": "memfw-memory/memories/m-b.md", "score": 0.8, "line": 1},
    ]
    assert [c.ref for c in _search(_memory(manifest, fake.url), "q")] == ["m-b"]


def test_handelize_collisions_in_the_manifest_are_dropped(fake: FakeQmd, tmp_path: Path) -> None:
    """``m_a`` and ``m-a`` share the stored name ``memories/m-a.md``: neither is returned."""

    docs = [
        {
            "id": memory_id,
            "path": f"memories/{memory_id}.md",
            "meta": {"privacy_class": "none", "tenant": ""},
        }
        for memory_id in ("m_a", "m-a", "m-b")
    ]
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {"schema_version": 1, "tree": "t", "documents": docs, "lifecycle_source_status": "ok"}
        ),
        encoding="utf-8",
    )
    fake.results = lambda arguments: [
        {"file": "memfw-memory/memories/m-a.md", "score": 0.9, "line": 1},
        {"file": "memfw-memory/memories/m-b.md", "score": 0.8, "line": 1},
    ]
    assert [c.ref for c in _search(_memory(manifest, fake.url), "q")] == ["m-b"]


# --------------------------------------------------------------------------- notes: fail closed


def test_notes_privacy_tenant_and_collisions(fake: FakeQmd) -> None:
    refs = [c.ref for c in _search(_notes(fake.url), NOTES_X["query"])]
    paths = {parse_note_ref(ref)[0] for ref in refs}  # type: ignore[index]
    recorded = " ".join(_recorded_files(NOTES_X))
    for path in ("private/diary.md", "private/tagged.md", "weird/unreadable.md"):
        assert path in recorded, path  # QMD returned it ...
        assert path not in paths, path  # ... local-private, sensitivity tag, unreadable form
    assert _search(_notes(fake.url), NOTES_X["query"], tenant="team-a") == []
    fake.results = lambda arguments: [
        {"file": "knowledge-hub/collide/a-b.md", "score": 0.9, "line": 1},  # two files, one name
        {"file": "knowledge-hub/missing.md", "score": 0.8, "line": 1},
        {"file": "knowledge-hub/top.md", "score": 0.7, "line": 1},
    ]
    assert [c.ref for c in _search(_notes(fake.url), "q")] == ["kh:top.md"]


def test_notes_without_a_privacy_field_need_an_explicit_legacy_level(fake: FakeQmd) -> None:
    default = NOTES_SPEC.factory({"endpoint": fake.url, "notes_root": str(NOTES)})
    refs = [c.ref for c in _search(default, NOTES_X["query"])]
    top, tips, week = NOTES_X["expected_refs"]
    # Only the notes that say shared/public; top.md has no privacy field and is withheld.
    assert refs == [tips, week]
    public_only = _notes(fake.url, clearance="public")
    assert [c.ref for c in _search(public_only, NOTES_X["query"])] == [tips]
    assert top == "kh:top.md"


def test_notes_never_follow_symlinks(fake: FakeQmd, tmp_path: Path) -> None:
    root = tmp_path / "kh"
    (root / "real").mkdir(parents=True)
    (root / "real" / "a.md").write_text("---\nprivacy: shared\n---\n# A\n\nbody\n", "utf-8")
    outside = tmp_path / "outside.md"
    outside.write_text("---\nprivacy: shared\n---\n# Out\n", "utf-8")
    (root / "link.md").symlink_to(outside)
    (root / "linkdir").symlink_to(root / "real", target_is_directory=True)
    fake.results = lambda arguments: [
        {"file": "knowledge-hub/link.md", "score": 0.9, "line": 4},
        {"file": "knowledge-hub/linkdir/a.md", "score": 0.8, "line": 4},
        {"file": "knowledge-hub/real/a.md", "score": 0.7, "line": 4},
    ]
    assert [c.ref for c in _search(_notes(fake.url, root), "q")] == ["kh:real/a.md#A"]
    link_root = tmp_path / "root-link"
    link_root.symlink_to(root, target_is_directory=True)
    assert _notes(fake.url, link_root).available().reason == "no_notes_root"
    assert _notes(fake.url, tmp_path / "absent").available().reason == "no_notes_root"


def _kernel_level(text: str, legacy: str) -> str:
    """How memfw itself reads a note's level (``NotesSegmentResolver._level``)."""

    try:
        frontmatter, _body, _offset = _split_frontmatter(text)
    except NoteParseError:
        return "unknown"  # the kernel withholds such a note
    return metadata_level(frontmatter, legacy)


#: Forms the kernel reads as local-private that a hand-written reader missed.
YAML_LEAK_FORMS = [
    "---\n? sensitivity\n: private\n---\n",  # explicit key
    "---\n<<: {sensitivity: private}\nprivacy: shared\n---\n",  # merge key
    "---\nbase: &b {sensitivity: private}\n<<: *b\nprivacy: shared\n---\n",  # merge via anchor
    '---\n"sensitivit\\x79": private\nprivacy: shared\n---\n',  # escaped quoted key
    '---\n"sensitivity": private\nprivacy: shared\n---\n',  # double-quoted key
    "---\n!!str sensitivity: private\nprivacy: shared\n---\n",  # tagged key
    "---\ntags:\n- sensitivity:private\nprivacy: shared\n---\n",  # unindented block list
    "---\n'privacy': local-private\n---\n",  # single-quoted key
    "---\n{privacy: local-private, tags: [a]}\n---\n",  # flow mapping
    "---\nprivacy: shared\nsensitivity: [private]\n---\n",
    "---\nprivacy: shared\ntags: [a, 'privacy:local-private']\n---\n",
]
#: A ``...`` line never closes the block (the kernel closes only on ``---``).
DOCUMENT_END_FORMS = [
    "---\ndesc: |\n  ...\nprivacy: local-private\n---\n",
    "---\nprivacy: public\n...\ntags: [privacy:local-private]\n---\n",
]
#: Texts for the split-parity check (where the body starts).
SPLIT_CASES = [
    *DOCUMENT_END_FORMS,
    "",
    "# Title\n",
    "---\n---\nbody\n",
    "---\nprivacy: shared\n---\nbody\n---\nmore\n",
    "---\nprivacy: shared\n  ---  \nbody\n",
    "---  \nprivacy: shared\n---\nbody\n",
    "---x\nprivacy: shared\n---\nbody\n",
    "  ---\nprivacy: public\n---\nbody\n",
    "---\r\nprivacy: shared\r\n---\r\nbody\r\n",
    "---\nprivacy: shared\n...\n---\nbody\n",
]
#: Read exactly as the kernel reads them.
SAME_AS_KERNEL = [
    "",
    "# Title\n\nbody\n",
    "---\n---\n",
    "---\nprivacy: shared\n---\n",
    "---\nprivacy: public\n---\n",
    "---\nprivacy: Shared  # inline comment\n---\n",
    "---\nprivacy:\n---\n",
    "---\nprivacy: secret\n---\n",
    "---\nprivacy: [shared]\n---\n",
    "---\nprivacy: {a: b}\n---\n",
    "---\nprivacy: shared\nprivacy: public\n---\n",
    "---\nsensitivity: low\n---\n",
    "---\nsensitivity: []\n---\n",
    "---\nprivacy: >\n  shared\n---\n",
    "---\nprivacy:\n  level: shared\n---\n",
    "---\ntitle: x\nprivacy: shared\ntags: [2024, a]\n---\n",
    "---\n: bad yaml [\n---\n",  # a YAML error: both withhold
    "---\n- a list\n---\n",  # not a mapping: both withhold
]
#: Stricter than the kernel on purpose (fail closed).
STRICTER_THAN_KERNEL = [
    "\ufeff---\nprivacy: shared\n---\n",  # BOM: also the level without frontmatter
    "---\nprivacy: shared\n# never closed\n",  # unterminated block
    "\n---\nprivacy: shared\n---\n",  # block not on line 1
    "# Title\n\nprivacy: public\n",  # a stray key in the head
    "# Title\n\n> Sensitivity: private\n",
    "---\ntags: a, sensitivity:private\nprivacy: shared\n---\n",  # tags not a list
    "---\ntags: {x: 1}\nprivacy: shared\n---\n",
    "---\ntags: [[sensitivity:private]]\nprivacy: shared\n---\n",  # nested list item
    "---\ntag: [sensitivity:private]\nprivacy: shared\n---\n",  # ``tag`` adds levels
]
_RANK = {"public": 0, "shared": 1, "local-private": 2, "unknown": 3}
LEGACY_LEVELS = ["public", "shared", "local-private"]


@pytest.mark.parametrize("front", YAML_LEAK_FORMS)
@pytest.mark.parametrize("legacy", LEGACY_LEVELS)
def test_yaml_forms_the_kernel_reads_as_private_are_private_here(front: str, legacy: str) -> None:
    text = front + "# Heading\n"
    assert _kernel_level(text, legacy) == "local-private", front
    assert note_level(text, legacy)[0] == "local-private", front


@pytest.mark.parametrize("front", SAME_AS_KERNEL)
@pytest.mark.parametrize("legacy", LEGACY_LEVELS)
def test_note_level_is_the_kernel_level(front: str, legacy: str) -> None:
    text = front + "# Heading\n"
    assert note_level(text, legacy)[0] == _kernel_level(text, legacy), front


@pytest.mark.parametrize("front", STRICTER_THAN_KERNEL)
@pytest.mark.parametrize("legacy", LEGACY_LEVELS)
def test_note_level_is_never_looser_than_the_kernel(front: str, legacy: str) -> None:
    text = front + "# Heading\n"
    adapter = note_level(text, legacy)[0]
    assert _RANK[adapter] >= _RANK[_kernel_level(text, legacy)], front
    if legacy != "local-private" and not front.startswith("\ufeff"):
        assert adapter in ("local-private", "unknown"), front


@pytest.mark.parametrize("front", DOCUMENT_END_FORMS)
@pytest.mark.parametrize("legacy", LEGACY_LEVELS)
def test_a_document_end_line_never_hides_a_private_key(front: str, legacy: str) -> None:
    text = front + "# Heading\n"
    adapter = note_level(text, legacy)[0]
    kernel = _kernel_level(text, legacy)
    assert kernel in ("local-private", "unknown"), front  # the kernel's own reading
    assert _RANK[adapter] >= _RANK[kernel] and adapter in ("local-private", "unknown"), front


@pytest.mark.parametrize("text", SPLIT_CASES)
def test_the_block_split_matches_the_kernel_split(text: str) -> None:
    """Same opening and closing rule as ``memfw.notes.index._split_frontmatter``."""

    level, body_start = note_level(text, "shared")
    try:
        frontmatter, _body, offset = _split_frontmatter(text)
    except NoteParseError:
        assert level == "unknown", text  # the kernel withholds it; so do we
        return
    if offset == 0:  # no frontmatter for the kernel
        assert body_start == 0, text
        assert _RANK[level] >= _RANK[metadata_level({}, "shared")], text
    else:
        assert body_start == offset, text
        assert _RANK[level] >= _RANK[metadata_level(frontmatter, "shared")], text


def test_every_private_yaml_form_is_withheld_end_to_end(fake: FakeQmd, tmp_path: Path) -> None:
    root = tmp_path / "kh"
    root.mkdir()
    forms = [*YAML_LEAK_FORMS, *DOCUMENT_END_FORMS, *STRICTER_THAN_KERNEL[1:]]
    names = [f"note{index:02d}.md" for index in range(len(forms))]
    for name, front in zip(names, forms, strict=True):
        (root / name).write_text(front + "# Heading\n\nbody\n", encoding="utf-8")
    (root / "ok.md").write_text("---\nprivacy: shared\n---\n# Heading\n\nbody\n", "utf-8")
    fake.results = lambda arguments: [
        {"file": f"knowledge-hub/{name}", "score": 0.5, "line": 1} for name in [*names, "ok.md"]
    ]
    assert [c.ref for c in _search(_notes(fake.url, root), "q", limit=50)] == ["kh:ok.md"]


def test_a_bom_note_is_read_like_one_without(fake: FakeQmd, tmp_path: Path) -> None:
    root = tmp_path / "kh"
    root.mkdir()
    (root / "bom.md").write_bytes("\ufeff---\nprivacy: shared\n---\n# H\n\nx\n".encode())
    (root / "bom-private.md").write_bytes(
        "\ufeff---\nprivacy: local-private\n---\n# H\n\nx\n".encode()
    )
    fake.results = lambda arguments: [
        {"file": "knowledge-hub/bom.md", "score": 0.5, "line": 6},
        {"file": "knowledge-hub/bom-private.md", "score": 0.4, "line": 6},
    ]
    assert [c.ref for c in _search(_notes(fake.url, root), "q")] == ["kh:bom.md#H"]


# --------------------------------------------------------------------------- refs


@pytest.mark.parametrize(
    ("path", "heading"),
    [
        ("Course A/Week 1 notes.md", "Disk migration checklist"),
        ("迁移/迁移 计划.md", "暂停定时任务"),
        ("a/b.md", ""),
        ("x%y/#z.md", "q\"u'o<t>e`s\\ and ] ) # %"),
        ("tab\tname.md", "line sep"),
    ],
)
def test_note_refs_are_valid_recall_refs_and_round_trip(path: str, heading: str) -> None:
    ref = note_ref(path, heading)
    Candidate(ref=ref, ref_kind="path", score=1.0, provider="qmd-notes", rank=1)
    assert parse_note_ref(ref) == (path, heading)
    assert ref.startswith("kh:") and (("#" in ref) == bool(heading))


# --------------------------------------------------------------------------- network


@pytest.mark.parametrize(
    ("endpoint", "allow_remote", "reason"),
    [
        ("http://127.0.0.1:8181/mcp", False, ""),
        ("http://localhost:8181/mcp", False, ""),
        ("http://[::1]:8181/mcp", False, ""),
        ("http://127.0.0.2:1/mcp", False, ""),
        ("http://192.0.2.10:8181/mcp", False, "endpoint_not_loopback"),
        ("http://qmd.example:8181/mcp", False, "endpoint_not_loopback"),
        ("http://localhost.example:8181/mcp", False, "endpoint_not_loopback"),
        ("https://qmd.example/mcp", True, ""),
        ("ftp://127.0.0.1/mcp", False, "bad_endpoint"),
        ("http://user:pw@127.0.0.1/mcp", False, "bad_endpoint"),
        ("http://127.0.0.1:99999/mcp", False, "bad_endpoint"),
        ("", False, "no_endpoint"),
    ],
)
def test_endpoint_policy(endpoint: str, allow_remote: bool, reason: str) -> None:
    assert check_endpoint(endpoint, allow_remote=allow_remote) == reason
    provider = NOTES_SPEC.factory(
        {"endpoint": endpoint, "allow_remote": allow_remote, "notes_root": str(NOTES)}
    )
    assert provider.available().reason == reason


def test_loopback_requests_bypass_the_environment_proxy(
    fake: FakeQmd, monkeypatch: pytest.MonkeyPatch
) -> None:
    dead = f"http://127.0.0.1:{_closed_port()}"
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY"):
        monkeypatch.setenv(name, dead)
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    assert _search(_notes(fake.url), NOTES_X["query"])  # reached the fake, not the proxy


def test_redirects_are_refused(tmp_path: Path) -> None:
    class Redirect(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            self.send_response(307)
            self.send_header("Location", "http://192.0.2.1/mcp")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args: Any) -> None:
            del args

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
    thread = threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True)
    thread.start()
    try:
        client = McpHttpClient(f"http://127.0.0.1:{server.server_address[1]}/mcp")
        with pytest.raises(QmdResponseError):
            client.call_tool("query", {}, deadline=time.monotonic() + 5)
    finally:
        server.shutdown()
        server.server_close()


def test_a_slow_drip_answer_is_cut_at_the_deadline() -> None:
    """The deadline bounds the whole read, not each recv; the client lock is released."""

    stop = threading.Event()

    class Drip(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "100000")
            self.end_headers()
            for _ in range(100):  # one byte every 0.1 s: each recv is quick
                if stop.is_set():
                    return
                try:
                    self.wfile.write(b" ")
                    self.wfile.flush()
                except OSError:
                    return
                time.sleep(0.1)

        def log_message(self, *args: Any) -> None:
            del args

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Drip)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True)
    thread.start()
    try:
        client = McpHttpClient(f"http://127.0.0.1:{server.server_address[1]}/mcp")
        for _ in range(2):  # the second call is not "busy": the first released the lock
            started = time.monotonic()
            with pytest.raises(ProviderUnavailable) as caught:
                client.call_tool("query", {}, deadline=time.monotonic() + 1.0)
            assert str(caught.value) == "timeout"
            assert time.monotonic() - started < 1.6
    finally:
        stop.set()
        server.shutdown()
        server.server_close()


def test_slow_headers_are_cut_at_the_deadline() -> None:
    """A server that sends its header lines one at a time cannot hold a call past it."""

    stop = threading.Event()

    class SlowHeaders(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            try:
                self.wfile.write(b"HTTP/1.1 200 OK\r\n")
                for index in range(100):  # one header line every 0.1 s: each recv is quick
                    if stop.is_set():
                        return
                    self.wfile.write(f"X-Drip-{index}: 1\r\n".encode())
                    self.wfile.flush()
                    time.sleep(0.1)
            except OSError:
                return

        def log_message(self, *args: Any) -> None:
            del args

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), SlowHeaders)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True)
    thread.start()
    try:
        client = McpHttpClient(f"http://127.0.0.1:{server.server_address[1]}/mcp")
        for _ in range(2):  # the second call is not "busy": the first released the lock
            started = time.monotonic()
            with pytest.raises(ProviderUnavailable) as caught:
                client.call_tool("query", {}, deadline=time.monotonic() + 1.0)
            assert str(caught.value) == "timeout"
            assert time.monotonic() - started < 1.6
    finally:
        stop.set()
        server.shutdown()
        server.server_close()


def test_session_restart_reinitializes_once(fake: FakeQmd) -> None:
    provider = _notes(fake.url)
    first = _search(provider, NOTES_X["query"])
    fake.restart()  # QMD reloaded a generation: the old session is gone
    assert _search(provider, NOTES_X["query"]) == first
    methods = [r["body"].get("method") for r in fake.requests]
    assert methods.count("initialize") == 2 and methods.count("tools/call") == 3


def test_event_stream_answers_are_read(fake: FakeQmd) -> None:
    fake.event_stream = True
    assert [c.ref for c in _search(_notes(fake.url), NOTES_X["query"])] == NOTES_X["expected_refs"]


@pytest.mark.parametrize("failure", ["closed", "timeout", "tool_error", "server_error"])
def test_qmd_failures_raise_instead_of_returning(fake: FakeQmd, failure: str) -> None:
    url = fake.url
    budget = RecallBudget(timeout_seconds=0.5 if failure == "timeout" else 5.0)
    if failure == "closed":
        url = f"http://127.0.0.1:{_closed_port()}/mcp"
    elif failure == "timeout":
        fake.delay = 1.5
    elif failure == "tool_error":
        fake.tool_error = True
    else:
        fake.status = 503
    provider = _notes(url)
    assert provider.available().available  # no network in available()
    expected = QmdResponseError if failure == "tool_error" else ProviderUnavailable
    started = time.monotonic()
    with pytest.raises(expected):
        _search(provider, NOTES_X["query"], budget=budget)
    assert time.monotonic() - started < 3.0


# --------------------------------------------------------------------------- T7 + swap drill


def _fts_and_qmd(root: Path, qmd: QmdMemoryProvider, timeout: float = 1.0) -> Coordinator:
    fts = FtsProvider(root, prepare=ensure_search_index)
    configs = {
        "fts": ProviderConfig(budget=RecallBudget(timeout_seconds=3.0)),
        "qmd": ProviderConfig(budget=RecallBudget(timeout_seconds=timeout)),
    }
    return Coordinator([fts, qmd], resolvers=default_resolvers(root), configs=configs)


@pytest.mark.parametrize("failure", ["closed", "timeout", "tool_error", "no_manifest"])
def test_t7_qmd_down_degrades_and_fts_answers(
    projected: tuple[Path, Path], fake: FakeQmd, failure: str, tmp_path: Path
) -> None:
    root, tree = projected
    url, manifest = fake.url, tree
    if failure == "closed":
        url = f"http://127.0.0.1:{_closed_port()}/mcp"
    elif failure == "timeout":
        fake.delay = 2.0
    elif failure == "tool_error":
        fake.tool_error = True
    else:
        manifest = tmp_path / "absent"
    coordinator = _fts_and_qmd(root, _memory(manifest, url), timeout=0.5)
    policy = FilterPolicy.compat_fts(tenant="")
    result = coordinator.search("cron migration", limit=10, policy=policy)
    alone = Coordinator(
        [FtsProvider(root, prepare=ensure_search_index)], resolvers=default_resolvers(root)
    ).search("cron migration", limit=10, policy=policy)
    assert result.ids == alone.ids and result.ids  # FTS answers alone, same as without QMD
    assert result.labels.fail_closed is False
    assert "qmd" in result.labels.degraded and "qmd" not in result.labels.providers
    expected = {
        "closed": {"unavailable"},
        # Whichever fires first: the coordinator's deadline or the adapter's socket timeout.
        "timeout": {"timeout", "unavailable"},
        "tool_error": {"error:exception"},
        "no_manifest": {"unavailable:no_manifest"},
    }[failure]
    assert result.labels.degraded["qmd"] in expected


def test_swap_drill_qmd_fuses_with_fts(projected: tuple[Path, Path], fake: FakeQmd) -> None:
    root, tree = projected
    result = _fts_and_qmd(root, _memory(tree, fake.url), timeout=5.0).search(
        "cron migration", limit=10, policy=FilterPolicy.compat_fts(tenant="")
    )
    assert result.labels.degraded == {}
    assert set(result.labels.providers) == {"fts", "qmd"}
    assert result.ids[0] == ID["active"]  # found by both
    hit = next(h for h in result.hits if h.memory_id == ID["active"])
    assert set(hit.providers) == {"fts", "qmd"}
    for name in ("private", "privacy_mismatch", "privacy_unknown", "tenant_other"):
        hits = [h for h in result.hits if h.memory_id == ID[name]]
        assert all("qmd" not in h.providers for h in hits), name


# --------------------------------------------------------------------------- registry, default off


def test_adapters_are_off_until_enabled(projected: tuple[Path, Path]) -> None:
    _root, tree = projected
    registry = AdapterRegistry()
    registry.register(MEMORY_SPEC)
    registry.register(NOTES_SPEC)
    settings = {"qmd": {"manifest": str(tree)}, "qmd-notes": {"notes_root": str(NOTES)}}
    for config in (AdaptersConfig(), AdaptersConfig.from_mapping(settings)):
        for name in ("qmd", "qmd-notes"):
            status = registry.load(name, config)
            assert not status.enabled and status.reason == "disabled" and status.adapter is None
    enabled = AdaptersConfig.from_mapping({"enabled": ["qmd", "qmd-notes"], **settings})
    memory = registry.load("qmd", enabled)
    assert memory.available and isinstance(memory.adapter, QmdMemoryProvider)
    notes = registry.load("qmd-notes", enabled)
    # No legacy_privacy given: available, but only explicitly labelled notes will show.
    assert notes.available and isinstance(notes.adapter, QmdNotesProvider)


@pytest.mark.parametrize(
    ("name", "settings"),
    [
        ("qmd", {}),
        ("qmd", {"manifest": "x", "api_key": "nope"}),
        ("qmd", {"manifest": "x", "searches": ["sql"]}),
        ("qmd", {"manifest": "x", "collection": "../etc"}),
        ("qmd-notes", {"notes_root": "x", "clearance": "public,local-private"}),
        ("qmd-notes", {"notes_root": "x", "legacy_privacy": "unknown"}),
        ("qmd-notes", {"notes_root": "x", "allow_remote": "yes"}),
    ],
)
def test_bad_settings_are_a_factory_error(name: str, settings: dict[str, Any]) -> None:
    registry = AdapterRegistry()
    registry.register(MEMORY_SPEC)
    registry.register(NOTES_SPEC)
    status = registry.load(name, AdaptersConfig.from_mapping({"enabled": [name], name: settings}))
    assert status.reason == "factory_error" and status.adapter is None
    parse = MemorySettings.from_mapping if name == "qmd" else NotesSettings.from_mapping
    with pytest.raises(ValueError):
        parse(settings)


def test_package_metadata_entry_points_and_isolation() -> None:
    data = tomllib.loads((ADAPTER / "pyproject.toml").read_text(encoding="utf-8"))
    project = data["project"]
    assert project["version"] == memfw_adapter_qmd.__version__ == "0.1.1"
    assert project["dependencies"] == ["memfw-ports==0.3.0", "PyYAML==6.0.3"]
    points = project["entry-points"]["memfw.adapters"]
    assert points == {
        "qmd": "memfw_adapter_qmd:MEMORY_SPEC",
        "qmd-notes": "memfw_adapter_qmd:NOTES_SPEC",
    }
    notice = (ADAPTER_SRC / "memfw_adapter_qmd" / "THIRD_PARTY.md").read_text(encoding="utf-8")
    assert "MIT License" in notice and "Copyright (c) 2024-2026 Tobi Lutke" in notice
    readme = (ADAPTER / "README.md").read_text(encoding="utf-8")
    assert "MIT" in readme and QMD_VERSION in readme
    # The adapter imports nothing of memfw but the ports, and memfw never imports it.
    for path in (ADAPTER_SRC / "memfw_adapter_qmd").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith(("import memfw", "from memfw")):
                assert stripped.startswith(("from memfw.ports", "from memfw_ports")), (path, line)
    for path in (ROOT / "src" / "memfw").rglob("*.py"):
        assert "memfw_adapter_qmd" not in path.read_text(encoding="utf-8"), path


def test_fixtures_hold_no_private_strings() -> None:
    for path in [FIXTURES / "qmd-2.5.3.json", *NOTES.rglob("*.md")]:
        text = path.read_text(encoding="utf-8")
        for needle in ("/Users/", "/home/", "/Volumes/", "192.168.", "10.0.", "@gmail"):
            assert needle not in text, (path, needle)
    assert stable_id("example_gen8_cron.md") == ID["active"]


# --------------------------------------------------------------------------- runtime loader


def test_factories_accept_the_runtime_loader_injected_keys(
    projected: tuple[Path, Path], fake: FakeQmd
) -> None:
    """``runtime.recall.adapters`` calls ``factory({**options, "vault", "embedder"})``."""

    root, tree = projected
    injected = {"vault": str(root), "embedder": object()}
    memory = memfw_adapter_qmd.memory_factory(
        {"endpoint": fake.url, "manifest": str(tree), **injected}
    )
    assert [c.ref for c in _search(memory, MEMORY["query"])] == MEMORY["expected_refs"]
    notes = memfw_adapter_qmd.notes_factory(
        {"endpoint": fake.url, "notes_root": str(NOTES), "legacy_privacy": "shared", **injected}
    )
    assert notes.available().available


def test_two_stage_loader_builds_the_qmd_provider(
    projected: tuple[Path, Path], fake: FakeQmd
) -> None:
    """Wiring through the runtime loader (``vault.yaml`` ``runtime.recall.adapters``)."""

    from memfw.runtime import recall_adapters as loader

    root, tree = projected
    entries = loader.parse_entries(
        {
            "runtime": {
                "recall": {
                    "adapters": [
                        {
                            "factory": "memfw_adapter_qmd:memory_factory",
                            "options": {"endpoint": fake.url, "manifest": str(tree)},
                        }
                    ]
                }
            }
        }
    )
    (loaded,) = loader.load_adapters(entries, root=root, embedder=None, taken=("fts",))
    assert isinstance(loaded.provider, QmdMemoryProvider) and loaded.provider.name == "qmd"


def test_note_refs_resolve_through_the_kernel_notes_resolver(
    projected: tuple[Path, Path], fake: FakeQmd, tmp_path: Path
) -> None:
    """The adapter's ``kh:`` refs are the ones ``NotesSegmentResolver`` parses and finds."""

    from memfw.notes.index import NotesIndex
    from memfw.recall.builtin import NotesSegmentResolver

    root, _tree = projected
    db = tmp_path / "notes.sqlite3"
    NotesIndex(db).index(NOTES)
    resolver = NotesSegmentResolver(
        NOTES, db, clearance=frozenset({"public", "shared"}), legacy_privacy_default="shared"
    )
    resolvers = default_resolvers(root, schemes={"kh": resolver})
    coordinator = Coordinator([_notes(fake.url)], resolvers=resolvers)
    result = coordinator.search(NOTES_X["query"], limit=10, policy=FilterPolicy())
    assert [hit.ref for hit in result.segments] == NOTES_X["expected_refs"]
    assert {(hit.path, hit.anchor) for hit in result.segments} == {
        parse_note_ref(ref) for ref in NOTES_X["expected_refs"]
    }
    assert result.ids == []  # sections are not memories


@pytest.mark.parametrize("qmd_up", [True, False])
def test_two_stage_runs_qmd_from_vault_yaml(
    fake: FakeQmd, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, qmd_up: bool
) -> None:
    """``runtime.recall.adapters`` → ``memory_search mode=two_stage`` end to end (T7 when down)."""

    from memfw.runtime import build_runtime

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    root, meta = build_vault(
        tmp_path, {name: spec for name, spec in DOCS.items() if name != "stub"}
    )
    tree = Path(export_projection(root, tmp_path / "out", lifecycle_source=meta, now=NOW)["tree"])
    endpoint = fake.url if qmd_up else f"http://127.0.0.1:{_closed_port()}/mcp"
    entry = {
        "factory": "memfw_adapter_qmd:memory_factory",
        "options": {"endpoint": endpoint, "manifest": str(tree)},
        "timeout_seconds": 3,
    }
    config = root / "vault.yaml"
    config.write_text(
        config.read_text(encoding="utf-8")
        + "runtime:\n  recall:\n    adapters: "
        + json.dumps([entry])
        + "\n",
        encoding="utf-8",
    )
    payload = build_runtime(root, record_access=False).search(
        _exchange("memory:cron")["query"], limit=10, mode="two_stage"
    )
    ids = [row["id"] for row in payload["results"]]
    assert ID["active"] in ids  # FTS finds it either way
    if qmd_up:
        assert payload["recall"]["providers"] == ["fts", "qmd"] and not payload["degraded"]
        row = next(r for r in payload["results"] if r["id"] == ID["active"])
        assert row["providers"] == ["fts", "qmd"]
    else:
        assert payload["recall"]["providers"] == ["fts"]
        assert payload["degraded"] and "qmd" in payload["recall"]["degraded"]
        assert payload["recall"]["fail_closed"] is False
