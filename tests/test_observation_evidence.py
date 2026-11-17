"""MM3: ``att:`` evidence is observation; the gate caps it; bypass spellings are folded."""

from __future__ import annotations

import base64
import hashlib
import json
import shutil
import urllib.parse
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from memfw.attachments import records as attachment_records
from memfw.attachments import store as attachment_store
from memfw.cli import app
from memfw.core import evidence_refs
from memfw.core.epistemic import (
    evidence_source_class,
    observation_cap_violations,
    validate_epistemic_write,
)
from memfw.core.errors import IntegrityError
from memfw.core.evidence_refs import (
    Locator,
    canonical_reference,
    check_locator,
    parse_attachment_reference,
    parse_locator,
    path_pointer,
    sha256_pointer_hex,
)
from memfw.core.repository import Repository
from memfw.memory_eval import load_multimodal_corpus, run_multimodal_causal_eval
from memfw.storage import evidence as storage_evidence
from memfw.storage.evidence import (
    attachment_backlinks,
    gate_evidence_classes,
    resolve_attachment_reference,
)

NOW = datetime(2026, 11, 17, 9, 0, tzinfo=UTC)
RECORD: dict[str, Any] = {
    "treatment": "deploy",
    "outcome": "latency",
    "estimand": "effect of the deploy on p99 latency",
    "identification": {"assumptions": ["no other change in the window"], "untested": []},
    "robustness": [{"check": "week before", "result": "same", "failed": False}],
}
SAMPLE_ID = "att-0190a000-0000-7000-8000-000000000000"


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


def _original(tmp_path: Path, name: str, data: bytes) -> Path:
    path = tmp_path / "in" / name
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(data)
    return path


def _attach(repository: Repository, path: Path, media_type: str) -> Any:
    return repository.add_attachment(path, media_type=media_type, embed=True).record


def _verified(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "epistemic": "inference",
        "causal": "verified",
        "causal_record": RECORD,
        "source": "synthetic",
        "provenance": "human-authored",
    }
    values.update(overrides)
    return values


def _blob(repository: Repository, sha: str) -> Path:
    store = repository.attachment_blob_store()
    assert store is not None
    return store.directory / sha[:2] / sha


# --- pure parsing -------------------------------------------------------------------


def test_attachment_id_shape_matches_the_records_module() -> None:
    assert (
        evidence_refs._ATTACHMENT_ID_RE.pattern
        == attachment_records._ID_RE.pattern.removeprefix("^").removesuffix("$")
    )
    assert storage_evidence._MAX_BYTES_KEY == attachment_store.MAX_BYTES_KEY
    assert storage_evidence._DEFAULT_MAX_BYTES == attachment_store.DEFAULT_MAX_BYTES


@pytest.mark.parametrize(
    "spelling",
    [
        f"att:{SAMPLE_ID}",
        f"ATT:{SAMPLE_ID.upper()}",
        f"  att:{SAMPLE_ID}  ",
        f"%61tt:{SAMPLE_ID}",
        f"%2561tt%3A{SAMPLE_ID}",
        f"\u200batt:{SAMPLE_ID}",
        f"ａｔｔ:{SAMPLE_ID}",
    ],
)
def test_att_spellings_fold_to_one_reference(spelling: str) -> None:
    parsed = parse_attachment_reference(spelling)
    assert parsed is not None
    assert parsed.attachment_id == SAMPLE_ID
    assert parsed.locator is None


def test_att_reference_locator_and_errors() -> None:
    parsed = parse_attachment_reference(f"att:{SAMPLE_ID}#page=3&bbox=1,2,3,4")
    assert parsed is not None and parsed.locator == "page=3&bbox=1,2,3,4"
    assert parse_attachment_reference("data:x") is None
    for bad in ("att:att-other", "att:../etc/passwd", f"att:{SAMPLE_ID}#", "att:"):
        with pytest.raises(ValueError):
            parse_attachment_reference(bad)


