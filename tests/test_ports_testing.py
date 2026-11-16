"""The contract kit accepts conforming adapters and catches each broken rule."""

from __future__ import annotations

import hashlib
import itertools
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from memfw.core.repository import Repository
from memfw.ports import (
    Availability,
    Candidate,
    Citation,
    DerivedView,
    DeriveRequest,
    IngestItem,
    Manifest,
    ProjectionPolicy,
    RecallBudget,
    StageResult,
    stage_candidates,
)
from memfw.ports.testing import (
    ContractError,
    check_deriver,
    check_ingestor,
    check_projector,
    check_recall_provider,
    tree_digest,
)


def _vault(tmp_path: Path) -> Path:
    repository = Repository(tmp_path / "vault")
    repository.init()
    repository.add("One", "first body", memory_id="m1")
    repository.add("Two", "second body", memory_id="m2")
    return repository.root


# -- recall ----------------------------------------------------------------


class Provider:
    name = "fake"

    def __init__(self, refs: Sequence[str] = ("a", "b", "c")) -> None:
        self.refs = list(refs)
        self.counter = itertools.count()

    def available(self) -> Availability:
        return Availability.ok()

    def _candidate(self, ref: str, rank: int) -> Candidate:
        return Candidate(ref=ref, ref_kind="memory_id", score=1.0, provider=self.name, rank=rank)

    def search(
        self,
        query: str,
        *,
        limit: int,
        budget: RecallBudget,
        tenant: str,
        as_of: datetime | None,
    ) -> Sequence[Candidate]:
        return [self._candidate(ref, i + 1) for i, ref in enumerate(self.refs[:limit])]


class TooMany(Provider):
    def search(self, query: str, **kwargs: object) -> Sequence[Candidate]:  # type: ignore[override]
        return [self._candidate(ref, i + 1) for i, ref in enumerate(self.refs)]


class BadRanks(Provider):
    def search(self, query: str, **kwargs: object) -> Sequence[Candidate]:  # type: ignore[override]
        return [self._candidate("a", 2)]


class OtherName(Provider):
    def search(self, query: str, **kwargs: object) -> Sequence[Candidate]:  # type: ignore[override]
        return [Candidate(ref="a", ref_kind="memory_id", score=1.0, provider="other", rank=1)]


class Flaky(Provider):
    def search(self, query: str, **kwargs: object) -> Sequence[Candidate]:  # type: ignore[override]
        shift = next(self.counter) % 2
        refs = self.refs[shift:] + self.refs[:shift]
        return [self._candidate(ref, i + 1) for i, ref in enumerate(refs)]


class Writer(Provider):
    vault: Path

    def search(self, query: str, **kwargs: object) -> Sequence[Candidate]:  # type: ignore[override]
        (self.vault / "memories" / "m1.md").write_text("changed", encoding="utf-8")
        return []


def test_recall_kit_accepts_a_conforming_provider(tmp_path: Path) -> None:
    seen = check_recall_provider(Provider(), queries=["q1", "q2"], limit=2, vault=_vault(tmp_path))
    assert [c.ref for c in seen["q1"]] == ["a", "b"]


@pytest.mark.parametrize(
    ("provider", "message"),
    [
        (TooMany(["a", "b", "c", "d"]), "more than limit"),
        (BadRanks(), "ranks"),
        (OtherName(), "another provider"),
        (Flaky(), "deterministic"),
        (object(), "protocol"),
    ],
)
def test_recall_kit_catches_broken_providers(provider: object, message: str) -> None:
    with pytest.raises(ContractError, match=message):
        check_recall_provider(provider, queries=["q"], limit=3)  # type: ignore[arg-type]


def test_recall_kit_catches_vault_writes(tmp_path: Path) -> None:
    provider = Writer()
    provider.vault = _vault(tmp_path)
    with pytest.raises(ContractError, match="changed the vault"):
        check_recall_provider(provider, queries=["q"], vault=provider.vault)


def test_recall_kit_unavailable_provider() -> None:
    class Down(Provider):
        def available(self) -> Availability:
            return Availability.missing("down")

    with pytest.raises(ContractError, match="unavailable"):
        check_recall_provider(Down(), queries=["q"])
    assert check_recall_provider(Down(), queries=["q"], require_available=False) == {}


# -- projector -------------------------------------------------------------


