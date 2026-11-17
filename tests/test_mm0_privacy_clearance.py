"""MM0: the ``privacy`` field and clearance filtering on the vault's exits.

Leak tests: under a ``public,shared`` clearance no ``local-private`` (or
unknown) memory or note reaches the HTTP read-only service, an OKF bundle,
the notes MCP server or the memfw MCP server. Each local-private document
carries a marker word; the leak count is the number of times a marker shows
up in what an exit returns, and must be 0. Without a clearance every exit
returns what it returned before MM0.
"""

from __future__ import annotations

import itertools
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

import scripts.memfw_readonly_service as service
from memfw.cli import app
from memfw.core.errors import IntegrityError
from memfw.core.models import EventKind, MemoryDocument
from memfw.core.repository import Repository
from memfw.index import MemoryIndex, read_path
from memfw.index.readonly import search_vault
from memfw.mcp.server import Gateway
from memfw.notes.feedback import FeedbackStore
from memfw.notes.index import NotesIndex
from memfw.notes.mcp import NotesGateway
from memfw.okf import export_okf
from memfw.ports import privacy
from memfw.related import RelatedIndex
from memfw.storage.markdown import MarkdownStorageMixin
from scripts.memfw_readonly_service import ServiceError, related_vault

BASE = datetime(2026, 11, 6, tzinfo=UTC)
REMOTE = "public,shared"
#: Words that appear only in documents a ``public,shared`` clearance withholds.
MARKERS = ("zebracorn", "quokkalith", "narwhaline", "axolotlite", "pangolium")


def _doc(memory_id: str, body: str, *, privacy_value: str | None = None, tags=()) -> MemoryDocument:
    return MemoryDocument(
        id=memory_id,
        title=f"corpus {memory_id}",
        body=body,
        tags=list(tags),
        privacy=privacy_value,
        created_at=BASE,
        updated_at=BASE,
    )


#: (document, admitted under public,shared with no legacy default set)
CASES: list[tuple[MemoryDocument, bool]] = [
    (_doc("m-public", "corpus keyword public text", privacy_value="public"), True),
    (_doc("m-shared", "corpus keyword shared text", privacy_value="Shared"), True),
    (_doc("m-private", "corpus keyword zebracorn", privacy_value="local-private"), False),
    # No field: the legacy default, local-private unless the vault sets one.
    (_doc("m-legacy", "corpus keyword quokkalith"), False),
    # Unrecognised value: unknown, never admitted.
    (_doc("m-unknown", "corpus keyword narwhaline", privacy_value="secret"), False),
    # Tags only ever make it stricter.
    (
        _doc(
            "m-tagged",
            "corpus keyword axolotlite",
            privacy_value="public",
            tags=("Sensitivity:Private",),
        ),
        False,
    ),
    (
        _doc(
            "m-ptag",
            "corpus keyword pangolium",
            privacy_value="shared",
            tags=("privacy:LOCAL-PRIVATE",),
        ),
        False,
    ),
]
ADMITTED = {document.id for document, admitted in CASES if admitted}
WITHHELD = {document.id for document, admitted in CASES if not admitted}


def _leaks(payload: object) -> int:
    text = json.dumps(payload, ensure_ascii=False) if not isinstance(payload, str) else payload
    return sum(text.count(marker) for marker in MARKERS)


def _vault(root: Path, *, legacy: str | None = None) -> Path:
    repository = Repository(root)
    repository.init()
    if legacy is not None:
        config = repository.read_config()
        config[privacy.LEGACY_PRIVACY_KEY] = legacy
        repository.configure(config)
    (root / "memories").mkdir(exist_ok=True)
    for document, _admitted in CASES:
        path = root / "memories" / f"{document.id}.md"
        path.write_bytes(MarkdownStorageMixin.render(document).encode("utf-8"))
    MemoryIndex(repository.index_path).rebuild(repository)
    return root


# ---------------------------------------------------------------------------
# The classification and its stdlib copy
# ---------------------------------------------------------------------------

