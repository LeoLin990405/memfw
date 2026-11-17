"""MM2a attachment proxies: the import format, locators, storage, events, budget, CLI."""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from memfw.attachments.proxies import (
    PROXY_MAX_BYTES_KEY,
    ProxyRecord,
    parse_proxy,
    proxies_dir,
    render_proxy,
)
from memfw.cli import app
from memfw.core.errors import IntegrityError, NotFoundError
from memfw.core.models import NON_MEMORY_EVENT_KINDS, EventKind
from memfw.core.repository import Repository
from memfw.ports import PORTS_API_VERSION, REF_KINDS, Candidate
from memfw.ports.proxies import (
    CoordinateSpace,
    Locator,
    ProxyFormatError,
    SourceDimensions,
    canonical_bbox,
    canonical_seconds,
    check_locator,
    load_proxy_import,
    medium_for,
    parse_proxy_import,
)
from memfw.ports.testing import ContractError, check_proxy_import
from memfw.stats import measure_physical_disk_bytes
from memfw.stats.runtime import measure_physical_breakdown

NOW = datetime(2026, 11, 17, 9, 0, tzinfo=UTC)
FIXTURES = Path(__file__).parent / "fixtures" / "proxies"
PDF = (FIXTURES / "source.pdf").read_bytes()
LETTER = (612.0, 792.0)


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


def _attach(repository: Repository, tmp_path: Path, data: bytes = PDF, **kwargs: Any) -> str:
    source = tmp_path / "in" / kwargs.pop("name", "scan.pdf")
    source.parent.mkdir(exist_ok=True)
    source.write_bytes(data)
    return repository.add_attachment(source, **kwargs).record.id


def _fixture(name: str = "ocr-pdf-v1.json") -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    return loaded


def _write(tmp_path: Path, payload: Any, name: str = "proxy.json") -> Path:
    path = tmp_path / "in" / name
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _cli(*args: str) -> Any:
    return CliRunner().invoke(app, list(args))


def _snapshot(repository: Repository) -> tuple[bytes, list[str]]:
    directory = proxies_dir(repository)
    files = sorted(p.name for p in directory.glob("*")) if directory.exists() else []
    return repository.events_path.read_bytes(), files


# --- coordinate conversion -----------------------------------------------------------


@pytest.mark.parametrize(
    ("space", "box", "expected"),
    [
        # Canonical already: points, bottom-left origin.
        (CoordinateSpace(), [72, 684, 540, 720], (72.0, 684.0, 540.0, 720.0)),
        # Points from the top-left (pdfplumber / PyMuPDF): y flips against the height.
        (CoordinateSpace(unit="pt", origin="top-left"), [72, 72, 540, 108], (72, 684, 540, 720)),
        # A page rendered at 144 dpi, OCR boxes in pixels from the top-left.
        (
            CoordinateSpace(unit="px", origin="top-left", dpi=144),
            [144, 144, 1080, 216],
            (72, 684, 540, 720),
        ),
        # 300 dpi render: 1 px = 0.24 pt.
        (
            CoordinateSpace(unit="px", origin="top-left", dpi=300),
            [300, 300, 600, 400],
            (72, 696, 144, 720),
        ),
        # Fractions of the page from the top-left.
        (
            CoordinateSpace(unit="fraction", origin="top-left"),
            [0.5, 0.25, 1.0, 0.5],
            (306, 396, 612, 594),
        ),
        # Fractions with a bottom-left origin only scale.
        (
            CoordinateSpace(unit="fraction", origin="bottom-left"),
            [0, 0, 0.5, 0.5],
            (0, 0, 306, 396),
        ),
    ],
)
def test_pdf_boxes_convert_to_points_from_the_bottom_left(
    space: CoordinateSpace, box: list[float], expected: tuple[float, ...]
) -> None:
    assert canonical_bbox(box, medium="pdf", space=space, page_size=LETTER) == tuple(
        float(value) for value in expected
    )


def test_image_boxes_convert_to_pixels_from_the_top_left() -> None:
    size = (1000.0, 500.0)
    assert canonical_bbox([10, 20, 30, 40], medium="image") == (10, 20, 30, 40)
    assert canonical_bbox(
        [0.1, 0.2, 0.3, 0.4],
        medium="image",
        space=CoordinateSpace(unit="fraction"),
        image_size=size,
    ) == (100, 100, 300, 200)
    assert canonical_bbox(
        [10, 0, 20, 100],
        medium="image",
        space=CoordinateSpace(origin="bottom-left"),
        image_size=size,
    ) == (10, 400, 20, 500)
    with pytest.raises(ProxyFormatError, match="not pt"):
        canonical_bbox([1, 1, 2, 2], medium="image", space=CoordinateSpace(unit="pt"))


