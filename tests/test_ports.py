"""Value types, staging and protocols of ``memfw.ports``."""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from memfw.core.repository import Repository
from memfw.ports import (
    CANDIDATES_DIR,
    Availability,
    Candidate,
    Citation,
    DerivedView,
    Deriver,
    DeriveRequest,
    IngestItem,
    Ingestor,
    Manifest,
    ProjectionPolicy,
    Projector,
    RecallBudget,
    RecallProvider,
    StageResult,
    StagingError,
    candidate_directory,
    candidate_id,
    check_name,
    check_view_citations,
    stage_candidates,
)
from memfw.ports.types import (
    ALLOWED_EXTRA_KEYS,
    MAX_EXTRA_BYTES,
    MAX_EXTRA_STRING,
    MAX_REF_CHARS,
    validate_candidate,
)

SHA = "a" * 64
NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)


def _candidate(**overrides: Any) -> Candidate:
    values: dict[str, Any] = {
        "ref": "mem-1",
        "ref_kind": "memory_id",
        "score": 0.5,
        "provider": "fts",
        "rank": 1,
    }
    values.update(overrides)
    return Candidate(**values)


def _manifest(**overrides: Any) -> Manifest:
    values: dict[str, Any] = {
        "projector": "markdown",
        "policy_version": "v1",
        "projected": 3,
        "skipped": {"private": 1},
        "content_sha256": SHA,
        "generated_at": NOW,
        "files": ("a.md", "sub/b.md"),
    }
    values.update(overrides)
    return Manifest(**values)


def _view(**overrides: Any) -> DerivedView:
    values: dict[str, Any] = {
        "deriver": "timeline",
        "kind": "summary",
        "content": "derived",
        "citations": (Citation("mem-1"), Citation("mem-2", version=2)),
    }
    values.update(overrides)
    return DerivedView(**values)


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    Repository(root).init()
    assert (root / "vault.yaml").is_file()
    return root


# --- check_name -----------------------------------------------------------


@pytest.mark.parametrize("value", ["a", "fts", "my-adapter_2", "a" * 64])
def test_check_name_accepts(value: str) -> None:
    assert check_name(value) == value


@pytest.mark.parametrize("value", ["", "A", "1abc", "-x", "has space", "a" * 65, "ü", "a.b"])
def test_check_name_rejects(value: str) -> None:
    with pytest.raises(ValueError, match="name"):
        check_name(value)


def test_check_name_rejects_non_string() -> None:
    with pytest.raises(ValueError):
        check_name(123)  # type: ignore[arg-type]


# --- Candidate -------------------------------------------------------------


def test_candidate_valid() -> None:
    candidate = _candidate(ref_kind="path", ref="notes/a.md", score=3, rank=7, extra={"page": 1})
    assert candidate.ref_kind == "path"
    assert candidate.score == 3
    assert dict(candidate.extra) == {"page": 1}
    assert dict(_candidate().extra) == {}


@pytest.mark.parametrize("ref", ["", "   ", "\t\n", "a b", "a\x00b", "x\ny"])
def test_candidate_bad_memory_id_ref(ref: str) -> None:
    with pytest.raises(ValueError, match="ref"):
        _candidate(ref=ref)


def test_candidate_non_string_ref() -> None:
    with pytest.raises(ValueError):
        _candidate(ref=None)


@pytest.mark.parametrize(
    "ref", ["memories/a.md", "files/迁移计划.md", "qmd://fleet-memory/a.md", "a/b-c_d.e~f"]
)
def test_candidate_path_refs_accepted(ref: str) -> None:
    assert _candidate(ref_kind="path", ref=ref).ref == ref


@pytest.mark.parametrize(
    "ref", ["a b", "a\tb", "a\x00b", "a\x7fb", 'x"y', "x'y", "a\\b", "<a>", "a`b", ""]
)
def test_candidate_path_refs_rejected(ref: str) -> None:
    with pytest.raises(ValueError, match="ref"):
        _candidate(ref_kind="path", ref=ref)