def test_locator_grammar() -> None:
    assert parse_locator("page=3&bbox=72,100,540,400") == Locator(
        page=3, bbox=(72.0, 100.0, 540.0, 400.0)
    )
    assert parse_locator("t=12.5,20") == Locator(t=(12.5, 20.0))
    assert parse_locator("t=4") == Locator(t=(4.0, 4.0))
    assert parse_locator("frame=t:65.0") == Locator(frame=65.0)
    for bad in (
        "page=0",
        "page=two",
        "bbox=1,2,3",
        "bbox=3,2,1,4",
        "bbox=-1,0,2,2",
        "t=20,12",
        "t=nan",
        "t=inf",
        "frame=65",
        "page=1&page=2",
        "zoom=2",
        "page",
        "x" * 600,
    ):
        with pytest.raises(ValueError):
            parse_locator(bad)


def test_locator_must_fit_the_media_type_and_bounds() -> None:
    check_locator(parse_locator("page=2"), "application/pdf")
    check_locator(parse_locator("page=2&bbox=0,0,10,10"), "application/pdf", pages=2)
    check_locator(parse_locator("bbox=0,0,10,10"), "image/png", width=10, height=10)
    check_locator(parse_locator("t=1,2"), "audio/mp4", duration=2)
    check_locator(parse_locator("frame=t:5&bbox=0,0,1,1"), "video/mp4")
    refused = [
        ("bbox=0,0,1,1", "application/pdf", {}),
        ("page=1", "image/png", {}),
        ("t=1", "image/png", {}),
        ("bbox=0,0,1,1", "audio/mp4", {}),
        ("page=1", "text/plain", {}),
        ("page=3", "application/pdf", {"pages": 2}),
        ("bbox=0,0,11,10", "image/png", {"width": 10}),
        ("t=1,3", "audio/mp4", {"duration": 2}),
        ("frame=t:9", "video/mp4", {"duration": 5}),
    ]
    for text, media, bounds in refused:
        with pytest.raises(ValueError):
            check_locator(parse_locator(text), media, **bounds)


def test_pointer_helpers() -> None:
    sha = "ab" * 32
    assert sha256_pointer_hex(f"SHA256:{sha.upper()}") == sha
    assert sha256_pointer_hex("sha256:ab:cd:ef:01") == "abcdef01"
    assert sha256_pointer_hex("sha256:abc") is None
    assert sha256_pointer_hex("sha256:not-hex-at-all") is None
    assert path_pointer("file:///tmp/a.png") == "/tmp/a.png"
    assert path_pointer("FILE://localhost/tmp/a.png") == "/tmp/a.png"
    assert path_pointer("file://otherhost/tmp/a.png") is None
    assert path_pointer("file:%2Ftmp%2Fa.png") == "/tmp/a.png"
    assert path_pointer("data:/tmp/a.png") == "/tmp/a.png"
    assert path_pointer("data:dataset-1") is None
    assert canonical_reference("sha256:ab\u200bcd") == "sha256:abcd"
    digest = hashlib.sha256(b"x").digest()
    assert sha256_pointer_hex("sha256:" + base64.b64encode(digest).decode()) == digest.hex()
    assert sha256_pointer_hex("sha256:" + digest.hex()[:8] + "\t" + digest.hex()[8:]) == (
        digest.hex()
    )
    assert path_pointer("data:copies/s.png") == "copies/s.png"
    assert path_pointer("dataset:./s.png") == "./s.png"
    assert path_pointer("result:synthetic-batch-42") is None


# --- the pure gate ------------------------------------------------------------------


def test_att_is_observation_by_prefix() -> None:
    assert evidence_source_class(f"att:{SAMPLE_ID}") == "observation"
    assert evidence_source_class(f"ATT:{SAMPLE_ID}#page=1") == "observation"
    assert evidence_source_class("att:") == "other"
    # Default classes unchanged for every other prefix.
    assert evidence_source_class("file:/tmp/x") == "raw"
    assert evidence_source_class("sha256:" + "a" * 64) == "raw"
    assert evidence_source_class("kh:Reports/x.pdf") == "other"


