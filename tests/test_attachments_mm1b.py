"""MM1b attachments: referenced storage under configured roots, path safety, detach,
relink and the missing state, purge, scan."""

from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile
import unicodedata
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
import zstandard
from typer.testing import CliRunner

from memfw.attachments import ATTACHMENTS_DIR
from memfw.attachments import blobs as blobs_module
from memfw.attachments import lifecycle as lifecycle_module
from memfw.attachments.locations import Location, LocationError, location_for_path
from memfw.attachments.store import embedded_total
from memfw.cli import app
from memfw.core.errors import IntegrityError, NotFoundError
from memfw.core.models import EventKind, Tier
from memfw.core.repository import Repository

NOW = datetime(2026, 11, 17, 9, 0, tzinfo=UTC)
MARKER = "zqxpurgemarker"


@pytest.fixture(autouse=True)
def private_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


def _repo(tmp_path: Path, *, roots: dict[str, Path] | None = None) -> Repository:
    counter = iter(range(1, 1_000_000))
    repository = Repository(
        tmp_path / "vault",
        clock=lambda: NOW,
        id_factory=lambda: hashlib.sha256(f"seed:{next(counter)}".encode()).hexdigest()[:32],
    )
    repository.init()
    if roots is not None:
        config = repository.read_config()
        config["attachments_roots"] = {name: str(path) for name, path in roots.items()}
        repository.configure(config)
    return repository


def _tree(tmp_path: Path) -> Path:
    root = tmp_path / "kh-root"
    (root / "papers").mkdir(parents=True)
    (root / "papers" / "a.pdf").write_bytes(b"%PDF-1.7 first paper")
    (root / "papers" / "b.pdf").write_bytes(b"%PDF-1.7 second paper")
    (root / "img").mkdir()
    (root / "img" / "photo.png").write_bytes(b"\x89PNG photo")
    return root


def _kh(tmp_path: Path) -> tuple[Repository, Path]:
    root = _tree(tmp_path)
    return _repo(tmp_path, roots={"kh": root}), root


def _cli(*args: str) -> Any:
    return CliRunner().invoke(app, list(args))


def _snapshot(repository: Repository) -> dict[str, bytes]:
    return {
        path.relative_to(repository.root).as_posix(): path.read_bytes()
        for path in sorted(repository.root.rglob("*"))
        if path.is_file() and path.name != ".memfw.lock"
    }


# --- referenced storage ---------------------------------------------------------------


def test_reference_records_a_location_and_copies_nothing(
    tmp_path: Path, private_home: Path
) -> None:
    repository, root = _kh(tmp_path)
    by_path = repository.add_attachment(root / "papers" / "a.pdf", reference=True).record
    by_location = repository.add_attachment("kh:papers/b.pdf", reference=True).record
    assert by_path.storage == by_location.storage == "referenced"
    assert by_path.location == "kh:papers/a.pdf"
    assert by_location.location == "kh:papers/b.pdf"
    assert by_path.sha256 == hashlib.sha256(b"%PDF-1.7 first paper").hexdigest()
    assert by_path.size == len(b"%PDF-1.7 first paper")
    assert by_path.original_name == "a.pdf"
    assert by_path.privacy_level == "local-private"
    # No copy, no blob store, no vault_id: a reference needs none of them.
    assert "vault_id" not in repository.read_config()
    assert not (private_home / ".local/share/memfw/blobs").exists()
    record_text = (repository.root / ATTACHMENTS_DIR / f"{by_path.id}.md").read_text()
    assert "location: kh:papers/a.pdf" in record_text
    assert str(root) not in record_text
    attach = [event for event in repository.events() if event.kind == EventKind.ATTACH]
    assert [event.details for event in attach] == [
        {"storage": "referenced", "size": by_path.size},
        {"storage": "referenced", "size": by_location.size},
    ]
    # The log never carries the location or the name.
    log = repository.events_path.read_text()
    assert "papers" not in log and "a.pdf" not in log
    assert repository.verify_attachments() == ([by_path.id, by_location.id], [])
    assert repository.fsck() == []
    # CLI.
    vault = str(repository.root)
    added = _cli("attach", "add", "kh:img/photo.png", "--reference", "-r", vault)
    assert added.exit_code == 0, added.output
    payload = json.loads(added.output)
    assert payload["storage"] == "referenced"
    assert payload["location"] == "kh:img/photo.png"
    listed = json.loads(_cli("attach", "list", "--json", "-r", vault).output)
    assert [row.get("location") for row in listed] == [
        "kh:papers/a.pdf",
        "kh:papers/b.pdf",
        "kh:img/photo.png",
    ]


def test_reference_ignores_the_embed_limits(tmp_path: Path) -> None:
    repository, root = _kh(tmp_path)
    config = repository.read_config()
    config.update(attachments_max_bytes=4, attachments_max_total_bytes=4)
    repository.configure(config)
    with pytest.raises(ValueError, match="--reference"):
        repository.add_attachment(root / "papers" / "a.pdf")
    record = repository.add_attachment(root / "papers" / "a.pdf", reference=True).record
    assert record.size > 4