def test_candidate_ref_length() -> None:
    _candidate(ref_kind="path", ref="x" * MAX_REF_CHARS)
    with pytest.raises(ValueError, match="512"):
        _candidate(ref_kind="path", ref="x" * (MAX_REF_CHARS + 1))
    with pytest.raises(ValueError, match="ref"):
        _candidate(ref="m" * 129)  # longer than a vault id


@pytest.mark.parametrize("ref_kind", ["", "id", "MEMORY_ID", "url", None])
def test_candidate_bad_ref_kind(ref_kind: Any) -> None:
    with pytest.raises(ValueError, match="ref_kind"):
        _candidate(ref_kind=ref_kind)


@pytest.mark.parametrize(
    "score", [float("nan"), float("inf"), float("-inf"), True, False, "0.5", None]
)
def test_candidate_bad_score(score: Any) -> None:
    with pytest.raises(ValueError, match="score"):
        _candidate(score=score)


@pytest.mark.parametrize("provider", ["", "FTS", "1x", "has space", "a" * 65])
def test_candidate_bad_provider(provider: str) -> None:
    with pytest.raises(ValueError, match="provider"):
        _candidate(provider=provider)


@pytest.mark.parametrize("rank", [0, -1, True, False, 1.0, "1", None])
def test_candidate_bad_rank(rank: Any) -> None:
    with pytest.raises(ValueError, match="rank"):
        _candidate(rank=rank)


@pytest.mark.parametrize(
    "key",
    ["body", "Snippet", "TEXT", "content", "summary", "preview", "Body ", "title", "Why", "k"],
)
def test_candidate_extra_key_not_in_allowlist(key: str) -> None:
    with pytest.raises(ValueError, match="not allowed"):
        _candidate(extra={key: "x"})


def test_candidate_allowlisted_keys() -> None:
    assert {"why", "score_raw", "rank_raw", "chunk", "page", "t", "lang", "kind"} == set(
        ALLOWED_EXTRA_KEYS
    )


def test_candidate_non_string_extra_key() -> None:
    with pytest.raises(ValueError, match="not allowed"):
        _candidate(extra={1: "x"})


@pytest.mark.parametrize("value", [[1], {"a": 1}, (1, 2), b"bytes", object()])
def test_candidate_non_scalar_extra(value: Any) -> None:
    with pytest.raises(ValueError, match="scalar"):
        _candidate(extra={"kind": value})


def test_candidate_non_finite_extra_number() -> None:
    with pytest.raises(ValueError, match="finite"):
        _candidate(extra={"score_raw": float("nan")})


def test_candidate_scalar_extras_accepted() -> None:
    extra = {"why": "x", "page": 1, "score_raw": 1.5, "t": True, "lang": None}
    assert dict(_candidate(extra=extra).extra) == extra


def test_candidate_long_extra_string() -> None:
    _candidate(extra={"why": "x" * MAX_EXTRA_STRING})
    with pytest.raises(ValueError, match="too long"):
        _candidate(extra={"why": "x" * (MAX_EXTRA_STRING + 1)})


def test_candidate_extra_total_size_cap() -> None:
    big = {key: "x" * MAX_EXTRA_STRING for key in ("why", "kind", "lang", "t")}
    assert sum(len(k) + len(v) + 2 for k, v in big.items()) > MAX_EXTRA_BYTES
    with pytest.raises(ValueError, match="too large"):
        _candidate(extra=big)


def test_validate_candidate_catches_subclass_and_mutation() -> None:
    class Sneaky(Candidate):
        pass

    with pytest.raises(ValueError, match="exactly"):
        Sneaky(ref="a", ref_kind="memory_id", score=1.0, provider="p", rank=1)
    candidate = _candidate()
    object.__setattr__(candidate, "extra", {"body": "leak"})
    with pytest.raises(ValueError, match="not allowed"):
        validate_candidate(candidate)
    object.__setattr__(candidate, "extra", {})
    object.__setattr__(candidate, "ref", "a b")
    with pytest.raises(ValueError, match="ref"):
        validate_candidate(candidate)