def test_observation_caps_run_before_the_early_return() -> None:
    evidence = [f"att:{SAMPLE_ID}"]
    with pytest.raises(IntegrityError, match="epistemic=fact"):
        validate_epistemic_write({"epistemic": "fact", "evidence": evidence}, actor="human")
    with pytest.raises(IntegrityError, match="observation only"):
        validate_epistemic_write(_verified(evidence=evidence), actor="human")
    # Within the caps: experience, inference, judgment, hypothesis.
    for epistemic in ("experience", "inference", "judgment", None):
        validate_epistemic_write({"epistemic": epistemic, "evidence": evidence}, actor="agent")
    validate_epistemic_write(
        {
            "epistemic": "inference",
            "causal": "hypothesis",
            "causal_record": RECORD,
            "evidence": evidence,
        },
        actor="agent",
    )
    # Chat, generated and bare pointers do not lift the cap.
    with pytest.raises(IntegrityError, match="epistemic=fact"):
        validate_epistemic_write(
            {"epistemic": "fact", "evidence": [*evidence, "chat:x", "llm:y", "m-1"]},
            actor="human",
        )


def test_observation_with_raw_data_is_not_capped() -> None:
    evidence = [f"att:{SAMPLE_ID}", "experiment:ab-1"]
    validate_epistemic_write({"epistemic": "fact", "evidence": evidence}, actor="human")
    validate_epistemic_write(_verified(evidence=evidence), actor="human")
    assert observation_cap_violations("fact", "verified", ["observation", "link"]) == []
    assert observation_cap_violations("fact", "verified", ["observation"]) == [
        "epistemic=fact",
        "causal=verified",
    ]
    assert observation_cap_violations("fact", "verified", ["raw"]) == []


def test_repository_classes_override_the_prefix() -> None:
    pointer = "sha256:" + "c" * 64
    validate_epistemic_write(_verified(evidence=[pointer]), actor="human")
    with pytest.raises(IntegrityError, match="observation only"):
        validate_epistemic_write(
            _verified(evidence=[pointer]),
            actor="human",
            evidence_classes={pointer: "observation"},
        )


# --- the repository layer ---------------------------------------------------------


def test_plain_evidence_reads_no_attachments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _repo(tmp_path)

    def boom(*_: Any, **__: Any) -> Any:
        raise AssertionError("attachments must not be read")

    monkeypatch.setattr(Repository, "attachment_records", boom)
    values = _verified(evidence=["dataset:x", "https://example.invalid/a", "chat:1", "m-2"])
    assert gate_evidence_classes(repository, values) == {}
    document = repository.add("claim", "body", **values)
    assert document.causal == "verified"


def test_file_and_sha256_keep_raw_class_without_attachments(tmp_path: Path) -> None:
    repository = _repo(tmp_path)
    copy = _original(tmp_path, "shot.png", b"synthetic screenshot")
    sha = hashlib.sha256(b"synthetic screenshot").hexdigest()
    for pointer in (f"file:{copy}", f"sha256:{sha}"):
        document = repository.add("claim", "body", **_verified(evidence=[pointer]))
        assert document.causal == "verified"


def test_att_evidence_must_resolve(tmp_path: Path) -> None:
    repository = _repo(tmp_path)
    shot = _attach(repository, _original(tmp_path, "s.png", b"png"), "image/png")
    document = repository.add(
        "saw it",
        "latency was 80 ms",
        epistemic="experience",
        evidence=[f"att:{shot.id}#bbox=0,0,10,10"],
    )
    assert document.evidence == [f"att:{shot.id}#bbox=0,0,10,10"]
    for bad, message in (
        (f"att:{SAMPLE_ID}", "missing attachment"),
        ("att:att-nope", "not a valid att: pointer"),
        (f"att:{shot.id}#page=1", "invalid locator"),
        (f"ATT:{shot.id.upper()}#t=1", "invalid locator"),
    ):
        with pytest.raises(IntegrityError, match=message):
            repository.add("x", "y", epistemic="experience", evidence=[bad])