class Projector:
    name = "listing"

    def __init__(self, *, salt: bool = False, touch_vault: bool = False) -> None:
        self.salt = salt
        self.touch_vault = touch_vault
        self.runs = 0

    def available(self) -> Availability:
        return Availability.ok()

    def export(self, vault: Path, out: Path, policy: ProjectionPolicy) -> Manifest:
        self.runs += 1
        out.mkdir(parents=True, exist_ok=True)
        files = []
        digest = hashlib.sha256()
        for path in sorted((vault / "memories").glob("*.md")):
            data = path.read_bytes() + (str(self.runs).encode() if self.salt else b"")
            (out / path.name).write_bytes(data)
            digest.update(data)
            files.append(path.name)
        if self.touch_vault:
            (vault / "memories" / "m1.md").write_text("x", encoding="utf-8")
        return Manifest(
            projector=self.name,
            policy_version=policy.version,
            projected=len(files),
            skipped={},
            content_sha256=digest.hexdigest(),
            generated_at=datetime.now(UTC),
            files=tuple(files),
        )


def test_projector_kit(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    policy = ProjectionPolicy(version="v1")
    manifest = check_projector(Projector(), vault=vault, out=tmp_path / "out", policy=policy)
    assert manifest.projected == 2
    with pytest.raises(ContractError, match="reproducible"):
        check_projector(Projector(salt=True), vault=vault, out=tmp_path / "o2", policy=policy)
    with pytest.raises(ContractError, match="changed the vault"):
        check_projector(
            Projector(touch_vault=True), vault=vault, out=tmp_path / "o3", policy=policy
        )


# -- ingestor --------------------------------------------------------------


class Ingestor:
    name = "importer"

    def available(self) -> Availability:
        return Availability.ok()

    def stage(
        self, vault: Path, items: Sequence[IngestItem], *, proposal_id: str, source: str
    ) -> StageResult:
        return stage_candidates(vault, self.name, items, proposal_id=proposal_id, source=source)


class Lax(Ingestor):
    def stage(
        self, vault: Path, items: Sequence[IngestItem], *, proposal_id: str, source: str
    ) -> StageResult:
        return StageResult(staged=("x",))


class Outside(Ingestor):
    def stage(
        self, vault: Path, items: Sequence[IngestItem], *, proposal_id: str, source: str
    ) -> StageResult:
        result = super().stage(vault, items, proposal_id=proposal_id, source=source)
        (vault / "memories" / "sneaky.md").write_text("x", encoding="utf-8")
        return result


class NotIdempotent(Ingestor):
    def stage(
        self, vault: Path, items: Sequence[IngestItem], *, proposal_id: str, source: str
    ) -> StageResult:
        if not proposal_id or not source:
            raise ValueError("required")
        return StageResult(staged=(f"id-{len(list((vault).glob('**/*')))}",))


ITEMS = [IngestItem(title="Note", body="external body", external_ref="ext-1")]


def test_ingestor_kit_accepts_reference_staging(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    result = check_ingestor(Ingestor(), vault=vault, items=ITEMS)
    assert len(result.staged) == 1


@pytest.mark.parametrize(
    ("ingestor", "message"),
    [
        (Lax(), "missing proposal_id"),
        (Outside(), "outside _candidates"),
        (NotIdempotent(), "wrote new candidates"),
    ],
)
def test_ingestor_kit_catches_broken_ingestors(
    tmp_path: Path, ingestor: Ingestor, message: str
) -> None:
    with pytest.raises(ContractError, match=message):
        check_ingestor(ingestor, vault=_vault(tmp_path), items=ITEMS)


# -- deriver ---------------------------------------------------------------


class Deriver:
    name = "summary"

    def __init__(self, *, extra_cite: str = "", touch: bool = False) -> None:
        self.extra_cite = extra_cite
        self.touch = touch

    def available(self) -> Availability:
        return Availability.ok()

    def derive(self, vault: Path, request: DeriveRequest) -> DerivedView:
        if self.touch:
            (vault / "memories" / "m2.md").write_text("x", encoding="utf-8")
        ids = list(request.memory_ids) + ([self.extra_cite] if self.extra_cite else [])
        return DerivedView(
            deriver=self.name,
            kind=request.kind,
            content="A view.",
            citations=tuple(Citation(memory_id) for memory_id in ids),
        )


def test_deriver_kit(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    request = DeriveRequest(kind="digest", memory_ids=("m1", "m2"))
    assert check_deriver(Deriver(), vault=vault, request=request).cited_ids() == ("m1", "m2")
    with pytest.raises(ContractError, match="not in the request"):
        check_deriver(Deriver(extra_cite="m9"), vault=vault, request=request)
    with pytest.raises(ContractError, match="changed the vault"):
        check_deriver(Deriver(touch=True), vault=vault, request=request)


def test_tree_digest_excludes_top_level_names(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    full = tree_digest(vault)
    assert any(key.startswith("memories/") for key in full)
    trimmed = tree_digest(vault, exclude=("memories",))
    assert not any(key.startswith("memories/") for key in trimmed)


# -- recall kit: smuggling, duplicates, tenants, budget, exceptions --------


class Subclassed(Candidate):
    pass


class SubclassProvider(Provider):
    def search(self, query: str, **kwargs: object) -> Sequence[Candidate]:  # type: ignore[override]
        # Bypass __post_init__: the kit must still catch the subclass.
        item = object.__new__(Subclassed)
        fields = {"ref": "a", "ref_kind": "memory_id", "score": 1.0, "provider": self.name}
        for key, value in {**fields, "rank": 1, "extra": {}}.items():
            object.__setattr__(item, key, value)
        return [item]


class Smuggler(Provider):
    def search(self, query: str, **kwargs: object) -> Sequence[Candidate]:  # type: ignore[override]
        item = self._candidate("a", 1)
        object.__setattr__(item, "extra", {"body": "the whole memory"})
        return [item]


class RefSmuggler(Provider):
    def search(self, query: str, **kwargs: object) -> Sequence[Candidate]:  # type: ignore[override]
        item = self._candidate("a", 1)
        object.__setattr__(item, "ref", "a b c full text")
        return [item]


class Duplicates(Provider):
    def search(self, query: str, **kwargs: object) -> Sequence[Candidate]:  # type: ignore[override]
        return [self._candidate("a", 1), self._candidate("a", 2)]


class SlowProvider(Provider):
    def search(self, query: str, **kwargs: object) -> Sequence[Candidate]:  # type: ignore[override]
        time.sleep(1.5)
        return []


class Exits(Provider):
    def search(self, query: str, **kwargs: object) -> Sequence[Candidate]:  # type: ignore[override]
        raise SystemExit(2)


class RaisesOnEmpty(Provider):
    def search(self, query: str, **kwargs: object) -> Sequence[Candidate]:  # type: ignore[override]
        if not query.strip():
            raise ValueError("query required")
        return super().search(
            query,
            limit=int(kwargs["limit"]),
            budget=RecallBudget(),
            tenant="",
            as_of=None,  # type: ignore[call-overload]
        )


@pytest.mark.parametrize(
    ("provider", "message"),
    [
        (Smuggler(), "not allowed"),
        (RefSmuggler(), "ref"),
        (Duplicates(), "same ref twice"),
        (Exits(), "BaseException"),
    ],
)
def test_recall_kit_catches_smuggling_and_contract_breaks(provider: object, message: str) -> None:
    with pytest.raises(ContractError, match=message):
        check_recall_provider(provider, queries=["q"], limit=3)  # type: ignore[arg-type]


def test_recall_kit_rejects_candidate_subclasses() -> None:
    with pytest.raises(ContractError, match="plain Candidate"):
        check_recall_provider(SubclassProvider(), queries=["q"])  # type: ignore[arg-type]


def test_recall_kit_enforces_budget() -> None:
    with pytest.raises(ContractError, match="past its budget"):
        check_recall_provider(
            SlowProvider(),
            queries=["q"],
            budget=RecallBudget(timeout_seconds=0.1),
            budget_allowance=1.0,
        )


def test_recall_kit_cross_tenant_refs() -> None:
    owners = {"a": "", "b": "alpha", "c": ""}
    with pytest.raises(ContractError, match="another tenant"):
        check_recall_provider(Provider(), queries=["q"], tenant_of=lambda c: owners.get(c.ref))
    with pytest.raises(ContractError, match="another tenant"):
        check_recall_provider(
            Provider(["a", "ghost"]), queries=["q"], tenant_of=lambda c: owners.get(c.ref)
        )
    check_recall_provider(
        Provider(["a", "c"]), queries=["q"], tenant_of=lambda c: owners.get(c.ref)
    )


def test_recall_kit_exception_contract() -> None:
    # An ordinary query must not raise.
    with pytest.raises(ContractError, match="ordinary query"):
        check_recall_provider(RaisesOnEmpty(), queries=[" "])
    # A declared error query may raise an Exception.
    check_recall_provider(RaisesOnEmpty(), queries=["q"], error_queries=[" "])
