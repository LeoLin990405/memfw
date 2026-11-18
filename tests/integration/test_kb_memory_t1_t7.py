"""Knowledge base <-> memory integration, T1-T7 (compat framework §0.1; gates v0.4.0).

One synthetic world, built per test by ``kb_support.build_kb``: a vault of
memories that cite a small Knowledge-Hub tree (``kh:<path>#<heading>``) and
attachments under it, a memfw notes index over the tree, three ``referenced``
attachments (a PDF, a PNG, a private PDF) with OCR proxies produced by the OCR
adapter from recorded output, and the eval question set ``eval.json``. No
network: QMD and Qdrant are the adapters' loopback fakes (or closed loopback
ports), OCR is the recorded pdftotext output or the recorded stdio MCP server,
embedders/rerankers off this host are fakes at the transport.

T1 unified recall + R@10; T2 ``kh:`` both ways + broken links; T3 referenced
PDF/PNG -> OCR proxy -> text search -> locator -> original + hash; T4 the
evidence gate with a note and an attachment; T5 ``local-private`` reaches no
exit (leaks = 0) + the embedding/reranker locality gate; T6 builtin notes vs
QMD overlap (a report, not a gate); T7 QMD/Qdrant down -> FTS, flagged.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import socket
import threading
import urllib.request
from collections.abc import Iterator
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from memfw.core.errors import IntegrityError, NotFoundError
from memfw.recall import FilterPolicy, parse_kh_ref
from memfw.recall.builtin import NotesSegmentResolver
from memfw.runtime import build_runtime
from tests.integration import kb_support as kb_mod
from tests.integration.kb_support import EVERY, KB, REMOTE, leaks

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

CAUSAL_RECORD: dict[str, Any] = {
    "treatment": "restore drill",
    "outcome": "checksum mismatch found",
    "estimand": "effect of the monthly drill on finding silent corruption",
    "identification": {"assumptions": ["the drill reads every file"], "untested": []},
    "robustness": [{"check": "second volume", "result": "same", "failed": False}],
}

# --- isolation ------------------------------------------------------------------------

_LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})
_CONNECT = socket.socket.connect
_CONNECT_EX = socket.socket.connect_ex
_CREATE = socket.create_connection
_GETADDRINFO = socket.getaddrinfo
_ENV = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "http_proxy",
    "https_proxy",
    "ALL_PROXY",
    "all_proxy",
    "DEEPSEEK_API_KEY",
    "MEMFW_MCP_CLEARANCE",
    "MEMFW_TENANT",
    "MEMFW_ALLOWED_SOURCES",
    "MEMFW_EMBEDDING_URL",
    "MEMFW_EMBEDDING_MODEL",
    "MEMFW_EMBEDDING_DIMENSION",
    "MEMFW_EMBEDDING_ALLOW_REMOTE",
    "MEMFW_NOTES_CLEARANCE",
    "MEMFW_READONLY_CLEARANCE",
    "MEMFW_READONLY_ROOT",
    "MEMFW_WRITE_TOKEN",
    "MEMFW_RELATED_DB",
)


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def loopback_only(disable_network: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """Narrow the suite's no-network guard to loopback (the fakes and closed ports)."""

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


@pytest.fixture
def kb(tmp_path: Path) -> KB:
    return kb_mod.build_kb(tmp_path)


def _closed_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _append_recall(kb: KB, recall: dict[str, Any]) -> None:
    path = kb.root / "vault.yaml"
    path.write_text(
        path.read_text(encoding="utf-8") + "runtime:\n  recall: " + json.dumps(recall) + "\n",
        encoding="utf-8",
    )


def _notes_adapter(kb: KB, **options: Any) -> dict[str, Any]:
    return {
        "factory": "tests.integration.kb_support:notes_factory",
        "options": {"index": str(kb.notes_db), **options},
    }


def _notes_wiring(kb: KB) -> dict[str, Any]:
    return {"root": str(kb.notes_root), "index": str(kb.notes_db)}


# --- T1: one query, three sources --------------------------------------------------------