def test_candidate_extra_is_copied() -> None:
    original: dict[str, Any] = {"page": 1}
    candidate = _candidate(extra=original)
    original["page"] = 2
    original["body"] = "leak"
    assert dict(candidate.extra) == {"page": 1}
    assert candidate.extra is not original


def test_candidate_is_frozen() -> None:
    candidate = _candidate()
    with pytest.raises(AttributeError):
        candidate.ref = "other"  # type: ignore[misc]


# --- RecallBudget ----------------------------------------------------------


def test_recall_budget_defaults_and_valid() -> None:
    budget = RecallBudget()
    assert budget.timeout_seconds > 0
    assert budget.max_candidates >= 1
    assert RecallBudget(timeout_seconds=0.01, max_candidates=1).max_candidates == 1


@pytest.mark.parametrize("timeout", [0, -1, -0.5, float("nan")])
def test_recall_budget_bad_timeout(timeout: float) -> None:
    with pytest.raises(ValueError, match="timeout_seconds"):
        RecallBudget(timeout_seconds=timeout)


@pytest.mark.parametrize("max_candidates", [0, -3, True, False])
def test_recall_budget_bad_max_candidates(max_candidates: Any) -> None:
    with pytest.raises(ValueError, match="max_candidates"):
        RecallBudget(max_candidates=max_candidates)


# --- Availability / StageResult / small types ------------------------------


def test_availability_helpers() -> None:
    assert Availability.ok() == Availability(True, "")
    missing = Availability.missing("missing_dependency")
    assert not missing.available
    assert missing.reason == "missing_dependency"


def test_stage_result_candidate_ids() -> None:
    result = StageResult(staged=("a", "b"), duplicates=("c",))
    assert result.candidate_ids == ("a", "b", "c")
    assert StageResult(staged=()).candidate_ids == ()


def test_ingest_item_validation_and_copy() -> None:
    metadata: dict[str, Any] = {"k": "v"}
    item = IngestItem(title="t", body="", metadata=metadata)
    metadata["k"] = "changed"
    assert dict(item.metadata) == {"k": "v"}
    with pytest.raises(ValueError, match="title"):
        IngestItem(title="  ", body="b")
    with pytest.raises(ValueError, match="body"):
        IngestItem(title="t", body=None)  # type: ignore[arg-type]


def test_projection_policy_and_derive_request() -> None:
    options: dict[str, Any] = {"a": 1}
    policy = ProjectionPolicy(version="v1", options=options)
    options["a"] = 2
    assert dict(policy.options) == {"a": 1}
    with pytest.raises(ValueError, match="version"):
        ProjectionPolicy(version=" ")
    assert DeriveRequest(kind="summary", memory_ids=("m",)).kind == "summary"
    with pytest.raises(ValueError, match="kind"):
        DeriveRequest(kind="Bad Kind", memory_ids=())


def test_citation_requires_memory_id() -> None:
    assert Citation("m", version=3).version == 3
    with pytest.raises(ValueError, match="memory_id"):
        Citation(" ")


# --- Manifest --------------------------------------------------------------


def test_manifest_valid_and_skipped_copied() -> None:
    skipped = {"private": 1}
    manifest = _manifest(skipped=skipped)
    skipped["private"] = -5
    assert dict(manifest.skipped) == {"private": 1}
    assert manifest.files == ("a.md", "sub/b.md")


@pytest.mark.parametrize("sha", ["", "a" * 63, "a" * 65, "A" * 64, "g" * 64, " " + "a" * 63])
def test_manifest_bad_sha(sha: str) -> None:
    with pytest.raises(ValueError, match="content_sha256"):
        _manifest(content_sha256=sha)


