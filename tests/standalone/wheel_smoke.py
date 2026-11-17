"""Smoke test for the standalone ``memfw-ports`` / ``memfw-recall`` wheels.

Run it with the interpreter of a venv that has the wheels and NOT memfw:

    python -I tests/standalone/wheel_smoke.py [--recall] [--deepseek] [--bm25s] [--qdrant] [--qmd]

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
``check_recall_provider`` against its loopback fake. Prints one
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
    _ok("import memfw_adapter_qmd (ports from memfw_ports)")
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
    print("standalone wheel smoke passed")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