def test_conversions_that_lack_what_they_need_are_refused() -> None:
    with pytest.raises(ProxyFormatError, match="dpi"):
        canonical_bbox([1, 1, 2, 2], medium="pdf", space=CoordinateSpace(unit="px"))
    with pytest.raises(ProxyFormatError, match="size"):
        canonical_bbox([1, 1, 2, 2], medium="pdf", space=CoordinateSpace(origin="top-left"))
    with pytest.raises(ProxyFormatError, match="size"):
        canonical_bbox([0, 0, 1, 1], medium="image", space=CoordinateSpace(unit="fraction"))
    with pytest.raises(ProxyFormatError, match="x0 < x1"):
        canonical_bbox([5, 1, 2, 2], medium="pdf")
    with pytest.raises(ProxyFormatError, match="no boxes"):
        canonical_bbox([1, 1, 2, 2], medium="audio")


def test_times_are_seconds_and_milliseconds_convert() -> None:
    assert canonical_seconds(12.5, what="t") == 12.5
    assert canonical_seconds(4500, CoordinateSpace(time_unit="ms"), what="t") == 4.5
    assert canonical_seconds(1 / 3, what="t") == 0.333


def test_locator_fragments_are_canonical_and_round_trip() -> None:
    cases = {
        Locator(page=3, bbox=(72, 600.5, 540, 640)): "page=3&bbox=72,600.5,540,640",
        Locator(bbox=(0, 0, 10.25, 3)): "bbox=0,0,10.25,3",
        Locator(t=(12.5, 20.0)): "t=12.5,20",
        Locator(frame=65.0): "frame=t:65",
        Locator(page=2): "page=2",
        Locator(): "",
    }
    for locator, fragment in cases.items():
        assert locator.fragment() == fragment
        assert Locator.parse(fragment) == locator
        assert Locator.parse("#" + fragment) == locator
    for bad in ("page=0", "page=x", "bbox=1,2,3", "t=5,1", "frame=65", "zoom=2", "page=1&page=2"):
        with pytest.raises(ProxyFormatError):
            Locator.parse(bad)


# --- bounds ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("medium", "locator", "dims", "message"),
    [
        ("pdf", Locator(page=4), SourceDimensions(pages=3), "past the last page"),
        (
            "pdf",
            Locator(page=1, bbox=(0, 0, 613, 10)),
            SourceDimensions(pages=3, page_sizes=(LETTER,)),
            "outside",
        ),
        (
            "pdf",
            Locator(page=2, bbox=(0, 0, 600, 800)),
            SourceDimensions(pages=2, page_sizes=(LETTER, (612.0, 1008.0))),
            None,
        ),
        (
            "pdf",
            Locator(page=1, bbox=(0, 0, 600, 800)),
            SourceDimensions(pages=2, page_sizes=(LETTER, (612.0, 1008.0))),
            "outside",
        ),
        ("pdf", Locator(bbox=(0, 0, 1, 1)), None, "needs its page"),
        ("pdf", Locator(t=(0, 1)), None, "cannot have t"),
        (
            "image",
            Locator(bbox=(0, 0, 1001, 10)),
            SourceDimensions(width=1000, height=500),
            "outside",
        ),
        ("image", Locator(page=1), None, "cannot have page"),
        ("audio", Locator(t=(40, 43)), SourceDimensions(duration=42), "past the end"),
        ("audio", Locator(frame=1), None, "cannot have frame"),
        ("video", Locator(frame=42.5), SourceDimensions(duration=42), "past the end"),
        ("video", Locator(t=(0, 42)), SourceDimensions(duration=42), None),
        ("other", Locator(page=9), None, None),
    ],
)
def test_out_of_bounds_locators_are_rejected(
    medium: str, locator: Locator, dims: SourceDimensions | None, message: str | None
) -> None:
    if message is None:
        assert check_locator(locator, medium=medium, dimensions=dims) == locator
    else:
        with pytest.raises(ProxyFormatError, match=message):
            check_locator(locator, medium=medium, dimensions=dims)


def test_negative_or_inverted_locators_never_construct() -> None:
    for kwargs in (
        {"bbox": (-1, 0, 1, 1)},
        {"bbox": (2, 0, 1, 1)},
        {"t": (-1, 2)},
        {"t": (3, 2)},
        {"frame": -0.5},
        {"page": 0},
        {"t": (0, 1), "frame": 1},
    ):
        with pytest.raises(ProxyFormatError):
            Locator(**kwargs)  # type: ignore[arg-type]


