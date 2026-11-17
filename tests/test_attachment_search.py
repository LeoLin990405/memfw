"""MM2b: proxy_segments FTS, ProxyProvider, MCP attachment_search / attachment_get, originals."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from memfw.cli import app
from memfw.core.errors import IntegrityError, NotFoundError
from memfw.core.repository import Repository
from memfw.index import proxies as proxy_index_module
from memfw.index.proxies import (
    LEGACY_PROXY_INDEX_NAME,
    drop_proxy_index,
    ensure_proxy_index,
    proxy_index_path,
    proxy_index_residue,
)
from memfw.mcp.schema import TOOLS
from memfw.mcp.server import Gateway
from memfw.ports.testing import check_recall_provider
from memfw.recall.builtin import ProxyProvider, ProxySegmentResolver
from memfw.stats import measure_physical_disk_bytes

NOW = datetime(2026, 11, 17, 9, 0, tzinfo=UTC)
FIXTURES = Path(__file__).parent / "fixtures" / "proxies"
PDF = (FIXTURES / "source.pdf").read_bytes()
REMOTE = "public,shared"
MARKER = "zqxproxymarker"


@pytest.fixture(autouse=True)
def private_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for name in ("MEMFW_MCP_CLEARANCE", "MEMFW_TENANT", "MEMFW_ALLOWED_SOURCES"):
        monkeypatch.delenv(name, raising=False)
    return home


def _repo(tmp_path: Path) -> Repository:
    counter = iter(range(1, 1_000_000))
    repository = Repository(
        tmp_path / "vault",
        clock=lambda: NOW,
        id_factory=lambda: hashlib.sha256(f"seed:{next(counter)}".encode()).hexdigest()[:32],
    )
    repository.init()
    return repository


def _attach(repository: Repository, tmp_path: Path, data: bytes, name: str, **kw: Any) -> str:
    source = tmp_path / "in" / name
    source.parent.mkdir(exist_ok=True)
    source.write_bytes(data)
    return repository.add_attachment(source, **kw).record.id


def _proxy(
    repository: Repository,
    tmp_path: Path,
    attachment_id: str,
    data: bytes,
    texts: list[str],
    **kw: Any,
) -> str:
    payload = {
        "format": "memfw.proxy",
        "version": 1,
        "kind": "ocr",
        "source_sha256": hashlib.sha256(data).hexdigest(),
        "coverage": {"status": "partial", "detail": "pages 1-2/3"},
        "producer": {"tool": "paddleocr", "version": "2.9.1"},
        "source": {"pages": 3, "page_size": [612, 792]},
        "segments": [
            {"text": text, "locator": {"page": index, "bbox": [72, 600, 540, 640]}}
            for index, text in enumerate(texts, start=1)
        ],
    }
    path = tmp_path / "in" / f"proxy-{attachment_id}-{len(texts)}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return repository.import_proxy(attachment_id, path, **kw).id


def _enable(repository: Repository, *, originals: bool = False) -> None:
    config = repository.read_config()
    config["mcp"] = {"attachments": True, "local_originals": originals}
    repository.configure(config)


def _corpus(tmp_path: Path) -> tuple[Repository, dict[str, str]]:
    """Three attachments: public, shared and local-private, each with a proxy."""

    repository = _repo(tmp_path)
    ids: dict[str, str] = {}
    for level in ("public", "shared", "local-private"):
        data = f"%PDF {level} original".encode()
        attachment = _attach(repository, tmp_path, data, f"{level}.pdf", privacy=level)
        word = MARKER if level == "local-private" else level
        _proxy(
            repository,
            tmp_path,
            attachment,
            data,
            [f"backup schedule {word} first page", f"备份 计划 {word}"],
            privacy=level,
        )
        ids[level] = attachment
    return repository, ids


def _leaks(payload: Any) -> int:
    return json.dumps(payload, ensure_ascii=False).count(MARKER)


# --- index ---------------------------------------------------------------------------


def test_no_proxy_index_without_proxies(tmp_path: Path) -> None:
    repository = _repo(tmp_path)
    _attach(repository, tmp_path, PDF, "a.pdf")
    assert ensure_proxy_index(repository, now=NOW) is None
    assert not proxy_index_path(repository).exists()


def test_index_is_separate_tenant_and_privacy_filtered(tmp_path: Path) -> None:
    repository, ids = _corpus(tmp_path)
    index = ensure_proxy_index(repository, now=NOW)
    assert index is not None and index.path == proxy_index_path(repository)
    everything = index.search("backup schedule", tenant="", clearance=None, limit=50)
    assert {hit.attachment_id for hit in everything} == set(ids.values())
    remote = index.search(
        "backup schedule", tenant="", clearance=frozenset(REMOTE.split(",")), limit=50
    )
    assert {hit.attachment_id for hit in remote} == {ids["public"], ids["shared"]}
    assert index.search("backup", tenant="work", clearance=None, limit=50) == []
    # Two-character CJK terms match through the instr fallback.
    short = index.search("备份", tenant="", clearance=frozenset({"public"}), limit=50)
    assert [hit.attachment_id for hit in short] == [ids["public"]]
    assert short[0].locator == "page=2&bbox=72,600,540,640"
    # The memory index never holds proxy text.
    from memfw.index import MemoryIndex

    MemoryIndex(repository.index_path).rebuild(repository)
    assert MARKER.encode() not in repository.index_path.read_bytes()


def test_index_follows_supersession_and_rebuilds_when_stale(tmp_path: Path) -> None:
    repository = _repo(tmp_path)
    attachment = _attach(repository, tmp_path, PDF, "a.pdf")
    first = _proxy(repository, tmp_path, attachment, PDF, ["old reading alpha"])
    index = ensure_proxy_index(repository, now=NOW)
    assert index is not None
    assert [h.proxy_id for h in index.search("alpha", tenant="", clearance=None, limit=5)] == [
        first
    ]
    second = _proxy(
        repository, tmp_path, attachment, PDF, ["new reading alpha", "x"], supersedes=first
    )
    assert not index.is_current(repository.events(), today=NOW.date())
    index = ensure_proxy_index(repository, now=NOW)
    assert index is not None
    assert [h.proxy_id for h in index.search("alpha", tenant="", clearance=None, limit=5)] == [
        second
    ]


def test_retention_ended_attachments_leave_search(tmp_path: Path) -> None:
    repository = _repo(tmp_path)
    attachment = _attach(repository, tmp_path, PDF, "a.pdf", retention="until:2026-11-16")
    _proxy(repository, tmp_path, attachment, PDF, ["expired scan text"])
    index = ensure_proxy_index(repository, now=NOW)
    assert index is not None
    assert index.search("expired scan", tenant="", clearance=None, limit=5) == []


def test_proxy_index_lives_outside_the_vault_and_is_private(
    tmp_path: Path, private_home: Path
) -> None:
    repository, _ids = _corpus(tmp_path)
    fingerprint = repository._tree_fingerprint()
    before = measure_physical_disk_bytes(repository.root)
    index = ensure_proxy_index(repository, now=NOW)
    assert index is not None
    path = index.path
    assert path.is_relative_to(private_home / ".local" / "share" / "memfw" / "proxy_index")
    assert not path.is_relative_to(repository.root)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    # Nothing in the vault changed: memory writes never hash the index, the
    # vault's size and snapshots never include it.
    assert repository._tree_fingerprint() == fingerprint
    assert measure_physical_disk_bytes(repository.root) == before
    assert not (repository.root / LEGACY_PROXY_INDEX_NAME).exists()


def test_legacy_in_vault_index_is_ignored(tmp_path: Path) -> None:
    repository, ids = _corpus(tmp_path)
    legacy = repository.root / LEGACY_PROXY_INDEX_NAME
    legacy.write_bytes(b"not sqlite")
    index = ensure_proxy_index(repository, now=NOW)
    assert index is not None and index.path != legacy
    assert legacy.read_bytes() == b"not sqlite"
    assert index.search("backup schedule", tenant="", clearance=None, limit=50)


def test_rebuild_works_on_a_read_only_vault(tmp_path: Path) -> None:
    repository, _ids = _corpus(tmp_path)
    directories = [repository.root, *[p for p in repository.root.rglob("*") if p.is_dir()]]
    modes = {path: stat.S_IMODE(path.stat().st_mode) for path in directories}
    try:
        for path in directories:
            os.chmod(path, 0o500)
        index = ensure_proxy_index(Repository(repository.root), now=NOW)
        assert index is not None
        assert index.search("backup schedule", tenant="", clearance=None, limit=50)
    finally:
        for path, mode in modes.items():
            os.chmod(path, mode)


def test_a_write_during_rebuild_leaves_the_index_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, ids = _corpus(tmp_path)
    original = proxy_index_module.active_proxies

    def racing(repo: Repository, *, today: Any) -> Any:
        rows = original(repo, today=today)
        data = b"%PDF public original"
        _proxy(repository, tmp_path, ids["public"], data, ["late arrival text", "y", "z"])
        return rows

    monkeypatch.setattr(proxy_index_module, "active_proxies", racing)
    index = ensure_proxy_index(repository, now=NOW)
    monkeypatch.setattr(proxy_index_module, "active_proxies", original)
    assert index is not None
    assert not index.is_current(
        repository.events(), today=NOW.date(), vault_id=repository.read_config()["vault_id"]
    )
    index = ensure_proxy_index(repository, now=NOW)
    assert index is not None
    assert index.search("late arrival", tenant="", clearance=None, limit=5)


def test_drop_and_residue_for_purge(tmp_path: Path) -> None:
    repository, ids = _corpus(tmp_path)
    assert proxy_index_residue(repository, ids["public"]) == 0  # no index yet
    ensure_proxy_index(repository, now=NOW)
    assert proxy_index_residue(repository, ids["public"]) == 4  # 2 rows + 2 FTS rows
    assert drop_proxy_index(repository) is True
    assert not proxy_index_path(repository).exists()
    assert proxy_index_residue(repository, ids["public"]) == 0
    assert drop_proxy_index(repository) is False


@dataclass
class _State:
    label: str


def test_detached_or_missing_attachments_leave_search_and_get(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, ids = _corpus(tmp_path)
    _enable(repository, originals=True)
    gateway = Gateway(repository.root, entity_enabled=False)
    assert len(gateway.attachment_search("backup schedule")["results"]) == 3
    states = {attachment: _State("active") for attachment in ids.values()}
    states[ids["public"]] = _State("detached")
    states[ids["shared"]] = _State("missing")
    # MM1b's state map (feature-detected); without it every attachment is active.
    monkeypatch.setattr(Repository, "attachment_state_map", lambda self: states, raising=False)
    found = gateway.attachment_search("backup schedule")["results"]
    assert {row["attachment_id"] for row in found} == {ids["local-private"]}
    index = ensure_proxy_index(repository, now=NOW)
    assert index is not None
    rebuilt = proxy_index_module.active_proxies(repository, today=NOW.date())
    assert {attachment.id for _record, attachment in rebuilt} == {ids["local-private"]}
    for gone in (ids["public"], ids["shared"]):
        with pytest.raises(NotFoundError):
            gateway.attachment_get(gone)
        with pytest.raises(NotFoundError):
            gateway.attachment_get(gone, original="bytes")


def test_damaged_record_reads_as_missing_under_a_clearance(tmp_path: Path) -> None:
    repository, ids = _corpus(tmp_path)
    _enable(repository)
    record = repository.root / "attachments" / f"{ids['local-private']}.md"
    record.write_bytes(record.read_bytes() + b"\nedited\n")
    remote = Gateway(repository.root, entity_enabled=False, clearance=REMOTE)
    with pytest.raises(NotFoundError) as damaged:
        remote.attachment_get(ids["local-private"])
    with pytest.raises(NotFoundError) as missing:
        remote.attachment_get("att-01a1e931-1818-772b-8713-76e6bea4a716")
    assert str(damaged.value).replace(ids["local-private"], "X") == str(missing.value).replace(
        "att-01a1e931-1818-772b-8713-76e6bea4a716", "X"
    )
    with pytest.raises(IntegrityError):
        Gateway(repository.root, entity_enabled=False).attachment_get(ids["local-private"])


# --- ProxyProvider ---------------------------------------------------------------------


def test_proxy_provider_passes_the_recall_contract(tmp_path: Path) -> None:
    repository, _ids = _corpus(tmp_path)
    ensure_proxy_index(repository, now=NOW)
    provider = ProxyProvider(repository.root, clearance=frozenset({"public"}))
    found = check_recall_provider(provider, queries=["backup schedule", "备份"])
    assert found
    candidates = provider.search(
        "backup schedule",
        limit=10,
        budget=__import__("memfw.ports").ports.RecallBudget(),
        tenant="",
        as_of=None,
    )
    assert candidates and all(c.ref_kind == "attachment_segment" for c in candidates)
    assert candidates[0].extra["locator"] == "page=1&bbox=72,600,540,640"
    resolved = ProxySegmentResolver().resolve([candidates[0].ref, "m-1"])
    assert list(resolved) == [candidates[0].ref]
    assert resolved[candidates[0].ref].memory_id == candidates[0].ref.replace(":", "_")
    assert ProxyProvider(tmp_path / "nowhere").available().reason == "no_vault"


# --- MCP --------------------------------------------------------------------------------


def test_tools_are_unchanged_without_the_switch(tmp_path: Path) -> None:
    repository, ids = _corpus(tmp_path)
    gateway = Gateway(repository.root, entity_enabled=False)
    assert gateway.tools() == TOOLS
    assert "attachment_search" not in gateway.handlers()
    _enable(repository)
    gateway = Gateway(repository.root, entity_enabled=False)
    names = [tool["name"] for tool in gateway.tools()]
    assert names[-2:] == ["attachment_search", "attachment_get"]
    assert names[:-2] == [tool["name"] for tool in TOOLS]


def test_attachment_search_returns_locators_and_filters_by_clearance(tmp_path: Path) -> None:
    repository, ids = _corpus(tmp_path)
    _enable(repository)
    local = Gateway(repository.root, entity_enabled=False)
    found = local.attachment_search("backup schedule", limit=10)
    assert {row["attachment_id"] for row in found["results"]} == set(ids.values())
    row = found["results"][0]
    assert row["segment"]["fragment"] == "page=1&bbox=72,600,540,640"
    assert row["segment"]["locator"] == {"page": 1, "bbox": [72, 600, 540, 640]}
    assert row["partial"] is True and row["coverage"]["status"] == "partial"
    assert row["status"] == "unverified"
    assert "original" not in row and "path" not in row
    assert found["recall"]["providers"] == ["proxy"]
    remote = Gateway(repository.root, entity_enabled=False, clearance=REMOTE)
    filtered = remote.attachment_search("backup schedule", limit=10)
    assert {row["attachment_id"] for row in filtered["results"]} == {ids["public"], ids["shared"]}
    assert _leaks(filtered) == 0
    assert _leaks(remote.attachment_search(MARKER, limit=10)) == 0
    tenant = Gateway(repository.root, entity_enabled=False, tenant="work")
    assert tenant.attachment_search("backup schedule")["results"] == []


def test_attachment_search_rechecks_the_proxy_file(tmp_path: Path) -> None:
    repository, ids = _corpus(tmp_path)
    _enable(repository)
    gateway = Gateway(repository.root, entity_enabled=False)
    ensure_proxy_index(repository, now=NOW)
    for path in (repository.root / "attachments" / "proxies").glob("*.md"):
        path.write_bytes(path.read_bytes().replace(b"first page", b"first PAGE"))
    # The index is current by the log, but every hand-edited file fails its hash.
    assert gateway.attachment_search("backup schedule")["results"] == []


def test_attachment_get_metadata_segments_and_locator_filter(tmp_path: Path) -> None:
    repository, ids = _corpus(tmp_path)
    _enable(repository)
    gateway = Gateway(repository.root, entity_enabled=False)
    got = gateway.attachment_get(ids["public"])
    assert got["media_type"] == "application/pdf"
    assert got["privacy"] == "public"
    assert [len(p["segments"]) for p in got["proxies"]] == [2]
    only = gateway.attachment_get(ids["public"], locator="page=2")
    assert [s["n"] for s in only["proxies"][0]["segments"]] == [2]
    assert "original" not in got
    remote = Gateway(repository.root, entity_enabled=False, clearance=REMOTE)
    with pytest.raises(NotFoundError) as withheld:
        remote.attachment_get(ids["local-private"])
    with pytest.raises(NotFoundError) as missing:
        remote.attachment_get("att-01a1e931-1818-772b-8713-76e6bea4a716")
    assert str(withheld.value).replace(ids["local-private"], "X") == str(missing.value).replace(
        "att-01a1e931-1818-772b-8713-76e6bea4a716", "X"
    )
    with pytest.raises(IntegrityError):
        gateway.attachment_get("../../etc/passwd")
    with pytest.raises(ValueError):
        gateway.attachment_get(ids["public"], locator="zoom=3")


# --- originals ---------------------------------------------------------------------------


def test_originals_need_the_explicit_local_switch(tmp_path: Path) -> None:
    repository, ids = _corpus(tmp_path)
    _enable(repository)
    gateway = Gateway(repository.root, entity_enabled=False)
    for mode in ("path", "bytes"):
        with pytest.raises(PermissionError, match="local_originals"):
            gateway.attachment_get(ids["public"], original=mode)


def test_local_originals_are_returned_only_after_a_sha_recheck(tmp_path: Path) -> None:
    repository, ids = _corpus(tmp_path)
    _enable(repository, originals=True)
    gateway = Gateway(repository.root, entity_enabled=False)
    data = b"%PDF public original"
    by_path = gateway.attachment_get(ids["public"], original="path")["original"]
    assert by_path["verified"] is True
    assert by_path["verified_at_call_only"] is True
    assert Path(by_path["path"]).read_bytes() == data
    by_bytes = gateway.attachment_get(ids["public"], original="bytes", offset=5, length=6)
    assert base64.b64decode(by_bytes["original"]["bytes_b64"]) == data[5:11]
    # A changed blob is refused, by path and by bytes.
    blob = Path(by_path["path"])
    blob.chmod(0o600)
    blob.write_bytes(b"%PDF tampered original")
    for mode in ("path", "bytes"):
        with pytest.raises(IntegrityError, match="no longer matches"):
            gateway.attachment_get(ids["public"], original=mode)
    # Same size, different bytes: caught by the hash; other size: refused before reading.
    blob.write_bytes(b"%PDF public origXnal")
    with pytest.raises(IntegrityError, match="no longer matches"):
        gateway.attachment_get(ids["public"], original="bytes")
    blob.unlink()
    with pytest.raises(IntegrityError, match="missing"):
        gateway.attachment_get(ids["public"], original="path")
    # A symlink (or FIFO) in the blob's place is never followed or read.
    target = tmp_path / "elsewhere.pdf"
    target.write_bytes(data)
    blob.symlink_to(target)
    with pytest.raises(IntegrityError, match="not a regular file"):
        gateway.attachment_get(ids["public"], original="bytes")
    blob.unlink()
    os.mkfifo(blob)
    with pytest.raises(IntegrityError, match="not a regular file"):
        gateway.attachment_get(ids["public"], original="bytes")


def test_bytes_mode_is_capped_by_original_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from memfw.runtime import attachments as runtime_attachments

    repository, ids = _corpus(tmp_path)
    _enable(repository, originals=True)
    monkeypatch.setattr(runtime_attachments, "MAX_BYTES_MODE_ORIGINAL", 4)
    gateway = Gateway(repository.root, entity_enabled=False)
    with pytest.raises(ValueError, match="served by path"):
        gateway.attachment_get(ids["public"], original="bytes", length=2)
    assert gateway.attachment_get(ids["public"], original="path")["original"]["verified"]


def test_withheld_attachments_never_return_originals(tmp_path: Path) -> None:
    repository, ids = _corpus(tmp_path)
    _enable(repository, originals=True)
    remote = Gateway(repository.root, entity_enabled=False, clearance=REMOTE)
    with pytest.raises(NotFoundError):
        remote.attachment_get(ids["local-private"], original="bytes")


def test_http_service_serves_no_attachment_or_proxy(tmp_path: Path) -> None:
    import scripts.memfw_readonly_service as service
    from memfw.index import MemoryIndex
    from memfw.index.readonly import search_vault

    repository, ids = _corpus(tmp_path)
    _enable(repository, originals=True)
    repository.add("Corpus note", "backup schedule memory", memory_id="m-plain")
    MemoryIndex(repository.index_path).rebuild(repository)
    ensure_proxy_index(repository, now=NOW)
    for search in (search_vault, service.search_vault):
        for query in ("backup schedule", MARKER, "备份"):
            result = search(repository.root, query, limit=50)
            assert {row["id"] for row in result["results"]} <= {"m-plain"}
            text = json.dumps(result, ensure_ascii=False)
            assert _leaks(result) == 0
            assert not any(value in text for value in ids.values())
    routes = Path(service.__file__).read_text(encoding="utf-8")
    assert "attachment" not in routes.lower()


def test_local_private_proxies_leak_through_no_other_exit(tmp_path: Path) -> None:
    from memfw.notes.mcp import NotesGateway
    from memfw.okf import export_okf
    from memfw.projection import export_projection

    repository, ids = _corpus(tmp_path)
    _enable(repository, originals=True)
    repository.add("Corpus note", "backup schedule memory", memory_id="m-plain")
    ensure_proxy_index(repository, now=NOW)
    root = repository.root
    for clearance in (None, REMOTE):
        gateway = Gateway(root, entity_enabled=False, clearance=clearance)
        for mode in ("fts", "hybrid", "multihop", "two_stage"):
            assert _leaks(gateway.search(MARKER, limit=50, mode=mode)) == 0
            assert _leaks(gateway.search("backup schedule", limit=50, mode=mode)) == 0
        assert _leaks(gateway.context(4000)) == 0
        assert _leaks(gateway.status()) == 0
    export_okf(root, tmp_path / "bundle")
    for path in (tmp_path / "bundle").rglob("*"):
        if path.is_file():
            assert MARKER.encode() not in path.read_bytes()
    export_projection(root, tmp_path / "projection", now=NOW)
    for path in (tmp_path / "projection").rglob("*"):
        if path.is_file():
            assert MARKER.encode() not in path.read_bytes()
    # The notes server indexes a notes library, never the vault's attachments.
    from memfw.notes import NotesIndex

    db = tmp_path / "notes.sqlite"
    NotesIndex(db).index(root)
    from memfw.notes.mcp import handle_request

    notes = NotesGateway(root, db, clearance=REMOTE)
    for query in ("backup schedule", MARKER):
        response = handle_request(
            notes,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "notes_search",
                    "arguments": {"query": query, "mode": "fts", "limit": 50},
                },
            },
        )
        assert response is not None
        payload = json.loads(response["result"]["content"][0]["text"])
        assert MARKER not in json.dumps(payload["results"], ensure_ascii=False)
    for args in (("list",), ("index", "search", MARKER), ("stats",)):
        output = CliRunner().invoke(app, [*args, "--root", str(root)]).output
        assert MARKER not in output, args


def test_mcp_switches_are_validated_and_handlers_route(tmp_path: Path) -> None:
    repository, ids = _corpus(tmp_path)
    config = repository.read_config()
    for bad in ({"attachments": "yes"}, {"local_originals": 1}, ["attachments"]):
        config["mcp"] = bad
        with pytest.raises(IntegrityError, match="mcp"):
            repository.configure(config)
    _enable(repository)
    handlers = Gateway(repository.root, entity_enabled=False).handlers()
    found = handlers["attachment_search"]({"query": "backup schedule", "limit": 2})
    assert len(found["results"]) == 2
    got = handlers["attachment_get"]({"id": ids["shared"], "locator": "page=1"})
    assert [s["n"] for s in got["proxies"][0]["segments"]] == [1]
    with pytest.raises(PermissionError):
        handlers["attachment_get"]({"id": ids["shared"], "original": "path"})


def test_an_index_from_another_vault_at_the_same_path_is_never_used(tmp_path: Path) -> None:
    import shutil

    repository, ids = _corpus(tmp_path)
    index = ensure_proxy_index(repository, now=NOW)
    assert index is not None
    assert index.meta() is not None and index.meta()["vault_path"]  # type: ignore[index]
    # Delete the vault and create another one at the same path with its own proxy.
    shutil.rmtree(repository.root)
    fresh = _repo(tmp_path)
    data = b"%PDF fresh original"
    attachment = _attach(fresh, tmp_path, data, "fresh.pdf")
    _proxy(fresh, tmp_path, attachment, data, ["fresh vault text"])
    rebuilt = ensure_proxy_index(fresh, now=NOW)
    assert rebuilt is not None and rebuilt.path == index.path
    hits = rebuilt.search("backup schedule fresh", tenant="", clearance=None, limit=50)
    assert {hit.attachment_id for hit in hits} == {attachment}