def test_manifest_naive_datetime() -> None:
    with pytest.raises(ValueError, match="timezone"):
        _manifest(generated_at=datetime(2026, 11, 16, 12, 0))


@pytest.mark.parametrize("name", ["/etc/passwd", "..", "../x", "a/../b", "a/.."])
def test_manifest_bad_file_paths(name: str) -> None:
    with pytest.raises(ValueError, match="relative"):
        _manifest(files=(name,))


def test_manifest_dotted_names_are_fine() -> None:
    assert _manifest(files=("..hidden", "a..b/c.md")).files == ("..hidden", "a..b/c.md")


def test_manifest_negative_counts() -> None:
    with pytest.raises(ValueError, match="projected"):
        _manifest(projected=-1)
    with pytest.raises(ValueError, match="skipped"):
        _manifest(skipped={"x": -1})


def test_manifest_bad_projector_name() -> None:
    with pytest.raises(ValueError, match="projector"):
        _manifest(projector="Bad")


# --- DerivedView -----------------------------------------------------------


def test_derived_view_valid_and_cited_ids() -> None:
    view = _view()
    assert view.read_only is True
    assert view.cited_ids() == ("mem-1", "mem-2")


def test_derived_view_requires_citations() -> None:
    with pytest.raises(ValueError, match="cite"):
        _view(citations=())


def test_derived_view_read_only_false() -> None:
    with pytest.raises(ValueError, match="read-only"):
        _view(read_only=False)


def test_derived_view_rejects_verified_cause() -> None:
    with pytest.raises(ValueError, match="verified"):
        _view(causal_cap="verified")


@pytest.mark.parametrize("cap", ["none", "association", "hypothesis"])
def test_derived_view_allowed_causal_caps(cap: str) -> None:
    assert _view(causal_cap=cap).causal_cap == cap


@pytest.mark.parametrize("cap", ["", "certain", "Fact"])
def test_derived_view_bad_epistemic_cap(cap: str) -> None:
    with pytest.raises(ValueError, match="epistemic_cap"):
        _view(epistemic_cap=cap)


def test_derived_view_bad_causal_cap() -> None:
    with pytest.raises(ValueError, match="causal_cap"):
        _view(causal_cap="proven")


def test_derived_view_bad_names() -> None:
    with pytest.raises(ValueError, match="deriver"):
        _view(deriver="X")
    with pytest.raises(ValueError, match="kind"):
        _view(kind="")


# --- check_view_citations --------------------------------------------------


def test_check_view_citations_passes_when_all_exist() -> None:
    view = _view()
    assert check_view_citations(view, lambda memory_id: True) is view


def test_check_view_citations_rejects_unknown() -> None:
    known = {"mem-1"}
    with pytest.raises(ValueError, match="1 unknown"):
        check_view_citations(_view(), known.__contains__)


# --- candidate_id ----------------------------------------------------------


def test_candidate_id_stable_and_sensitive() -> None:
    item = IngestItem(title="t", body="b", external_ref="ext-1")
    first = candidate_id("ing", "src", item)
    assert first == candidate_id("ing", "src", item)
    assert first.startswith("cand-")
    assert len(first) == len("cand-") + 24
    assert candidate_id("other", "src", item) != first
    assert candidate_id("ing", "src2", item) != first
    other_ref = IngestItem(title="t", body="b", external_ref="ext-2")
    assert candidate_id("ing", "src", other_ref) != first
    # Same external ref, changed content: a different candidate.
    changed = IngestItem(title="t", body="changed", external_ref="ext-1")
    assert candidate_id("ing", "src", changed) != first
    with_meta = IngestItem(title="t", body="b", external_ref="ext-1", metadata={"k": 1})
    assert candidate_id("ing", "src", with_meta) != first