def test_known_dimensions_must_agree_with_the_declared_ones() -> None:
    payload = _fixture()
    known = SourceDimensions(pages=3, page_sizes=(LETTER,))
    assert parse_proxy_import(payload, media_type="application/pdf", known=known).source == known
    with pytest.raises(ProxyFormatError, match="source.pages does not match"):
        parse_proxy_import(payload, media_type="application/pdf", known=SourceDimensions(pages=2))
    # Known dimensions also bound a proxy that declares none.
    del payload["source"]
    payload["coordinates"] = None
    payload["segments"] = [{"text": "x", "locator": {"page": 5}}]
    with pytest.raises(ProxyFormatError, match="past the last page"):
        parse_proxy_import(payload, media_type="application/pdf", known=known)


def test_medium_follows_the_media_type() -> None:
    assert medium_for("application/pdf") == "pdf"
    assert medium_for("image/png") == "image"
    assert medium_for("AUDIO/mpeg") == "audio"
    assert medium_for("video/mp4") == "video"
    assert medium_for("text/plain") == "other"


# --- the import format ----------------------------------------------------------------


def test_the_recorded_fixtures_are_valid_v1_files() -> None:
    sha = hashlib.sha256(PDF).hexdigest()
    parsed = check_proxy_import(
        (FIXTURES / "ocr-pdf-v1.json").read_text(encoding="utf-8"),
        media_type="application/pdf",
        source_sha256=sha,
    )
    assert [segment.locator.fragment() for segment in parsed.segments] == [
        "page=1&bbox=72,684,540,720",
        "page=2&bbox=72,624,360,648",
        "page=2",
    ]
    transcript = check_proxy_import(
        (FIXTURES / "transcript-audio-v1.json").read_text(encoding="utf-8"),
        media_type="audio/mpeg",
    )
    assert [segment.locator.t for segment in transcript.segments] == [(0, 4.5), (4.5, 9.0)]
    assert transcript.to_json()["speaker_labels"] == "unverified"
    with pytest.raises(ContractError, match="source_sha256"):
        check_proxy_import(
            (FIXTURES / "ocr-pdf-v1.json").read_text(encoding="utf-8"),
            media_type="application/pdf",
            source_sha256="1" * 64,
        )


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda p: p.pop("coverage"), "missing required keys: coverage"),
        (lambda p: p.pop("source_sha256"), "missing required keys: source_sha256"),
        (lambda p: p.update(version=2), "unsupported version"),
        (lambda p: p.update(version=True), "unsupported version"),
        (lambda p: p.update(format="other"), "format must be"),
        (lambda p: p.update(extra=1), "unknown keys: extra"),
        (lambda p: p.update(kind="summary"), "kind must be"),
        (lambda p: p.update(coverage="full"), "coverage must be an object"),
        (lambda p: p.update(coverage={"status": "most"}), "full or partial"),
        (lambda p: p.update(kind="keyframes", coverage={"status": "full"}), "partial"),
        (lambda p: p.update(segments=[]), "segments must not be empty"),
        (lambda p: p.update(source_sha256="ABC"), "64 lowercase hex"),
        (lambda p: p.update(producer="robot"), "producer must be"),
        (lambda p: p.update(created_at="2026-11-17T08:00:00"), "timezone"),
        (lambda p: p.update(privacy="secret"), "privacy must be"),
        (lambda p: p.update(supersedes="../x"), "supersedes must be"),
        (lambda p: p.update(speaker_labels="unverified"), "only a transcript"),
        (lambda p: p["segments"][0].update(speaker="A"), "only a transcript has speakers"),
        (lambda p: p["segments"][0].update(text="  "), "empty"),
        (lambda p: p["segments"][0].update(confidence=1.5), "0..1"),
        (lambda p: p["segments"][0].update(text="a\n<!-- memfw:segment {} -->"), "marker"),
        (lambda p: p["segments"][0]["locator"].update(t=[0, 1]), "cannot have t"),
        (lambda p: p["segments"][0]["locator"].update(page=4), "past the last page"),
        (lambda p: p["segments"][0]["locator"].update(bbox=[0, 0, 2000, 10]), "outside"),
        (lambda p: p["segments"][0]["locator"].pop("page"), "needs its page"),
        (lambda p: p["coordinates"].pop("dpi"), "dpi"),
    ],
)
def test_invalid_files_are_refused_with_the_reason(change: Any, message: str) -> None:
    payload = _fixture()
    change(payload)
    with pytest.raises(ProxyFormatError, match=message):
        parse_proxy_import(payload, media_type="application/pdf")


