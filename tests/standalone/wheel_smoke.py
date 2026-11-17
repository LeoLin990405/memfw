"""Smoke test for the standalone ``memfw-ports`` / ``memfw-recall`` wheels.

Run it with the interpreter of a venv that has the wheels and NOT memfw:

    python -I tests/standalone/wheel_smoke.py [--recall] [--deepseek] [--bm25s] [--qdrant]
        [--qmd] [--ocr] [--pageindex] [--langextract] [--lightrag] [--graphrag]

It refuses to run when ``memfw`` is importable, imports the wheels, and runs
the contract kit (``memfw_ports.testing``) against the reference
implementations on a throwaway vault. With ``--recall`` it also runs
``memfw_recall.Coordinator`` over the reference recall provider; with
``--deepseek`` the ``memfw-adapter-deepseek`` wheel's backend passes
``check_model_backend`` against its loopback fake (no network); with ``--bm25s``
the ``memfw-adapter-bm25s`` wheel's provider passes ``check_recall_provider``
on a throwaway vault (index outside it, under the temporary root); with
``--qdrant`` the ``memfw-adapter-qdrant`` wheel's provider syncs a throwaway
vault into its loopback ``FakeQdrant`` and passes ``check_recall_provider``;
with ``--qmd``
the ``memfw-adapter-qmd`` wheel's two recall providers pass
``check_recall_provider`` against its loopback fake; with ``--ocr`` the
``memfw-adapter-ocr`` wheel's deriver passes ``check_deriver`` with an
in-process fake engine (no OCR tool); with ``--pageindex`` the
``memfw-adapter-pageindex`` wheel's provider passes ``check_recall_provider``
over a tree it builds (titles mode, no model); with ``--langextract`` the
``memfw-adapter-langextract`` wheel's deriver passes ``check_deriver`` over a
replayed LangExtract output (LangExtract itself is not installed, no model
runs); with ``--lightrag`` the
``memfw-adapter-lightrag`` wheel's deriver passes ``check_deriver`` against an
in-process fake server; with ``--graphrag`` the
``memfw-adapter-graphrag`` wheel's deriver passes ``check_deriver`` over a
small CSV output (GraphRAG is not installed or run). Prints one
``ok`` line per check and exits non-zero on the first failure. Standard
library only; not collected by pytest (``tests/test_standalone_ports_wheel.py``
drives it).
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

T0 = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)

MEMORIES = {
    "m-backup": ("Homelab backup", "nightly homelab backup to the NAS", ""),
    "m-hal": ("HAL9000 rack", "HAL9000 rack in the homelab, backup power", ""),
    "m-nas1": ("Disk migration", "pause cron before the disk migration", ""),
    "m-zh": ("迁移计划", "迁移之前先暂停定时任务，再做备份", ""),
    "m-priv": ("Private backup", "private backup keys", "sensitivity:private"),
    "m-alpha": ("Alpha backup", "tenant alpha homelab backup notes", ""),
}
TENANTS = {"m-alpha": "alpha"}


def _write_vault(root: Path) -> Path:
    vault = root / "vault"
    (vault / "memories").mkdir(parents=True)
    (vault / "vault.yaml").write_text("schema_version: 1\n", encoding="utf-8")
    for index, (memory_id, (title, body, tag)) in enumerate(sorted(MEMORIES.items())):
        tags = f"tags:\n- {tag}\n" if tag else "tags: []\n"
        text = (
            "---\n"
            f"id: {memory_id}\n"
            f"title: {title}\n"
            f"tenant: '{TENANTS.get(memory_id, '')}'\n"
            f"created_at: '2026-01-0{index + 1}T00:00:00+00:00'\n"
            "version: 1\n"
            f"{tags}"
            "---\n"
            f"{body}\n"
        )
        (vault / "memories" / f"{memory_id}.md").write_text(text, encoding="utf-8")
    return vault


def _ok(what: str) -> None:
    print(f"ok {what}")


def _ports(root: Path) -> None:
    import memfw_ports
    from memfw_ports import (
        ClaimEvidence,
        DeriveRequest,
        FixedClock,
        IngestItem,
        ProjectionPolicy,
        SeededIdGen,
        StagedClaim,
        SystemClock,
        UsageSignal,
        UuidIdGen,
    )
    from memfw_ports.reference import (
        ConcatDeriver,
        FileClaimStager,
        InMemoryRecallProvider,
        JsonIngestor,
        JsonlProjector,
        KeywordClaimVerifier,
        MarkdownIngestor,
        MemoryProjectionStore,
        MemoryUsageSink,
        MirrorProjector,
        NullUsageSink,
        TimelineDeriver,
    )
    from memfw_ports.testing import (
        check_claim_stager,
        check_claim_verifier,
        check_clock,
        check_deriver,
        check_id_gen,
        check_ingestor,
        check_projection_store,
        check_projector,
        check_recall_provider,
        check_usage_sink,
    )

    leaked = sorted(m for m in sys.modules if m == "memfw" or m.startswith("memfw."))
    assert not leaked, leaked
    _ok(f"import memfw_ports from {Path(memfw_ports.__file__).parent.parent.name}")

    vault = _write_vault(root)
    provider = InMemoryRecallProvider.from_vault(vault)
    seen = check_recall_provider(
        provider,
        queries=["homelab backup", "HAL9000", "迁移 备份", ""],
        vault=vault,
        tenant_of=lambda candidate: TENANTS.get(candidate.ref, ""),
    )
    assert seen["HAL9000"][0].ref == "m-hal", seen["HAL9000"]
    alpha = check_recall_provider(provider, queries=["homelab backup"], tenant="alpha")
    assert [c.ref for c in alpha["homelab backup"]] == ["m-alpha"], alpha
    _ok("check_recall_provider InMemoryRecallProvider")

    for projector in (JsonlProjector(clock=FixedClock(T0)), MirrorProjector()):
        out = root / f"out-{projector.name}"
        manifest = check_projector(projector, vault=vault, out=out, policy=ProjectionPolicy("v1"))
        assert manifest.projected == len(MEMORIES) - 1, manifest
        assert manifest.skipped == {"private": 1}, manifest
        _ok(f"check_projector {type(projector).__name__}")

    items = [
        IngestItem(title="Note one", body="first body", external_ref="ext-1"),
        IngestItem(title="Note two", body="second body", external_ref="ext-2"),
        IngestItem(title="Note one", body="first body", external_ref="ext-1"),
    ]
    for ingestor in (JsonIngestor(), MarkdownIngestor()):
        result = check_ingestor(ingestor, vault=vault, items=items)
        assert len(result.staged) == 2 and len(result.duplicates) == 1, result
        _ok(f"check_ingestor {type(ingestor).__name__}")

    request = DeriveRequest(kind="digest", memory_ids=("m-nas1", "m-backup", "missing"))
    for deriver in (ConcatDeriver(), TimelineDeriver()):
        view = check_deriver(deriver, vault=vault, request=request)
        assert set(view.cited_ids()) == {"m-nas1", "m-backup"}, view
        _ok(f"check_deriver {type(deriver).__name__}")

    for clock in (SystemClock(), FixedClock(T0)):
        check_clock(clock)
        _ok(f"check_clock {type(clock).__name__}")
    for id_gen in (UuidIdGen(), SeededIdGen("smoke")):
        check_id_gen(id_gen)
        _ok(f"check_id_gen {type(id_gen).__name__}")

    answers = check_claim_verifier(
        KeywordClaimVerifier(),
        pairs=[
            ("Uploader retry uses exponential backoff.", "The uploader retry uses backoff."),
            ("Port is 8080.", "Unrelated text about lunch."),
        ],
    )
    assert answers == ["support", "neutral"], answers
    _ok("check_claim_verifier KeywordClaimVerifier")
    claim = StagedClaim(
        claim="Uploader retry uses exponential backoff.",
        oid="conversation-smoke",
        evidence=(ClaimEvidence(ref="conv:smoke:s1@0", session_id="s1"),),
    )
    assert check_claim_stager(FileClaimStager(), vault=vault, claims=[claim])
    _ok("check_claim_stager FileClaimStager")
    signals = [UsageSignal(memory_ids=("m-backup",), session_ref="s1", app="smoke", at=T0)]
    memory = MemoryUsageSink()
    check_usage_sink(memory, signals=signals, vault=vault, stored=memory.rows)
    check_usage_sink(NullUsageSink(), signals=signals, vault=vault)
    _ok("check_usage_sink MemoryUsageSink NullUsageSink")

    check_projection_store(MemoryProjectionStore(), vault=vault)
    _ok("check_projection_store MemoryProjectionStore")


def _recall(root: Path) -> None:
    import memfw_ports.types
    from memfw_ports.reference import InMemoryRecallProvider
    from memfw_recall import Coordinator, FilterPolicy, MemoryMeta, Resolution
    from memfw_recall import _ports as seam

    leaked = sorted(m for m in sys.modules if m == "memfw" or m.startswith("memfw."))
    assert not leaked, leaked
    assert seam.Candidate is memfw_ports.types.Candidate  # the wheel's ports, not a copy
    _ok("import memfw_recall (ports from memfw_ports)")

    vault = _write_vault(root / "recall")

    class Ids:
        """Resolves the fixture ids to the metadata the filter policy reads."""

        def resolve(self, refs: Sequence[str]) -> dict[str, Resolution]:
            return {
                ref: Resolution(
                    ref,
                    MemoryMeta(
                        memory_id=ref,
                        tenant=TENANTS.get(ref, ""),
                        sensitivity_private=MEMORIES[ref][2] == "sensitivity:private",
                        status="confirmed",
                    ),
                )
                for ref in refs
                if ref in MEMORIES
            }

    provider = InMemoryRecallProvider.from_vault(vault)
    coordinator = Coordinator([provider], resolvers={"memory_id": Ids()})
    result = coordinator.search("homelab backup", limit=5, policy=FilterPolicy())
    assert result.ids, result
    assert "m-priv" not in result.ids and "m-alpha" not in result.ids, result.ids
    assert result.ids[0] == "m-backup", result.ids
    alpha = coordinator.search("homelab backup", limit=5, policy=FilterPolicy(tenant="alpha"))
    assert alpha.ids == ["m-alpha"], alpha.ids
    _ok(f"Coordinator.search over InMemoryRecallProvider -> {result.ids}")


def _deepseek() -> None:
    """The adapter wheel over the memfw-ports wheel, against its loopback fake only."""

    from memfw_adapter_deepseek import DeepSeekBackend, Settings
    from memfw_adapter_deepseek import _ports as seam
    from memfw_adapter_deepseek.fake import FakeDeepSeek
    from memfw_ports.models import ModelReply
    from memfw_ports.testing import check_model_backend

    assert seam.ModelReply is ModelReply, seam.ModelReply
    _ok("import memfw_adapter_deepseek (ports from memfw_ports)")
    with FakeDeepSeek() as fake:
        backend = DeepSeekBackend(Settings(base_url=fake.url), key="smoke-test-key")
        replies = check_model_backend(backend)
        assert len(replies) == 2 and len(fake.requests) == 2, fake.requests
        assert fake.requests[0]["path"] == "/chat/completions", fake.requests[0]
    _ok("check_model_backend DeepSeekBackend (fake server)")


def _bm25s(root: Path) -> None:
    """The bm25s adapter wheel over the memfw-ports wheel, on a throwaway vault."""

    import memfw_ports.types
    from memfw_adapter_bm25s import _ports as seam
    from memfw_adapter_bm25s import factory
    from memfw_ports.testing import check_recall_provider, tree_digest

    leaked = sorted(m for m in sys.modules if m == "memfw" or m.startswith("memfw."))
    assert not leaked, leaked
    assert seam.Candidate is memfw_ports.types.Candidate, seam.Candidate
    _ok("import memfw_adapter_bm25s (ports from memfw_ports)")
    vault = _write_vault(root / "bm25s")
    with (vault / "vault.yaml").open("a", encoding="utf-8") as handle:
        handle.write("legacy_privacy_default: public\n")
    before = tree_digest(vault)
    provider = factory({"vault": str(vault), "data_dir": str(root / "bm25s-data")})
    seen = check_recall_provider(
        provider,
        queries=["homelab backup", "HAL9000", "迁移 备份", ""],
        vault=vault,
        tenant_of=lambda candidate: TENANTS.get(candidate.ref, ""),
    )
    assert seen["HAL9000"][0].ref == "m-hal", seen["HAL9000"]
    assert [c.ref for c in seen["迁移 备份"]] == ["m-zh"], seen["迁移 备份"]
    assert "m-priv" not in [c.ref for hits in seen.values() for c in hits]
    alpha = check_recall_provider(provider, queries=["homelab backup"], tenant="alpha")
    assert [c.ref for c in alpha["homelab backup"]] == ["m-alpha"], alpha
    assert tree_digest(vault) == before
    _ok("check_recall_provider Bm25sProvider")


def _qdrant(root: Path) -> None:
    """The Qdrant adapter wheel over the memfw-ports wheel, against its loopback fake only."""

    import memfw_ports.types
    from memfw_adapter_qdrant import _ports as seam
    from memfw_adapter_qdrant import factory
    from memfw_adapter_qdrant.fake import FakeQdrant
    from memfw_adapter_qdrant.sync import sync
    from memfw_ports.testing import check_recall_provider, tree_digest

    leaked = sorted(m for m in sys.modules if m == "memfw" or m.startswith("memfw."))
    assert not leaked, leaked
    assert seam.Candidate is memfw_ports.types.Candidate, seam.Candidate
    _ok("import memfw_adapter_qdrant (ports from memfw_ports)")

    class Letters:
        """26 buckets, one per letter: enough to rank the fixture memories."""

        dimension = 26
        fingerprint = "smoke-letters"

        def embed(self, texts: Sequence[str], *, role: str = "document") -> list[list[float]]:
            out = []
            for text in texts:
                vector = [float(text.lower().count(chr(97 + i))) for i in range(26)]
                out.append(vector if any(vector) else [1.0] * 26)
            return out

    vault = _write_vault(root / "qdrant")
    with (vault / "vault.yaml").open("a", encoding="utf-8") as handle:
        handle.write("legacy_privacy_default: public\n")
    before = tree_digest(vault)
    with FakeQdrant() as fake:
        provider = factory({"vault": str(vault), "url": fake.url, "embedder": Letters()})
        report = sync(provider)
        assert report.upserted == len(MEMORIES) - 1, report  # m-priv never sent
        sent = {p["payload"]["memory_id"] for p in fake.collections["memfw"]["points"].values()}
        assert "m-priv" not in sent, sent
        seen = check_recall_provider(
            provider,
            queries=["homelab backup", "HAL9000 rack"],
            vault=vault,
            tenant_of=lambda candidate: TENANTS.get(candidate.ref, ""),
        )
        assert seen["homelab backup"], seen
        alpha = check_recall_provider(provider, queries=["homelab backup"], tenant="alpha")
        assert [c.ref for c in alpha["homelab backup"]] == ["m-alpha"], alpha
    assert tree_digest(vault) == before
    _ok("check_recall_provider QdrantProvider (fake server)")


def _qmd(root: Path) -> None:
    """The QMD adapter wheel over the memfw-ports wheel, against its loopback fake only."""

    import json

    from memfw_adapter_qmd import _ports as seam
    from memfw_adapter_qmd import memory_factory, notes_factory
    from memfw_adapter_qmd.fake import FakeQmd
    from memfw_ports.testing import check_recall_provider
    from memfw_ports.types import Candidate

    assert seam.Candidate is Candidate, seam.Candidate
    from importlib.resources import files

    notice = files("memfw_adapter_qmd").joinpath("THIRD_PARTY.md").read_text(encoding="utf-8")
    assert "MIT License" in notice and "github.com/tobi/qmd" in notice, notice[:200]
    _ok("import memfw_adapter_qmd (ports from memfw_ports, QMD notice shipped)")
    manifest = root / "qmd-manifest.json"
    documents = [
        {"id": "m-a", "path": "memories/m-a.md", "meta": {"privacy_class": "none", "tenant": ""}},
        {
            "id": "m-p",
            "path": "memories/m-p.md",
            "meta": {"privacy_class": "local-private", "tenant": ""},
        },
    ]
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "tree": "tree-smoke",
                "documents": documents,
                "lifecycle_source_status": "ok",
            }
        ),
        encoding="utf-8",
    )
    notes = root / "kh"
    (notes / "Course A").mkdir(parents=True)
    (notes / "Course A" / "Week 1.md").write_text(
        "---\nprivacy: shared\n---\n# Week 1\n\nbackup notes\n", encoding="utf-8"
    )

    def results(arguments: object) -> list[dict[str, object]]:
        collection = arguments["collections"][0]  # type: ignore[index]
        if collection == "memfw-memory":
            return [
                {"file": "memfw-memory/memories/m-p.md", "score": 0.9, "line": 1},
                {"file": "memfw-memory/memories/m-a.md", "score": 0.8, "line": 1},
            ]
        return [{"file": "knowledge-hub/Course-A/Week-1.md", "score": 0.7, "line": 6}]

    with FakeQmd(results=results) as fake:
        memory = memory_factory({"endpoint": fake.url, "manifest": str(manifest)})
        found = check_recall_provider(memory, queries=["backup"], limit=5)["backup"]
        assert [c.ref for c in found] == ["m-a"], found
        note = notes_factory({"endpoint": fake.url, "notes_root": str(notes)})
        found = check_recall_provider(note, queries=["backup"], limit=5)["backup"]
        assert [c.ref for c in found] == ["kh:Course%20A/Week%201.md#Week%201"], found
    _ok("check_recall_provider QmdMemoryProvider + QmdNotesProvider (fake server)")


def _ocr(root: Path) -> None:
    """The OCR adapter wheel over the memfw-ports wheel, with a fake engine."""

    import hashlib
    import json

    from memfw_adapter_ocr import OcrDeriver, OcrLine, OcrPage, OcrResult, Settings
    from memfw_adapter_ocr import _ports as seam
    from memfw_ports.testing import check_deriver
    from memfw_ports.types import Availability, DeriveRequest

    assert seam.DeriveRequest is DeriveRequest, seam.DeriveRequest
    _ok("import memfw_adapter_ocr (ports from memfw_ports)")

    class Engine:
        name = "fake"

        def available(self) -> Availability:
            return Availability.ok()

        def extract(self, path: Path, *, media: str, budget: object, workdir: Path) -> OcrResult:
            return OcrResult("fake", "1", "pdf", 1, (OcrPage(1, (OcrLine("hello"),)),))

    originals = root / "originals"
    originals.mkdir()
    (originals / "a.pdf").write_bytes(b"%PDF-1.4 smoke")
    sha = hashlib.sha256(b"%PDF-1.4 smoke").hexdigest()
    deriver = OcrDeriver(Settings(source_roots=(str(originals),)), backend=Engine())
    att = "att-01a1e931-1818-772b-8713-76e6bea4a716"
    request = DeriveRequest(
        kind="ocr_proxy",
        memory_ids=(att,),
        options={
            "source": "a.pdf",
            "sha256": sha,
            "media_type": "application/pdf",
            "privacy": "shared",
        },
    )
    vault = root / "ocr-vault"
    vault.mkdir()
    view = check_deriver(deriver, vault=vault, request=request)
    assert json.loads(view.content)["segments"][0]["locator"] == {"page": 1}, view.content
    _ok("check_deriver OcrDeriver (fake engine)")


def _pageindex(root: Path) -> None:
    """The PageIndex adapter wheel over the memfw-ports wheel (titles mode, no model)."""

    from memfw_adapter_pageindex import PageIndexProvider, Settings, build_markdown_tree, write_tree
    from memfw_adapter_pageindex import _ports as seam
    from memfw_ports.testing import check_recall_provider
    from memfw_ports.types import Candidate

    assert seam.Candidate is Candidate, seam.Candidate
    _ok("import memfw_adapter_pageindex (ports from memfw_ports)")
    kh = root / "pageindex-kh"
    kh.mkdir()
    (kh / "a.md").write_text("# Backups\n\n## Nightly backup check\n", encoding="utf-8")
    index = root / "pageindex-index"
    write_tree(index, build_markdown_tree(kh, "kh:a.md", privacy="shared"))
    provider = PageIndexProvider(Settings(index_dir=str(index), mode="titles"))
    seen = check_recall_provider(provider, queries=["nightly backup"], limit=3)
    assert seen["nightly backup"][0].ref == "kh:a.md#Nightly%20backup%20check", seen
    _ok("check_recall_provider PageIndexProvider (titles mode)")
    (kh / "p.md").write_text("---\ntags:\n- privacy:local-private\n---\n# P\n", encoding="utf-8")
    assert build_markdown_tree(kh, "kh:p.md", privacy="shared")["privacy"] == "local-private"
    _ok("PageIndex note privacy via PyYAML + metadata_level")


def _langextract(root: Path) -> None:
    """The deriver wheel over the memfw-ports wheel, LangExtract replaced by a replay."""

    from memfw_adapter_langextract import TASK, LangExtractDeriver, Settings, candidates
    from memfw_adapter_langextract import _ports as seam
    from memfw_ports.models import ModelReply
    from memfw_ports.testing import check_deriver
    from memfw_ports.types import DeriveRequest

    assert seam.ModelReply is ModelReply, seam.ModelReply
    _ok("import memfw_adapter_langextract (ports from memfw_ports)")
    vault = root / "lx-vault"
    (vault / "memories").mkdir(parents=True)
    (vault / "vault.yaml").write_text("schema_version: 1\n", encoding="utf-8")
    text = "The backup job copies the vault to the NAS."
    for memory_id, privacy in (("m-pub", "public"), ("m-priv", "local-private")):
        (vault / "memories" / f"{memory_id}.md").write_text(
            f"---\nid: {memory_id}\nprivacy: {privacy}\nversion: 1\ntags: []\n---\n{text}\n",
            encoding="utf-8",
        )

    class Backend:
        name, local, tasks = "smoke", True, (TASK,)

        def run(self, task: str, data: dict[str, object], *, max_tokens: int) -> ModelReply:
            return ModelReply("```json\n{}\n```", 5, 5)

    sent: list[object] = []

    def runner(documents: object, call: object, buffer: int) -> list[dict[str, object]]:
        sent.append(documents)
        assert callable(call) and call("prompt") == "```json\n{}\n```"
        span = {"start_pos": 4, "end_pos": 14}
        row = {
            "extraction_class": "entity",
            "extraction_text": "backup job",
            "char_interval": span,
            "alignment_status": "match_exact",
            "attributes": {"type": "service"},
        }
        return [{"document_id": "m-pub", "text": text, "extractions": [row]}]

    deriver = LangExtractDeriver(Settings(), backend=Backend(), runner=runner)
    view = check_deriver(
        deriver, vault=vault, request=DeriveRequest("entities", ("m-pub", "m-priv"))
    )
    assert view.cited_ids() == ("m-pub",) and sent == [[("m-pub", text)]], (view, sent)
    assert len(candidates(view)) == 1
    _ok("check_deriver LangExtractDeriver (replayed output)")


def _lightrag(root: Path) -> None:
    """The deriver wheel over the memfw-ports wheel, against an in-process fake server."""

    import io
    import json

    from memfw_adapter_lightrag import LightRagDeriver, Settings, candidates
    from memfw_adapter_lightrag import _ports as seam
    from memfw_adapter_lightrag.deriver import source_name
    from memfw_ports.reference._vault import read_memory
    from memfw_ports.testing import check_deriver
    from memfw_ports.types import DeriveRequest

    assert seam.DerivedView.__module__.startswith("memfw_ports"), seam.DerivedView
    _ok("import memfw_adapter_lightrag (ports from memfw_ports)")
    vault = root / "lr-vault"
    (vault / "memories").mkdir(parents=True)
    (vault / "vault.yaml").write_text("schema_version: 1\n", encoding="utf-8")
    for memory_id, privacy in (("m-pub", "public"), ("m-priv", "local-private")):
        (vault / "memories" / f"{memory_id}.md").write_text(
            f"---\nid: {memory_id}\nprivacy: {privacy}\nversion: 2\ntags: []\n---\nbody\n",
            encoding="utf-8",
        )
    sent: list[str] = []
    memory = read_memory(vault, "m-pub")
    assert memory is not None
    name = source_name(memory, Settings().max_chars)
    local = "http://127.0.0.1:11434"
    config = {"llm_binding_host": local, "embedding_binding_host": local}
    health = {"status": "healthy", "core_version": "1.5.7", "configuration": config}

    def opener(request: object, timeout: float) -> io.BytesIO:
        url = request.full_url  # type: ignore[attr-defined]
        sent.append(url)
        if url.endswith("/health"):
            return io.BytesIO(json.dumps(health).encode("utf-8"))
        entity = {"entity_name": "Backup", "entity_type": "service", "file_path": name}
        data = {"entities": [entity], "relationships": []}
        reply = {"status": "success", "message": "", "data": data, "metadata": {}}
        return io.BytesIO(json.dumps(reply).encode("utf-8"))

    deriver = LightRagDeriver(Settings(), environ={"LIGHTRAG_API_KEY": "smoke"}, opener=opener)
    request = DeriveRequest("graph", ("m-pub", "m-priv"), {"query": "backup"})
    view = check_deriver(deriver, vault=vault, request=request)
    assert view.cited_ids() == ("m-pub",), view
    assert sent[-2:] == ["http://127.0.0.1:9621/health", "http://127.0.0.1:9621/query/data"], sent
    assert len(candidates(view)) == 1
    _ok("check_deriver LightRagDeriver (fake server)")


def _graphrag(root: Path) -> None:
    """The deriver wheel over the memfw-ports wheel, reading a tiny CSV output (no GraphRAG)."""

    from memfw_adapter_graphrag import GraphRagDeriver, Settings, candidates
    from memfw_adapter_graphrag import _ports as seam
    from memfw_ports.testing import check_deriver
    from memfw_ports.types import DeriveRequest

    assert seam.DerivedView.__module__.startswith("memfw_ports"), seam.DerivedView
    _ok("import memfw_adapter_graphrag (ports from memfw_ports)")
    vault = root / "gr-vault"
    (vault / "memories").mkdir(parents=True)
    (vault / "vault.yaml").write_text("schema_version: 1\n", encoding="utf-8")
    for memory_id, privacy in (("m-pub", "public"), ("m-priv", "local-private")):
        (vault / "memories" / f"{memory_id}.md").write_text(
            f"---\nid: {memory_id}\nprivacy: {privacy}\nversion: 2\ntags: []\n---\nbody\n",
            encoding="utf-8",
        )
    project = root / "gr-project"
    output = project / "output"
    output.mkdir(parents=True)
    tables = {
        "documents": "id,title,text_unit_ids\nd1,m-pub.v2.txt,['u1']\nd2,m-priv.v2.txt,['u2']\n",
        "text_units": "id,document_id\nu1,d1\nu2,d2\n",
        "entities": "id,title,type,description,text_unit_ids\n"
        "e1,BACKUP,service,a job,['u1']\ne2,KEYS,concept,private,['u2']\n",
    }
    for name, text in tables.items():
        (output / f"{name}.csv").write_text(text, encoding="utf-8")
    deriver = GraphRagDeriver(Settings(root=str(project)), runner=lambda *a: 1)
    request = DeriveRequest("graph", ("m-pub", "m-priv"))
    view = check_deriver(deriver, vault=vault, request=request)
    assert view.cited_ids() == ("m-pub",) and "KEYS" not in view.content, view
    assert len(candidates(view)) == 1
    _ok("check_deriver GraphRagDeriver (recorded-shape output)")


def main(argv: list[str]) -> int:
    if importlib.util.find_spec("memfw") is not None:
        print("memfw is importable here; run this in a venv without memfw", file=sys.stderr)
        return 2
    with tempfile.TemporaryDirectory(prefix="memfw-wheel-smoke-") as tmp:
        root = Path(tmp)
        _ports(root)
        if "--recall" in argv:
            _recall(root)
        if "--deepseek" in argv:
            _deepseek()
        if "--bm25s" in argv:
            _bm25s(root)
        if "--qdrant" in argv:
            _qdrant(root)
        if "--qmd" in argv:
            _qmd(root)
        if "--ocr" in argv:
            _ocr(root)
        if "--pageindex" in argv:
            _pageindex(root)
        if "--langextract" in argv:
            _langextract(root)
        if "--lightrag" in argv:
            _lightrag(root)
        if "--graphrag" in argv:
            _graphrag(root)
    print("standalone wheel smoke passed")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
