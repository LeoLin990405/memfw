"""MM1a attachments: blob store outside the vault, records, events, ``memfw attach``."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from memfw.attachments import (
    ATTACHMENTS_DIR,
    AttachmentRecord,
    BlobLocationError,
    BlobStore,
    parse_record,
    render_record,
)
from memfw.attachments import blobs as blobs_module
from memfw.attachments.records import new_attachment_id
from memfw.cli import app
from memfw.core.errors import IntegrityError, NotFoundError
from memfw.core.models import (
    ATTACHMENT_EVENT_KINDS,
    MEMORY_EVENT_KINDS,
    NON_MEMORY_EVENT_KINDS,
    EventKind,
)
from memfw.core.repository import Repository
from memfw.ports.privacy import LEGACY_PRIVACY_KEY

NOW = datetime(2026, 11, 17, 9, 0, tzinfo=UTC)
MARKER = "zqxattachmarker"


@pytest.fixture(autouse=True)
def private_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
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


def _file(tmp_path: Path, name: str, data: bytes) -> Path:
    path = tmp_path / "in" / name
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(data)
    return path


def _blob_dir(repository: Repository) -> Path:
    store = repository.attachment_blob_store()
    assert store is not None
    return store.directory


def _cli(*args: str) -> Any:
    return CliRunner().invoke(app, list(args))


# --- the event manifest -------------------------------------------------------------


def test_every_event_kind_is_classified_exactly_once() -> None:
    kinds = set(EventKind)
    assert MEMORY_EVENT_KINDS | NON_MEMORY_EVENT_KINDS == kinds
    assert not MEMORY_EVENT_KINDS & NON_MEMORY_EVENT_KINDS
    assert ATTACHMENT_EVENT_KINDS <= NON_MEMORY_EVENT_KINDS
    for name in (
        "ATTACH",
        "ATTACH_UPDATE",
        "DETACH",
        "ATTACH_PURGE",
        "ATTACH_MISSING",
        "ATTACH_RELINK",
        "PROXY_ADD",
        "PROXY_SUPERSEDE",
    ):
        assert EventKind[name] in NON_MEMORY_EVENT_KINDS


def test_synced_markers_match_the_history_guard() -> None:
    from memfw.history.transcripts import SYNCED_MARKERS

    assert blobs_module.SYNCED_MARKERS == SYNCED_MARKERS


# --- add / list / show / verify -------------------------------------------------------


def test_add_list_show_verify(tmp_path: Path, private_home: Path) -> None:
    root = tmp_path / "vault"
    assert _cli("init", str(root)).exit_code == 0
    source = _file(tmp_path, "scan.png", b"\x89PNG fake image bytes")
    added = _cli("attach", "add", str(source), "--root", str(root), "--note", "first page")
    assert added.exit_code == 0, added.output
    payload = json.loads(added.output)
    attachment_id = payload["id"]
    sha = hashlib.sha256(source.read_bytes()).hexdigest()
    assert payload == {
        "deduplicated": False,
        "id": attachment_id,
        "media_type": "image/png",
        "privacy": "local-private",
        "sha256": sha,
        "size": len(source.read_bytes()),
        "storage": "embedded",
    }
    # The record is in attachments/, never in memories/.
    assert (root / ATTACHMENTS_DIR / f"{attachment_id}.md").is_file()
    assert not list((root / "memories").glob("*.md"))
    # The blob is outside the vault, under the data dir, private.
    vault_id = yaml.safe_load((root / "vault.yaml").read_text())["vault_id"]
    blob_dir = private_home / ".local/share/memfw/blobs" / vault_id
    blob = blob_dir / sha[:2] / sha
    assert blob.read_bytes() == source.read_bytes()
    assert stat.S_IMODE(blob.stat().st_mode) == 0o600
    for directory in (blob_dir, blob.parent):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert root.resolve() not in blob.resolve().parents
    # The record never carries an absolute path or the bytes.
    record_text = (root / ATTACHMENTS_DIR / f"{attachment_id}.md").read_text()
    assert str(tmp_path) not in record_text
    assert "fake image bytes" not in record_text

    listed = _cli("attach", "list", "--root", str(root))
    assert listed.exit_code == 0
    assert (
        listed.output == f"{attachment_id}\timage/png\t{payload['size']}\tlocal-private\tscan.png\n"
    )
    shown = json.loads(_cli("attach", "show", attachment_id, "--root", str(root)).output)
    assert shown["sha256"] == sha
    assert shown["note"] == "first page"
    assert shown["privacy_level"] == "local-private"
    assert shown["cited_by"] == []
    verified = _cli("attach", "verify", "--root", str(root))
    assert verified.exit_code == 0
    assert json.loads(verified.output) == {"checked": 1, "ok": 1, "problems": []}
    # Exactly two events: the vault_id config change and the attach.
    repository = Repository(root)
    kinds = [event.kind for event in repository.events()]
    assert kinds == [EventKind.CONFIG_CHANGE, EventKind.ATTACH]
    attach = repository.events()[-1]
    assert attach.memory_id == attachment_id
    assert attach.details == {"storage": "embedded"}
    assert attach.after_hash == hashlib.sha256(record_text.encode()).hexdigest()


def test_show_unknown_and_malformed_ids(tmp_path: Path) -> None:
    repository = _repo(tmp_path)
    root = str(repository.root)
    missing = _cli("attach", "show", new_attachment_id(NOW, b"x" * 10), "--root", root)
    assert missing.exit_code == 1
    assert "not found" in missing.output
    for bad in ("../memories/x", "att-1", "m-1"):
        result = _cli("attach", "show", bad, "--root", root)
        assert result.exit_code == 1
        assert "Invalid attachment id" in result.output


def test_dedupe_by_sha(tmp_path: Path) -> None:
    repository = _repo(tmp_path)
    first = repository.add_attachment(_file(tmp_path, "a.txt", b"same bytes"))
    second = repository.add_attachment(_file(tmp_path, "b.txt", b"same bytes"))
    other = repository.add_attachment(_file(tmp_path, "c.txt", b"other bytes"))
    assert not first.deduplicated
    assert second.deduplicated
    assert not other.deduplicated
    assert first.record.id != second.record.id
    assert first.record.sha256 == second.record.sha256
    store = repository.attachment_blob_store()
    assert store is not None
    assert sorted(store.stored()) == sorted({first.record.sha256, other.record.sha256})
    assert repository.attachment_ids() == [first.record.id, second.record.id, other.record.id]
    assert repository.verify_attachments() == (repository.attachment_ids(), [])


# --- tamper and missing ----------------------------------------------------------------


def _statuses(repository: Repository) -> list[tuple[str | None, str]]:
    return [(item.attachment_id, item.status) for item in repository.verify_attachments()[1]]


def test_tampered_and_missing_blobs_are_detected(tmp_path: Path) -> None:
    repository = _repo(tmp_path)
    same = repository.add_attachment(_file(tmp_path, "a.bin", b"aaaa")).record
    grown = repository.add_attachment(_file(tmp_path, "b.bin", b"bbbb")).record
    gone = repository.add_attachment(_file(tmp_path, "c.bin", b"cccc")).record
    store = repository.attachment_blob_store()
    assert store is not None
    for record, data in ((same, b"AAAA"), (grown, b"bbbbb")):
        path = store.path_for(record.sha256)
        os.chmod(path, 0o600)
        path.write_bytes(data)
    store.path_for(gone.sha256).unlink()
    assert _statuses(repository) == [
        (same.id, "blob_modified"),
        (grown.id, "blob_modified"),
        (gone.id, "blob_missing"),
    ]
    result = _cli("attach", "verify", "--root", str(repository.root))
    assert result.exit_code == 1
    single = _cli("attach", "verify", gone.id, "--root", str(repository.root))
    assert single.exit_code == 1
    assert json.loads(single.output)["problems"][0]["status"] == "blob_missing"
    issues = repository.fsck()
    assert [(row["memory_id"], row["kind"]) for row in issues] == [
        (same.id, "attachment_blob_modified"),
        (grown.id, "attachment_blob_modified"),
        (gone.id, "attachment_blob_missing"),
    ]
    assert not [e for e in repository.events() if e.kind == EventKind.EXTERNAL_DELETE]
    # A tampered blob is never reused for a new registration of its address.
    with pytest.raises(IntegrityError, match="attach verify"):
        repository.add_attachment(_file(tmp_path, "again.bin", b"aaaa"))


def test_symlinked_blob_is_not_followed(tmp_path: Path) -> None:
    repository = _repo(tmp_path)
    record = repository.add_attachment(_file(tmp_path, "a.bin", b"aaaa")).record
    store = repository.attachment_blob_store()
    assert store is not None
    path = store.path_for(record.sha256)
    decoy = tmp_path / "decoy"
    decoy.write_bytes(b"aaaa")
    path.unlink()
    path.symlink_to(decoy)
    assert _statuses(repository) == [(record.id, "blob_not_regular")]


def test_tampered_missing_and_untracked_records(tmp_path: Path) -> None:
    repository = _repo(tmp_path)
    edited = repository.add_attachment(_file(tmp_path, "a.txt", b"a")).record
    removed = repository.add_attachment(_file(tmp_path, "b.txt", b"b")).record
    directory = repository.root / ATTACHMENTS_DIR
    path = directory / f"{edited.id}.md"
    path.write_text(path.read_text().replace("local-private", "public"))
    (directory / f"{removed.id}.md").unlink()
    stray = new_attachment_id(NOW, b"s" * 10)
    (directory / f"{stray}.md").write_text("---\nid: x\n---\n")
    with pytest.raises(IntegrityError, match="hash mismatch"):
        repository.get_attachment(edited.id)
    with pytest.raises(NotFoundError):
        repository.get_attachment(removed.id)
    with pytest.raises(NotFoundError):
        repository.get_attachment(stray)
    statuses = _statuses(repository)
    assert (edited.id, "record_modified") in statuses
    assert (removed.id, "record_missing") in statuses
    assert (stray, "record_untracked") in statuses
    # A modified but readable record still references its blob; the blob of
    # the missing record has no readable record left, so it reads as an orphan
    # (reported only: MM1a never deletes a blob).
    orphans = [p.sha256 for p in repository.verify_attachments()[1] if p.status == "blob_orphan"]
    assert orphans == [removed.sha256]
    before = len(repository.events())
    issues = repository.fsck()
    assert {row["kind"] for row in issues} == {
        "attachment_record_modified",
        "attachment_record_missing",
        "attachment_record_untracked",
        "attachment_blob_orphan",
    }
    # fsck reports; it writes no event and no external_delete for attachments.
    assert len(repository.events()) == before
    listed = _cli("attach", "list", "--root", str(repository.root))
    assert listed.exit_code == 1


# --- fsck -------------------------------------------------------------------------------


def test_fsck_is_clean_and_never_projects_attachments(tmp_path: Path) -> None:
    repository = _repo(tmp_path)
    memory = repository.add("Note", "A plain memory.")
    record = repository.add_attachment(_file(tmp_path, "a.pdf", b"%PDF-1.7 tiny")).record
    assert repository.fsck() == []
    replayed = repository.replay()
    assert memory.id in replayed
    assert record.id not in replayed
    assert record.id not in {document.id for document in repository.list()}
    # Even with the record gone, fsck does not register an external delete.
    (repository.root / ATTACHMENTS_DIR / f"{record.id}.md").unlink()
    issues = repository.fsck()
    assert [row["kind"] for row in issues] == [
        "attachment_record_missing",
        "attachment_blob_orphan",
    ]
    assert EventKind.EXTERNAL_DELETE not in {event.kind for event in repository.events()}
    # Log compaction keeps attach events out of the memory snapshot too.
    repository.compact_log(keep_recent=1)
    assert record.id in repository.attachment_ids()
    assert record.id not in repository.replay()


def test_fsck_without_attachments_reads_no_blob_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _repo(tmp_path)
    repository.add("Note", "A plain memory.")

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("blob store opened without attachments")

    monkeypatch.setattr(BlobStore, "open", refuse)
    assert repository.fsck() == []
    assert "vault_id" not in repository.read_config()


def test_orphan_blob_is_reported_never_deleted(tmp_path: Path) -> None:
    repository = _repo(tmp_path)
    repository.add_attachment(_file(tmp_path, "a.txt", b"kept"))
    store = repository.attachment_blob_store()
    assert store is not None
    with _file(tmp_path, "o.txt", b"orphan").open("rb") as stream:
        staged = store.stage(stream, max_bytes=100)
    store.link(staged)
    statuses = _statuses(repository)
    assert statuses == [(None, "blob_orphan")]
    result = _cli("attach", "verify", "--root", str(repository.root))
    assert result.exit_code == 0
    assert json.loads(result.output)["problems"][0]["sha256"] == staged.sha256
    issues = repository.fsck()
    assert issues == [
        {
            "memory_id": None,
            "kind": "attachment_blob_orphan",
            "expected_hash": None,
            "actual_hash": staged.sha256,
            "message": None,
        }
    ]
    assert store.path_for(staged.sha256).exists()


def test_recover_drops_an_uncommitted_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _repo(tmp_path)
    repository.ensure_vault_id()
    original = Repository._append_event

    def crash(self: Repository, kind: EventKind, *args: Any, **kwargs: Any) -> Any:
        if kind == EventKind.ATTACH:
            raise RuntimeError("crash before the attach event")
        return original(self, kind, *args, **kwargs)

    monkeypatch.setattr(Repository, "_append_event", crash)
    with pytest.raises(RuntimeError):
        repository.add_attachment(_file(tmp_path, "a.txt", b"crash"))
    monkeypatch.setattr(Repository, "_append_event", original)
    assert repository.pending_path.exists()
    assert list((repository.root / ATTACHMENTS_DIR).glob("*.md"))
    changed = repository.recover_pending()
    assert len(changed) == 1
    assert not list((repository.root / ATTACHMENTS_DIR).glob("*.md"))
    assert repository.attachment_ids() == []
    # The blob written first is now an orphan: reported, kept.
    assert _statuses(repository) == [(None, "blob_orphan")]


def test_failed_checks_write_nothing(tmp_path: Path) -> None:
    repository = _repo(tmp_path)
    before = repository.events_path.read_bytes(), repository.config_path.read_bytes()
    source = _file(tmp_path, "a.txt", b"x")
    for kwargs in (
        {"privacy": "secret"},
        {"retention": "forever"},
        {"captured_at": "2026-01-01T00:00:00"},
        {"media_type": "not a type"},
    ):
        with pytest.raises(ValueError):
            repository.add_attachment(source, **kwargs)
    assert (repository.events_path.read_bytes(), repository.config_path.read_bytes()) == before
    assert not repository.pending_path.exists()
    assert not (repository.root / ATTACHMENTS_DIR).exists()


# --- limits and media ------------------------------------------------------------------


def test_size_limits(tmp_path: Path) -> None:
    repository = _repo(tmp_path)
    config = repository.read_config()
    config.update(attachments_max_bytes=8, attachments_max_total_bytes=10)
    repository.configure(config)
    with pytest.raises(ValueError, match="--reference"):
        repository.add_attachment(_file(tmp_path, "big.txt", b"123456789"))
    assert "vault_id" not in repository.read_config()
    repository.add_attachment(_file(tmp_path, "a.txt", b"123456"))
    with pytest.raises(ValueError, match="attachments_max_total_bytes"):
        repository.add_attachment(_file(tmp_path, "b.txt", b"abcdef"))
    assert not repository.pending_path.exists()
    # The same bytes again add nothing to the total.
    assert repository.add_attachment(_file(tmp_path, "c.txt", b"123456")).deduplicated
    store = repository.attachment_blob_store()
    assert store is not None
    assert len(list(store.stored())) == 1
    assert not list(store.directory.glob(".stage-*"))


def test_audio_and_video_need_embed(tmp_path: Path) -> None:
    repository = _repo(tmp_path)
    for name in ("talk.mp3", "clip.mp4"):
        with pytest.raises(ValueError, match="--embed"):
            repository.add_attachment(_file(tmp_path, name, b"media"))
    record = repository.add_attachment(_file(tmp_path, "talk.mp3", b"media"), embed=True).record
    assert record.media_type == "audio/mpeg"
    refused = _cli("attach", "add", str(_file(tmp_path, "v.mp4", b"v")), "-r", str(repository.root))
    assert refused.exit_code == 1


def test_config_keys_are_validated(tmp_path: Path) -> None:
    repository = _repo(tmp_path)
    for key, value in (
        ("vault_id", "../escape"),
        ("vault_id", 7),
        ("attachments_max_bytes", 0),
        ("attachments_max_total_bytes", True),
        ("attachments_blob_root", ""),
    ):
        config = repository.read_config()
        config[key] = value
        with pytest.raises(IntegrityError):
            repository.configure(config)


# --- path safety --------------------------------------------------------------------


def test_source_must_be_a_regular_file(tmp_path: Path) -> None:
    repository = _repo(tmp_path)
    target = _file(tmp_path, "real.txt", b"real")
    link = tmp_path / "in" / "link.txt"
    link.symlink_to(target)
    with pytest.raises(ValueError, match="symlink"):
        repository.add_attachment(link)
    with pytest.raises(ValueError, match="regular"):
        repository.add_attachment(tmp_path / "in")
    with pytest.raises(ValueError, match="no such file"):
        repository.add_attachment(tmp_path / "in" / "missing.txt")
    assert repository.attachment_ids() == []


def _set_root(repository: Repository, root: Path | str) -> None:
    config = repository.read_config()
    config["attachments_blob_root"] = str(root)
    repository.configure(config)


def test_blob_root_must_be_private(tmp_path: Path) -> None:
    repository = _repo(tmp_path)
    source = _file(tmp_path, "a.txt", b"a")
    # Inside the vault.
    _set_root(repository, repository.root / "blobs")
    with pytest.raises(BlobLocationError, match="vault"):
        repository.add_attachment(source)
    # Inside a git work tree.
    work = tmp_path / "work"
    (work / ".git").mkdir(parents=True)
    _set_root(repository, work / "blobs")
    with pytest.raises(BlobLocationError, match="git"):
        repository.add_attachment(source)
    # A synced folder.
    _set_root(repository, Path.home() / "Dropbox" / "blobs")
    with pytest.raises(BlobLocationError, match="synced"):
        repository.add_attachment(source)
    # A symlinked root.
    real = tmp_path / "real-root"
    real.mkdir()
    (tmp_path / "link-root").symlink_to(real)
    _set_root(repository, tmp_path / "link-root")
    with pytest.raises(BlobLocationError, match="symlink"):
        repository.add_attachment(source)
    # A symlinked vault blob directory.
    good = tmp_path / "good-root"
    _set_root(repository, good)
    record = repository.add_attachment(source).record
    store = repository.attachment_blob_store()
    assert store is not None
    assert store.directory.parent == good.resolve()
    moved = tmp_path / "moved"
    store.directory.rename(moved)
    store.directory.symlink_to(moved)
    with pytest.raises(BlobLocationError, match="symlink"):
        repository.attachment_blob_store()
    assert _statuses(repository) == [(record.id, "blob_unavailable")]
    assert repository.attachment_ids() == [record.id]


# --- privacy --------------------------------------------------------------------------


def test_privacy_on_records(tmp_path: Path) -> None:
    repository = _repo(tmp_path)
    config = repository.read_config()
    config[LEGACY_PRIVACY_KEY] = "shared"
    repository.configure(config)
    default = repository.add_attachment(_file(tmp_path, "a.txt", b"a")).record
    shared = repository.add_attachment(_file(tmp_path, "b.txt", b"b"), privacy=" Shared ").record
    tagged = repository.add_attachment(
        _file(tmp_path, "c.txt", b"c"), privacy="public", tags=["privacy:local-private"]
    ).record
    assert (default.privacy, default.privacy_level) == ("local-private", "local-private")
    assert (shared.privacy, shared.privacy_level) == ("shared", "shared")
    assert (tagged.privacy, tagged.privacy_level) == ("public", "local-private")
    # A record without the field is local-private, whatever legacy_privacy_default says.
    raw = render_record(shared).decode().replace("privacy: shared\n", "")
    assert parse_record(raw.encode(), shared.id).privacy_level == "local-private"
    # An unrecognised value and a sensitivity key fail closed.
    odd = render_record(shared).decode().replace("privacy: shared", "privacy: team")
    assert parse_record(odd.encode(), shared.id).privacy_level == "unknown"
    sensitive = (
        render_record(shared).decode().replace("retention:", "sensitivity: private\nretention:")
    )
    assert parse_record(sensitive.encode(), shared.id).privacy_level == "local-private"
    assert repository.get_attachment(tagged.id).privacy_level == "local-private"


def test_record_round_trip_and_unknown_keys(tmp_path: Path) -> None:
    record = AttachmentRecord(
        id=new_attachment_id(NOW, b"r" * 10),
        sha256="a" * 64,
        size=3,
        media_type="application/pdf",
        recorded_at=NOW.isoformat(),
        original_name="report.pdf",
        origin="scanner",
        captured_at="2026-11-01T08:00:00+00:00",
        retention="until:2027-01-01",
        rights="own work",
        tags=("topic:x",),
        note="Two pages.",
    )
    raw = render_record(record)
    assert parse_record(raw, record.id) == record
    future = raw.decode().replace("retention:", "proxies: []\nretention:").encode()
    assert parse_record(future, record.id).ignored_keys == ("proxies",)
    with pytest.raises(IntegrityError):
        parse_record(raw, new_attachment_id(NOW, b"q" * 10))


def test_uuid7_ids_are_time_ordered() -> None:
    early = new_attachment_id(datetime(2026, 1, 1, tzinfo=UTC), b"\xff" * 10)
    late = new_attachment_id(datetime(2026, 1, 2, tzinfo=UTC), b"\x00" * 10)
    assert early < late
    assert early[4:].split("-")[2].startswith("7")


# --- exits: attachments are not served anywhere yet -----------------------------------


def _leaky_vault(tmp_path: Path) -> tuple[Repository, str]:
    repository = _repo(tmp_path)
    repository.add("Corpus note", "corpus keyword about the scan", memory_id="m-plain")
    record = repository.add_attachment(
        _file(tmp_path, f"{MARKER}.pdf", f"%PDF {MARKER} bytes".encode()),
        privacy="public",
        origin=MARKER,
        note=f"corpus keyword {MARKER}",
        tags=[f"topic:{MARKER}"],
    ).record
    from memfw.index import MemoryIndex

    MemoryIndex(repository.index_path).rebuild(repository)
    return repository, record.id


def _clean(payload: Any, attachment_id: str) -> bool:
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return MARKER not in text and attachment_id not in text


def test_attachments_leak_through_no_existing_exit(tmp_path: Path) -> None:
    import scripts.memfw_readonly_service as service
    from memfw.index.readonly import search_vault
    from memfw.mcp.server import Gateway
    from memfw.okf import export_okf
    from memfw.projection import export_projection

    repository, attachment_id = _leaky_vault(tmp_path)
    root = repository.root
    # Vault reads.
    assert [document.id for document in repository.list()] == ["m-plain"]
    # HTTP read path (library and the standalone script), no clearance at all.
    for search in (search_vault, service.search_vault):
        result = search(root, "corpus keyword", limit=50)
        assert [row["id"] for row in result["results"]] == ["m-plain"]
        assert _clean(result, attachment_id)
        assert _clean(search(root, MARKER, limit=50), attachment_id)
    # memfw MCP.
    gateway = Gateway(root, entity_enabled=False)
    for mode in ("fts", "hybrid", "multihop", "two_stage"):
        assert _clean(gateway.search("corpus keyword", limit=50, mode=mode), attachment_id)
        assert _clean(gateway.search(MARKER, limit=50, mode=mode), attachment_id)
    assert _clean(gateway.context(4000), attachment_id)
    status = gateway.status()
    assert status["memories"] == 1
    assert _clean(status, attachment_id)
    with pytest.raises(NotFoundError):
        gateway.get(attachment_id)
    # OKF export.
    export_okf(root, tmp_path / "bundle")
    bundle = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((tmp_path / "bundle").rglob("*"))
        if path.is_file()
    )
    assert _clean(bundle, attachment_id)
    # Recall projection.
    export_projection(root, tmp_path / "projection", now=NOW)
    projected = "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in sorted((tmp_path / "projection").rglob("*"))
        if path.is_file()
    )
    assert _clean(projected, attachment_id)
    # Obsidian board projection.
    from memfw.projections import ObsidianProjector

    ObsidianProjector().build(root, tmp_path / "board")
    board = "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in sorted((tmp_path / "board").rglob("*"))
        if path.is_file()
    )
    assert _clean(board, attachment_id)
    # CLI read commands (other than `memfw attach`).
    for args in (("list",), ("index", "search", MARKER), ("stats",)):
        output = _cli(*args, "--root", str(root)).output
        assert _clean(output, attachment_id), args


# --- write cost is independent of blob volume -----------------------------------------


def test_vault_writes_never_read_blobs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repository = _repo(tmp_path)
    for index in range(3):
        data = bytes([index]) * (2 * 1024 * 1024)
        repository.add_attachment(_file(tmp_path, f"big{index}.bin", data))
    blob_dir = _blob_dir(repository)
    fingerprint = repository._tree_fingerprint()
    checks: list[str] = []
    original = BlobStore.check

    def counted(self: BlobStore, sha256: str, **kwargs: Any) -> Any:
        checks.append(sha256)
        return original(self, sha256, **kwargs)

    monkeypatch.setattr(BlobStore, "check", counted)
    os.chmod(blob_dir, 0)
    try:
        # A memory write (and its whole-vault fingerprint) never touches the blobs.
        repository.add("Note", "Unrelated memory write.")
        repository.update(repository.list()[0].id, body="Edited.")
    finally:
        os.chmod(blob_dir, 0o700)
    assert checks == []
    assert repository._tree_fingerprint() != fingerprint  # the memory writes did land
    # Adding a new original re-hashes only its own address, not the stored blobs.
    checks.clear()
    repository.add_attachment(_file(tmp_path, "small.txt", b"small"))
    assert checks == [hashlib.sha256(b"small").hexdigest()]


def test_fingerprint_ignores_blob_volume(tmp_path: Path) -> None:
    repository = _repo(tmp_path)
    record = repository.add_attachment(_file(tmp_path, "a.bin", b"a" * 1024)).record
    before = repository._tree_fingerprint()
    store = repository.attachment_blob_store()
    assert store is not None
    path = store.path_for(record.sha256)
    os.chmod(path, 0o600)
    path.write_bytes(b"b" * (3 * 1024 * 1024))
    assert repository._tree_fingerprint() == before


# --- citations ----------------------------------------------------------------------


def test_show_lists_citing_memories(tmp_path: Path) -> None:
    repository = _repo(tmp_path)
    record = repository.add_attachment(_file(tmp_path, "a.pdf", b"%PDF")).record
    repository.add("Cites", "see scan", memory_id="m-cites", evidence=[f"att:{record.id}#page=1"])
    repository.add("Other", "no scan", memory_id="m-other", evidence=["att:att-other"])
    assert repository.attachment_citations(record.id) == ["m-cites"]
    shown = json.loads(_cli("attach", "show", record.id, "--root", str(repository.root)).output)
    assert shown["cited_by"] == ["m-cites"]


# --- review fixes ---------------------------------------------------------------------


def test_dashes_in_values_never_corrupt_a_record(tmp_path: Path) -> None:
    repository = _repo(tmp_path)
    record = repository.add_attachment(
        _file(tmp_path, "a---b.txt", b"dashes"),
        origin="scan\n---\nline",
        rights="---",
        tags=["x---", "---", "a\n---"],
        note="body\n---\nmore",
    ).record
    read = repository.get_attachment(record.id)
    assert read.original_name == "a---b.txt"
    assert read.origin == "scan\n---\nline"
    assert read.rights == "---"
    assert read.tags == ("x---", "---", "a\n---")
    assert read.note == "body\n---\nmore"
    assert read.recorded_at == record.recorded_at
    assert repository.verify_attachments() == ([record.id], [])
    # Frontmatter is split on whole lines only.
    raw = render_record(read)
    assert parse_record(raw, read.id) == read
    with pytest.raises(IntegrityError, match="frontmatter"):
        parse_record(b"--- \nid: x\n---\n", read.id)
    with pytest.raises(IntegrityError, match="unterminated"):
        parse_record(b"---\nid: x\nrights: x---\n", read.id)


def test_a_record_that_does_not_read_back_is_refused(tmp_path: Path) -> None:
    record = AttachmentRecord(
        id=new_attachment_id(NOW, b"r" * 10),
        sha256="a" * 64,
        size=3,
        media_type="text/plain",
        recorded_at=NOW.isoformat(),
        # A tuple is written as a YAML list and reads back as a list.
        sensitivity=("private",),
    )
    with pytest.raises(ValueError, match="read back"):
        render_record(record)