def test_t1_unified_recall_labels_sources_and_reports_r_at_10(kb: KB) -> None:
    data = kb_mod.load_json("eval.json")
    assert frozenset(data["clearance"].split(",")) == REMOTE
    coordinator = kb_mod.unified_coordinator(kb, clearance=REMOTE)
    recall = kb_mod.Recall()
    sources_seen: set[str] = set()
    for question in kb_mod.questions(data):
        result = coordinator.search(question.query, limit=10, policy=FilterPolicy())
        assert not result.labels.fail_closed and not result.labels.degraded, question.id
        for hit in result.hits:
            assert hit.providers and set(hit.providers) <= {"fts"}  # memory hits: the vault
        for segment in result.segments:
            assert segment.source in {"notes", "attachments"}
            assert segment.providers == (("kh-notes",) if segment.source == "notes" else ("proxy",))
            sources_seen.add(segment.source)
        if result.hits:
            sources_seen.add("memories")
        assert leaks([result.ids, [s.snippet for s in result.segments]]) == 0
        kb_mod.score(result, question, kb.attachments, recall)
    report = recall.as_dict()
    print("T1", json.dumps(report, sort_keys=True), "misses:", recall.misses)
    assert sources_seen == {"memories", "notes", "attachments"}
    assert set(report["by_source"]) == {"memories", "notes", "attachments"}
    assert report["targets"] == sum(q.targets for q in kb_mod.questions(data))
    assert report["recall_at_10"] >= 0.9, recall.misses
    for value in report["by_source"].values():
        assert value > 0.0  # memories and notes (and attachments) each hit


def test_t1_runtime_two_stage_returns_memories_and_note_sections(kb: KB) -> None:
    """The production path: ``runtime.recall`` (adapter + ``notes``) through ``two_stage``."""

    _append_recall(kb, {"adapters": [_notes_adapter(kb)], "notes": _notes_wiring(kb)})
    runtime = build_runtime(kb.root, clearance=REMOTE)
    payload = runtime.search("restore drill checksum mismatch", limit=10, mode="two_stage")
    assert payload["degraded"] is False and not payload["errors"]
    assert payload["results"][0]["id"] == "m-restore-mismatch"
    assert "fts" in payload["results"][0]["providers"]
    notes = payload["notes"]
    assert notes and {row["source"] for row in notes} == {"notes"}
    first = notes[0]
    assert (first["path"], first["heading"]) == ("ops/backup-runbook.md", "Restore drill")
    assert first["ref"].startswith("kh:ops/backup-runbook.md#")
    assert "checksum" in first["content"] and "kh-notes" in first["providers"]
    assert payload["recall"]["providers"] == ["fts", "kh-notes"]
    assert leaks(payload) == 0


# --- T2: kh: both ways ----------------------------------------------------------------------


def _kh_evidence(kb: KB) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for document in kb.repository.list():
        refs = [ref for ref in (document.evidence or []) if ref.startswith("kh:")]
        if refs:
            out[document.id] = refs
    return out