def test_audio_and_video_default_to_a_reference_inside_a_root(tmp_path: Path) -> None:
    repository, root = _kh(tmp_path)
    (root / "talk.mp3").write_bytes(b"audio")
    record = repository.add_attachment(root / "talk.mp3").record
    assert record.storage == "referenced" and record.location == "kh:talk.mp3"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "clip.mp4").write_bytes(b"video")
    with pytest.raises(ValueError, match="--embed"):
        repository.add_attachment(outside / "clip.mp4")
    assert repository.add_attachment(outside / "clip.mp4", embed=True).record.storage == "embedded"
    with pytest.raises(ValueError, match="exclude"):
        repository.add_attachment(root / "talk.mp3", embed=True, reference=True)


def test_roots_config_is_validated(tmp_path: Path) -> None:
    repository = _repo(tmp_path / "nest")
    for value in (
        "not a mapping",
        {"Upper": "/data"},
        {"file": "/data"},
        {"http": "/data"},
        {"att": "/data"},
        {"kh": ""},
        {"kh": "relative/dir"},
        {"kh": "/data/../etc"},
        {"kh": 7},
        {"1kh": "/data"},
    ):
        config = repository.read_config()
        config["attachments_roots"] = value
        with pytest.raises(IntegrityError):
            repository.configure(config)
    # Relative to the vault, the home directory and the blob root: refused when
    # configured, and (for a hand-edited vault.yaml) when used.
    for value, message in (
        ({"in": str(repository.root / "inside")}, "inside the vault"),
        ({"up": str(tmp_path / "nest")}, "contain the vault"),
        ({"slash": "/"}, "/ or the home"),
        ({"home": "~"}, "/ or the home"),
        ({"local": str(Path.home() / ".local")}, "contain the blob root"),
    ):
        config = repository.read_config()
        config["attachments_roots"] = value
        with pytest.raises(IntegrityError, match=message):
            repository.configure(config)
    (repository.root / "inside").mkdir()
    (repository.root / "inside" / "x.txt").write_bytes(b"x")
    text = repository.config_path.read_text()
    repository.config_path.write_text(
        text + f"attachments_roots:\n  in: {repository.root / 'inside'}\n"
    )
    with pytest.raises(IntegrityError, match="inside the vault"):
        repository.add_attachment(repository.root / "inside" / "x.txt", reference=True)


# --- path safety ----------------------------------------------------------------------