def test_unreadable_attachment_record_is_refused(tmp_path: Path) -> None:
    repository = _repo(tmp_path)
    shot = _attach(repository, _original(tmp_path, "s.png", b"png"), "image/png")
    (repository.root / "attachments" / f"{shot.id}.md").write_text("tampered")
    with pytest.raises(IntegrityError, match="cannot be read"):
        repository.add("x", "y", epistemic="experience", evidence=[f"att:{shot.id}"])


def test_segment_level_when_a_segment_index_says_so(tmp_path: Path) -> None:
    repository = _repo(tmp_path)
    report = _attach(repository, _original(tmp_path, "r.pdf", b"%PDF"), "application/pdf")
    plain = resolve_attachment_reference(repository, f"att:{report.id}#page=2")
    assert plain.level == "attachment"
    assert plain.locator == Locator(page=2)
    seen: list[tuple[str, Locator]] = []

    def exists(identifier: str, locator: Locator) -> bool:
        seen.append((identifier, locator))
        return True

    located = resolve_attachment_reference(
        repository, f"att:{report.id}#page=2", segment_exists=exists
    )
    assert located.level == "segment"
    assert seen == [(report.id, Locator(page=2))]
    whole = resolve_attachment_reference(repository, f"att:{report.id}", segment_exists=exists)
    assert whole.level == "attachment" and whole.locator is None


def _bypass_pointers(repository: Repository, tmp_path: Path) -> tuple[Any, list[str]]:
    data = b"synthetic screenshot: latency fell after the deploy"
    shot = _attach(repository, _original(tmp_path, "shot.png", data), "image/png")
    copy = tmp_path / "elsewhere" / "copy.png"
    copy.parent.mkdir()
    copy.write_bytes(data)
    linked = tmp_path / "elsewhere" / "link.png"
    linked.symlink_to(_blob(repository, shot.sha256))
    sha = shot.sha256
    pointers = [
        f"sha256:{sha}",
        f"SHA256:{sha.upper()}",
        f"Sha256:{sha[:12]}",
        f"sha256:{sha[:20]}\u200b{sha[20:]}",
        f"sha256%3A{sha}",
        f"file:{copy}",
        f"file://{copy}",
        f"FILE://localhost{copy}",
        "file:" + urllib.parse.quote(str(copy), safe=""),
        f"file:{_blob(repository, sha)}",
        f"file:{linked}",
        f"data:{copy}",
        f"result:{shot.id}",
        f"experiment:run-{sha}",
        f"https://example.invalid/{shot.id}.png",
        # Review M1: other separators, base64 digests, SRI, relative paths, link syntax.
        "sha256:" + "\t".join(sha[i : i + 16] for i in range(0, 64, 16)),
        "sha256:" + ",".join(sha[i : i + 8] for i in range(0, 64, 8)),
        f"sha256:{sha[:32]}/{sha[32:]}",
        "sha256:" + base64.b64encode(bytes.fromhex(sha)).decode(),
        "sha256:" + base64.urlsafe_b64encode(bytes.fromhex(sha)).decode().rstrip("="),
        "data:sha256-" + base64.b64encode(bytes.fromhex(sha)).decode(),
        "data:../elsewhere/copy.png",
        "dataset:./../elsewhere/copy.png",
        f"[the screenshot](att:{shot.id}#bbox=0,0,1,1)",
        f"see <ATT:{shot.id.upper()}>",
    ]
    return shot, pointers


def test_bypass_spellings_are_observation(tmp_path: Path) -> None:
    repository = _repo(tmp_path)
    _, pointers = _bypass_pointers(repository, tmp_path)
    for pointer in pointers:
        with pytest.raises(IntegrityError, match="observation only"):
            repository.add("claim", "body", **_verified(evidence=[pointer]))
        with pytest.raises(IntegrityError, match="observation only"):
            repository.add("claim", "body", epistemic="fact", evidence=[pointer])
        classes = gate_evidence_classes(repository, {"evidence": [pointer]})
        assert classes == {pointer: "observation"}, pointer