def test_t2_kh_evidence_resolves_and_reverse_lookup_agrees(kb: KB) -> None:
    from memfw.edges import collect_edges

    cited = _kh_evidence(kb)
    resolver = NotesSegmentResolver(kb.notes_root, kb.notes_db, clearance=EVERY)
    every_ref = sorted({ref for refs in cited.values() for ref in refs})
    resolved = resolver.resolve(every_ref)
    # Forward: each resolvable pointer opens the named section of the named note.
    nightly = resolved["kh:ops/backup-runbook.md#Nightly%20snapshot"]
    assert (nightly.source, nightly.path, nightly.anchor) == (
        "notes",
        "ops/backup-runbook.md",
        "Nightly snapshot",
    )
    assert "02:00" in nightly.snippet and nightly.privacy == "public"
    migration = resolved["kh:迁移/数据库迁移.md#步骤"]
    assert migration.path == "迁移/数据库迁移.md" and "定时任务" in migration.snippet
    for ref, segment in resolved.items():
        parsed = parse_kh_ref(ref)
        assert parsed is not None and segment.path == parsed[0]
        assert not parsed[1] or segment.anchor == parsed[1]
    # Broken links are reported, by memory.
    broken = {
        memory_id: [ref for ref in refs if ref not in resolved]
        for memory_id, refs in cited.items()
        if any(ref not in resolved for ref in refs)
    }
    print("T2 broken links:", json.dumps(broken, ensure_ascii=False, sort_keys=True))
    assert broken == kb.corpus["broken_links"]
    # Reverse: note -> memories citing it, from the edge projection.
    edges = [edge for edge in collect_edges(kb.repository) if edge.relation == "cites"]
    backlinks: dict[str, set[str]] = {}
    for edge in edges:
        parsed = parse_kh_ref(edge.target)
        if parsed is not None:
            backlinks.setdefault(parsed[0], set()).add(edge.source)
    assert backlinks["ops/backup-runbook.md"] == {
        "m-nightly-backup",
        "m-restore-mismatch",
        "m-broken-heading",
    }
    assert backlinks["迁移/数据库迁移.md"] == {"m-db-migration"}
    # Both directions agree: every forward pointer has its backlink, and back.
    forward = {
        (memory_id, parse_kh_ref(ref)[0])  # type: ignore[index]
        for memory_id, refs in cited.items()
        for ref in refs
    }
    reverse = {(memory_id, path) for path, ids in backlinks.items() for memory_id in ids}
    assert forward == reverse
    # Attachments have their own reverse lookup.
    assert kb.repository.attachment_citations(kb.attachments["restore-report"]) == [
        "m-restore-mismatch"
    ]


def test_t2_broken_links_are_counted_by_two_stage(kb: KB) -> None:
    broken = kb.corpus["broken_links"]
    refs = ["kh:ops/backup-runbook.md#Nightly%20snapshot", *broken["m-broken-file"]]
    refs += broken["m-broken-heading"]
    _append_recall(
        kb,
        {
            "adapters": [
                {
                    "factory": "tests.recall_adapter_stub:refs_factory",
                    "options": {"name": "kh-cited", "refs": refs},
                }
            ],
            "notes": _notes_wiring(kb),
        },
    )
    payload = build_runtime(kb.root, clearance=REMOTE).search(
        "when does the nightly backup run", limit=10, mode="two_stage"
    )  # a semantic route: every provider is asked (an exact one asks lexical providers only)
    assert [row["path"] for row in payload["notes"]] == ["ops/backup-runbook.md"]
    assert payload["recall_metrics"]["dropped"].get("unresolved") == 2


# --- T3: referenced originals, OCR proxies, locators -----------------------------------------


def _pdf_pages(data: bytes) -> int:
    return data.count(b"/Type /Page ")


def _open_original(kb: KB, attachment_id: str) -> bytes:
    from memfw.attachments.locations import Location, open_location
    from memfw.attachments.store import attachment_roots

    record = kb.repository.get_attachment(attachment_id)
    descriptor, _stat = open_location(
        Location.parse(record.location), attachment_roots(kb.repository)
    )
    with os.fdopen(descriptor, "rb") as handle:
        return handle.read()