def test_duplicate_keys_and_bad_json_are_refused() -> None:
    with pytest.raises(ProxyFormatError, match="duplicate key"):
        load_proxy_import('{"kind": "ocr", "kind": "extract"}', media_type="application/pdf")
    with pytest.raises(ProxyFormatError, match="not valid JSON"):
        load_proxy_import("{", media_type="application/pdf")
    with pytest.raises(ProxyFormatError, match="one JSON object"):
        load_proxy_import("[]", media_type="application/pdf")


def test_ports_carry_the_format_and_the_segment_ref_kind() -> None:
    assert PORTS_API_VERSION == "0.3.0"
    assert "attachment_segment" in REF_KINDS
    ref = "prx-01a1e931-1818-772b-8713-76e6bea4a716:12"
    candidate = Candidate(
        ref, "attachment_segment", 1.0, "proxy", 1, {"locator": "page=3&bbox=1,2,3,4"}
    )
    assert candidate.ref == ref
    for bad in ("prx-x:1", ref.replace(":12", ":0"), ref.replace(":12", ""), "m-1"):
        with pytest.raises(ValueError):
            Candidate(bad, "attachment_segment", 1.0, "proxy", 1)


# --- storage ----------------------------------------------------------------------------


def test_import_stores_a_proxy_outside_memories_with_canonical_locators(tmp_path: Path) -> None:
    repository = _repo(tmp_path)
    attachment_id = _attach(repository, tmp_path)
    record = repository.import_proxy(attachment_id, FIXTURES / "ocr-pdf-v1.json")
    path = proxies_dir(repository) / f"{record.id}.md"
    assert path.is_file()
    assert not any(repository.memories_dir.glob("*.md"))
    assert repository.list() == []
    assert record.privacy == "local-private"
    assert record.status == "unverified"
    assert record.medium == "pdf"
    assert repository.get_proxy(record.id) == record
    text = path.read_text(encoding="utf-8")
    assert '<!-- memfw:segment {"n":1,"locator":"page=1&bbox=72,684,540,720"' in text
    assert "dpi" not in text  # only canonical units are stored
    event = repository.events()[-1]
    assert event.kind == EventKind.PROXY_ADD
    assert event.memory_id == record.id
    assert event.after_hash == hashlib.sha256(path.read_bytes()).hexdigest()
    assert event.details == {
        "projection": "none",
        "attachment": attachment_id,
        "kind": "ocr",
        "segments": 3,
    }
    assert EventKind.PROXY_ADD in NON_MEMORY_EVENT_KINDS
    assert "Invoice" not in json.dumps([e.model_dump(mode="json") for e in repository.events()])
    assert repository.fsck() == []


def test_mismatched_source_sha_writes_nothing(tmp_path: Path) -> None:
    repository = _repo(tmp_path)
    attachment_id = _attach(repository, tmp_path, b"other bytes", name="other.pdf")
    before = _snapshot(repository)
    with pytest.raises(ValueError, match="source_sha256 does not match"):
        repository.import_proxy(attachment_id, FIXTURES / "ocr-pdf-v1.json")
    assert _snapshot(repository) == before


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda p: p.update(attachment_id="att-01a1e931-1818-772b-8713-76e6bea4a716"), "the file"),
        (lambda p: p["segments"][0]["locator"].update(page=9), "past the last page"),
        (lambda p: p.pop("coverage"), "coverage"),
        (lambda p: p.update(privacy="shared"), "less private"),
    ],
)
def test_refused_imports_write_nothing(tmp_path: Path, change: Any, message: str) -> None:
    repository = _repo(tmp_path)
    attachment_id = _attach(repository, tmp_path)
    payload = _fixture()
    change(payload)
    before = _snapshot(repository)
    with pytest.raises(ValueError, match=message):
        repository.import_proxy(attachment_id, _write(tmp_path, payload))
    assert _snapshot(repository) == before