def test_att_inside_link_syntax_is_checked(tmp_path: Path) -> None:
    repository = _repo(tmp_path)
    with pytest.raises(IntegrityError, match="missing attachment"):
        repository.add("x", "y", epistemic="experience", evidence=[f"[x](att:{SAMPLE_ID})"])
    shot = _attach(repository, _original(tmp_path, "s.png", b"png"), "image/png")
    with pytest.raises(IntegrityError, match="invalid locator"):
        repository.add("x", "y", epistemic="experience", evidence=[f"[x](att:{shot.id}#page=2)"])
    cited = repository.add("x", "y", epistemic="experience", evidence=[f"[x](att:{shot.id})"])
    assert repository.attachment_citations(shot.id) == [cited.id]


def test_kh_pointer_to_a_pdf_cannot_carry_verified(tmp_path: Path) -> None:
    repository = _repo(tmp_path)
    report = _attach(repository, _original(tmp_path, "r.pdf", b"%PDF"), "application/pdf")
    with pytest.raises(IntegrityError, match="points outside the vault"):
        repository.add("claim", "body", **_verified(evidence=["kh:Reports/r.pdf"]))
    with pytest.raises(IntegrityError, match="observation only"):
        repository.add(
            "claim",
            "body",
            epistemic="fact",
            evidence=["kh:Reports/r.pdf", f"att:{report.id}#page=1"],
        )


def test_unrelated_raw_pointers_keep_their_class(tmp_path: Path) -> None:
    repository = _repo(tmp_path)
    _attach(repository, _original(tmp_path, "s.png", b"png"), "image/png")
    other = _original(tmp_path, "other.csv", b"a,b\n1,2\n")
    for pointer in (f"file:{other}", "sha256:" + "d" * 64, "dataset:x", "file:/nonexistent/x"):
        assert gate_evidence_classes(repository, {"evidence": [pointer]}) == {}
        document = repository.add("claim", "body", **_verified(evidence=[pointer]))
        assert document.causal == "verified"


def test_update_and_proposals_use_the_same_classes(tmp_path: Path) -> None:
    repository = _repo(tmp_path)
    shot, pointers = _bypass_pointers(repository, tmp_path)
    seen = repository.add(
        "saw it", "latency 80 ms", epistemic="experience", evidence=[f"att:{shot.id}"]
    )
    with pytest.raises(IntegrityError, match="epistemic=fact"):
        repository.update(seen.id, epistemic="fact")
    with pytest.raises(IntegrityError, match="epistemic=fact"):
        repository.update(seen.id, epistemic="fact", evidence=[pointers[0]])
    with pytest.raises(IntegrityError, match="observation only"):
        repository.record_proposal(
            memory_id=seen.id,
            operation="update",
            changes={"epistemic": "fact"},
            rationale="the screenshot shows it",
            agent_id="planner",
            signature="mock:planner",
        )
    with pytest.raises(IntegrityError, match="missing attachment"):
        repository.record_proposal(
            memory_id=seen.id,
            operation="update",
            changes={"evidence": [f"att:{SAMPLE_ID}"]},
            rationale="cite it",
            agent_id="planner",
            signature="mock:planner",
        )
    # A post-MM3 fact backed by raw data cannot drop the raw data.
    backed = repository.add(
        "fact", "body", epistemic="fact", evidence=[f"att:{shot.id}", "experiment:ab"]
    )
    with pytest.raises(IntegrityError, match="epistemic=fact"):
        repository.update(backed.id, evidence=[f"att:{shot.id}"])