def test_t3_referenced_pdf_and_image_proxies_locate_the_original(kb: KB) -> None:
    from memfw.index.proxies import ProxyIndex

    report_id = kb.attachments["restore-report"]
    photo_id = kb.attachments["rack-photo"]
    for name, identifier in kb.attachments.items():
        record = kb.repository.get_attachment(identifier)
        assert record.storage == "referenced"
        assert record.location == "kh:" + next(
            item["path"] for item in kb.corpus["attachments"] if item["name"] == name
        )
    index = ProxyIndex(kb.proxy_index)
    # PDF: text -> page + bbox (points, bottom-left) -> the original.
    hits = index.search("checksum mismatch volume", tenant="", clearance=REMOTE, limit=5)
    assert hits and hits[0].attachment_id == report_id
    hit = hits[0]
    assert hit.kind == "extract" and hit.privacy == "public"
    fields = dict(part.split("=", 1) for part in hit.locator.split("&"))
    assert fields["page"] == "1"
    x0, y0, x1, y1 = (float(v) for v in fields["bbox"].split(","))
    original = _open_original(kb, report_id)
    record = kb.repository.get_attachment(report_id)
    assert hashlib.sha256(original).hexdigest() == record.sha256
    assert original == kb_mod.synthetic_pdf(kb_mod.SCAN_PAGES)
    assert 1 <= int(fields["page"]) <= _pdf_pages(original) == 2
    # The text was drawn at (72, 700) on a 612 x 792 page: the box holds that baseline.
    assert 0 <= x0 <= 72 < x1 <= 612 and 0 <= y0 <= 700 < y1 <= 792
    page_two = index.search("retention ninety days", tenant="", clearance=REMOTE, limit=5)
    assert page_two and page_two[0].locator.startswith("page=2&")
    # Image: text -> bbox (pixels, top-left) inside the picture.
    shots = index.search("patch panel", tenant="", clearance=REMOTE, limit=5)
    assert shots and shots[0].attachment_id == photo_id and shots[0].kind == "ocr"
    box = [float(v) for v in shots[0].locator.removeprefix("bbox=").split(",")]
    width, height = kb_mod.RACK_SIZE
    assert box == [12.0, 20.0, 260.0, 48.0]
    assert 0 <= box[0] < box[2] <= width and 0 <= box[1] < box[3] <= height
    photo = _open_original(kb, photo_id)
    assert hashlib.sha256(photo).hexdigest() == kb.repository.get_attachment(photo_id).sha256
    # The locator is valid evidence for this original.
    cited = kb.repository.add(
        "Patch panel label",
        "The patch panel label in rack four is legible.",
        privacy="shared",
        epistemic="experience",
        evidence=[f"att:{photo_id}#{shots[0].locator}"],
    )
    assert cited.id in kb.repository.attachment_citations(photo_id)
    # Hash verification of the referenced originals.
    checked, problems = kb.repository.verify_attachments()
    assert sorted(checked) == sorted(kb.attachments.values()) and problems == []
    assert kb.repository.fsck(hash_referenced=True) == []


def test_t3_a_changed_original_fails_verification(kb: KB) -> None:
    report_id = kb.attachments["restore-report"]
    (kb.notes_root / "scans" / "restore-report.pdf").write_bytes(
        kb_mod.synthetic_pdf(["Tampered page"])
    )
    _checked, problems = kb.repository.verify_attachments([report_id])
    assert [problem.status for problem in problems] == ["referenced_modified"]


def test_t3_mcp_attachment_get_returns_the_located_segment(kb: KB) -> None:
    from memfw.mcp.server import Gateway

    config = kb.repository.read_config()
    config["mcp"] = {"attachments": True, "local_originals": False}
    kb.repository.configure(config)
    gateway = Gateway(kb.root, entity_enabled=False, clearance="public,shared")
    found = gateway.attachment_search("checksum mismatch", limit=5)
    rows = found["results"]
    assert rows and rows[0]["attachment_id"] == kb.attachments["restore-report"]
    fragment = rows[0]["segment"]["fragment"]
    assert fragment.startswith("page=1&bbox=")
    detail = gateway.attachment_get(kb.attachments["restore-report"], locator="page=1")
    text = json.dumps(detail, ensure_ascii=False)
    assert "checksum mismatch" in text and "Snapshot retention" not in text
    assert leaks(found) == 0 and leaks(detail) == 0


# --- T4: the evidence gate with a note and an attachment ---------------------------------


def _note_and_attachment(kb: KB) -> list[str]:
    from memfw.index.proxies import ProxyIndex

    hit = ProxyIndex(kb.proxy_index).search(
        "checksum mismatch", tenant="", clearance=REMOTE, limit=1
    )[0]
    return ["kh:ops/backup-runbook.md#Restore%20drill", f"att:{hit.attachment_id}#{hit.locator}"]