def test_privacy_defaults_local_private_and_never_loosens_the_attachment(tmp_path: Path) -> None:
    repository = _repo(tmp_path)
    shared = _attach(repository, tmp_path, privacy="shared")
    record = repository.import_proxy(shared, FIXTURES / "ocr-pdf-v1.json")
    assert record.privacy_level == "local-private"
    stricter = repository.import_proxy(shared, FIXTURES / "ocr-pdf-v1.json", privacy="shared")
    assert stricter.privacy_level == "shared"
    with pytest.raises(ValueError, match="less private"):
        repository.import_proxy(shared, FIXTURES / "ocr-pdf-v1.json", privacy="public")
    payload = _fixture()
    payload["privacy"] = "local-private"
    with pytest.raises(ValueError, match="differ"):
        repository.import_proxy(shared, _write(tmp_path, payload), privacy="shared")
    attachment = repository.get_attachment(shared)
    assert stricter.effective_level(attachment) == "shared"
    assert stricter.effective_level(None) == "unknown"


def test_supersedes_marks_the_old_proxy_and_keeps_its_file(tmp_path: Path) -> None:
    repository = _repo(tmp_path)
    attachment_id = _attach(repository, tmp_path)
    first = repository.import_proxy(attachment_id, FIXTURES / "ocr-pdf-v1.json")
    second = repository.import_proxy(
        attachment_id, FIXTURES / "ocr-pdf-v1.json", supersedes=first.id
    )
    assert second.supersedes == first.id
    supersede = repository.events()[-1]
    assert supersede.kind == EventKind.PROXY_SUPERSEDE
    assert supersede.memory_id == first.id
    assert supersede.details == {"projection": "none", "attachment": attachment_id, "by": second.id}
    assert (proxies_dir(repository) / f"{first.id}.md").exists()
    active = [row[0] for row in repository.proxy_records(attachment_id)]
    assert active == [second.id]
    everything = repository.proxy_records(attachment_id, include_superseded=True)
    assert [(row[0], row[2]) for row in everything] == [(first.id, second.id), (second.id, None)]
    with pytest.raises(ValueError, match="already superseded"):
        repository.import_proxy(attachment_id, FIXTURES / "ocr-pdf-v1.json", supersedes=first.id)
    other = _attach(repository, tmp_path)
    with pytest.raises(ValueError, match="is not a proxy of"):
        repository.import_proxy(other, FIXTURES / "ocr-pdf-v1.json", supersedes=second.id)
    with pytest.raises(ValueError, match="is not a proxy of"):
        repository.import_proxy(
            attachment_id,
            FIXTURES / "ocr-pdf-v1.json",
            supersedes="prx-01a1e931-1818-772b-8713-76e6bea4a716",
        )
    assert repository.fsck() == []


def test_unknown_attachment_and_bad_ids_are_refused(tmp_path: Path) -> None:
    repository = _repo(tmp_path)
    with pytest.raises(NotFoundError):
        repository.import_proxy(
            "att-01a1e931-1818-772b-8713-76e6bea4a716", FIXTURES / "ocr-pdf-v1.json"
        )
    with pytest.raises(IntegrityError):
        repository.import_proxy("../../etc", FIXTURES / "ocr-pdf-v1.json")
    with pytest.raises(IntegrityError):
        repository.get_proxy("prx-../x")


def test_rendering_reads_back_tricky_text_and_speakers(tmp_path: Path) -> None:
    repository = _repo(tmp_path)
    attachment_id = _attach(repository, tmp_path, b"audio", name="talk.mp3", embed=True)
    payload = _fixture("transcript-audio-v1.json")
    payload["source_sha256"] = hashlib.sha256(b"audio").hexdigest()
    payload["segments"][0]["speaker"] = "S1 --> <b>"
    payload["segments"][1]["text"] = "line one\n\n---\n<!-- a comment -->\n  indented"
    record = repository.import_proxy(attachment_id, _write(tmp_path, payload))
    raw = (proxies_dir(repository) / f"{record.id}.md").read_bytes()
    assert parse_proxy(raw, record.id) == record
    assert record.segments[0].speaker == "S1 --> <b>"
    assert record.segments[1].text == payload["segments"][1]["text"]
    assert b"speaker_labels: unverified" in raw


def test_parse_refuses_hand_damaged_proxy_files(tmp_path: Path) -> None:
    repository = _repo(tmp_path)
    attachment_id = _attach(repository, tmp_path)
    record = repository.import_proxy(attachment_id, FIXTURES / "ocr-pdf-v1.json")
    raw = render_proxy(record).decode()
    for damaged in (
        raw.replace("segment_count: 3", "segment_count: 2"),
        raw.replace('{"n":2', '{"n":3'),
        raw.replace("page=1&bbox=72,684,540,720", "page=1&bbox=72,684,640,720"),
        raw.replace("medium: pdf", "medium: tape"),
        raw.replace("---\n", "", 1),
    ):
        with pytest.raises(IntegrityError):
            parse_proxy(damaged.encode(), record.id)
    assert isinstance(record, ProxyRecord)