def test_escape_cases_are_all_refused(tmp_path: Path) -> None:
    repository, root = _kh(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_bytes(b"outside the root")
    (root / "link-out.txt").symlink_to(secret)
    (root / "link-in.pdf").symlink_to(root / "papers" / "a.pdf")
    (root / "dir-out").symlink_to(outside, target_is_directory=True)
    (root / "Report.pdf").write_bytes(b"report")
    nfd = unicodedata.normalize("NFD", "café.pdf")
    (root / nfd).write_bytes(b"accented")
    os.mkfifo(root / "pipe")
    before = _snapshot(repository)
    refused: list[tuple[str | Path, type[Exception], str]] = [
        # `..`, absolute and outside-root paths.
        (root / "papers" / ".." / ".." / "outside" / "secret.txt", LocationError, r"'\.\.'"),
        (root / ".." / "outside" / "secret.txt", LocationError, r"'\.\.'"),
        (secret, LocationError, "not inside any configured"),
        (Path("/etc/hosts"), LocationError, "not inside any configured"),
        ("kh:../outside/secret.txt", LocationError, r"'\.\.'"),
        ("kh:papers/../../outside/secret.txt", LocationError, r"'\.\.'"),
        ("kh:/etc/hosts", LocationError, "relative"),
        ("kh:", LocationError, "relative path"),
        ("kh:papers//a.pdf", LocationError, "components"),
        ("kh:./papers/a.pdf", LocationError, "components"),
        ("kh:papers\\a.pdf", LocationError, "backslash"),
        ("kh:papers/a\x00.pdf", LocationError, "control"),
        ("kh:papers/a\n.pdf", LocationError, "control"),
        # Symlinks anywhere in the path, pointing out of or inside the root.
        (root / "link-out.txt", LocationError, "symlink"),
        ("kh:link-in.pdf", LocationError, "symlink"),
        ("kh:dir-out/secret.txt", LocationError, "symlink"),
        (root / "dir-out" / "secret.txt", LocationError, "symlink"),
        # Case and normalization tricks: only the exact on-disk spelling opens.
        ("kh:report.pdf", ValueError, "no such file"),
        ("kh:PAPERS/a.pdf", ValueError, "no such file"),
        ("kh:" + unicodedata.normalize("NFC", "café.pdf"), ValueError, "no such file"),
        (Path(str(root).upper()) / "papers" / "a.pdf", LocationError, "not inside any"),
        # Not a regular file.
        ("kh:papers", LocationError, "regular"),
        ("kh:pipe", LocationError, "regular"),
        # Not a configured root.
        ("zz:papers/a.pdf", LocationError, "not inside any configured"),
    ]
    for target, error, message in refused:
        with pytest.raises(error, match=message):
            repository.add_attachment(target, reference=True)
        assert _snapshot(repository) == before, target
    # The exact spellings are accepted.
    assert repository.add_attachment("kh:Report.pdf", reference=True).record.location == (
        "kh:Report.pdf"
    )
    assert repository.add_attachment(f"kh:{nfd}", reference=True).record.location == f"kh:{nfd}"


def test_a_symlink_planted_after_the_add_is_refused_by_verify(tmp_path: Path) -> None:
    repository, root = _kh(tmp_path)
    record = repository.add_attachment("kh:papers/a.pdf", reference=True).record
    moved = tmp_path / "moved-papers"
    (root / "papers").rename(moved)
    (root / "papers").symlink_to(moved, target_is_directory=True)
    statuses = [(p.attachment_id, p.status) for p in repository.verify_attachments()[1]]
    assert statuses == [(record.id, "referenced_unsafe")]


def test_linux_style_roots_are_matched_lexically() -> None:
    roots = {
        "nas": Path("/srv/notes/Knowledge-Hub"),
        "media": Path("/srv/notes/Knowledge-Hub/media"),
    }
    found = location_for_path(Path("/srv/notes/Knowledge-Hub/papers/2026/x.pdf"), roots)
    assert found == Location("nas", ("papers", "2026", "x.pdf"))
    assert str(found) == "nas:papers/2026/x.pdf"
    # The most specific root wins.
    assert str(location_for_path(Path("/srv/notes/Knowledge-Hub/media/a.mp4"), roots)) == (
        "media:a.mp4"
    )
    for refused in (
        "/srv/notes/knowledge-hub/papers/x.pdf",  # case differs
        "/srv/notes/Knowledge-Hub/../secret.txt",
        "/srv/notes/Knowledge-Hub",  # the root itself
        "/srv/notes/Knowledge-Hub-other/x.pdf",  # a sibling prefix
        "/etc/passwd",
    ):
        with pytest.raises(LocationError):
            location_for_path(Path(refused), roots)
    assert Location.parse("nas:papers/2026/x.pdf").parts == ("papers", "2026", "x.pdf")
    for bad in ("nas:papers\\x.pdf", "nas:/abs", "NAS:x", "nas:a/../b", "nocolon"):
        with pytest.raises(LocationError):
            Location.parse(bad)


def _case_sensitive(directory: Path) -> bool:
    probe = directory / "CaseProbe"
    probe.write_bytes(b"")
    try:
        return not (directory / "caseprobe").exists()
    finally:
        probe.unlink()


def test_case_sensitive_names_are_distinct_attachments(tmp_path: Path) -> None:
    repository, root = _kh(tmp_path)
    if not _case_sensitive(root):
        pytest.skip("needs a case-sensitive filesystem (Linux CI)")
    (root / "papers" / "A.pdf").write_bytes(b"upper case twin")
    lower = repository.add_attachment("kh:papers/a.pdf", reference=True).record
    upper = repository.add_attachment("kh:papers/A.pdf", reference=True).record
    assert lower.sha256 != upper.sha256
    assert repository.verify_attachments()[1] == []


# --- verify, missing, relink ------------------------------------------------------------


def test_verify_missing_candidates_and_relink(tmp_path: Path) -> None:
    repository, root = _kh(tmp_path)
    vault = str(repository.root)
    record = repository.add_attachment("kh:papers/a.pdf", reference=True).record
    original = (root / "papers" / "a.pdf").read_bytes()
    # Modified in place.
    (root / "papers" / "a.pdf").write_bytes(b"%PDF-1.7 first paper, edited")
    problems = repository.verify_attachments()[1]
    assert [(p.attachment_id, p.status) for p in problems] == [(record.id, "referenced_modified")]
    assert _cli("attach", "verify", "-r", vault).exit_code == 1
    (root / "papers" / "a.pdf").write_bytes(original)
    # Moved: verify reports it missing and suggests where the same bytes are now.
    (root / "archive" / "2026").mkdir(parents=True)
    (root / "papers" / "a.pdf").rename(root / "archive" / "2026" / "a-renamed.pdf")
    (root / "img" / "copy.pdf").write_bytes(original)
    (root / "img" / "same-size.pdf").write_bytes(b"x" * len(original))
    before = _snapshot(repository)
    result = _cli("attach", "verify", "-r", vault)
    assert result.exit_code == 1
    problem = json.loads(result.output)["problems"][0]
    assert problem["status"] == "referenced_missing"
    assert problem["candidates"] == ["kh:archive/2026/a-renamed.pdf", "kh:img/copy.pdf"]
    # Verify never relinks or writes.
    assert _snapshot(repository) == before
    assert repository.get_attachment(record.id).location == "kh:papers/a.pdf"
    no_candidates = json.loads(_cli("attach", "verify", "--no-candidates", "-r", vault).output)
    assert "candidates" not in no_candidates["problems"][0]
    # --record-missing appends one attach_missing, once.
    recorded = json.loads(_cli("attach", "verify", "--record-missing", "-r", vault).output)
    assert recorded["recorded_missing"] == [record.id]
    again = json.loads(_cli("attach", "verify", "--record-missing", "-r", vault).output)
    assert again["recorded_missing"] == []
    kinds = [event.kind for event in repository.events()]
    assert kinds.count(EventKind.ATTACH_MISSING) == 1
    assert json.loads(_cli("attach", "show", record.id, "-r", vault).output)["state"] == "missing"
    # Relink only to the same bytes.
    with pytest.raises(ValueError, match="not the same original"):
        repository.relink_attachment(record.id, "kh:img/same-size.pdf")
    with pytest.raises(LocationError):
        repository.relink_attachment(record.id, "kh:../outside.pdf")
    relinked = _cli("attach", "relink", record.id, "kh:archive/2026/a-renamed.pdf", "-r", vault)
    assert relinked.exit_code == 0, relinked.output
    assert repository.get_attachment(record.id).location == "kh:archive/2026/a-renamed.pdf"
    assert repository.attachment_state_map()[record.id].label == "active"
    assert repository.verify_attachments()[1] == []
    relink_event = repository.events()[-1]
    assert relink_event.kind == EventKind.ATTACH_RELINK
    assert relink_event.details == {"projection": "none"}
    assert "archive" not in repository.events_path.read_text()
    assert not list((repository.root / ATTACHMENTS_DIR).glob("*.prev"))
    # An embedded attachment cannot be relinked.
    embedded = repository.add_attachment(_outside_file(tmp_path, "e.txt", b"embedded")).record
    with pytest.raises(IntegrityError, match="referenced"):
        repository.relink_attachment(embedded.id, "kh:img/copy.pdf")


def _outside_file(tmp_path: Path, name: str, data: bytes) -> Path:
    path = tmp_path / "in" / name
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(data)
    return path


def test_an_unmounted_root_is_unavailable_not_missing(tmp_path: Path) -> None:
    repository, root = _kh(tmp_path)
    record = repository.add_attachment("kh:papers/a.pdf", reference=True).record
    root.rename(tmp_path / "unmounted")
    problems = repository.verify_attachments()[1]
    assert [(p.attachment_id, p.status) for p in problems] == [
        (record.id, "referenced_unavailable")
    ]
    result = _cli("attach", "verify", "--record-missing", "-r", str(repository.root))
    assert result.exit_code == 0
    assert json.loads(result.output)["recorded_missing"] == []
    # Unconfigured on this host: also unavailable.
    config = repository.read_config()
    config["attachments_roots"] = {}
    repository.configure(config)
    assert [p.status for p in repository.verify_attachments()[1]] == ["referenced_unavailable"]


def test_fsck_reports_a_missing_reference_without_searching(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, root = _kh(tmp_path)
    record = repository.add_attachment("kh:papers/a.pdf", reference=True).record
    (root / "papers" / "a.pdf").unlink()

    def no_walk(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("fsck must not walk the root")

    from memfw.attachments import store as store_module

    monkeypatch.setattr(store_module, "walk_root", no_walk)
    assert [(row["memory_id"], row["kind"]) for row in repository.fsck()] == [
        (record.id, "attachment_referenced_missing")
    ]


# --- detach ---------------------------------------------------------------------------------


def test_detach_is_soft(tmp_path: Path) -> None:
    repository, root = _kh(tmp_path)
    vault = str(repository.root)
    referenced = repository.add_attachment("kh:papers/a.pdf", reference=True).record
    embedded = repository.add_attachment(_outside_file(tmp_path, "e.txt", b"embedded")).record
    for record in (referenced, embedded):
        assert _cli("attach", "detach", record.id, "-r", vault).exit_code == 0
    with pytest.raises(IntegrityError, match="already detached"):
        repository.detach_attachment(referenced.id)
    with pytest.raises(NotFoundError):
        repository.detach_attachment("att-01a1e931-1818-772b-8713-76e6bea4a716")
    # Hidden from list, shown with --all; record and blob kept.
    assert json.loads(_cli("attach", "list", "--json", "-r", vault).output) == []
    rows = json.loads(_cli("attach", "list", "--json", "--all", "-r", vault).output)
    assert [row["state"] for row in rows] == ["detached", "detached"]
    assert repository.get_attachment(embedded.id) == embedded
    store = repository.attachment_blob_store()
    assert store is not None and store.has(embedded.sha256)
    # A detached referenced original is not checked any more; the blob still is.
    (root / "papers" / "a.pdf").unlink()
    assert repository.verify_attachments()[1] == []
    store.path_for(embedded.sha256).write_bytes(b"tampered")
    assert [p.status for p in repository.verify_attachments()[1]] == ["blob_modified"]
    with pytest.raises(IntegrityError, match="detached"):
        repository.relink_attachment(referenced.id, "kh:papers/b.pdf")
    detach = [event for event in repository.events() if event.kind == EventKind.DETACH]
    assert [event.details for event in detach] == [{"projection": "none"}] * 2


# --- purge ------------------------------------------------------------------------------------


def _grep(paths: list[Path], needle: bytes) -> list[str]:
    """Every file under ``paths`` containing ``needle``, including inside .tar.zst archives."""

    hits: list[str] = []
    for base in paths:
        if not base.exists():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            data = path.read_bytes()
            if needle in data:
                hits.append(str(path))
            if path.name.endswith(".tar.zst"):
                raw = zstandard.ZstdDecompressor().stream_reader(io.BytesIO(data)).read()
                if needle in raw:
                    hits.append(f"{path} (decompressed)")
                with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
                    for member in archive.getmembers():
                        extracted = archive.extractfile(member)
                        if extracted is not None and needle in extracted.read():
                            hits.append(f"{path}!{member.name}")
    return hits


def test_purge_needs_confirmation_and_lists_citing_memories(tmp_path: Path) -> None:
    repository, _ = _kh(tmp_path)
    vault = str(repository.root)
    record = repository.add_attachment(_outside_file(tmp_path, "scan.pdf", b"%PDF bytes")).record
    repository.add(
        "Cites it", "Seen on the scan.", memory_id="m-cites", evidence=[f"att:{record.id}#page=1"]
    )
    repository.add("Mentions it", f"See att:{record.id} for the original.", memory_id="m-mention")
    repository.add("Unrelated", "Nothing here.", memory_id="m-other")
    before = _snapshot(repository)
    store = repository.attachment_blob_store()
    assert store is not None
    planned = _cli("attach", "purge", record.id, "-r", vault)
    assert planned.exit_code == 0, planned.output
    plan = json.loads(planned.output)
    assert plan["purged"] is False
    assert plan["blob"] == "delete"
    assert plan["cited_by"] == ["m-cites"]
    assert plan["mentioned_by"] == ["m-mention"]
    assert any("m-cites" in notice for notice in plan["notices"])
    assert any("snapshot" in copy for copy in plan["irrecoverable_copies"])
    # Nothing was written.
    assert _snapshot(repository) == before
    assert store.has(record.sha256)
    with pytest.raises(IntegrityError, match="confirmation"):
        repository.purge_attachment(record.id)
    assert _snapshot(repository) == before
    assert _cli("attach", "purge", "-r", vault).exit_code == 1
    assert _cli("attach", "purge", record.id, "--orphans", "-r", vault).exit_code == 1


def test_purge_leaves_nothing_behind(tmp_path: Path, private_home: Path) -> None:
    repository, root = _kh(tmp_path)
    vault = str(repository.root)
    blob_root = private_home / ".local/share/memfw/blobs"
    # An embedded original whose bytes, name, note and origin carry the marker.
    embedded_source = _outside_file(
        tmp_path, f"{MARKER}-scan.pdf", f"%PDF-1.7 {MARKER} body".encode()
    )
    embedded = repository.add_attachment(
        embedded_source,
        note=f"note about {MARKER}",
        origin=f"scanner {MARKER}",
        rights=f"rights {MARKER}",
        tags=[f"tag-{MARKER}"],
    ).record
    # A referenced original with the marker in its location.
    (root / f"{MARKER}-dir").mkdir()
    (root / f"{MARKER}-dir" / "ref.pdf").write_bytes(b"%PDF-1.7 referenced")
    referenced = repository.add_attachment(f"kh:{MARKER}-dir/ref.pdf", reference=True).record
    # A memory citing it, an index that has seen everything, a cold archive.
    repository.add("Cold", "cold body", memory_id="m-cold", tier=Tier.COLD)
    repository.add(
        "Cites", "Seen on the scan.", memory_id="m-cites", evidence=[f"att:{embedded.id}"]
    )
    repository.cold_archive("cold-1")
    assert _cli("index", "rebuild", "--root", vault).exit_code == 0
    # An interrupted add of the same bytes left a stale stage of this vault.
    store = repository.attachment_blob_store()
    assert store is not None
    with embedded_source.open("rb") as stream:
        stale = blobs_module.stage_blob(
            store.directory.parent, stream, vault=repository.root, max_bytes=1 << 20
        )
    old = stale.temporary.stat().st_mtime - 2 * blobs_module.STALE_STAGE_SECONDS
    os.utime(stale.temporary, (old, old))
    needle = MARKER.encode()
    assert _grep([repository.root, blob_root], needle)
    for record in (embedded, referenced):
        result = _cli("attach", "purge", record.id, "--yes", "-r", vault)
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["purged"] is True and payload["still_containing"] == []
    assert _grep([repository.root, blob_root], needle) == []
    # The external original is not memfw's copy: untouched.
    assert (root / f"{MARKER}-dir" / "ref.pdf").read_bytes() == b"%PDF-1.7 referenced"
    # The log keeps only ids; the citing memory is unchanged; the vault is clean.
    purges = [event for event in repository.events() if event.kind == EventKind.ATTACH_PURGE]
    assert [(event.memory_id, event.details) for event in purges] == [
        (embedded.id, {"projection": "none", "blob": "deleted"}),
        (referenced.id, {"projection": "none", "blob": "none"}),
    ]
    assert repository.get("m-cites").evidence == [f"att:{embedded.id}"]
    assert repository.attachment_ids() == []
    # The citing memory was listed before the purge and is left to a human;
    # fsck now reports its dangling pointer (MM3), and nothing else.
    assert [(row["memory_id"], row["kind"]) for row in repository.fsck()] == [
        ("m-cites", "evidence_attachment_invalid")
    ]
    assert repository.verify_attachments() == ([], [])
    with pytest.raises(NotFoundError):
        repository.get_attachment(embedded.id)
    with pytest.raises(NotFoundError):
        repository.purge_attachment(embedded.id, confirm=True)


def test_purge_keeps_a_shared_blob_and_passes_on_its_size(tmp_path: Path) -> None:
    repository, _ = _kh(tmp_path)
    first = repository.add_attachment(_outside_file(tmp_path, "a.txt", b"123456")).record
    second = repository.add_attachment(_outside_file(tmp_path, "b.txt", b"123456")).record
    store = repository.attachment_blob_store()
    assert store is not None
    plan = repository.attachment_purge_plan(first.id)
    assert plan.blob == "keep_shared" and plan.shared_with == (second.id,)
    assert any("not deleted" in notice for notice in plan.notices)
    repository.purge_attachment(first.id, confirm=True)
    assert store.has(first.sha256)
    purge = repository.events()[-1]
    assert purge.details == {"projection": "none", "blob": "kept", "heir": second.id}
    # The kept blob still counts toward the total.
    assert embedded_total(repository.events(), store) == 6
    repository.purge_attachment(second.id, confirm=True)
    assert not store.has(first.sha256)
    assert embedded_total(repository.events(), store) == 0
    assert repository.verify_attachments() == ([], [])


def test_purge_orphans(tmp_path: Path) -> None:
    repository, _ = _kh(tmp_path)
    vault = str(repository.root)
    kept = repository.add_attachment(_outside_file(tmp_path, "a.txt", b"kept")).record
    store = repository.attachment_blob_store()
    assert store is not None
    with _outside_file(tmp_path, "o.txt", b"orphan").open("rb") as stream:
        orphan = blobs_module.stage_blob(
            store.directory.parent, stream, vault=repository.root, max_bytes=100
        )
    store.link(orphan)
    planned = json.loads(_cli("attach", "purge", "--orphans", "-r", vault).output)
    assert planned == {
        "orphans": [orphan.sha256],
        "stale_stages": [],
        "blocked": None,
        "purged": False,
    }
    assert store.has(orphan.sha256)
    # Blocked while a committed record cannot be read.
    record_path = repository.root / ATTACHMENTS_DIR / f"{kept.id}.md"
    original = record_path.read_bytes()
    record_path.write_bytes(b"damaged")
    assert repository.attachment_orphan_plan().orphans == ()
    assert repository.attachment_orphan_plan().blocked
    record_path.write_bytes(original)
    done = json.loads(_cli("attach", "purge", "--orphans", "--yes", "-r", vault).output)
    assert done["purged"] is True and done["orphans"] == [orphan.sha256]
    assert not store.has(orphan.sha256) and store.has(kept.sha256)
    assert repository.verify_attachments()[1] == []


def test_recover_finishes_an_interrupted_purge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, _ = _kh(tmp_path)
    record = repository.add_attachment(_outside_file(tmp_path, "a.txt", b"purge me")).record
    store = repository.attachment_blob_store()
    assert store is not None

    def crash(*args: Any, **kwargs: Any) -> bool:
        raise OSError("simulated crash after the purge event")

    with monkeypatch.context() as patch:
        patch.setattr(lifecycle_module, "_remove_blob", crash)
        with pytest.raises(OSError, match="simulated"):
            repository.purge_attachment(record.id, confirm=True)
    assert repository.pending_path.exists()
    assert store.has(record.sha256)
    repository.recover_pending()
    assert not store.has(record.sha256)
    assert not (repository.root / ATTACHMENTS_DIR / f"{record.id}.md").exists()
    assert repository.fsck() == []


def test_recover_restores_a_record_after_an_interrupted_relink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, root = _kh(tmp_path)
    record = repository.add_attachment("kh:papers/a.pdf", reference=True).record
    path = repository.root / ATTACHMENTS_DIR / f"{record.id}.md"
    before = path.read_bytes()
    (root / "papers" / "a.pdf").rename(root / "moved.pdf")
    original = Repository._append_event

    def crash(self: Repository, kind: EventKind, *args: Any, **kwargs: Any) -> Any:
        if kind == EventKind.ATTACH_RELINK:
            raise OSError("simulated crash before the relink event")
        return original(self, kind, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Repository, "_append_event", crash)
        with pytest.raises(OSError, match="simulated"):
            repository.relink_attachment(record.id, "kh:moved.pdf")
    assert path.read_bytes() != before
    repository.recover_pending()
    assert path.read_bytes() == before
    assert not list(path.parent.glob("*.prev"))
    assert repository.get_attachment(record.id).location == "kh:papers/a.pdf"


# --- scan -----------------------------------------------------------------------------------


def test_scan_is_a_dry_run_inventory(tmp_path: Path, private_home: Path) -> None:
    repository, root = _kh(tmp_path)
    vault = str(repository.root)
    (root / "private").mkdir()
    (root / "private" / "notes.txt").write_bytes(b"n")
    (root / "img" / "身份证.jpg").write_bytes(b"jpeg")
    (root / "papers" / "bank-statement-2026.pdf").write_bytes(b"%PDF-1.7 first paper")
    (root / "papers" / "a-copy.pdf").write_bytes(b"%PDF-1.7 first paper")
    (root / "link.pdf").symlink_to(root / "papers" / "a.pdf")
    (root / "dir-link").symlink_to(root / "papers", target_is_directory=True)
    (root / "back\\slash.pdf").write_bytes(b"odd name")
    repository.add_attachment("kh:papers/b.pdf", reference=True)
    before = _snapshot(repository)
    result = _cli("attach", "scan", "kh:", "--hash", "-r", vault)
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["root"] == "kh" and report["start"] == ""
    assert report["files"] == 7
    assert report["bytes"] == sum(
        path.stat().st_size
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink() and "\\" not in path.name
    )
    assert report["by_media_type"]["application/pdf"]["files"] == 4
    assert report["likely_private"] == 3  # private/, 身份证, bank-statement
    assert report["already_registered"] == 1
    assert report["refused_symlinks"] == 2
    assert report["refused_names"] == 1
    assert report["duplicates"] == {"groups": 1, "files": 3}
    assert report["writes"] == "none (dry run)"
    # Writes nothing: not the vault, not the blob store, no vault_id.
    assert _snapshot(repository) == before
    assert "vault_id" not in repository.read_config()
    assert not (private_home / ".local/share/memfw/blobs").exists()
    # A sub-directory, by location or by path.
    sub = json.loads(_cli("attach", "scan", "kh:papers", "-r", vault).output)
    assert sub["files"] == 4 and "duplicates" not in sub
    by_path = json.loads(_cli("attach", "scan", str(root / "img"), "-r", vault).output)
    assert by_path["start"] == "img" and by_path["files"] == 2
    for refused in ("kh:../x", str(tmp_path), "kh:dir-link", "zz:"):
        assert _cli("attach", "scan", refused, "-r", vault).exit_code == 1, refused


def test_event_log_never_names_a_location(tmp_path: Path) -> None:
    repository, root = _kh(tmp_path)
    record = repository.add_attachment("kh:papers/a.pdf", reference=True).record
    (root / "papers" / "a.pdf").rename(root / "img" / "renamed-original.pdf")
    repository.record_missing_attachments([record.id])
    repository.relink_attachment(record.id, "kh:img/renamed-original.pdf")
    repository.detach_attachment(record.id)
    repository.purge_attachment(record.id, confirm=True)
    log = repository.events_path.read_text()
    for text in ("papers", "renamed-original", "a.pdf", str(root)):
        assert text not in log
    assert yaml.safe_load(repository.config_path.read_text())["attachments_roots"] == {
        "kh": str(root)
    }


# --- review follow-ups: copies, fsck cost, unmounted roots, hard links ----------------


def _copy_vault(repository: Repository, target: Path) -> Repository:
    import shutil

    shutil.copytree(repository.root, target, symlinks=True)
    return Repository(target, clock=lambda: NOW)


def test_a_copied_vault_never_deletes_the_originals_blobs(tmp_path: Path) -> None:
    from memfw.attachments.blobs import BlobOwnerError

    live, _ = _kh(tmp_path)
    kept = live.add_attachment(_outside_file(tmp_path, "a.txt", b"live blob A")).record
    store = live.attachment_blob_store()
    assert store is not None and store.owner_status() == "own"
    copy = _copy_vault(live, tmp_path / "snapshot")
    # The copy shares the vault_id, so it sees the same blob directory, but not as its own.
    copy_store = copy.attachment_blob_store()
    assert copy_store is not None and copy_store.directory == store.directory
    assert copy_store.owner_status() == "foreign"
    # An orphan the copy cannot tell apart from its own.
    with _outside_file(tmp_path, "b.txt", b"live blob B").open("rb") as stream:
        orphan = blobs_module.stage_blob(
            store.directory.parent, stream, vault=live.root, max_bytes=100
        )
    store.link(orphan)
    statuses = [p.status for p in copy.verify_attachments()[1]]
    assert "blob_store_foreign_owner" in statuses
    assert copy.attachment_purge_plan(kept.id).blob == "refused_owner"
    with pytest.raises(BlobOwnerError):
        copy.purge_attachment(kept.id, confirm=True)
    assert copy.attachment_orphan_plan().blocked
    assert copy.purge_orphan_blobs(confirm=True).orphans == ()
    with pytest.raises(BlobOwnerError):
        copy.add_attachment(_outside_file(tmp_path, "c.txt", b"written by the copy"))
    assert store.has(kept.sha256) and store.has(orphan.sha256)
    # Recovery in the copy of a purge committed in the copy does not delete either.
    with copy._locked():
        copy._append_event(EventKind.ATTACH_PURGE, kept.id, actor="human", details={})
    copy._atomic_write(copy.pending_path, b'{"operation": "purge_attachment", "targets": []}')
    copy.recover_pending()
    assert store.has(kept.sha256)
    # Splitting a (fresh) copy off: new vault_id, blobs copied, then it may purge its own.
    copy = _copy_vault(live, tmp_path / "eval-copy")
    assert _cli("attach", "reown", "--new-vault-id", "-r", str(copy.root)).exit_code == 0
    assert copy.attachment_blob_store().owner_status() == "foreign"  # type: ignore[union-attr]
    done = _cli("attach", "reown", "--new-vault-id", "--yes", "-r", str(copy.root))
    assert done.exit_code == 0, done.output
    assert json.loads(done.output)["copied"] == 1
    own = copy.attachment_blob_store()
    assert own is not None and own.directory != store.directory
    assert own.owner_status() == "own"
    assert copy.verify_attachments()[1] == []
    copy.purge_attachment(kept.id, confirm=True)
    assert not own.has(kept.sha256)
    # The live vault is untouched and clean (B is its orphan).
    assert store.has(kept.sha256) and store.has(orphan.sha256)
    assert [p.status for p in live.verify_attachments()[1]] == ["blob_orphan"]


def test_a_moved_vault_reowns_its_blob_directory(tmp_path: Path) -> None:
    live, _ = _kh(tmp_path)
    record = live.add_attachment(_outside_file(tmp_path, "a.txt", b"moved")).record
    (tmp_path / "vault").rename(tmp_path / "moved-vault")
    moved = Repository(tmp_path / "moved-vault", clock=lambda: NOW)
    store = moved.attachment_blob_store()
    assert store is not None and store.owner_status() == "foreign"
    assert "blob_store_foreign_owner" in [p.status for p in moved.verify_attachments()[1]]
    assert _cli("attach", "reown", "--yes", "-r", str(moved.root)).exit_code == 0
    assert store.owner_status() == "own"
    assert moved.verify_attachments()[1] == []
    moved.purge_attachment(record.id, confirm=True)
    assert not store.has(record.sha256)


def test_fsck_only_stats_referenced_originals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from memfw.attachments import store as store_module

    repository, root = _kh(tmp_path)
    vault = str(repository.root)
    record = repository.add_attachment("kh:papers/a.pdf", reference=True).record
    for index in range(5):
        (root / "papers" / f"extra-{index}.pdf").write_bytes(f"extra {index}".encode())
        repository.add_attachment(f"kh:papers/extra-{index}.pdf", reference=True)
    original = (root / "papers" / "a.pdf").read_bytes()
    # Same size, different bytes: only a hash sees it.
    (root / "papers" / "a.pdf").write_bytes(bytes(reversed(original)))
    with monkeypatch.context() as patch:
        patch.setattr(
            store_module, "hash_stream", lambda *a: pytest.fail("fsck hashed an original")
        )
        assert repository.fsck() == []
    rows = json.loads(_cli("fsck", "--hash-referenced", "-r", vault).output)
    assert [(row["memory_id"], row["kind"]) for row in rows] == [
        (record.id, "attachment_referenced_modified")
    ]
    # A size change is seen without hashing.
    (root / "papers" / "a.pdf").write_bytes(original + b"!")
    assert [row["kind"] for row in repository.fsck()] == ["attachment_referenced_modified"]
    # One directory listing per directory for the whole pass.
    calls = {"n": 0}
    real = os.listdir

    def counted(path: Any = ".") -> list[str]:
        calls["n"] += 1
        return real(path)

    from memfw.attachments import locations as locations_module

    monkeypatch.setattr(locations_module.os, "listdir", counted)
    repository.verify_attachments(candidates=False)
    assert calls["n"] == 2  # the root and papers/, not one per original


def test_an_empty_or_all_missing_root_is_unavailable_and_missing_clears(tmp_path: Path) -> None:
    repository, root = _kh(tmp_path)
    vault = str(repository.root)
    first = repository.add_attachment("kh:papers/a.pdf", reference=True).record
    second = repository.add_attachment("kh:papers/b.pdf", reference=True).record
    # An empty mount point: everything unavailable, nothing recorded missing.
    hidden = tmp_path / "hidden-tree"
    root.rename(hidden)
    root.mkdir()
    result = _cli("attach", "verify", "--record-missing", "-r", vault)
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert {p["status"] for p in payload["problems"]} == {"referenced_unavailable"}
    assert payload["recorded_missing"] == []
    # Every original under the root gone (the root is not empty): also unavailable.
    (root / "unrelated.txt").write_bytes(b"x")
    assert {p.status for p in repository.verify_attachments()[1]} == {"referenced_unavailable"}
    # Re-checked under the lock: an original that is there is never recorded missing.
    (root / "unrelated.txt").unlink()
    root.rmdir()
    hidden.rename(root)
    assert repository.record_missing_attachments([first.id]) == []
    # One really missing: recorded, then cleared when it is back.
    moved = root / "papers" / "a.pdf"
    moved.rename(root / "a-away.pdf")
    recorded = json.loads(_cli("attach", "verify", "--record-missing", "-r", vault).output)
    assert recorded["recorded_missing"] == [first.id]
    assert repository.attachment_state_map()[first.id].label == "missing"
    (root / "a-away.pdf").rename(moved)
    back = json.loads(_cli("attach", "verify", "--record-missing", "-r", vault).output)
    assert back["recorded_found"] == [first.id] and back["problems"] == []
    assert repository.attachment_state_map()[first.id].label == "active"
    found = [event for event in repository.events() if event.kind == EventKind.ATTACH_FOUND]
    assert [(event.memory_id, event.details) for event in found] == [
        (first.id, {"projection": "none"})
    ]
    assert repository.attachment_state_map()[second.id].label == "active"


def test_hard_linked_originals_are_refused(tmp_path: Path) -> None:
    repository, root = _kh(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    os.link(root / "papers" / "a.pdf", outside / "elsewhere.pdf")
    with pytest.raises(LocationError, match="hard links"):
        repository.add_attachment("kh:papers/a.pdf", reference=True)
    assert repository.attachment_ids() == []


def test_recover_scrubs_stores_like_a_purge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sqlite3

    repository, _ = _kh(tmp_path)
    name = f"{MARKER}-recovered.txt"
    record = repository.add_attachment(_outside_file(tmp_path, name, b"bytes")).record
    database = repository.root / "derived.sqlite3"
    connection = sqlite3.connect(database)
    # Some builds (Debian/Ubuntu) zero freed pages by default; keep them.
    connection.execute("PRAGMA secure_delete = OFF")
    connection.execute("CREATE TABLE t (v TEXT)")
    connection.execute("INSERT INTO t VALUES (?)", (name * 50,))
    connection.commit()
    connection.execute("DELETE FROM t")
    connection.commit()
    connection.close()
    assert MARKER.encode() in database.read_bytes()  # freed pages still hold it

    def crash(*args: Any, **kwargs: Any) -> bool:
        raise OSError("simulated crash after the purge event")

    with monkeypatch.context() as patch:
        patch.setattr(lifecycle_module, "_remove_blob", crash)
        with pytest.raises(OSError, match="simulated"):
            repository.purge_attachment(record.id, confirm=True)
    repository.recover_pending()
    assert MARKER.encode() not in database.read_bytes()