def test_t4_observation_citing_a_note_and_an_attachment_passes_the_gate(kb: KB) -> None:
    from memfw.core.epistemic import evidence_source_class
    from memfw.storage.evidence import gate_evidence_classes

    evidence = _note_and_attachment(kb)
    assert evidence_source_class(evidence[1]) == "observation"
    assert evidence_source_class(evidence[0]) == "other"  # a note is not raw evidence
    assert gate_evidence_classes(kb.repository, {"evidence": evidence}) == {}
    seen = kb.repository.add(
        "Drill observation",
        "The drill report shows a checksum mismatch on volume three.",
        privacy="public",
        epistemic="experience",
        evidence=evidence,
    )
    assert seen.epistemic == "experience" and seen.causal == "none"
    stored = kb.repository.get(seen.id)
    assert stored.evidence == evidence and stored.causal == "none"
    hypothesis = kb.repository.add(
        "Drill effect",
        "Monthly drills probably surface silent corruption early.",
        privacy="public",
        epistemic="inference",
        causal="hypothesis",
        causal_record=CAUSAL_RECORD,
        evidence=evidence,
    )
    assert hypothesis.causal == "hypothesis"  # kept, never upgraded
    assert kb.repository.get(hypothesis.id).causal == "hypothesis"
    assert seen.id in kb.repository.attachment_citations(kb.attachments["restore-report"])


def test_t4_note_plus_attachment_cannot_carry_fact_or_verified(kb: KB) -> None:
    evidence = _note_and_attachment(kb)
    with pytest.raises(IntegrityError, match="observation only"):
        kb.repository.add("claim", "body", epistemic="fact", evidence=evidence)
    with pytest.raises(IntegrityError):
        kb.repository.add(
            "claim",
            "body",
            epistemic="inference",
            causal="verified",
            causal_record=CAUSAL_RECORD,
            evidence=evidence,
        )
    # The registered original spelled as its kh: location is the same observation.
    location = "kh:scans/restore-report.pdf"
    with pytest.raises(IntegrityError, match="observation only"):
        kb.repository.add("claim", "body", epistemic="fact", evidence=[location, evidence[1]])
    from memfw.storage.evidence import gate_evidence_classes

    assert gate_evidence_classes(kb.repository, {"evidence": [location]}) == {
        location: "observation"
    }
    before = {document.id for document in kb.repository.list()}
    assert {"m-restore-mismatch"} <= before


# --- T5: local-private reaches no exit ----------------------------------------------------


PRIVATE_QUERIES = (
    "zebracorn passphrase",
    "quokkalith pantry",
    "narwhaline pantry",
    "okapiscan safe ledger",
    "passphrase hint diary",
    "offline safe ledger",
)


def test_t5_notes_path_leaks_nothing(kb: KB) -> None:
    from memfw.notes.mcp import NotesGateway

    gateway = NotesGateway(kb.notes_root, kb.notes_db, clearance="public,shared")
    total = 0
    for query in PRIVATE_QUERIES:
        for mode in ("fts", "hybrid"):
            out = gateway.call("notes_search", {"query": query, "mode": mode, "limit": 20})
            total += leaks(out, echo=[query])
            assert all(
                row["path"] not in {"personal/diary.md", "legacy/old-note.md"}
                for row in out["results"]
            )
    resolver = NotesSegmentResolver(kb.notes_root, kb.notes_db)  # default: public,shared
    private_refs = [
        "kh:personal/diary.md#Passphrase%20hint",
        "kh:personal/diary.md",
        "kh:legacy/old-note.md#Pantry",
    ]
    assert resolver.resolve(private_refs) == {}
    assert NotesSegmentResolver(kb.notes_root, kb.notes_db, clearance=EVERY).resolve(
        private_refs[:1]
    )  # the note exists: withheld, not broken
    _append_recall(kb, {"adapters": [_notes_adapter(kb)], "notes": _notes_wiring(kb)})
    runtime = build_runtime(kb.root, clearance=REMOTE)
    for query in PRIVATE_QUERIES:
        payload = runtime.search(query, limit=20, mode="two_stage")
        total += leaks(payload, echo=[query])
        assert not {row["id"] for row in payload["results"]} & {
            "m-passphrase",
            "m-ledger",
            "m-legacy",
        }
    print("T5 notes/two_stage leaks:", total)
    assert total == 0