# --- budget -------------------------------------------------------------------------------


def test_proxies_have_their_own_budget_outside_the_vault_size(tmp_path: Path) -> None:
    repository = _repo(tmp_path)
    attachment_id = _attach(repository, tmp_path)
    before_total = measure_physical_disk_bytes(repository.root)
    before_breakdown = measure_physical_breakdown(repository)
    record = repository.import_proxy(attachment_id, FIXTURES / "ocr-pdf-v1.json")
    # The proxy file is not counted; only the event line grows the vault.
    assert measure_physical_breakdown(repository)["other"] == before_breakdown["other"]
    events_growth = measure_physical_breakdown(repository)["events"] - before_breakdown["events"]
    assert measure_physical_disk_bytes(repository.root) - before_total == events_growth
    usage = repository.proxy_usage()
    size = (proxies_dir(repository) / f"{record.id}.md").stat().st_size
    assert usage == {"files": 1, "bytes": size, "max_bytes": 64 * 1024 * 1024}
    config = repository.read_config()
    config[PROXY_MAX_BYTES_KEY] = size + 10
    repository.configure(config)
    before = _snapshot(repository)
    with pytest.raises(ValueError, match=PROXY_MAX_BYTES_KEY):
        repository.import_proxy(attachment_id, FIXTURES / "ocr-pdf-v1.json")
    assert _snapshot(repository) == before
    config[PROXY_MAX_BYTES_KEY] = 0
    with pytest.raises(IntegrityError, match=PROXY_MAX_BYTES_KEY):
        repository.configure(config)


# --- integrity and recovery ------------------------------------------------------------


def test_fsck_reports_damaged_missing_and_untracked_proxies(tmp_path: Path) -> None:
    repository = _repo(tmp_path)
    attachment_id = _attach(repository, tmp_path)
    record = repository.import_proxy(attachment_id, FIXTURES / "ocr-pdf-v1.json")
    path = proxies_dir(repository) / f"{record.id}.md"
    original = path.read_bytes()
    path.write_bytes(original.replace(b"Invoice", b"Receipt"))
    assert [issue["kind"] for issue in repository.fsck()] == ["proxy_record_modified"]
    path.unlink()
    assert [issue["kind"] for issue in repository.fsck()] == ["proxy_record_missing"]
    path.write_bytes(original)
    (path.parent / "notes.md").write_text("hand-written", encoding="utf-8")
    issues = repository.fsck()
    assert [(i["kind"], i["memory_id"]) for i in issues] == [("proxy_record_untracked", None)]
    # fsck never registers an external_delete for a proxy.
    assert not any(e.kind == EventKind.EXTERNAL_DELETE for e in repository.events())


def test_fsck_adds_nothing_without_proxies(tmp_path: Path) -> None:
    repository = _repo(tmp_path)
    assert repository.proxy_fsck_issues() == []
    _attach(repository, tmp_path)
    assert repository.proxy_fsck_issues() == []
    assert not proxies_dir(repository).exists()


def test_recover_removes_a_proxy_no_event_committed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _repo(tmp_path)
    attachment_id = _attach(repository, tmp_path)

    original_append = Repository._append_event

    def crash(*args: Any, **kwargs: Any) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(Repository, "_append_event", crash)
    with pytest.raises(KeyboardInterrupt):
        repository.import_proxy(attachment_id, FIXTURES / "ocr-pdf-v1.json")
    monkeypatch.setattr(Repository, "_append_event", original_append)
    assert len(list(proxies_dir(repository).glob("*.md"))) == 1
    assert repository.pending_path.exists()
    repository.recover_pending()
    assert list(proxies_dir(repository).glob("*.md")) == []
    assert repository.fsck() == []


# --- CLI ----------------------------------------------------------------------------------