def test_candidate_id_without_external_ref_uses_content() -> None:
    a = candidate_id("ing", "src", IngestItem(title="t", body="b"))
    assert a == candidate_id("ing", "src", IngestItem(title="t", body="b"))
    assert a != candidate_id("ing", "src", IngestItem(title="t", body="c"))
    assert a != candidate_id("ing", "src", IngestItem(title="u", body="b"))


# --- stage_candidates ------------------------------------------------------


def _files_under(root: Path) -> set[Path]:
    return {path for path in root.rglob("*") if path.is_file()}


@pytest.mark.parametrize("proposal_id", ["", " ", "-x", "has space", "a" * 129, None])
def test_stage_requires_proposal_id(vault: Path, proposal_id: Any) -> None:
    before = _files_under(vault)
    with pytest.raises(StagingError, match="proposal_id"):
        stage_candidates(
            vault, "ing", [IngestItem(title="t", body="b")], proposal_id=proposal_id, source="s"
        )
    assert _files_under(vault) == before
    assert not (vault / CANDIDATES_DIR).exists()


@pytest.mark.parametrize("source", ["", " ", "/abs", "has space", None])
def test_stage_requires_source(vault: Path, source: Any) -> None:
    before = _files_under(vault)
    with pytest.raises(StagingError, match="source"):
        stage_candidates(
            vault, "ing", [IngestItem(title="t", body="b")], proposal_id="p-1", source=source
        )
    assert _files_under(vault) == before
    assert not (vault / CANDIDATES_DIR).exists()


def test_stage_writes_only_under_candidates(vault: Path) -> None:
    before = _files_under(vault)
    items = [IngestItem(title="a", body="1"), IngestItem(title="b", body="2", external_ref="e")]
    result = stage_candidates(vault, "ing", items, proposal_id="p-1", source="github:org/repo")
    assert len(result.staged) == 2
    assert result.duplicates == ()
    new_files = _files_under(vault) - before
    directory = vault / CANDIDATES_DIR / "ing"
    assert new_files == {directory / f"{identifier}.json" for identifier in result.staged}
    for path in new_files:
        assert path.parent == directory
    import json

    record = json.loads((directory / f"{result.staged[1]}.json").read_text())
    assert record["status"] == "staged"
    assert record["proposal_id"] == "p-1"
    assert record["source"] == "github:org/repo"
    assert record["external_ref"] == "e"
    assert record["ingestor"] == "ing"
    assert record["candidate_id"] == result.staged[1]


def test_stage_is_idempotent(vault: Path) -> None:
    items = [IngestItem(title="a", body="1"), IngestItem(title="b", body="2")]
    first = stage_candidates(vault, "ing", items, proposal_id="p-1", source="s")
    directory = vault / CANDIDATES_DIR / "ing"
    snapshot = {path: path.read_bytes() for path in directory.iterdir()}
    mtimes = {path: path.stat().st_mtime_ns for path in directory.iterdir()}
    second = stage_candidates(vault, "ing", items, proposal_id="p-2", source="s")
    assert second.staged == ()
    assert second.duplicates == first.staged
    assert second.candidate_ids == first.candidate_ids
    assert {path: path.read_bytes() for path in directory.iterdir()} == snapshot
    assert {path: path.stat().st_mtime_ns for path in directory.iterdir()} == mtimes


def test_stage_duplicate_within_batch(vault: Path) -> None:
    item = IngestItem(title="a", body="1")
    result = stage_candidates(vault, "ing", [item, item], proposal_id="p", source="s")
    assert len(result.staged) == 1
    assert result.duplicates == result.staged
    assert len(list((vault / CANDIDATES_DIR / "ing").iterdir())) == 1


def test_stage_empty_items(vault: Path) -> None:
    result = stage_candidates(vault, "ing", [], proposal_id="p", source="s")
    assert result == StageResult(staged=(), duplicates=())