def test_legacy_verified_memory_stays_writable(tmp_path: Path) -> None:
    """v2.1 H3: evidence registered as an attachment later does not block the memory."""

    repository = _repo(tmp_path)
    data = b"synthetic original cited before it was registered"
    source = _original(tmp_path, "old.png", data)
    sha = hashlib.sha256(data).hexdigest()
    verified = repository.add(
        "old verified", "body", **_verified(evidence=[f"sha256:{sha}", f"file:{source}"])
    )
    fact = repository.add("old fact", "body", epistemic="fact", evidence=[f"sha256:{sha}"])
    hypothesis = repository.add(
        "old hypothesis",
        "body",
        epistemic="inference",
        causal="hypothesis",
        causal_record=RECORD,
        evidence=[f"sha256:{sha}"],
    )
    assert not [issue for issue in repository.fsck() if issue["kind"].startswith("evidence_")]
    attachment = _attach(repository, source, "image/png")
    # Later writes that keep the level are not refused; the agent may edit too.
    repository.update(verified.id, body="body, corrected")
    repository.update(verified.id, actor="mechanism", limitations=["single site"])
    repository.update(fact.id, body="fact, corrected")
    # Evidence the write adds is still classified: a new observation is fine
    # alongside the old pointer, and the old pointer keeps its raw class.
    repository.update(verified.id, evidence=[f"sha256:{sha}", f"file:{source}", "chat:x"])
    # A write that raises the level reclassifies everything.
    with pytest.raises(IntegrityError, match="observation only"):
        repository.update(hypothesis.id, causal="verified")
    with pytest.raises(IntegrityError, match="epistemic=fact"):
        repository.update(hypothesis.id, epistemic="fact")
    # Review M2: a human body rewrite supersedes the grandfathered memory, and
    # the replacement keeps the predecessor's evidence classes ...
    replacement = repository.supersede(fact.id, "old fact", "fact, rewritten")
    assert replacement.epistemic == "fact"
    # ... but a supersede that raises the level is reclassified.
    with pytest.raises(IntegrityError, match="observation only"):
        repository.supersede(
            hypothesis.id, "now verified", "body", overrides={"causal": "verified"}
        )
    review = sorted(
        issue["memory_id"]
        for issue in repository.fsck()
        if issue["kind"] == "evidence_observation_review"
    )
    assert review == sorted([verified.id, fact.id, replacement.id])
    assert attachment.id in {link.attachment_id for link in attachment_backlinks(repository)}


def test_backlinks_are_a_read_only_projection(tmp_path: Path) -> None:
    repository = _repo(tmp_path)
    shot = _attach(repository, _original(tmp_path, "s.png", b"png shot"), "image/png")
    other = _attach(repository, _original(tmp_path, "o.png", b"png other"), "image/png")
    first = repository.add(
        "a", "b", epistemic="experience", evidence=[f"att:{shot.id}#bbox=0,0,1,1"]
    )
    second = repository.add(
        "c",
        "d",
        epistemic="experience",
        evidence=[f"ATT:{shot.id.upper()}", f"att:{other.id}"],
    )
    third = repository.add("e", "f", epistemic="inference", evidence=[f"sha256:{shot.sha256}"])
    fourth = repository.add(
        "g", "h", epistemic="inference", evidence=[f"file:{_blob(repository, shot.sha256)}"]
    )
    repository.add("i", "j", evidence=["dataset:unrelated"])
    before = repository._tree_fingerprint()
    links = attachment_backlinks(repository, shot.id)
    assert repository._tree_fingerprint() == before
    assert sorted({link.memory_id for link in links}) == sorted(
        [first.id, second.id, third.id, fourth.id]
    )
    vias = {(link.memory_id, link.via) for link in links}
    assert (first.id, "att") in vias and (third.id, "sha256") in vias
    # The blob path names the attachment by its location (and carries its hash).
    assert {via for memory, via in vias if memory == fourth.id} <= {"file", "embedded"}
    located = [link for link in links if link.memory_id == first.id]
    assert located[0].locator == "bbox=0,0,1,1"
    assert located[0].to_json()["attachment_id"] == shot.id
    assert repository.attachment_citations(shot.id) == sorted(
        [first.id, second.id, third.id, fourth.id]
    )
    assert repository.attachment_citations(other.id) == [second.id]
    shown = json.loads(
        CliRunner().invoke(app, ["attach", "show", shot.id, "--root", str(repository.root)]).output
    )
    assert shown["cited_by"] == sorted([first.id, second.id, third.id, fourth.id])