def test_cli_import_list_show_budget(tmp_path: Path) -> None:
    repository = _repo(tmp_path)
    attachment_id = _attach(repository, tmp_path)
    root = str(repository.root)
    result = _cli(
        "attach",
        "proxy",
        "import",
        attachment_id,
        str(FIXTURES / "ocr-pdf-v1.json"),
        "--root",
        root,
    )
    assert result.exit_code == 0, result.output
    imported = json.loads(result.output)
    assert imported["coverage"] == {"status": "partial", "detail": "pages 1-2/3"}
    assert imported["privacy"] == "local-private"
    listed = json.loads(_cli("attach", "proxy", "list", attachment_id, "--root", root).output)
    assert [row["id"] for row in listed] == [imported["id"]]
    assert "segments" not in listed[0]
    shown = json.loads(_cli("attach", "proxy", "show", imported["id"], "--root", root).output)
    assert shown["segments"][0]["fragment"] == "page=1&bbox=72,684,540,720"
    assert shown["superseded_by"] is None
    budget = json.loads(_cli("attach", "proxy", "budget", "--root", root).output)
    assert budget["files"] == 1
    payload = _fixture()
    payload.pop("coverage")
    bad = _cli(
        "attach", "proxy", "import", attachment_id, str(_write(tmp_path, payload)), "--root", root
    )
    assert bad.exit_code == 1
    assert "coverage" in bad.output


def test_fixture_is_not_mutated_by_parsing() -> None:
    payload = _fixture()
    before = copy.deepcopy(payload)
    parse_proxy_import(payload, media_type="application/pdf")
    assert payload == before


# --- review fixes (PR #91) -------------------------------------------------------------


def test_crash_between_add_and_supersede_leaves_one_active_proxy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _repo(tmp_path)
    attachment_id = _attach(repository, tmp_path)
    first = repository.import_proxy(attachment_id, FIXTURES / "ocr-pdf-v1.json")
    original_append = Repository._append_event

    def crash_on_supersede(self: Repository, kind: EventKind, *args: Any, **kw: Any) -> Any:
        if kind == EventKind.PROXY_SUPERSEDE:
            raise KeyboardInterrupt
        return original_append(self, kind, *args, **kw)

    monkeypatch.setattr(Repository, "_append_event", crash_on_supersede)
    with pytest.raises(KeyboardInterrupt):
        repository.import_proxy(attachment_id, FIXTURES / "ocr-pdf-v1.json", supersedes=first.id)
    monkeypatch.setattr(Repository, "_append_event", original_append)
    repository.recover_pending()
    active = [row[0] for row in repository.proxy_records(attachment_id)]
    assert len(active) == 1 and active[0] != first.id
    assert repository.proxy_state().superseded_by[first.id] == active[0]
    issues = [(issue["kind"], issue["memory_id"]) for issue in repository.fsck()]
    assert issues == [("proxy_supersede_missing", first.id)]
    with pytest.raises(ValueError, match="already superseded"):
        repository.import_proxy(attachment_id, FIXTURES / "ocr-pdf-v1.json", supersedes=first.id)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda p: p["segments"][0].update(confidence=10**400), "too large"),
        (lambda p: p["segments"][0]["locator"].update(bbox=[0, 0, 10**400, 1]), "too large"),
        (lambda p: p.update(version=1.0), "unsupported version"),
        (lambda p: p["segments"][0].update(locator=[]), "locator must be an object"),
        (lambda p: p["segments"][0].update(locator=""), "locator must be an object"),
        (lambda p: p["segments"][0].update(locator=0), "locator must be an object"),
        (lambda p: p.update(coordinates={"unit": "pt", "dpi": 72}), "only to unit px"),
        (lambda p: p["segments"][0].update(text="a\x01b"), "control"),
        (lambda p: p["segments"][0].update(text="a\x85b"), "control"),
        (lambda p: p["segments"][0].update(text="evil‮etxt"), "bidirectional"),
        (lambda p: p["segments"][0].update(text="iso⁦late"), "bidirectional"),
    ],
)
def test_review_format_refusals(change: Any, message: str) -> None:
    payload = _fixture()
    change(payload)
    with pytest.raises(ProxyFormatError, match=message):
        parse_proxy_import(payload, media_type="application/pdf")


def test_null_locator_and_line_separators_are_accepted() -> None:
    payload = _fixture()
    payload["segments"][2]["locator"] = None
    payload["segments"][2]["text"] = "one two three\tfour"
    parsed = parse_proxy_import(payload, media_type="application/pdf")
    assert parsed.segments[2].locator == Locator()
    assert parsed.segments[2].text == "one\ntwo\nthree\tfour"


def test_deep_json_is_a_format_error_and_a_contract_error() -> None:
    deep = "[" * 100_000 + "]" * 100_000
    with pytest.raises(ProxyFormatError, match="nested too deeply|not valid JSON"):
        load_proxy_import(deep, media_type="application/pdf")
    with pytest.raises(ContractError):
        check_proxy_import(deep, media_type="application/pdf")