FIELD_VALUES = [None, "public", "PUBLIC", " shared ", "local-private", "Local-Private", "x", ""]
TAG_SETS = [
    (),
    ("privacy:public",),
    ("privacy:fleet",),
    ("privacy:none",),
    ("privacy:local-private",),
    ("privacy:weird",),
    ("sensitivity:private",),
    ("SENSITIVITY:secret",),
    ("privacy:none", "privacy:LOCAL-PRIVATE"),
]
LEGACIES = ["public", "shared", "local-private", "bogus", None]
CLEARANCES = [
    None,
    "",
    "public",
    "public,shared",
    "PUBLIC, Shared",
    "shared,bogus",
    ",",
    ["shared"],
]


def test_stdlib_copy_matches_the_ports_module() -> None:
    for value, tags, legacy in itertools.product(FIELD_VALUES, TAG_SETS, LEGACIES):
        expected = privacy.effective_level(value, tags, legacy=legacy)  # type: ignore[arg-type]
        assert read_path.effective_level(value, tags, legacy=legacy) == expected
        metadata = {"privacy": value, "tags": list(tags)}
        assert read_path.metadata_level(metadata, legacy) == privacy.metadata_level(
            metadata,
            legacy,  # type: ignore[arg-type]
        )
        assert read_path.legacy_default(legacy) == privacy.legacy_default(legacy)
    for clearance in CLEARANCES:
        parsed = privacy.parse_clearance(clearance)
        assert read_path.parse_clearance(clearance) == parsed
        assert read_path.clearance_is_valid(clearance) == privacy.clearance_is_valid(clearance)
        for level in (*privacy.PRIVACY_LEVELS, "unknown"):
            assert read_path.admits(parsed, level) == privacy.admits(parsed, level)
    assert read_path.PRIVACY_LEVELS == privacy.PRIVACY_LEVELS
    assert read_path.LEGACY_PRIVACY_KEY == privacy.LEGACY_PRIVACY_KEY


def test_strictest_wins_and_unknown_is_never_admitted() -> None:
    assert privacy.effective_level("public", ["sensitivity:private"]) == "local-private"
    assert privacy.effective_level("local-private", ["privacy:public"]) == "local-private"
    assert privacy.effective_level(None, [], legacy="shared") == "shared"
    assert privacy.effective_level(None, []) == "local-private"
    assert privacy.effective_level("Top-Secret", []) == "unknown"
    assert privacy.effective_level("shared", ["sensitivity:secret"]) == "unknown"
    every = frozenset(privacy.PRIVACY_LEVELS)
    assert not privacy.admits(every, "unknown")
    assert privacy.admits(None, "unknown")  # no clearance configured: no filtering
    assert privacy.parse_clearance("shared,typo") == frozenset({"public"})
    assert privacy.parse_clearance(None) is None


# ---------------------------------------------------------------------------
# The field: old files byte-identical, write rules
# ---------------------------------------------------------------------------


def test_files_without_privacy_keep_their_bytes(tmp_path: Path) -> None:
    repository = Repository(tmp_path / "vault")
    repository.init()
    document = repository.add("plain", "a memory from before MM0", memory_id="old")
    raw = (repository.memories_dir / "old.md").read_text(encoding="utf-8")
    assert "privacy" not in raw
    parsed = repository.parse(raw, expected_id="old")
    assert parsed.privacy is None
    assert repository.render(parsed) == raw
    assert "privacy" not in document.model_dump()
    updated = repository.update("old", body="edited body")
    assert "privacy" not in (repository.memories_dir / "old.md").read_text(encoding="utf-8")
    assert updated.privacy is None


def test_privacy_is_written_only_when_set(tmp_path: Path) -> None:
    repository = Repository(tmp_path / "vault")
    repository.init()
    repository.add("set", "body", memory_id="set", privacy=" Shared ")
    raw = (repository.memories_dir / "set.md").read_text(encoding="utf-8")
    assert "\nprivacy: shared\n" in raw
    parsed = repository.get("set")
    assert parsed.privacy == "shared"
    assert repository.render(parsed) == raw
    with pytest.raises(IntegrityError):
        repository.add("bad", "body", privacy="secret")