def test_stage_files_are_private(vault: Path) -> None:
    result = stage_candidates(
        vault, "ing", [IngestItem(title="a", body="1")], proposal_id="p", source="s"
    )
    path = vault / CANDIDATES_DIR / "ing" / f"{result.staged[0]}.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_stage_refuses_symlinked_candidates_dir(vault: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, vault / CANDIDATES_DIR)
    with pytest.raises(StagingError, match="symlink"):
        stage_candidates(
            vault, "ing", [IngestItem(title="a", body="1")], proposal_id="p", source="s"
        )
    assert list(outside.iterdir()) == []


def test_stage_refuses_symlinked_ingestor_dir(vault: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (vault / CANDIDATES_DIR).mkdir()
    os.symlink(outside, vault / CANDIDATES_DIR / "ing")
    with pytest.raises(StagingError, match="symlink"):
        stage_candidates(
            vault, "ing", [IngestItem(title="a", body="1")], proposal_id="p", source="s"
        )
    assert list(outside.iterdir()) == []


def test_stage_refuses_non_vault_dir(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(StagingError, match="vault"):
        stage_candidates(
            plain, "ing", [IngestItem(title="a", body="1")], proposal_id="p", source="s"
        )
    assert not (plain / CANDIDATES_DIR).exists()


def test_stage_refuses_bad_ingestor_name(vault: Path) -> None:
    with pytest.raises(ValueError, match="ingestor"):
        stage_candidates(
            vault, "../evil", [IngestItem(title="a", body="1")], proposal_id="p", source="s"
        )


def test_candidate_directory(vault: Path) -> None:
    directory = candidate_directory(vault, "ing")
    assert directory == (vault / CANDIDATES_DIR / "ing").resolve()
    assert directory.is_dir()


def test_stage_ids_differ_across_ingestors(vault: Path) -> None:
    item = IngestItem(title="a", body="1")
    one = stage_candidates(vault, "one", [item], proposal_id="p", source="s")
    two = stage_candidates(vault, "two", [item], proposal_id="p", source="s")
    assert one.staged != two.staged
    assert one.staged[0] == candidate_id("one", "s", item)


# --- protocols -------------------------------------------------------------


class FakeRecall:
    name = "fake-recall"

    def available(self) -> Availability:
        return Availability.ok()

    def search(
        self,
        query: str,
        *,
        limit: int,
        budget: RecallBudget,
        tenant: str,
        as_of: datetime | None,
    ) -> Sequence[Candidate]:
        return [_candidate(provider=self.name)]


class FakeProjector:
    name = "fake-projector"

    def available(self) -> Availability:
        return Availability.ok()

    def export(self, vault: Path, out: Path, policy: ProjectionPolicy) -> Manifest:
        return _manifest(projector=self.name)


class FakeIngestor:
    name = "fake-ingestor"

    def available(self) -> Availability:
        return Availability.ok()

    def stage(
        self,
        vault: Path,
        items: Sequence[IngestItem],
        *,
        proposal_id: str,
        source: str,
    ) -> StageResult:
        return stage_candidates(vault, self.name, items, proposal_id=proposal_id, source=source)


class FakeDeriver:
    name = "fake-deriver"

    def available(self) -> Availability:
        return Availability.ok()

    def derive(self, vault: Path, request: DeriveRequest) -> DerivedView:
        return _view(deriver=self.name)


class OnlyAvailable:
    name = "partial"

    def available(self) -> Availability:
        return Availability.ok()


class NoName:
    def available(self) -> Availability:
        return Availability.ok()

    def search(self, query: str, **kwargs: Any) -> Sequence[Candidate]:
        return []

    def export(self, *args: Any) -> Manifest:
        raise NotImplementedError

    def stage(self, *args: Any, **kwargs: Any) -> StageResult:
        raise NotImplementedError

    def derive(self, *args: Any) -> DerivedView:
        raise NotImplementedError


@pytest.mark.parametrize(
    ("instance", "protocol"),
    [
        (FakeRecall(), RecallProvider),
        (FakeProjector(), Projector),
        (FakeIngestor(), Ingestor),
        (FakeDeriver(), Deriver),
    ],
)
def test_fakes_satisfy_protocols(instance: object, protocol: type) -> None:
    assert isinstance(instance, protocol)


@pytest.mark.parametrize("protocol", [RecallProvider, Projector, Ingestor, Deriver])
def test_missing_method_fails_protocol(protocol: type) -> None:
    assert not isinstance(OnlyAvailable(), protocol)


@pytest.mark.parametrize("protocol", [RecallProvider, Projector, Ingestor, Deriver])
def test_missing_name_fails_protocol(protocol: type) -> None:
    assert not isinstance(NoName(), protocol)


def test_fake_ingestor_stages(vault: Path) -> None:
    result = FakeIngestor().stage(
        vault, [IngestItem(title="a", body="1")], proposal_id="p", source="s"
    )
    assert (vault / CANDIDATES_DIR / "fake-ingestor" / f"{result.staged[0]}.json").is_file()


def test_stage_same_ref_changed_content_is_a_new_candidate(vault: Path) -> None:
    first = stage_candidates(
        vault, "ing", [IngestItem("t", "v1", external_ref="x")], proposal_id="p1", source="s"
    )
    second = stage_candidates(
        vault, "ing", [IngestItem("t", "v2", external_ref="x")], proposal_id="p2", source="s"
    )
    assert len(first.staged) == 1 and len(second.staged) == 1
    assert first.staged != second.staged
    directory = vault / CANDIDATES_DIR / "ing"
    assert len(list(directory.glob("*.json"))) == 2
    record = json.loads((directory / f"{second.staged[0]}.json").read_text())
    assert record["proposal_id"] == "p2"
    assert record["body"] == "v2"


def test_stage_validates_every_item_before_writing(vault: Path) -> None:
    items = [IngestItem("ok", "b"), IngestItem("bad", "b", metadata={"k": object()})]  # type: ignore[dict-item]
    with pytest.raises(StagingError, match="serialis"):
        stage_candidates(vault, "ing", items, proposal_id="p", source="s")
    assert not (vault / CANDIDATES_DIR / "ing").exists() or not list(
        (vault / CANDIDATES_DIR / "ing").iterdir()
    )
    with pytest.raises(StagingError, match="IngestItem"):
        stage_candidates(vault, "ing", ["nope"], proposal_id="p", source="s")  # type: ignore[list-item]


def test_stage_non_finite_metadata_rejected(vault: Path) -> None:
    with pytest.raises(StagingError):
        stage_candidates(
            vault,
            "ing",
            [IngestItem("t", "b", metadata={"x": float("nan")})],
            proposal_id="p",
            source="s",
        )


def test_stage_conflicting_existing_file_is_an_error(vault: Path) -> None:
    item = IngestItem("t", "b", external_ref="x")
    staged = stage_candidates(vault, "ing", [item], proposal_id="p", source="s").staged[0]
    path = vault / CANDIDATES_DIR / "ing" / f"{staged}.json"
    record = json.loads(path.read_text())
    record["content_sha256"] = "0" * 64
    path.write_text(json.dumps(record))
    with pytest.raises(StagingError, match="different candidate"):
        stage_candidates(vault, "ing", [item], proposal_id="p", source="s")


def test_stage_leaves_no_temporary_files(vault: Path) -> None:
    item = IngestItem("t", "b")
    stage_candidates(vault, "ing", [item], proposal_id="p", source="s")
    stage_candidates(vault, "ing", [item], proposal_id="p", source="s")
    names = [path.name for path in (vault / CANDIDATES_DIR / "ing").iterdir()]
    assert len(names) == 1 and names[0].endswith(".json") and not names[0].startswith(".")