def test_stray_files_count_toward_the_budget_and_fsck(tmp_path: Path) -> None:
    repository = _repo(tmp_path)
    attachment_id = _attach(repository, tmp_path)
    record = repository.import_proxy(attachment_id, FIXTURES / "ocr-pdf-v1.json")
    directory = proxies_dir(repository)
    size = (directory / f"{record.id}.md").stat().st_size
    (directory / "notes.txt").write_bytes(b"x" * 10)
    (directory / "sub").mkdir()
    (directory / "sub" / f"{record.id}.md").write_bytes(b"y" * 5)
    assert repository.proxy_usage()["files"] == 3
    assert repository.proxy_usage()["bytes"] == size + 15
    untracked = sorted(
        issue["message"] or ""
        for issue in repository.fsck()
        if issue["kind"] == "proxy_record_untracked"
    )
    assert untracked == ["notes.txt", f"sub/{record.id}.md"]


def test_privacy_is_rechecked_under_the_lock(tmp_path: Path) -> None:
    repository = _repo(tmp_path)
    attachment_id = _attach(repository, tmp_path)  # local-private
    record = repository.import_proxy(attachment_id, FIXTURES / "ocr-pdf-v1.json")
    from dataclasses import replace

    looser = replace(record, privacy="shared", supersedes=None)
    before = _snapshot(repository)
    with pytest.raises(ValueError, match="less private"):
        repository.add_proxy_record(looser)
    assert _snapshot(repository) == before


def test_forged_supersedes_are_ignored_and_reported(tmp_path: Path) -> None:
    repository = _repo(tmp_path)
    first_att = _attach(repository, tmp_path)
    second_att = _attach(repository, tmp_path)
    mine = repository.import_proxy(first_att, FIXTURES / "ocr-pdf-v1.json")
    theirs = repository.import_proxy(second_att, FIXTURES / "ocr-pdf-v1.json")
    replaced = repository.import_proxy(first_att, FIXTURES / "ocr-pdf-v1.json", supersedes=mine.id)
    forged = [
        "prx-01a1e931-1818-772b-8713-76e6bea4a716",
        "prx-01a1e931-1818-772b-8713-76e6bea4a717",
        "prx-01a1e931-1818-772b-8713-76e6bea4a718",
    ]
    with repository._locked():
        # Names another attachment's proxy.
        repository._append_event(
            EventKind.PROXY_ADD,
            forged[0],
            actor="human",
            after_hash="0" * 64,
            details={"attachment": first_att, "kind": "ocr", "supersedes": theirs.id},
        )
        # Names an already-superseded proxy.
        repository._append_event(
            EventKind.PROXY_ADD,
            forged[1],
            actor="human",
            after_hash="1" * 64,
            details={"attachment": first_att, "kind": "ocr", "supersedes": mine.id},
        )
        # Names a proxy that does not exist.
        repository._append_event(
            EventKind.PROXY_ADD,
            forged[2],
            actor="human",
            after_hash="2" * 64,
            details={"attachment": first_att, "kind": "ocr", "supersedes": forged[0][:-1] + "9"},
        )
    state = repository.proxy_state()
    assert state.active(theirs.id)
    assert state.superseded_by[mine.id] == replaced.id
    assert {(by, target) for by, target in state.invalid} == {
        (forged[0], theirs.id),
        (forged[1], mine.id),
        (forged[2], forged[0][:-1] + "9"),
    }
    kinds = [(issue["kind"], issue["memory_id"]) for issue in repository.fsck()]
    assert ("proxy_supersede_invalid", forged[0]) in kinds
    assert ("proxy_supersede_invalid", forged[1]) in kinds
    assert not any(kind == "proxy_supersede_missing" for kind, _ in kinds)
    assert [row[0] for row in repository.proxy_records(second_att)] == [theirs.id]


@pytest.mark.parametrize(
    "app", ["app-manual:a\x85b", "app-manual:a‮b", "app-manual:a‏b", "app-manual:﻿x"]
)
def test_app_manual_producer_names_are_printable(app: str) -> None:
    payload = _fixture()
    payload["producer"] = app
    with pytest.raises(ProxyFormatError):
        parse_proxy_import(payload, media_type="application/pdf")


@pytest.mark.parametrize("char", ["​", "‎", "‏", "؜", "﻿"])
def test_invisible_marks_are_refused_in_text(char: str) -> None:
    payload = _fixture()
    payload["segments"][0]["text"] = f"a{char}b"
    with pytest.raises(ProxyFormatError, match="invisible"):
        parse_proxy_import(payload, media_type="application/pdf")