def test_unrecognised_privacy_in_a_file_stays_readable_and_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    Repository(root).init()
    path = root / "memories" / "odd.md"
    path.parent.mkdir(exist_ok=True)
    text = MarkdownStorageMixin.render(_doc("odd", "body")).replace(
        "\n---\n\nbody", "\nprivacy:\n- a\n- b\n---\n\nbody"
    )
    path.write_text(text, encoding="utf-8")
    document = Repository(root).get("odd")
    assert privacy.effective_level(document.privacy, document.tags) == "unknown"


def test_only_a_human_may_loosen_privacy(tmp_path: Path) -> None:
    repository = Repository(tmp_path / "vault")
    repository.init()
    repository.add("x", "body", memory_id="x", privacy="local-private")
    with pytest.raises(IntegrityError, match="loosen"):
        repository.update("x", actor="mechanism", privacy="public")
    repository.update("x", actor="human", privacy="public")
    repository.update("x", actor="mechanism", privacy="local-private")
    assert repository.get("x").privacy == "local-private"
    with pytest.raises(IntegrityError):
        repository.update("x", privacy="secret")


def test_legacy_default_is_validated_and_set_with_one_config_change(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    repository = Repository(root)
    repository.init()
    assert repository.legacy_privacy_default() == "local-private"
    with pytest.raises(IntegrityError):
        repository.configure({**repository.read_config(), "legacy_privacy_default": "open"})
    before = sum(1 for event in repository.events() if event.kind == EventKind.CONFIG_CHANGE)
    runner = CliRunner()
    shown = runner.invoke(app, ["privacy", "legacy-default", "--root", str(root)])
    assert shown.exit_code == 0, shown.output
    assert json.loads(shown.output) == {
        "legacy_privacy_default": "local-private",
        "configured": False,
    }
    changed = runner.invoke(app, ["privacy", "legacy-default", "SHARED", "--root", str(root)])
    assert changed.exit_code == 0, changed.output
    assert Repository(root).legacy_privacy_default() == "shared"
    after = sum(1 for event in Repository(root).events() if event.kind == EventKind.CONFIG_CHANGE)
    assert after == before + 1
    bad = runner.invoke(app, ["privacy", "legacy-default", "open", "--root", str(root)])
    assert bad.exit_code != 0


def test_cli_add_takes_privacy(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    Repository(root).init()
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["add", "--title", "t", "--body", "b", "--privacy", "public", "--root", str(root)],
    )
    assert result.exit_code == 0, result.output
    assert Repository(root).get(result.output.strip()).privacy == "public"
    refused = runner.invoke(
        app, ["add", "--title", "t", "--body", "b", "--privacy", "open", "--root", str(root)]
    )
    assert refused.exit_code != 0


# ---------------------------------------------------------------------------
# Exit 1: HTTP read-only service
# ---------------------------------------------------------------------------


def test_http_search_leaks_nothing_under_remote_clearance(tmp_path: Path) -> None:
    root = _vault(tmp_path / "vault")
    result = search_vault(root, "corpus keyword", limit=50, clearance=REMOTE)
    assert {item["id"] for item in result["results"]} == ADMITTED
    assert _leaks(result) == 0
    # The script's copy of the library behaves the same.
    scripted = service.search_vault(root, "corpus keyword", limit=50, clearance=REMOTE)
    assert {item["id"] for item in scripted["results"]} == ADMITTED


def test_http_search_without_clearance_is_unchanged(tmp_path: Path) -> None:
    root = _vault(tmp_path / "vault")
    result = search_vault(root, "corpus keyword", limit=50)
    assert {item["id"] for item in result["results"]} == ADMITTED | WITHHELD
    assert all("privacy" not in item for item in result["results"])


def test_http_legacy_default_shared_keeps_old_memories_served(tmp_path: Path) -> None:
    root = _vault(tmp_path / "vault", legacy="shared")
    result = search_vault(root, "corpus keyword", limit=50, clearance=REMOTE)
    assert {item["id"] for item in result["results"]} == ADMITTED | {"m-legacy"}


def test_http_short_query_branch_is_filtered_too(tmp_path: Path) -> None:
    root = _vault(tmp_path / "vault")
    result = search_vault(root, "co", limit=50, clearance=REMOTE)
    assert result["results"]
    assert {item["id"] for item in result["results"]} <= ADMITTED
    assert _leaks(result) == 0


@pytest.mark.parametrize("edges", [False, True])
def test_http_related_leaks_nothing_under_remote_clearance(tmp_path: Path, edges: bool) -> None:
    root = _vault(tmp_path / "vault")
    related_db = None
    if edges:
        related_db = root / "related.sqlite3"
        RelatedIndex(related_db).rebuild_memory(Repository(root))
    result = related_vault(root, "m-public", limit=50, related_db=related_db, clearance=REMOTE)
    assert not {item["id"] for item in result["results"]} & WITHHELD
    assert _leaks(result) == 0
    for withheld in WITHHELD:
        with pytest.raises(ServiceError) as caught:
            related_vault(root, withheld, limit=10, related_db=related_db, clearance=REMOTE)
        assert caught.value.status == 404
    # Without a clearance the source memory is still served.
    assert related_vault(root, "m-private", limit=10, related_db=related_db)["memory_id"]


@pytest.fixture
def loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Undo the suite-wide socket guard (conftest.py): this test talks to a loopback server."""

    monkeypatch.undo()


def test_http_service_reads_the_clearance_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, loopback: None
) -> None:
    from tests.test_readonly_service import READ_TOKEN, _post

    root = _vault(tmp_path / "vault")
    monkeypatch.setenv("MEMFW_READONLY_ROOT", str(root))
    monkeypatch.setenv("MEMFW_READONLY_CLEARANCE", REMOTE)
    monkeypatch.setenv("MEMFW_READONLY_TOKEN", READ_TOKEN)
    monkeypatch.delenv("MEMFW_WRITE_TOKEN", raising=False)
    monkeypatch.delenv("MEMFW_RELATED_DB", raising=False)
    monkeypatch.delenv("MEMFW_TENANT", raising=False)
    import threading
    from http.server import ThreadingHTTPServer

    server = ThreadingHTTPServer(("127.0.0.1", 0), service.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        status, body = _post(base, "/search", {"query": "corpus keyword", "limit": 50})
        assert status == 200
        assert {item["id"] for item in body["results"]} == ADMITTED
        assert _leaks(body) == 0
        status, body = _post(base, "/related", {"memory_id": "m-private"})
        assert status == 404
        assert _leaks(body) == 0
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_http_service_refuses_to_start_with_a_malformed_clearance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _vault(tmp_path / "vault")
    monkeypatch.setenv("MEMFW_READONLY_TOKEN", "read-token")
    monkeypatch.delenv("MEMFW_READONLY_CLEARANCE", raising=False)
    with pytest.raises(SystemExit, match="clearance"):
        service.main(["--root", str(root), "--clearance", "public,everyone"])
    monkeypatch.setenv("MEMFW_READONLY_CLEARANCE", "x")
    assert service.configured_clearance() == frozenset({"public"})


# ---------------------------------------------------------------------------
# Exit 2: OKF export
# ---------------------------------------------------------------------------


def _bundle_text(bundle: Path) -> str:
    return "\n".join(path.read_text(encoding="utf-8") for path in sorted(bundle.rglob("*.*")))


def test_okf_export_leaks_nothing_under_remote_clearance(tmp_path: Path) -> None:
    root = _vault(tmp_path / "vault")
    result = export_okf(root, tmp_path / "bundle", clearance=REMOTE)
    assert result.valid
    assert result.concepts == len(ADMITTED)
    text = _bundle_text(tmp_path / "bundle")
    assert _leaks(text) == 0
    for withheld in WITHHELD:
        assert withheld not in text
    manifest = json.loads((tmp_path / "bundle" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["privacy"] == {"clearance": ["public", "shared"], "withheld_count": 5}
    front = yaml.safe_load(
        (tmp_path / "bundle" / "m-shared.md").read_text(encoding="utf-8").split("---\n")[1]
    )
    assert front["memfw"]["privacy"] == "Shared"


def test_okf_export_without_clearance_is_unchanged(tmp_path: Path) -> None:
    root = _vault(tmp_path / "vault")
    result = export_okf(root, tmp_path / "bundle")
    assert result.concepts == len(CASES)
    manifest = json.loads((tmp_path / "bundle" / "manifest.json").read_text(encoding="utf-8"))
    assert "privacy" not in manifest
    front = yaml.safe_load(
        (tmp_path / "bundle" / "m-legacy.md").read_text(encoding="utf-8").split("---\n")[1]
    )
    assert "privacy" not in front["memfw"]


def test_okf_cli_clearance(tmp_path: Path) -> None:
    root = _vault(tmp_path / "vault")
    runner = CliRunner()
    result = runner.invoke(
        app, ["okf", "export", str(tmp_path / "out"), "--root", str(root), "--clearance", REMOTE]
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["concepts"] == len(ADMITTED)
    refused = runner.invoke(
        app, ["okf", "export", str(tmp_path / "o2"), "--root", str(root), "--clearance", "all"]
    )
    assert refused.exit_code != 0
    assert not (tmp_path / "o2").exists()


# ---------------------------------------------------------------------------
# Exit 3a: notes MCP
# ---------------------------------------------------------------------------


def _notes(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "notes"
    root.mkdir()
    notes = {
        "pub.md": "---\nprivacy: public\n---\n# Corpus\n\ncorpus keyword public note\n",
        "shr.md": "---\nprivacy: SHARED\n---\n# Corpus\n\ncorpus keyword shared note\n",
        "prv.md": "---\nprivacy: local-private\n---\n# Corpus\n\ncorpus keyword zebracorn\n",
        "old.md": "# Corpus\n\ncorpus keyword quokkalith\n",
        "tag.md": (
            "---\nprivacy: public\ntags: [sensitivity:private]\n---\n# Corpus\n\n"
            "corpus keyword axolotlite\n"
        ),
    }
    for name, text in notes.items():
        (root / name).write_text(text, encoding="utf-8")
    db = tmp_path / "notes.sqlite"
    NotesIndex(db).index(root)
    return root, db


def _call(gateway: NotesGateway, name: str, arguments: dict) -> dict:
    from memfw.notes.mcp import handle_request

    response = handle_request(
        gateway,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    )
    assert response is not None
    return response["result"]


def test_notes_mcp_leaks_nothing_under_remote_clearance(tmp_path: Path) -> None:
    root, db = _notes(tmp_path)
    related_db = tmp_path / "related.sqlite"
    RelatedIndex(related_db).rebuild_notes(db)
    feedback = FeedbackStore(tmp_path / "feedback.sqlite")
    gateway = NotesGateway(
        root, db, clearance=REMOTE, related_db=related_db, feedback_store=feedback
    )
    found = _call(
        gateway,
        "notes_search",
        {"query": "corpus keyword", "mode": "fts", "limit": 50, "task_id": "t1"},
    )
    payload = json.loads(found["content"][0]["text"])
    assert {hit["path"] for hit in payload["results"]} == {"pub.md", "shr.md"}
    assert _leaks(payload) == 0
    trace = _call(gateway, "retrieval_trace", {"trace_id": payload["trace_id"]})
    trace_payload = json.loads(trace["content"][0]["text"])
    assert {c["path"] for c in trace_payload["candidates"]} <= {"pub.md", "shr.md"}
    for path in ("prv.md", "old.md", "tag.md"):
        got = _call(gateway, "notes_get", {"path": path})
        assert got["isError"] is True
        assert _leaks(got) == 0
        related = _call(gateway, "notes_related", {"path": path})
        assert related["isError"] is True
    related = _call(gateway, "notes_related", {"path": "pub.md", "limit": 50})
    related_payload = json.loads(related["content"][0]["text"])
    assert {item["id"] for item in related_payload["related"]} <= {"shr.md"}
    assert _leaks(related_payload) == 0
    assert json.loads(_call(gateway, "notes_get", {"path": "pub.md"})["content"][0]["text"])


def test_notes_mcp_without_clearance_is_unchanged(tmp_path: Path) -> None:
    root, db = _notes(tmp_path)
    gateway = NotesGateway(root, db)
    found = _call(gateway, "notes_search", {"query": "corpus keyword", "mode": "fts", "limit": 50})
    payload = json.loads(found["content"][0]["text"])
    assert {hit["path"] for hit in payload["results"]} == {
        "pub.md",
        "shr.md",
        "prv.md",
        "old.md",
        "tag.md",
    }
    assert _call(gateway, "notes_get", {"path": "prv.md"})["isError"] is False


def test_notes_legacy_default_admits_old_notes(tmp_path: Path) -> None:
    root, db = _notes(tmp_path)
    gateway = NotesGateway(root, db, clearance=REMOTE, legacy_privacy_default="shared")
    found = _call(gateway, "notes_search", {"query": "corpus keyword", "mode": "fts", "limit": 50})
    payload = json.loads(found["content"][0]["text"])
    assert {hit["path"] for hit in payload["results"]} == {"pub.md", "shr.md", "old.md"}


# ---------------------------------------------------------------------------
# Exit 3b: memfw MCP
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["fts", "hybrid", "multihop", "two_stage"])
def test_memfw_mcp_search_leaks_nothing_under_remote_clearance(tmp_path: Path, mode: str) -> None:
    root = _vault(tmp_path / "vault")
    gateway = Gateway(root, entity_enabled=False, clearance=REMOTE)
    found = gateway.search("corpus keyword", limit=50, mode=mode)
    assert found["results"]
    assert {row["id"] for row in found["results"]} <= ADMITTED
    assert _leaks(found) == 0


@pytest.mark.parametrize("mode", ["fts", "hybrid", "multihop", "two_stage"])
def test_memfw_mcp_search_without_clearance_is_unchanged(tmp_path: Path, mode: str) -> None:
    root = _vault(tmp_path / "vault")
    found = Gateway(root, entity_enabled=False).search("corpus keyword", limit=50, mode=mode)
    assert {row["id"] for row in found["results"]} & WITHHELD


def test_memfw_mcp_get_related_context_status_leak_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _vault(tmp_path / "vault")
    RelatedIndex(root / ".memfw-related.sqlite").rebuild_memory(Repository(root))
    monkeypatch.setenv("MEMFW_MCP_CLEARANCE", REMOTE)
    gateway = Gateway(root, entity_enabled=False)
    for withheld in WITHHELD:
        with pytest.raises(ValueError):
            gateway.get(withheld)
        with pytest.raises(ValueError):
            gateway.related(withheld)
    assert gateway.get("m-public")["id"] == "m-public"
    related = gateway.related("m-public", limit=50)
    assert not {row["id"] for row in related["related"]} & WITHHELD
    assert _leaks(related) == 0
    for v, policy in ((1, None), (2, "compat_fts"), (2, "strict")):
        context = gateway.context(4000, v=v, policy=policy)
        assert _leaks(context) == 0
    status = gateway.status()
    assert sum(status["layers"].values()) == len(ADMITTED)


def test_memfw_mcp_without_clearance_serves_private_memories(tmp_path: Path) -> None:
    root = _vault(tmp_path / "vault")
    gateway = Gateway(root, entity_enabled=False)
    assert gateway.get("m-private")["id"] == "m-private"
    assert sum(gateway.status()["layers"].values()) == len(CASES)