def test_fsck_reports_att_evidence_that_no_longer_resolves(tmp_path: Path) -> None:
    repository = _repo(tmp_path)
    shot = _attach(repository, _original(tmp_path, "s.png", b"png"), "image/png")
    cited = repository.add("a", "b", epistemic="experience", evidence=[f"att:{shot.id}"])
    assert not [issue for issue in repository.fsck() if issue["kind"].startswith("evidence_")]
    record = repository.root / "attachments" / f"{shot.id}.md"
    shutil.move(record, tmp_path / "moved.md")
    issues = [
        issue for issue in repository.fsck() if issue["kind"] == "evidence_attachment_invalid"
    ]
    assert [issue["memory_id"] for issue in issues] == [cited.id]


def test_fsck_unchanged_without_attachment_evidence(tmp_path: Path) -> None:
    repository = _repo(tmp_path)
    repository.add("a", "b", evidence=["dataset:x", "file:/srv/x.csv"])
    assert repository.fsck() == []


def test_cli_dry_run_checks_att_evidence(tmp_path: Path) -> None:
    repository = _repo(tmp_path)
    shot = _attach(repository, _original(tmp_path, "s.png", b"png"), "image/png")
    runner = CliRunner()
    base = ["add", "--title", "t", "--body", "b", "--root", str(repository.root), "--dry-run"]
    ok = runner.invoke(app, [*base, "--epistemic", "experience", "--evidence", f"att:{shot.id}"])
    assert ok.exit_code == 0, ok.output
    refused = runner.invoke(app, [*base, "--epistemic", "fact", "--evidence", f"att:{shot.id}"])
    assert refused.exit_code != 0
    assert "epistemic=fact" in refused.output
    bypass = runner.invoke(
        app, [*base, "--epistemic", "fact", "--evidence", f"sha256:{shot.sha256}"]
    )
    assert bypass.exit_code != 0
    assert "epistemic=fact" in bypass.output
    missing_vault = runner.invoke(
        app,
        [
            "add",
            "--title",
            "t",
            "--body",
            "b",
            "--root",
            str(tmp_path / "none"),
            "--dry-run",
            "--epistemic",
            "experience",
            "--evidence",
            f"att:{shot.id}",
        ],
    )
    assert missing_vault.exit_code != 0
    assert "existing vault" in missing_vault.output


# --- the multimodal pseudo-causal corpus --------------------------------------------


def test_multimodal_corpus_has_no_spurious_upgrade(tmp_path: Path) -> None:
    report = run_multimodal_causal_eval(workdir=tmp_path / "eval")
    payload = report.as_dict()
    assert payload["cases"] >= 30
    assert payload["spurious_promotions"] == 0
    assert payload["fact_upgrades"] == 0
    assert payload["false_accepts"] == 0
    assert payload["false_rejects"] == 0
    assert payload["bypass_attempts"] >= 15
    assert payload["bypasses_blocked"] == payload["bypass_attempts"]
    groups = payload["groups"]
    assert groups["spurious"]["accepted"] == 0
    assert groups["control"]["accepted"] == groups["control"]["cases"]


def test_multimodal_corpus_is_package_data() -> None:
    corpus = load_multimodal_corpus()
    names = [case["name"] for case in corpus["cases"]]
    assert len(names) == len(set(names))
    assert {case["group"] for case in corpus["cases"]} == {
        "spurious",
        "bypass",
        "hygiene",
        "control",
    }


def test_eval_causal_multimodal_cli(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app, ["eval", "causal", "--multimodal", "--json", "--workdir", str(tmp_path / "w")]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["spurious_promotions"] == 0
    assert payload["fact_upgrades"] == 0
    text = CliRunner().invoke(app, ["eval", "causal", "--multimodal"])
    assert text.exit_code == 0
    assert "spurious_promotions: 0" in text.output