def test_t5_http_okf_and_mcp_leak_nothing(
    kb: KB, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import scripts.memfw_readonly_service as service
    from memfw.mcp.server import Gateway
    from memfw.okf import export_okf
    from tests.test_readonly_service import READ_TOKEN, _post

    withheld = {"m-passphrase", "m-ledger", "m-legacy"}
    total = 0
    # HTTP read-only service, configured as deployed (public,shared).
    monkeypatch.setenv("MEMFW_READONLY_ROOT", str(kb.root))
    monkeypatch.setenv("MEMFW_READONLY_CLEARANCE", "public,shared")
    monkeypatch.setenv("MEMFW_READONLY_TOKEN", READ_TOKEN)
    server = ThreadingHTTPServer(("127.0.0.1", 0), service.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        for query in PRIVATE_QUERIES:
            status, body = _post(base, "/search", {"query": query, "limit": 50})
            assert status == 200
            total += leaks(body, echo=[query])
            assert not {item["id"] for item in body["results"]} & withheld
        for memory_id in sorted(withheld):
            status, body = _post(base, "/related", {"memory_id": memory_id})
            assert status == 404
            total += leaks(body)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    # OKF export.
    bundle = tmp_path / "bundle"
    result = export_okf(kb.root, bundle, clearance="public,shared")
    assert result.valid
    text = "\n".join(p.read_text(encoding="utf-8") for p in sorted(bundle.rglob("*.*")))
    total += leaks(text)
    assert not any(memory_id in text for memory_id in withheld)
    # memfw MCP: memory search in every mode, attachment search and get.
    config = kb.repository.read_config()
    config["mcp"] = {"attachments": True, "local_originals": True}
    kb.repository.configure(config)
    gateway = Gateway(kb.root, entity_enabled=False, clearance="public,shared")
    for query in PRIVATE_QUERIES:
        for mode in ("fts", "hybrid", "two_stage"):
            total += leaks(gateway.search(query, limit=20, mode=mode), echo=[query])
        total += leaks(gateway.attachment_search(query, limit=20), echo=[query])
    with pytest.raises((IntegrityError, NotFoundError)):  # withheld = not there
        gateway.attachment_get(kb.attachments["safe-ledger"])
    print("T5 http/okf/mcp leaks:", total)
    assert total == 0


def test_t5_attachment_segments_are_withheld_by_clearance(kb: KB) -> None:
    """Proxy segments of the local-private scan never come back under public,shared.

    (Memory hits of the bare coordinator are not an exit: a memory without a
    ``privacy`` field is classed by the runtime's MM0 clearance, which the
    HTTP/OKF/MCP tests above go through.)
    """

    from memfw.index.proxies import ProxyIndex

    coordinator = kb_mod.unified_coordinator(kb, clearance=REMOTE)
    index = ProxyIndex(kb.proxy_index)
    private = kb.attachments["safe-ledger"]
    total = 0
    for query in PRIVATE_QUERIES:
        result = coordinator.search(query, limit=20, policy=FilterPolicy())
        total += leaks([(s.path, s.anchor, s.title, s.snippet) for s in result.segments])
        assert private not in {s.path for s in result.segments}
        assert all(
            hit.attachment_id != private
            for hit in index.search(query, tenant="", clearance=REMOTE, limit=20)
        )
    # Cleared for local-private, the same segment is there: withheld, not missing.
    every = index.search("okapiscan ledger", tenant="", clearance=EVERY, limit=5)
    assert every and every[0].attachment_id == private
    assert total == 0


class _Response(io.BytesIO):
    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


EMBED_URL = "https://embeddings.example.invalid/v1/embeddings"
RERANK_URL = "https://rerank.example.invalid/v1/rerank"


@pytest.fixture
def off_host(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """A fake off-host embedding + rerank service at the transport: everything it receives."""

    from memfw.notes import http as notes_http

    received: list[tuple[str, str]] = []

    def fake_open(request: urllib.request.Request, *, timeout: float) -> _Response:
        body = request.data
        assert isinstance(body, bytes)
        payload = json.loads(body.decode("utf-8"))
        received.append((request.full_url, json.dumps(payload, ensure_ascii=False)))
        if request.full_url == EMBED_URL:
            data = [
                {"index": i, "embedding": [float((len(t) + j) % 7) + 1.0 for j in range(8)]}
                for i, t in enumerate(payload["input"])
            ]
            return _Response(json.dumps({"data": data}).encode())
        documents = payload.get("documents", [])
        results = [{"index": i, "relevance_score": 1.0 / (i + 1)} for i in range(len(documents))]
        return _Response(json.dumps({"results": results}).encode())

    monkeypatch.setattr(notes_http, "open_http", fake_open)
    return received


def test_t5_off_host_embedder_and_reranker_receive_nothing_private(
    kb: KB, tmp_path: Path, off_host: list[tuple[str, str]]
) -> None:
    pytest.importorskip("sqlite_vec")
    from memfw.mcp.server import Gateway
    from memfw.notes import HttpEmbeddingProvider, NotesIndex, NotesRetriever, RetrievalConfig
    from memfw.notes.retrieval import RERANK_WITHHELD, HttpRerankerProvider

    embedder = HttpEmbeddingProvider(EMBED_URL, model="test", dimension=8, retries=0)
    # Memory vectors (hybrid): only public/shared memories, no query text.
    gateway = Gateway(kb.root, embedder=embedder, entity_enabled=False, allowed_sources=())
    payload = gateway.search("passphrase zebracorn pantry", limit=10, mode="hybrid")
    assert payload["degraded"] is True
    sent = "\n".join(body for url, body in off_host if url == EMBED_URL)
    assert "restic snapshot" in sent  # public memories were embedded
    assert "passphrase zebracorn pantry" not in sent  # the query never left
    # Notes vectors: only public/shared notes.
    notes = NotesIndex(tmp_path / "remote-notes.sqlite3", embedder=embedder)
    notes.index(kb.notes_root)
    sent = "\n".join(body for url, body in off_host if url == EMBED_URL)
    assert "parallel trends" in sent
    # The reranker off this host gets neither the query nor a passage.
    before = len(off_host)
    result = NotesRetriever(
        NotesIndex(kb.notes_db),
        config=RetrievalConfig(mode="fts", rerank_candidates=10),
        reranker=HttpRerankerProvider(RERANK_URL, model="test", retries=0),
    ).search("zebracorn passphrase hint", limit=5)
    assert RERANK_WITHHELD in result.errors and result.degraded
    assert all(url != RERANK_URL for url, _body in off_host[before:])
    total = sum(leaks(body) for _url, body in off_host)
    print("T5 off-host requests:", len(off_host), "leaks:", total)
    assert total == 0


# --- T6: builtin notes index vs the QMD notes adapter (report) --------------------------------


def test_t6_builtin_vs_qmd_overlap_report() -> None:
    import scripts.qmd_notes_consistency as t6

    args = argparse.Namespace(
        notes_root=str(kb_mod.QMD_FIXTURES / "notes"),
        recording=str(kb_mod.QMD_FIXTURES / "qmd-2.5.3.json"),
        endpoint=None,
        collection="knowledge-hub",
        clearance="public,shared",
        legacy_privacy="shared",
        k=10,
        timeout=5.0,
        query=["migration checklist", "暂停定时任务", "reading list"],
    )
    report = t6.report(args)
    print("T6", json.dumps(report["mean"], sort_keys=True))
    assert report["schema"] == t6.SCHEMA and report["gate"] is False
    assert all(row["qmd_status"] == "ok" for row in report["queries"])
    for value in report["mean"].values():
        assert value is None or 0.0 <= value <= 1.0


# --- T7: degradation ----------------------------------------------------------------------


@pytest.fixture
def qmd_slow() -> Iterator[Any]:
    from memfw_adapter_qmd.fake import FakeQmd

    with FakeQmd() as fake:
        fake.delay = 2.0
        yield fake


def _qmd_adapter(kb: KB, endpoint: str) -> dict[str, Any]:
    return {
        "factory": "memfw_adapter_qmd:notes_factory",
        "options": {
            "endpoint": endpoint,
            "notes_root": str(kb.notes_root),
            "clearance": "public,shared",
        },
        "timeout_seconds": 0.5,
    }


def _qdrant_adapter(url: str) -> dict[str, Any]:
    return {
        "factory": "tests.integration.kb_support:qdrant_factory",
        "options": {"url": url},
        "timeout_seconds": 0.5,
    }


QUERY = "restore drill checksum mismatch"


def _baseline(kb: KB) -> dict[str, Any]:
    _append_recall(kb, {"adapters": [_notes_adapter(kb)], "notes": _notes_wiring(kb)})
    payload = build_runtime(kb.root, clearance=REMOTE).search(QUERY, limit=10, mode="two_stage")
    assert payload["degraded"] is False
    return payload


@pytest.mark.parametrize("failure", ["qmd_closed", "qmd_timeout", "qdrant_closed", "both"])
def test_t7_qmd_or_qdrant_down_falls_back_to_fts_and_flags_degraded(
    tmp_path: Path, failure: str, qmd_slow: Any
) -> None:
    clean = kb_mod.build_kb(tmp_path / "clean")
    expected = _baseline(clean)
    kb = kb_mod.build_kb(tmp_path / "down")
    adapters = [_notes_adapter(kb)]
    dead = f"http://127.0.0.1:{_closed_port()}"
    if failure in ("qmd_closed", "both"):
        adapters.append(_qmd_adapter(kb, dead + "/mcp"))
    if failure == "qmd_timeout":
        adapters.append(_qmd_adapter(kb, qmd_slow.url))
    if failure in ("qdrant_closed", "both"):
        adapters.append(_qdrant_adapter(dead))
    _append_recall(kb, {"adapters": adapters, "notes": _notes_wiring(kb)})
    payload = build_runtime(kb.root, clearance=REMOTE).search(QUERY, limit=10, mode="two_stage")
    assert payload["degraded"] is True
    degraded = payload["recall"]["degraded"]
    print("T7", failure, json.dumps(degraded, sort_keys=True))
    want = {
        "qmd_closed": {"qmd-notes"},
        "qmd_timeout": {"qmd-notes"},
        "qdrant_closed": {"qdrant"},
        "both": {"qmd-notes", "qdrant"},
    }[failure]
    assert set(degraded) == want
    for name in want:
        assert degraded[name].startswith(("unavailable", "timeout")), degraded
    assert "fts" in payload["recall"]["providers"] and not payload["recall"]["fail_closed"]
    # FTS (and the builtin notes index) answer as if the adapter were not configured.
    assert [row["id"] for row in payload["results"]] == [row["id"] for row in expected["results"]]
    assert [row["ref"] for row in payload["notes"]] == [row["ref"] for row in expected["notes"]]
    assert leaks(payload) == 0


def test_t7_notes_index_missing_degrades_and_memories_answer(kb: KB) -> None:
    missing = kb.notes_db.with_name("gone.sqlite3")
    _append_recall(
        kb,
        {
            "adapters": [_notes_adapter(kb, index=str(missing))],
            "notes": {"root": str(kb.notes_root), "index": str(missing)},
        },
    )
    payload = build_runtime(kb.root, clearance=REMOTE).search(QUERY, limit=10, mode="two_stage")
    assert payload["degraded"] is True
    assert payload["recall"]["degraded"] == {"kh-notes": "unavailable:no_index"}
    assert payload["results"][0]["id"] == "m-restore-mismatch"
    assert "notes" not in payload
