"""Smoke test for the standalone ``memfw-ports`` / ``memfw-recall`` wheels.

Run it with the interpreter of a venv that has the wheels and NOT memfw:

    python -I tests/standalone/wheel_smoke.py [--recall]

It refuses to run when ``memfw`` is importable, imports the wheels, and runs
the contract kit (``memfw_ports.testing``) against the reference
implementations on a throwaway vault. With ``--recall`` it also runs
``memfw_recall.Coordinator`` over the reference recall provider. Prints one
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
        MirrorProjector,
        TimelineDeriver,
    )
    from memfw_ports.testing import (
        check_claim_stager,
        check_claim_verifier,
        check_clock,
        check_deriver,
        check_id_gen,
        check_ingestor,
        check_projector,
        check_recall_provider,
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


def main(argv: list[str]) -> int:
    if importlib.util.find_spec("memfw") is not None:
        print("memfw is importable here; run this in a venv without memfw", file=sys.stderr)
        return 2
    with tempfile.TemporaryDirectory(prefix="memfw-wheel-smoke-") as tmp:
        root = Path(tmp)
        _ports(root)
        if "--recall" in argv:
            _recall(root)
    print("standalone wheel smoke passed")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
